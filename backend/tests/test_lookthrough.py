"""ETF look-through and the concentration view (FR-23, FR-24, FR-27)."""

from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quant.models import EtfHolding, Instrument, PriceEOD
from quant.portfolio import lookthrough, service

from .conftest import login, make_user

TODAY = date(2025, 2, 28)
NVDA = "US67066G1040"
AAPL = "US0378331005"
SAP = "DE0007164600"
WORLD = "IE0000000002"
OTHER_ETF = "IE0000000003"
LEV = "IE0000000004"
COIN = "XF000BTC0017"

ISHARES = """iShares Core Example UCITS ETF
Fund Holdings as of,"Feb 27, 2025"
Inception Date,"Jan 01, 2010"
Shares Outstanding,"1,000,000.00"
Stock,"-"
Bond,"-"
Cash,"-"
Other,"-"
 
Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date
"NVDA","NVIDIA CORP","Information Technology","Equity","12,345,678.00","10.00","12,345,678.00","1,000","123.45","United States","NASDAQ","USD","1.00","USD","-"
"AAPL","APPLE INC","Information Technology","Equity","1,000.00","20.00","1,000.00","10","100.00","United States","NASDAQ","USD","1.00","USD","-"
"SAP","SAP","Information Technology","Equity","500.00","5.00","500.00","5","100.00","Germany","XETRA","EUR","1.00","EUR","-"
"USD","USD CASH","Cash and/or Derivatives","Cash","10.00","0.50","10.00","10","1.00","United States","-","USD","1.00","USD","-"
 
The content contained herein is owned or licensed by the issuer.
"""


# --- the parser ----------------------------------------------------------------------------


def test_the_ishares_layout_is_read() -> None:
    rows = lookthrough.parse_holdings(ISHARES)
    assert [r.name for r in rows] == ["NVIDIA CORP", "APPLE INC", "SAP", "USD CASH"]
    nvda = rows[0]
    assert nvda.ticker == "NVDA" and nvda.weight == D("10.00") and nvda.country == "United States"
    assert nvda.currency == "USD" and nvda.sector == "Information Technology"
    assert nvda.isin is None  # this layout has no ISIN column
    assert sum(r.weight for r in rows) == D("35.50")


def test_a_semicolon_file_with_comma_decimals() -> None:
    text = (
        "Name;ISIN;Weight;Sector;Country\n"
        "Apple;US0378331005;12,5;Technology;US\n"
        "SAP;DE0007164600;7,5;Technology;DE\n"
        "Bad ISIN;XX;5;;\n"
    )
    rows = lookthrough.parse_holdings(text)
    assert [(r.name, r.weight) for r in rows] == [
        ("Apple", D("12.5")), ("SAP", D("7.5")), ("Bad ISIN", D("5")),
    ]  # fmt: skip
    assert rows[0].isin == "US0378331005"
    assert rows[2].isin is None  # a bad ISIN is dropped, the row stays


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "no header"),
        ("Name,Value\nA,1\n", "no header"),
        ("Name,Weight\n", "no holdings"),
        ("Name,Weight\nA,abc\nB,-\n", "no holdings"),
        ("Name,Weight\nA,5\nB,6\n", "add up to 11.0%"),
        ("Name,Weight\nA,60\nB,60\n", "add up to 120.0%"),
    ],
)
def test_bad_files_are_refused(text: str, why: str) -> None:
    with pytest.raises(lookthrough.HoldingsFormatError, match=why):
        lookthrough.parse_holdings(text)


# --- the exposure view ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "today", lambda: TODAY)


def instrument(db: Session, isin: str, name: str, **kw: Any) -> Instrument:
    row = Instrument(
        code=isin, isin=isin, name=name, asset_class=kw.pop("asset_class", "stock"),
        currency="EUR", **kw,
    )  # fmt: skip
    db.add(row)
    db.flush()
    db.add(PriceEOD(instrument_id=row.id, date=TODAY, close=D("100"), currency="EUR", source="t"))
    db.add(
        PriceEOD(
            instrument_id=row.id, date=TODAY - timedelta(days=1), close=D("100"),
            currency="EUR", source="t",
        )
    )  # fmt: skip
    db.commit()
    return row


def buy(client: TestClient, isin: str, name: str, cls: str, shares: str = "10") -> None:
    body = {
        "side": "buy", "date": "2025-01-06", "isin": isin, "name": name, "asset_class": cls,
        "shares": shares, "price": "100",
    }  # fmt: skip
    assert client.post("/api/transactions", json=body).status_code == 201


def upload(client: TestClient, isin: str, text: str = ISHARES, **form: str) -> Any:
    return client.post(
        f"/api/lookthrough/{isin}",
        files={"file": ("holdings.csv", text.encode(), "text/csv")},
        data={"as_of": "2025-02-27", "source": "issuer", **form},
    )


def view(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/concentration")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def by(body: dict[str, Any], key: str) -> dict[str, D]:
    return {s["label"]: D(s["pct"]) for s in body[key]}


@pytest.fixture
def book(owner: TestClient, db: Session) -> TestClient:
    """NVIDIA directly (1000) and a world ETF (1000): the ETF holds NVIDIA too."""
    instrument(db, NVDA, "NVIDIA Corp.", sector="Technology", symbols={"yahoo": "NVDA"})
    instrument(db, WORLD, "World ETF", asset_class="etf")
    buy(owner, NVDA, "NVIDIA Corp.", "STOCK")
    buy(owner, WORLD, "World ETF", "FUND")
    return owner


def test_the_overlap_of_a_fund_and_a_share_shows(book: TestClient) -> None:
    assert upload(book, WORLD).status_code == 201
    body = view(book)
    assert D(body["total"]) == 2000 and body["unlooked"] == []
    nvda = next(u for u in body["underlying"] if u["isin"] == NVDA)
    # The holdings file names NVIDIA by ticker: it is matched to the share held directly.
    assert D(nvda["direct_pct"]) == 50 and D(nvda["indirect_pct"]) == 5
    assert D(nvda["total_pct"]) == 55
    assert nvda["via"] == [{"fund": "World ETF", "pct": "5.00"}]
    names = [u["name"] for u in body["underlying"]]
    assert names[0] == "NVIDIA Corp."  # the biggest first
    assert "APPLE INC" in names and "SAP" in names


def test_sector_country_and_currency_use_the_look_through(book: TestClient) -> None:
    upload(book, WORLD)
    body = view(book)
    country = by(body, "by_country")
    assert country["US"] == D("65.25")  # 1000 + 100 + 200 + 5 (the cash line) of 2000
    assert country["DE"] == D("2.50")
    assert country[lookthrough.OTHER_IN_FUNDS] == D("32.25")  # the file covers 35.5% of the fund
    sector = by(body, "by_sector")
    assert sector["Information Technology"] == D("67.50")  # "Technology" is folded in
    currency = by(body, "by_currency")
    assert currency["USD"] == D("65.25") and currency["EUR"] == D("2.50")
    assert body["covered_pct"] == "100.00"
    assert by(body, "by_class") == {"stock": D("50.00"), "etf": D("50.00")}


def test_a_fund_without_a_file_is_flagged_not_guessed(book: TestClient, db: Session) -> None:
    instrument(db, OTHER_ETF, "Other ETF", asset_class="etf")
    buy(book, OTHER_ETF, "Other ETF", "FUND")
    upload(book, WORLD)
    body = view(book)
    assert body["unlooked"] == ["Other ETF"]
    assert any("not looked through" in w and "Other ETF" in w for w in body["warnings"])
    assert by(body, "by_sector")[lookthrough.NOT_LOOKED_THROUGH] == D("33.33")
    assert D(body["covered_pct"]) == D("50.00")  # one of the two funds is looked through
    other = next(p for p in body["positions"] if p["isin"] == OTHER_ETF)
    assert other["looked_through"] is False
    nvda = next(p for p in body["positions"] if p["isin"] == NVDA)
    assert nvda["looked_through"] is None  # not a fund


def test_without_any_file_everything_direct_still_adds_up(book: TestClient) -> None:
    body = view(book)
    assert body["unlooked"] == ["World ETF"] and body["covered_pct"] == "0.00"
    assert by(body, "by_country")["US"] == D("50.00")
    assert sum(by(body, "by_sector").values()) == D("100.00")


def test_instrument_types_are_shown(book: TestClient, db: Session) -> None:
    instrument(db, LEV, "WisdomTree Oil 3x Daily Short", asset_class="etf")
    instrument(db, COIN, "Bitcoin", asset_class="crypto")
    buy(book, LEV, "WisdomTree Oil 3x Daily Short", "FUND")
    buy(book, COIN, "Bitcoin", "CRYPTO", shares="10")
    upload(book, WORLD)
    body = view(book)
    lev = next(p for p in body["positions"] if p["isin"] == LEV)
    assert lev["type"] == "leveraged" and lev["trading_book"] is True
    assert "decays" in lev["flags"][0]
    assert lev["looked_through"] is None  # a leveraged ETP is not a plain fund to look through
    coin = next(p for p in body["positions"] if p["isin"] == COIN)
    assert coin["type"] == "crypto" and coin["crypto_linked"] is True
    assert D(body["crypto_pct"]) == D("25.00")  # 1000 of 4000
    assert by(body, "by_sector")["Crypto"] == D("25.00")


def test_a_holdings_file_with_isins_merges_by_isin(book: TestClient, db: Session) -> None:
    instrument(db, AAPL, "Apple", sector="Technology", symbols={"yahoo": "AAPL"})
    buy(book, AAPL, "Apple", "STOCK", shares="5")  # 500 directly
    text = f"Name,ISIN,Weight,Country\nApple,{AAPL},20,US\nSAP,{SAP},10,Germany\n"
    assert upload(book, WORLD, text).status_code == 201
    body = view(book)
    apple = next(u for u in body["underlying"] if u["isin"] == AAPL)
    assert D(apple["direct_pct"]) == D("20.00") and D(apple["indirect_pct"]) == D("8.00")
    sap = next(u for u in body["underlying"] if u["isin"] == SAP)
    assert sap["country"] == "DE" and sap["direct_pct"] == "0.00"


# --- the API ----------------------------------------------------------------------------------


def test_upload_list_replace_delete(owner: TestClient, db: Session) -> None:
    instrument(db, WORLD, "World ETF", asset_class="etf")
    first = upload(owner, WORLD)
    assert first.status_code == 201, first.text
    assert first.json()["rows"] == 4 and D(first.json()["total_weight"]) == D("35.50")
    assert first.json()["name"] == "World ETF" and first.json()["as_of"] == "2025-02-27"
    listing = owner.get("/api/lookthrough").json()
    assert [f["etf_isin"] for f in listing] == [WORLD] and listing[0]["source"] == "issuer"
    replace = upload(owner, WORLD, "Name,Weight\nOnly,50\n")
    assert replace.json()["rows"] == 1
    assert db.scalar(select(func.count()).select_from(EtfHolding)) == 1  # the old rows are gone
    assert owner.delete(f"/api/lookthrough/{WORLD}").status_code == 204
    assert owner.get("/api/lookthrough").json() == []


def test_bad_uploads_are_refused(owner: TestClient, db: Session) -> None:
    assert upload(owner, "NOPE").status_code == 422
    bad = upload(owner, WORLD, "nothing useful here")
    assert bad.status_code == 422 and "header" in bad.text
    assert db.scalar(select(func.count()).select_from(EtfHolding)) == 0
    sums = upload(owner, WORLD, "Name,Weight\nA,5\n")
    assert sums.status_code == 422 and "add up" in sums.text
    latin = "Name;Weight\nBürgerliche Brauerei;60\n".encode("latin-1")
    ok = owner.post(f"/api/lookthrough/{WORLD}", files={"file": ("h.csv", latin, "text/csv")})
    assert ok.status_code == 201  # a Latin-1 file is read too


def test_only_an_admin_uploads_but_everyone_reads(
    owner: TestClient, client: TestClient, db: Session
) -> None:
    assert upload(owner, WORLD).status_code == 201
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert upload(client, WORLD).status_code == 403
    assert client.delete(f"/api/lookthrough/{WORLD}").status_code == 403
    assert [f["etf_isin"] for f in client.get("/api/lookthrough").json()] == [WORLD]
    # Their own concentration is empty: the files are shared, the portfolios are not.
    empty = view(client)
    assert D(empty["total"]) == 0 and empty["positions"] == []


def test_the_disclaimer_gate_covers_concentration(client: TestClient, db: Session) -> None:
    make_user(db, "new@example.com", accepted=False)
    assert login(client, "new@example.com") == 200
    assert client.get("/api/concentration").status_code == 403
    assert client.get("/api/lookthrough").status_code == 403

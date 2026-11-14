"""Tech sub-groups, tags, consolidation, redundancy and the trading book (FR-45 to FR-48)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ingest.alerts import create_alerts
from quant.models import EtfHolding, Instrument, Notification, PriceEOD
from quant.portfolio import consolidation, service
from quant.portfolio import subgroups as sg

from .conftest import login, make_user

TODAY = date(2025, 2, 28)
NVDA = "US67066G1040"
MSFT = "US5949181045"
NOVO = "DK0062498333"
WORLD = "IE0000000002"
NDX1 = "IE00B53SZB19"
NDX2 = "IE00BFZXGZ54"
LEV = "IE0000000004"
LEV2 = "IE0000000005"
KO = "DE000KO000001"
DAX = "DE0008469008"


# --- pure: sub-group tags, names, overlap ------------------------------------------------------


@pytest.mark.parametrize(
    ("sector", "industry", "name", "isin", "group"),
    [
        ("Information Technology", "Semiconductors", "Acme Chips", None, sg.SEMIS),
        ("Information Technology", None, "NVIDIA Corp.", None, sg.SEMIS),
        ("Information Technology", None, "Microsoft Corp", None, sg.SOFTWARE),
        ("Consumer Discretionary", "Internet Retail", "Some Shop", None, sg.INTERNET),
        ("Consumer Discretionary", None, "Amazon.com Inc", None, sg.INTERNET),
        ("Communication", None, "Some Media Co", None, sg.INTERNET),
        ("Information Technology", None, "Apple Inc", None, sg.HARDWARE),
        ("Financials", "Credit Services", "Payments Ltd", None, sg.FINTECH),
        ("Financials", None, "PayPal Holdings", None, sg.FINTECH),
        ("Information Technology", None, "Accenture plc", None, sg.IT_SERVICES),
        ("Communication", "Entertainment", "Stream Co", None, sg.MEDIA),
        ("Information Technology", None, "SMA Solar Technology", None, sg.CLEANTECH),
        ("Energy", "Solar", "Sun Power Co", None, sg.CLEANTECH),
        ("Financials", None, "Coinbase Global", "US19260Q1076", sg.CRYPTO_EQUITIES),
        ("Information Technology", None, "Unknown Co", None, sg.SOFTWARE),  # tech, no clue
        ("Health Care", "Drug Manufacturers", "Novo Nordisk A/S", None, sg.NON_TECH),
        ("Unknown", None, None, None, sg.NON_TECH),
    ],
)
def test_auto_subgroup(
    sector: str, industry: str | None, name: str | None, isin: str | None, group: str
) -> None:
    assert sg.auto_subgroup(sector, industry, name, isin) == group


def test_the_taxonomy_has_nine_tech_groups() -> None:
    assert len(sg.TECH_GROUPS) == 9 and sg.NON_TECH not in sg.TECH_GROUPS
    assert sg.is_tech(sg.CLEANTECH) and not sg.is_tech(sg.NON_TECH)


def test_company_and_index_keys() -> None:
    assert consolidation.company_key("Alphabet Inc. Class A") == "alphabet"
    assert consolidation.company_key("ALPHABET INC CLASS C") == "alphabet"
    assert consolidation.company_key("SAP SE") == "sap"
    a = consolidation.index_key("iShares NASDAQ 100 UCITS ETF (Acc)")
    b = consolidation.index_key("Invesco EQQQ NASDAQ-100 UCITS ETF")
    assert a == b == "nasdaq100|"
    momentum = consolidation.index_key("iShares MSCI World Momentum Factor UCITS ETF")
    assert momentum == "msci_world|momentum"
    assert momentum != consolidation.index_key("iShares Core MSCI World UCITS ETF")
    assert consolidation.index_key("Some Bond Fund") is None


def holding(name: str, weight: str, isin: str | None = None) -> EtfHolding:
    return EtfHolding(
        etf_isin="X", name=name, ticker=None, holding_isin=isin, weight=D(weight),
        as_of=TODAY, source="t",
    )  # fmt: skip


def test_shared_weight_is_the_smaller_weight_of_each_common_name() -> None:
    a = [holding("X", "50"), holding("Y", "30"), holding("Z", "20")]
    b = [holding("X", "40"), holding("Y", "40"), holding("Z", "10"), holding("W", "10")]
    assert consolidation.shared_weight(a, b) == D("80")
    assert consolidation.shared_weight(a, [holding("Q", "100")]) == 0
    same = [holding("X", "60", isin="US67066G1040")]
    assert consolidation.shared_weight(same, [holding("Other", "70", isin="US67066G1040")]) == 60


# --- helpers over the API --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "today", lambda: TODAY)


def instrument(db: Session, isin: str, name: str, cls: str = "stock", **kw: Any) -> Instrument:
    row = Instrument(code=isin, isin=isin, name=name, asset_class=cls, currency="EUR", **kw)
    db.add(row)
    db.flush()
    for ago in (0, 1):
        db.add(
            PriceEOD(
                instrument_id=row.id, date=TODAY - timedelta(days=ago), close=D("100"),
                currency="EUR", source="t",
            )
        )  # fmt: skip
    db.commit()
    return row


def buy(
    client: TestClient, isin: str, name: str, cls: str, shares: str, day: str = "2025-01-06"
) -> None:
    body = {
        "side": "buy", "date": day, "isin": isin, "name": name, "asset_class": cls,
        "shares": shares, "price": "100",
    }  # fmt: skip
    assert client.post("/api/transactions", json=body).status_code == 201


def sell(
    client: TestClient, isin: str, name: str, cls: str, shares: str, day: str, price: str
) -> None:
    body = {
        "side": "sell", "date": day, "isin": isin, "name": name, "asset_class": cls,
        "shares": shares, "price": price,
    }  # fmt: skip
    assert client.post("/api/transactions", json=body).status_code == 201


def cash(client: TestClient, amount: str) -> None:
    deposit = {"side": "deposit", "date": "2025-01-02", "amount": "50000"}
    assert client.post("/api/transactions", json=deposit).status_code == 201
    assert client.post("/api/cash/balance", json={"amount": amount}).status_code == 201


def get(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- FR-47: sub-groups and tags --------------------------------------------------------------------


@pytest.fixture
def techbook(owner: TestClient, db: Session) -> TestClient:
    """NVIDIA, Microsoft, a non-tech share and a world ETF, 1000 or 500 each."""
    instrument(db, NVDA, "NVIDIA Corp.", sector="Technology")
    instrument(db, MSFT, "Microsoft Corp", sector="Technology")
    instrument(db, NOVO, "Novo Nordisk A/S", sector="Healthcare")
    instrument(db, WORLD, "World ETF", "etf")
    buy(owner, NVDA, "NVIDIA Corp.", "STOCK", "10")
    buy(owner, MSFT, "Microsoft Corp", "STOCK", "10")
    buy(owner, NOVO, "Novo Nordisk A/S", "STOCK", "5")
    buy(owner, WORLD, "World ETF", "FUND", "10")
    cash(owner, "46500")  # 50000 deposited, 3500 spent: 46500 left, so history agrees
    return owner


def test_every_share_gets_one_subgroup(techbook: TestClient) -> None:
    body = get(techbook, "/api/book/subgroups")
    groups = {g["key"]: g for g in body["groups"]}
    assert set(groups) == {"semis", "software"}  # only groups with something in them
    assert D(groups["semis"]["value"]) == 1000 and groups["semis"]["members"][0]["isin"] == NVDA
    assert D(groups["software"]["pct_of_tech"]) == 50
    assert D(body["non_tech"]) == 500
    stocks = {s["isin"]: s for s in body["stocks"]}
    assert stocks[NOVO]["subgroup"] == "non_tech" and stocks[NVDA]["overridden"] is False
    assert stocks[MSFT]["label"] == "Software and cloud"
    assert body["labels"]["cleantech"] == "Clean-tech and energy tech"


def test_the_user_can_override_the_subgroup(techbook: TestClient) -> None:
    tag = techbook.put(f"/api/book/tags/{MSFT}", json={"subgroup": "internet"})
    assert tag.status_code == 200 and tag.json()["subgroup"] == "internet"
    body = get(techbook, "/api/book/subgroups")
    assert {g["key"] for g in body["groups"]} == {"semis", "internet"}
    row = next(s for s in body["stocks"] if s["isin"] == MSFT)
    assert row["overridden"] is True and row["auto"] == "software"
    # A non-tech share can be pulled into tech, and back out.
    techbook.put(f"/api/book/tags/{NOVO}", json={"subgroup": "software"})
    assert D(get(techbook, "/api/book/subgroups")["non_tech"]) == 0
    refused = techbook.put(f"/api/book/tags/{NOVO}", json={"subgroup": "magic"})
    assert refused.status_code == 422 and "sub-group must be one of" in refused.text
    techbook.put(f"/api/book/tags/{NOVO}", json={"subgroup": None})
    assert D(get(techbook, "/api/book/subgroups")["non_tech"]) == 500


def test_the_tech_bucket_follows_the_subgroups(techbook: TestClient) -> None:
    buckets = {b["bucket"]: b for b in get(techbook, "/api/allocation")["buckets"]}
    assert D(buckets["tech_stocks"]["value"]) == 2000  # NVIDIA and Microsoft
    assert D(buckets["other"]["value"]) == 500  # the non-tech share
    techbook.put(f"/api/book/tags/{NOVO}", json={"subgroup": "software"})
    moved = {b["bucket"]: b for b in get(techbook, "/api/allocation")["buckets"]}
    assert D(moved["tech_stocks"]["value"]) == 2500 and D(moved["other"]["value"]) == 0


def test_a_subgroup_cap_is_a_guardrail(techbook: TestClient) -> None:
    ok = techbook.put("/api/book/subgroups/limits", json={"limits": {"semis": {"cap": "10"}}})
    assert ok.status_code == 200, ok.text
    semis = next(g for g in ok.json()["groups"] if g["key"] == "semis")
    assert D(semis["pct"]) == 2 and semis["breached"] is False  # 1000 of 50000
    tight = techbook.put("/api/book/subgroups/limits", json={"limits": {"semis": {"cap": "1"}}})
    assert next(g for g in tight.json()["groups"] if g["key"] == "semis")["breached"] is True
    rule = next(r for r in get(techbook, "/api/allocation")["rules"] if r["rule"] == "subgroup")
    assert rule["subject"] == "Semiconductors and equipment" and rule["breached"] is True
    assert D(rule["limit_pct"]) == 1
    inverted = {"semis": {"target": "20", "cap": "10"}}
    assert techbook.put("/api/book/subgroups/limits", json={"limits": inverted}).status_code == 422
    unknown = {"limits": {"bananas": {"cap": "10"}}}
    assert techbook.put("/api/book/subgroups/limits", json=unknown).status_code == 422
    # A group with only a target still shows, with no cap and no breach.
    only = techbook.put(
        "/api/book/subgroups/limits", json={"limits": {"cleantech": {"target": "5"}}}
    )
    clean = next(g for g in only.json()["groups"] if g["key"] == "cleantech")
    assert D(clean["target"]) == 5 and clean["cap"] is None and clean["breached"] is False


def test_tags_are_partial_clearable_and_private(
    techbook: TestClient, client: TestClient, db: Session
) -> None:
    assert techbook.put(f"/api/book/tags/{NVDA}", json={"pinned": True}).json()["pinned"] is True
    later = techbook.put(f"/api/book/tags/{NVDA}", json={"note": "AI chips"}).json()
    assert later["pinned"] is True and later["note"] == "AI chips"  # the pin stays
    cleared = techbook.put(f"/api/book/tags/{NVDA}", json={"note": None}).json()
    assert cleared["note"] is None and cleared["pinned"] is True
    assert [t["isin"] for t in techbook.get("/api/book/tags").json()] == [NVDA]
    bad = [
        {"stop_price": "0"}, {"barrier": "-1"}, {"tracker_until": "2025-02-01"},
        {"stop_price": "abc"},
    ]  # fmt: skip
    for body in bad:
        assert techbook.put(f"/api/book/tags/{NVDA}", json=body).status_code == 422, body
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert client.get("/api/book/tags").json() == []
    assert client.delete(f"/api/book/tags/{NVDA}").status_code == 404
    client.cookies.clear()
    assert login(client, "owner@example.com") == 200
    assert client.delete(f"/api/book/tags/{NVDA}").status_code == 204
    assert client.delete(f"/api/book/tags/{NVDA}").status_code == 404


# --- FR-45: consolidation ---------------------------------------------------------------------------


@pytest.fixture
def small_book(owner: TestClient, db: Session) -> TestClient:
    """Big holdings (about 10000) and a handful of tiny ones."""
    instrument(db, NVDA, "NVIDIA Corp.", sector="Technology")
    instrument(db, WORLD, "World ETF", "etf")
    instrument(db, "US02079K3059", "Alphabet Inc. Class A", sector="Communication")
    instrument(db, "US02079K1079", "ALPHABET INC CLASS C", sector="Communication")
    for n in range(1, 4):
        instrument(db, f"US000000000{n}", f"Tiny {n}")
    instrument(db, "US0000000009", "No Price Corp")
    # The last one has prices, then they are removed: no usable price at all.
    db.query(PriceEOD).filter(
        PriceEOD.instrument_id
        == db.scalar(select(Instrument.id).where(Instrument.isin == "US0000000009"))
    ).delete()
    db.commit()
    buy(owner, NVDA, "NVIDIA Corp.", "STOCK", "50")
    buy(owner, WORLD, "World ETF", "FUND", "40")
    buy(owner, "US02079K3059", "Alphabet Inc. Class A", "STOCK", "20")
    buy(owner, "US02079K1079", "ALPHABET INC CLASS C", "STOCK", "0.1")  # the same company
    buy(owner, "US0000000001", "Tiny 1", "STOCK", "0.1")  # plain small: left alone
    buy(owner, "US0000000002", "Tiny 2", "STOCK", "0.1")  # pinned
    buy(owner, "US0000000003", "Tiny 3", "STOCK", "0.1")  # a tracker
    buy(owner, "US0000000009", "No Price Corp", "STOCK", "3")
    cash(owner, "40000")
    owner.put("/api/book/tags/US0000000002", json={"pinned": True})
    owner.put("/api/book/tags/US0000000003", json={"tracker_until": "2025-08-28"})
    return owner


def statuses(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["isin"]: s for s in body["small"]}


def test_small_positions_get_a_status_and_none_is_sold_for_being_small(
    small_book: TestClient,
) -> None:
    body = get(small_book, "/api/book/consolidation")
    by = statuses(body)
    assert by["US0000000001"]["status"] == "review"  # small, nothing wrong: left alone
    assert by["US0000000002"]["status"] == "protected"
    assert by["US0000000003"]["status"] == "tracker" and "2025-08-28" in by["US0000000003"]["why"]
    twin = by["US02079K1079"]
    assert twin["status"] == "consolidate" and "Alphabet Inc. Class A" in twin["suggestion"]
    lost = by["US0000000009"]
    assert lost["status"] == "exit_candidate" and lost["value"] is None
    assert "No usable price" in lost["why"]
    assert "NVDA" not in by and NVDA not in by  # the big ones are not listed
    assert D(by["US0000000001"]["pct"]) < D("0.5")


def test_prompts_are_limited_and_most_urgent_first(small_book: TestClient) -> None:
    body = get(small_book, "/api/book/consolidation")
    assert [p["kind"] for p in body["prompts"]] == ["exit_candidate", "consolidate"]
    assert body["held_back"] == 0
    limited = small_book.put("/api/book/settings", json={"weekly_prompts": 1}).json()
    assert [p["kind"] for p in limited["prompts"]] == ["exit_candidate"]
    assert limited["held_back"] == 1 and limited["settings"]["weekly_prompts"] == 1
    none = small_book.put("/api/book/settings", json={"weekly_prompts": 0}).json()
    assert none["prompts"] == [] and none["held_back"] == 2


def test_the_position_count_against_the_soft_target(small_book: TestClient) -> None:
    body = get(small_book, "/api/book/consolidation")
    assert body["direct"] == 8 and body["min_positions"] == 40 and body["state"] == "below"
    assert body["look_through"] == 7  # the one without a price is not valued
    tight = small_book.put("/api/book/settings", json={"min_positions": 2, "max_positions": 5})
    assert tight.json()["state"] == "above"
    assert any("soft target of 2 to 5" in n for n in tight.json()["notes"])
    assert any("not built yet" in n for n in tight.json()["notes"])
    bad = small_book.put("/api/book/settings", json={"min_positions": 90, "max_positions": 80})
    assert bad.status_code == 422 and "minimum" in bad.text


def test_an_expired_tracker_comes_up_again(small_book: TestClient, db: Session) -> None:
    from quant.models import PositionTag, User

    user = db.scalar(select(User).where(User.email == "owner@example.com"))
    assert user is not None
    rls.bypass(db)
    tag = db.get(PositionTag, (user.id, "US0000000003"))
    assert tag is not None
    tag.tracker_until = date(2025, 1, 1)  # the period ran out
    db.commit()
    by = statuses(get(small_book, "/api/book/consolidation"))
    assert by["US0000000003"]["status"] == "review"
    assert "tracker period is over" in by["US0000000003"]["why"]
    prompts = get(small_book, "/api/book/consolidation")["prompts"]
    assert "US0000000003" in {p["isin"] for p in prompts}  # it is asked about again


# --- FR-46: redundancy ------------------------------------------------------------------------------


@pytest.fixture
def twins(owner: TestClient, db: Session) -> TestClient:
    instrument(db, NDX1, "iShares NASDAQ 100 UCITS ETF (Acc)", "etf")
    instrument(db, NDX2, "Invesco EQQQ NASDAQ-100 UCITS ETF", "etf")
    instrument(db, WORLD, "iShares Core MSCI World UCITS ETF", "etf")
    buy(owner, NDX1, "iShares NASDAQ 100 UCITS ETF (Acc)", "FUND", "20")
    buy(owner, NDX2, "Invesco EQQQ NASDAQ-100 UCITS ETF", "FUND", "10")
    buy(owner, WORLD, "iShares Core MSCI World UCITS ETF", "FUND", "10")
    cash(owner, "46000")
    return owner


def test_two_funds_on_the_same_index_are_redundant(twins: TestClient) -> None:
    [pair] = get(twins, "/api/book/consolidation")["overlaps"]
    assert {pair["a_isin"], pair["b_isin"]} == {NDX1, NDX2}
    assert pair["basis"] == "index" and pair["overlap_pct"] is None
    assert pair["intentional"] is False
    assert "follow the same index" in pair["suggestion"]
    assert "new money into iShares NASDAQ 100" in pair["suggestion"]  # the bigger one


def test_the_savings_plan_is_redirected_not_sold(
    twins: TestClient, db: Session, admin: Any
) -> None:
    from quant.models import Transaction, User

    user = db.scalar(select(User).where(User.email == "owner@example.com"))
    assert user is not None
    rls.bypass(db)
    for n, day in enumerate((date(2024, 12, 20), date(2025, 1, 20), date(2025, 2, 20))):
        db.add(
            Transaction(
                user_id=user.id,
                broker="tr",
                external_id=f"p-{n}",
                executed_at=datetime(day.year, day.month, day.day, 9, tzinfo=UTC),
                date=day,
                kind="trade",
                category="TRADING",
                type="BUY",
                asset_class="FUND",
                isin=NDX2,
                name="Invesco EQQQ NASDAQ-100 UCITS ETF",
                shares=D(1),
                price=D(100),
                amount=D(-100),
                currency="EUR",
                savings_plan=True,
            )  # fmt: skip
        )
    db.commit()
    [pair] = get(twins, "/api/book/consolidation")["overlaps"]
    assert "Redirect the savings plan of Invesco EQQQ" in pair["suggestion"]
    assert "100 EUR a month" in pair["suggestion"] and "no sale and no tax" in pair["suggestion"]


def test_an_intentional_overlap_is_left_out_of_the_prompts_and_asked_again_a_year_later(
    twins: TestClient, db: Session
) -> None:
    from quant.models import User
    from quant.portfolio import book

    mark = twins.post("/api/book/overlaps", json={"a": NDX2, "b": NDX1, "intentional": True})
    assert mark.status_code == 200
    [pair] = mark.json()["overlaps"]
    assert pair["intentional"] is True and pair["reconfirm"] is False
    # Same pair, either order: still one mark.
    twins.post("/api/book/overlaps", json={"a": NDX1, "b": NDX2, "intentional": True})
    assert len(get(twins, "/api/book/consolidation")["overlaps"]) == 1
    # Confirmed over a year ago: the app asks again.
    user = db.scalar(select(User).where(User.email == "owner@example.com"))
    assert user is not None
    rls.bypass(db)
    book.set_intentional(db, user.id, NDX1, NDX2, True, TODAY - timedelta(days=400))
    [later] = get(twins, "/api/book/consolidation")["overlaps"]
    assert later["intentional"] is True and later["reconfirm"] is True
    twins.post("/api/book/overlaps", json={"a": NDX1, "b": NDX2, "intentional": True})  # renewed
    assert get(twins, "/api/book/consolidation")["overlaps"][0]["reconfirm"] is False
    off = twins.post("/api/book/overlaps", json={"a": NDX1, "b": NDX2, "intentional": False})
    assert off.json()["overlaps"][0]["intentional"] is False
    same = twins.post("/api/book/overlaps", json={"a": NDX1, "b": NDX1})
    assert same.status_code == 422 and "two different" in same.text


def test_an_intentional_overlap_still_counts_in_the_caps(twins: TestClient) -> None:
    twins.post("/api/book/overlaps", json={"a": NDX1, "b": NDX2, "intentional": True})
    body = get(twins, "/api/concentration")
    assert {p["isin"] for p in body["positions"]} >= {NDX1, NDX2}  # both still counted


def upload(client: TestClient, isin: str, text: str) -> None:
    response = client.post(
        f"/api/lookthrough/{isin}", files={"file": ("h.csv", text.encode(), "text/csv")}
    )
    assert response.status_code == 201, response.text


def test_two_holdings_files_settle_it_by_weight(twins: TestClient) -> None:
    upload(twins, NDX1, "Name,Weight\nX,50\nY,30\nZ,20\n")
    upload(twins, NDX2, "Name,Weight\nX,40\nY,40\nZ,10\nW,10\n")
    [pair] = get(twins, "/api/book/consolidation")["overlaps"]
    assert pair["basis"] == "holdings" and D(pair["overlap_pct"]) == 80
    assert "share 80% of their holdings" in pair["suggestion"]
    # Files that differ outweigh a matching name.
    upload(twins, NDX2, "Name,Weight\nA,50\nB,50\n")
    assert get(twins, "/api/book/consolidation")["overlaps"] == []


# --- FR-48: the trading book -----------------------------------------------------------------------


@pytest.fixture
def trades(owner: TestClient, db: Session) -> TestClient:
    instrument(db, NVDA, "NVIDIA Corp.", sector="Technology")
    instrument(db, LEV, "WisdomTree Oil 3x Daily Short", "etf")
    instrument(db, LEV2, "Boost Gas 2x Leveraged", "etf")
    instrument(db, KO, "Turbo Long DAX", "etf")
    instrument(db, DAX, "DAX index fund", "etf")
    buy(owner, NVDA, "NVIDIA Corp.", "STOCK", "100")  # 10000 long-term
    buy(owner, LEV, "WisdomTree Oil 3x Daily Short", "FUND", "5", day="2025-02-01")  # 27 days
    buy(owner, KO, "Turbo Long DAX", "DERIVATIVE", "2", day="2025-02-20")
    # Two closed trades: a win and a loss, of five units each.
    buy(owner, LEV2, "Boost Gas 2x Leveraged", "FUND", "5", day="2025-01-06")
    sell(owner, LEV2, "Boost Gas 2x Leveraged", "FUND", "5", "2025-02-01", "120")
    buy(owner, "IE0000000006", "Short Oil 2x Daily", "FUND", "5", day="2025-01-10")
    sell(owner, "IE0000000006", "Short Oil 2x Daily", "FUND", "5", "2025-01-20", "90")
    cash(owner, "39000")
    return owner


def test_the_trading_book_holds_leveraged_products_and_knockouts(trades: TestClient) -> None:
    body = get(trades, "/api/book/trading")
    isins = {o["isin"]: o for o in body["open"]}
    assert set(isins) == {LEV, KO}
    assert isins[LEV]["type"] == "leveraged" and isins[KO]["type"] == "knockout"
    assert D(body["value"]) == 700  # 500 + 200
    assert D(body["cap"]) == 3 and body["over_cap"] is False  # 700 of ~50000
    assert "abolished in 2024" in body["tax_note"]
    # It is outside the long-term picture.
    alloc = get(trades, "/api/allocation")
    assert D(alloc["trading_book"]) == 700
    assert {b["bucket"]: D(b["value"]) for b in alloc["buckets"]}["tech_stocks"] == 10000


def test_holding_time_barrier_and_stop_alerts(trades: TestClient, db: Session) -> None:
    trades.put(f"/api/book/tags/{LEV}", json={"stop_price": "120"})  # the price is 100: at the stop
    trades.put(
        f"/api/book/tags/{KO}",
        json={"barrier": "97", "barrier_isin": DAX, "barrier_long": True},
    )  # the DAX fund is 100: 3% from the barrier
    body = get(trades, "/api/book/trading")
    by = {o["isin"]: o for o in body["open"]}
    assert D(by[KO]["barrier_distance_pct"]) == D("3.00")
    assert [a["code"] for a in by[KO]["alerts"]] == ["barrier"]
    assert {a["code"] for a in by[LEV]["alerts"]} == {"hold", "stop"}
    hold = next(a for a in by[LEV]["alerts"] if a["code"] == "hold")
    assert "Held for 27 days" in hold["text"] and "limit is 10 days" in hold["text"]
    assert by[LEV]["days_held"] == 27 and D(by[LEV]["stop_price"]) == 120
    # A knock-out far from its barrier is quiet; a short barrier is measured the other way.
    trades.put(f"/api/book/tags/{KO}", json={"barrier": "80"})
    quiet = {o["isin"]: o for o in get(trades, "/api/book/trading")["open"]}[KO]
    assert quiet["alerts"] == [] and D(quiet["barrier_distance_pct"]) == D("20.00")
    trades.put(f"/api/book/tags/{KO}", json={"barrier": "103", "barrier_long": False})
    short = {o["isin"]: o for o in get(trades, "/api/book/trading")["open"]}[KO]
    assert D(short["barrier_distance_pct"]) == D("3.00") and short["alerts"][0]["code"] == "barrier"
    # Alerts come first in the list.
    assert get(trades, "/api/book/trading")["open"][0]["alerts"] != []
    # The limits are the user's to change.
    loose = trades.put("/api/book/settings", json={"hold_days": 60, "barrier_pct": 1}).json()
    assert loose["settings"]["hold_days"] == 60
    after = {o["isin"]: o for o in get(trades, "/api/book/trading")["open"]}
    assert {a["code"] for a in after[LEV]["alerts"]} == {"stop"} and after[KO]["alerts"] == []


def test_the_statistics_of_the_closed_trades(trades: TestClient) -> None:
    stats = get(trades, "/api/book/trading")["stats"]
    assert stats["closed"] == 2 and stats["wins"] == 1 and D(stats["hit_rate"]) == 50
    assert D(stats["pnl"]) == 50  # +100 and -50
    assert D(stats["avg_days"]) == D("18.0")  # 26 days and 10 days, five units each
    assert stats["expired"] == 0
    assert {int(y): D(v) for y, v in stats["by_year"].items()} == {2025: D("50.00")}


def test_a_short_term_tag_puts_a_share_in_the_trading_book(trades: TestClient) -> None:
    trades.put(f"/api/book/tags/{NVDA}", json={"short_term": True})
    body = get(trades, "/api/book/trading")
    assert NVDA in {o["isin"] for o in body["open"]}
    assert D(body["value"]) == 10700 and body["over_cap"] is True  # over the 3% cap
    alloc = get(trades, "/api/allocation")
    assert {b["bucket"]: D(b["value"]) for b in alloc["buckets"]}["tech_stocks"] == 0
    assert D(alloc["trading_book"]) == 10700
    assert get(trades, "/api/book/subgroups")["groups"] == []  # not in a long-term group


def test_a_thesis_is_asked_for_until_the_journal_has_one(trades: TestClient) -> None:
    needs = get(trades, "/api/book/trading")["needs_thesis"]
    assert {isin for isin, _ in needs} == {LEV, KO}
    entry = {"action": "bought", "name": "Oil short", "reason": "A spike to fade", "isin": LEV}
    assert trades.post("/api/journal", json=entry).status_code == 201
    after = get(trades, "/api/book/trading")["needs_thesis"]
    assert {isin for isin, _ in after} == {KO}


def test_trading_alerts_are_stored_once(trades: TestClient, db: Session) -> None:
    trades.put(f"/api/book/tags/{LEV}", json={"stop_price": "120"})

    def run() -> int:
        rls.bypass(db)
        result = create_alerts(db, TODAY, datetime(2025, 2, 28, 12, tzinfo=UTC))
        assert result.errors == {} and result.warnings == {}
        return result.rows_written

    first = run()
    rls.bypass(db)
    made = [n for n in db.scalars(select(Notification)) if n.kind == "trading_book"]
    assert {(n.isin, n.dedupe_key.split(":")[1]) for n in made} == {(LEV, "hold"), (LEV, "stop")}
    assert all(n.severity == "warning" for n in made) and first >= 2
    assert run() == 0  # the same day: nothing new


def test_the_gate_covers_the_book_endpoints(client: TestClient, db: Session) -> None:
    make_user(db, "new@example.com", accepted=False)
    assert login(client, "new@example.com") == 200
    for path in (
        "/api/book/tags",
        "/api/book/subgroups",
        "/api/book/consolidation",
        "/api/book/trading",
    ):
        assert client.get(path).status_code == 403, path

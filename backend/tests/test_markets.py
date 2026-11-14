"""Commodities, key drivers, the regime, the hedge check and ETCs (FR-60 to FR-64), and the
FRED provider and the long first fetch behind them."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from quant.config import Settings
from quant.ingest.prices import LONG_BACKFILL_DAYS, ingest_prices
from quant.models import CommodityProduct, FxRate, Instrument, PriceEOD
from quant.portfolio import markets, service
from quant.providers import fred
from quant.providers.base import ProviderError
from quant.providers.registry import PRICE_PROVIDERS, price_providers

from .conftest import login, make_user
from .test_coingecko import FakeFetcher
from .test_ingest import FakePriceProvider, series

TODAY = date(2025, 2, 28)


# --- the FRED provider -------------------------------------------------------------------------


def test_fred_csv_is_read() -> None:
    body = "observation_date,DFII10\n2025-01-02,2.10\n2025-01-03,.\n2025-01-06,2.15\n"
    result = fred.parse_csv(body, date(2025, 1, 1), date(2025, 1, 31))
    assert [(b.date, b.close) for b in result.bars] == [
        (date(2025, 1, 2), D("2.10")), (date(2025, 1, 6), D("2.15")),
    ]  # fmt: skip
    assert result.currency is None
    old = "DATE,VALUE\n2024-12-31,1.0\n2025-01-02,2.0\n"  # the older header, and a day out of range
    assert [b.close for b in fred.parse_csv(old, date(2025, 1, 1), date(2025, 1, 31)).bars] == [2]


def test_fred_bad_or_empty_payloads() -> None:
    assert fred.parse_csv("", date(2025, 1, 1), date(2025, 1, 2)).bars == []
    with pytest.raises(ProviderError, match="unexpected payload"):
        fred.parse_csv("<html>blocked</html>", date(2025, 1, 1), date(2025, 1, 2))
    with pytest.raises(ProviderError, match="malformed row"):
        fred.parse_csv("DATE,X\nnot-a-date,1\n", date(2025, 1, 1), date(2025, 1, 2))


def test_fred_is_a_registered_default_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QT_PRICE_PROVIDERS", raising=False)
    assert "fred" in PRICE_PROVIDERS and "fred" in Settings().price_providers
    fetcher = FakeFetcher("observation_date,DFII10\n2025-01-02,1.5\n")
    [provider] = price_providers(["fred"], fetcher)  # type: ignore[arg-type]
    result = provider.fetch_eod("DFII10", date(2025, 1, 1), date(2025, 1, 31))
    assert [b.close for b in result.bars] == [D("1.5")]
    name, _, params, _ = fetcher.calls[0]
    assert name == "fred" and params == {"id": "DFII10", "cosd": "2025-01-01", "coed": "2025-01-31"}


def test_commodity_and_macro_series_are_first_fetched_over_five_years(db: Session) -> None:
    gold = Instrument(
        code="GOLD", name="Gold", asset_class="commodity", currency="USD", symbols={"fake": "GC=F"}
    )
    share = Instrument(
        code="ACME", name="Acme", asset_class="stock", currency="USD", symbols={"fake": "ACME"}
    )
    db.add_all([gold, share])
    db.commit()
    provider = FakePriceProvider(
        "fake", {"GC=F": series("USD", (TODAY, "2000")), "ACME": series("USD", (TODAY, "10"))}
    )
    ingest_prices(db, [provider], TODAY, backfill_days=400)
    starts = {symbol: start for symbol, start, _ in provider.calls}
    assert starts["GC=F"] == TODAY - timedelta(days=LONG_BACKFILL_DAYS)
    assert starts["ACME"] == TODAY - timedelta(days=400)
    assert LONG_BACKFILL_DAYS >= 5 * 365


# --- pure helpers ----------------------------------------------------------------------------------


def line(start: date, days: int, first: str, step: str) -> markets.Series:
    return [(start + timedelta(days=i), D(first) + D(step) * i) for i in range(days)]


def test_changes_need_enough_history() -> None:
    s = line(date(2024, 1, 2), 424, "2000", "1")  # 2024-01-02 to 2025-02-28, 2000 to 2423
    assert markets.change(s, 7) == pytest.approx(D(7) / 2416 * 100)
    assert markets.change(s, 365) == pytest.approx(D(365) / 2058 * 100)
    assert markets.change(s, 5 * 365) is None  # the history is 14 months
    assert markets.change(s[:1], 7) is None and markets.change([], 7) is None
    short = line(date(2025, 2, 1), 28, "100", "1")
    assert markets.change(short, 7) is not None and markets.change(short, 30) is None


def test_point_change_and_value_on() -> None:
    s = line(date(2025, 1, 1), 60, "2.00", "0.01")
    assert markets.point_change(s, 30) == D("0.30")
    assert markets.point_change(s, 365) is None
    assert markets.value_on(s, date(2025, 1, 10)) == D("2.09")
    assert markets.value_on(s, date(2024, 12, 1)) is None  # before the first
    assert markets.value_on(s, date(2025, 3, 5)) == s[-1][1]  # a few days after the last
    assert markets.value_on(s, date(2025, 6, 1)) is None  # too long after


def test_hedge_verdict_words() -> None:
    assert "hedging" in markets.hedge_verdict(-0.4) and "not" not in markets.hedge_verdict(-0.4)
    assert "independently" in markets.hedge_verdict(0.1)
    assert "not hedging you" in markets.hedge_verdict(0.6)
    assert "Not enough" in markets.hedge_verdict(None)


# --- data helpers -------------------------------------------------------------------------------------


def add(db: Session, code: str, cls: str, cur: str, points: markets.Series) -> Instrument:
    inst = Instrument(code=code, name=code, asset_class=cls, currency=cur)
    db.add(inst)
    db.flush()
    for day, close in points:
        db.add(PriceEOD(instrument_id=inst.id, date=day, close=close, currency=cur, source="t"))
    db.commit()
    return inst


def fx(db: Session, start: date, days: int, rate: str = "1.10") -> None:
    for i in range(days):
        db.add(FxRate(quote="USD", date=start + timedelta(days=i), rate=D(rate), source="t"))
    db.commit()


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "today", lambda: TODAY)


@pytest.fixture(autouse=True)
def no_seeded_products(db: Session) -> None:
    """The migration lists a few ETCs. The first test of a run would still see them."""
    db.execute(delete(CommodityProduct))
    db.commit()


def get(client: TestClient) -> dict:  # type: ignore[type-arg]
    response = client.get("/api/markets")
    assert response.status_code == 200, response.text
    body: dict = response.json()  # type: ignore[type-arg]
    return body


# --- FR-60: the commodity panel -----------------------------------------------------------------------


def test_the_commodity_panel(owner: TestClient, db: Session) -> None:
    add(db, "GOLD", "commodity", "USD", line(date(2024, 1, 2), 424, "2000", "1"))
    fx(db, date(2024, 1, 1), 430)
    body = get(owner)
    rows = {r["code"]: r for r in body["commodities"]}
    gold = rows["GOLD"]
    assert D(gold["usd"]) == 2423 and gold["as_of"] == "2025-02-28" and gold["stale"] is False
    assert D(gold["eur"]) == D("2202.73")  # at 1.10
    assert D(gold["changes"]["1w"]) == D("0.29") and D(gold["changes"]["1m"]) == D("1.25")
    assert D(gold["changes"]["1y"]) == D("17.74") and gold["changes"]["5y"] is None
    assert D(gold["low_52w"]) == 2059 and D(gold["high_52w"]) == 2423
    assert D(gold["range_pos"]) == 100  # at its 52-week high
    assert gold["unit"] == "USD per troy ounce"
    # A commodity with no stored prices still has a row, marked stale.
    silver = rows["SILVER"]
    assert silver["usd"] is None and silver["stale"] is True and silver["changes"]["1w"] is None
    assert [r["code"] for r in body["commodities"]] == [
        "GOLD", "SILVER", "WTI", "BRENT", "COPPER", "NATGAS",
    ]  # fmt: skip


def test_an_old_price_is_marked_stale(owner: TestClient, db: Session) -> None:
    add(db, "GOLD", "commodity", "USD", line(date(2024, 12, 1), 30, "2000", "1"))  # to 2024-12-30
    assert get(owner)["commodities"][0]["stale"] is True


# --- FR-61 and FR-63: key drivers and the regime ---------------------------------------------------------


@pytest.fixture
def macro(db: Session) -> None:
    start = date(2024, 10, 1)  # 150 days to 2025-02-28
    add(db, "DFII10", "macro", "PCT", line(start, 151, "2.00", "0.004"))  # +0.36 in 90 days
    add(db, "DTWEXBGS", "macro", "IDX", line(start, 151, "100", "0.04"))  # +3.5% in 90 days
    add(db, "BAMLH0A0HYM2", "macro", "PCT", line(start, 151, "3.00", "0.005"))  # +0.45
    cpi = [(date(2023, 10, 1), D("300"))]
    for m in range(1, 17):  # monthly to 2025-02-01, steady growth: inflation not moving
        y, mo = divmod(9 + m, 12)
        cpi.append((date(2023 + y, mo + 1, 1), D("300") * D("1.003") ** m))
    add(db, "CPIAUCSL", "macro", "IDX", cpi)
    add(db, "BRENT", "commodity", "USD", line(start, 151, "70", "0.1"))  # +11.8% in 90 days
    add(db, "WTI", "commodity", "USD", line(start, 151, "65", "0.1"))
    add(db, "GOLD", "commodity", "USD", line(start, 151, "2000", "1"))
    add(db, "SILVER", "commodity", "USD", line(start, 151, "20", "0.02"))


def test_gold_and_oil_drivers(owner: TestClient, macro: None) -> None:
    groups = {g["title"]: g for g in get(owner)["drivers"]}
    items = {i["label"]: i for i in groups["Gold and silver"]["items"]}
    real = items["10-year real yield"]
    assert D(real["value"]) == D("2.600") and "+0.36 points over 3 months" in real["detail"]
    dollar = items["US dollar index (broad)"]
    assert "+3.5% over 3 months" in dollar["detail"]
    ratio = items["Gold to silver ratio"]
    assert D(ratio["value"]) == pytest.approx(D(2150) / D(23))  # about 93.5
    assert "average of the last year" in ratio["detail"]
    oil = {i["label"]: i for i in groups["Oil"]["items"]}
    assert D(oil["Brent minus WTI"]["value"]) == D("5.0")
    eia = oil["Weekly US oil inventories (EIA)"]
    assert eia["available"] is False and "Not connected" in eia["detail"]


def test_crypto_drivers_follow_the_nasdaq(owner: TestClient, db: Session) -> None:
    pattern = [D(1), D("1.02"), D("0.99"), D("1.01"), D("0.98")]
    levels = []
    level = D(100)
    for i in range(120):
        level *= pattern[i % 5]
        levels.append((date(2024, 11, 1) + timedelta(days=i), level))
    add(db, "SXRV", "etf", "EUR", levels)
    add(db, "BTC", "crypto", "EUR", [(d, v * 500) for d, v in levels])  # the same moves
    crypto = {
        i["label"]: i for g in get(owner)["drivers"] if g["title"] == "Crypto" for i in g["items"]
    }
    btc = crypto["Bitcoin"]
    assert btc["available"] is True and D(btc["value"]) == pytest.approx(
        levels[-1][1] * 500, rel=D("1e-6")
    )
    assert "volatility" in btc["detail"] and "a year (30 days)" in btc["detail"]
    assert "correlation with the Nasdaq-100 1.00 (90 days)" in btc["detail"]
    assert "from its highest stored close" in btc["detail"]
    eth = crypto["Ethereum"]
    assert eth["available"] is False and eth["detail"] == "no data"


def test_the_regime_is_a_rule_of_thumb(owner: TestClient, macro: None) -> None:
    regime = get(owner)["regime"]
    votes = {s["name"]: s["vote"] for s in regime["signals"]}
    assert votes["Real yield (3 months)"] == -1  # up 0.36 points: risk-off
    assert votes["US dollar (3 months)"] == -1  # up 3.5%: risk-off
    assert votes["Credit spread (3 months)"] == 0  # up 0.45 points, under the limit
    assert votes["Inflation (3 months)"] == 0 and votes["Brent oil (3 months)"] == 0
    assert regime["score"] == -2 and regime["label"] == "risk-off"
    assert "real yields" in regime["implication"] and "not to sell by rule" in regime["implication"]
    assert "not a forecast" in regime["disclaimer"]
    inflation = next(s for s in regime["signals"] if s["name"].startswith("Inflation"))
    assert "% a year" in inflation["detail"]


def test_few_signals_means_unknown(owner: TestClient, db: Session) -> None:
    add(db, "DFII10", "macro", "PCT", line(date(2024, 10, 1), 151, "2.00", "0.004"))
    regime = get(owner)["regime"]
    assert regime["label"] == "unknown" and "Too few" in regime["implication"]
    assert sum(1 for s in regime["signals"] if s["available"]) == 1


def test_calm_signals_are_mixed(owner: TestClient, db: Session) -> None:
    flat = line(date(2024, 10, 1), 151, "2.00", "0")
    for code, cls, cur in (("DFII10", "macro", "PCT"), ("DTWEXBGS", "macro", "IDX"),
                           ("BAMLH0A0HYM2", "macro", "PCT")):  # fmt: skip
        add(db, code, cls, cur, flat)
    assert get(owner)["regime"]["label"] == "mixed"


# --- FR-64: is gold hedging? ---------------------------------------------------------------------------------


def test_the_hedge_correlation_windows(db: Session) -> None:
    pattern = [0, 10, -5, 7, -12, 3]
    closes: markets.Series = []
    level = D(2000)
    for i in range(300):
        level += D(pattern[i % 6]) + D(1)
        closes.append((TODAY - timedelta(days=299 - i), level))
    add(db, "GOLD", "commodity", "USD", closes)
    fx(db, TODAY - timedelta(days=320), 330)
    gold = markets.returns_of(closes)
    against = {d: -2 * r for d, r in gold.items()}
    hedge = markets.hedge_correlation(db, TODAY, against)
    assert hedge.available is True
    assert [w.days for w in hedge.windows] == [60, 120, 250]
    assert all(w.correlation == pytest.approx(-1.0) for w in hedge.windows)
    assert "hedging" in hedge.verdict and "not hedging you" not in hedge.verdict
    together = markets.hedge_correlation(db, TODAY, dict(gold))
    assert all(w.correlation == pytest.approx(1.0) for w in together.windows)
    assert "not hedging you" in together.verdict
    few = markets.hedge_correlation(db, TODAY, {TODAY: 0.01})
    assert few.available is False and "Not enough" in few.verdict
    assert markets.hedge_correlation(db, TODAY, {}).available is False


def test_the_api_has_a_hedge_block(owner: TestClient) -> None:
    hedge = get(owner)["hedge"]
    assert hedge["available"] is False and hedge["windows"] == []
    assert "Not enough" in hedge["verdict"]


# --- FR-62: tradable products ------------------------------------------------------------------------------------


def product(**kw: object) -> dict[str, object]:
    return {
        "commodity": "gold", "isin": "DE000A0S9GB0", "name": "Xetra-Gold", "kind": "physical",
        "issuer": "Deutsche Boerse Commodities", **kw,
    }  # fmt: skip


def test_add_list_and_remove_a_product(owner: TestClient, db: Session) -> None:
    created = owner.post("/api/markets/products", json=product(ter="0.35", tr_available=True))
    assert created.status_code == 201, created.text
    [row] = created.json()
    assert (
        row["name"] == "Xetra-Gold" and D(row["ter"]) == D("0.35") and row["tr_available"] is True
    )
    assert "vault" in row["risk"]
    owner.post(
        "/api/markets/products",
        json=product(isin="XS0000000001", name="Oil Fut", kind="futures", commodity="wti"),
    )
    listed = get(owner)["products"]
    assert [(p["commodity"], p["name"]) for p in listed] == [
        ("gold", "Xetra-Gold"),
        ("wti", "Oil Fut"),
    ]
    futures = next(p for p in listed if p["kind"] == "futures")
    assert "Contango" in futures["risk"] and futures["tr_available"] is None  # unknown, not "no"
    assert owner.delete(f"/api/markets/products/{row['id']}").status_code == 204
    assert owner.delete(f"/api/markets/products/{row['id']}").status_code == 404
    assert (
        db.scalar(select(CommodityProduct).where(CommodityProduct.isin == "DE000A0S9GB0")) is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"isin": "SHORT"},
        {"isin": "1234567890AB"},
        {"commodity": "platinum"},
        {"kind": "magic"},
        {"ter": "-1"},
        {"name": ""},
    ],
)
def test_bad_products_are_refused(owner: TestClient, bad: dict[str, object]) -> None:
    assert owner.post("/api/markets/products", json=product(**bad)).status_code == 422


def test_a_product_cannot_be_listed_twice(owner: TestClient) -> None:
    assert owner.post("/api/markets/products", json=product()).status_code == 201
    again = owner.post("/api/markets/products", json=product(name="Same again"))
    assert again.status_code == 409 and "already listed" in again.text


def test_only_an_admin_edits_products_but_everyone_reads_them(
    owner: TestClient, client: TestClient, db: Session
) -> None:
    owner.post("/api/markets/products", json=product())
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert (
        client.post("/api/markets/products", json=product(isin="XS0000000002")).status_code == 403
    )
    assert client.delete("/api/markets/products/1").status_code == 403
    assert [p["name"] for p in get(client)["products"]] == ["Xetra-Gold"]


def test_the_note_says_nothing_is_traded_and_the_gate_holds(
    owner: TestClient, client: TestClient, db: Session
) -> None:
    assert "places no trade" in get(owner)["note"]
    make_user(db, "new@example.com", accepted=False)
    client.cookies.clear()
    assert login(client, "new@example.com") == 200
    assert client.get("/api/markets").status_code == 403

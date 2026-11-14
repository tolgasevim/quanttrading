"""Performance and risk (FR-21, FR-22) over synthetic prices and manual transactions."""

from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant.models import Instrument, PriceEOD, User
from quant.portfolio import performance, service

from .conftest import login, make_user

A, B = "US0000000001", "US0000000002"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "today", lambda: date(2025, 2, 28))


def instrument(db: Session, code: str, isin: str | None, currency: str = "EUR") -> Instrument:
    row = Instrument(code=code, isin=isin, name=code, asset_class="stock", currency=currency)
    db.add(row)
    db.flush()
    return row


def prices(db: Session, row: Instrument, start: date, end: date, path: dict[date, str]) -> None:
    """A close for every weekday: the value of the latest date in `path` on or before the day."""
    level = D(next(iter(path.values())))
    for day in performance.weekdays(start, end):
        level = D(path[day]) if day in path else level
        db.add(PriceEOD(instrument_id=row.id, date=day, close=level, currency="EUR", source="t"))
    db.commit()


def buy(client: TestClient, day: str, isin: str, shares: str, price: str, **kw: str) -> None:
    body = {"side": "buy", "date": day, "isin": isin, "name": isin, "shares": shares,
            "price": price, **kw}  # fmt: skip
    assert client.post("/api/transactions", json=body).status_code == 201


def get(client: TestClient, **params: str) -> dict:  # type: ignore[type-arg]
    response = client.get("/api/performance", params=params)
    assert response.status_code == 200, response.text
    body: dict = response.json()  # type: ignore[type-arg]
    return body


def test_buys_are_flows_not_return(owner: TestClient, db: Session) -> None:
    alpha = instrument(db, "ALPHA", A)
    bench = instrument(db, "SXRV", "IE00B53SZB19")
    start, end = date(2025, 1, 6), date(2025, 2, 28)
    prices(db, alpha, start, end, {start: "100", date(2025, 1, 31): "110"})
    prices(db, bench, start, end, {start: "200", date(2025, 1, 31): "220"})
    buy(owner, "2025-01-06", A, "10", "100")
    buy(owner, "2025-02-10", A, "10", "110")  # more money in: not a gain
    body = get(owner)
    assert body["has_history"] is True
    assert body["start"] == "2025-01-06" and body["end"] == "2025-02-28"
    assert body["twr"] == pytest.approx(0.10, abs=1e-9)
    assert body["benchmark_twr"] == pytest.approx(0.10, abs=1e-9)
    assert D(body["current_value"]) == D("2200.00")
    assert D(body["net_invested"]) == D("2100.00")
    assert body["xirr"] is not None and body["xirr"] > 0
    assert body["benchmark_xirr"] is not None and body["benchmark_xirr"] > 0
    assert body["estimated_days"] == 0 and body["unpriced"] == []
    series = body["series"]
    assert series[0]["portfolio"] == 100 and series[0]["benchmark"] == 100
    assert series[-1]["portfolio"] == pytest.approx(110.0)
    assert series[-1]["benchmark"] == pytest.approx(110.0)
    # Too short for beta, volatility and VaR: they are blank, not made up.
    assert body["beta"] is None and body["var95"] is None
    assert {b["code"] for b in body["benchmarks"]} == {"SXRV", "EUNL", "SXR8"}


def test_a_sale_and_income_count_correctly(owner: TestClient, db: Session) -> None:
    alpha = instrument(db, "ALPHA", A)
    instrument(db, "SXRV", "IE00B53SZB19")
    start, end = date(2025, 1, 6), date(2025, 2, 28)
    prices(db, alpha, start, end, {start: "100"})  # flat price
    buy(owner, "2025-01-06", A, "10", "100")
    sell = {"side": "sell", "date": "2025-02-03", "isin": A, "shares": "4", "price": "100"}
    assert owner.post("/api/transactions", json=sell).status_code == 201
    dividend = {"side": "dividend", "date": "2025-02-05", "isin": A, "amount": "30"}
    assert owner.post("/api/transactions", json=dividend).status_code == 201
    body = get(owner)
    # Flat price: the only return is the dividend of 30 on a value of 600.
    assert body["twr"] == pytest.approx(30 / 600)
    assert D(body["current_value"]) == D("600.00")
    assert D(body["income"]) == D("30.00")
    assert D(body["net_invested"]) == D("600.00")  # 1000 in, 400 out
    assert body["benchmark_twr"] is None  # no benchmark prices stored
    assert any("benchmark" in w for w in body["warnings"])


def test_a_holding_without_prices_is_valued_at_the_last_trade(
    owner: TestClient, db: Session
) -> None:
    buy(owner, "2025-01-06", B, "5", "50")
    body = get(owner)
    assert D(body["current_value"]) == D("250.00")
    assert body["estimated"] == [B]
    assert body["estimated_days"] == body["days"] > 0
    assert any("last trade" in w for w in body["warnings"])


def test_the_comparison_starts_where_the_benchmark_does(owner: TestClient, db: Session) -> None:
    alpha = instrument(db, "ALPHA", A)
    bench = instrument(db, "SXRV", "IE00B53SZB19")
    start, end = date(2025, 1, 6), date(2025, 2, 28)
    prices(db, alpha, start, end, {start: "100", date(2025, 2, 17): "120"})
    prices(db, bench, date(2025, 2, 3), end, {date(2025, 2, 3): "50"})
    buy(owner, "2025-01-06", A, "10", "100")
    body = get(owner)
    assert body["start"] == "2025-02-03"
    assert any("2025-02-03" in w for w in body["warnings"])
    assert body["twr"] == pytest.approx(0.20)  # from the 3rd of February: the 17th's +20%
    assert body["benchmark_twr"] == pytest.approx(0.0)


def test_year_to_date_starts_from_the_last_day_of_the_year_before(
    owner: TestClient, db: Session
) -> None:
    alpha = instrument(db, "ALPHA", A)
    bench = instrument(db, "SXRV", "IE00B53SZB19")
    start, end = date(2024, 12, 2), date(2025, 2, 28)
    prices(db, alpha, start, end, {start: "100", date(2025, 1, 10): "110"})
    prices(db, bench, start, end, {start: "100"})
    buy(owner, "2024-12-02", A, "10", "100")
    everything = get(owner)
    ytd = get(owner, period="ytd")
    assert everything["start"] == "2024-12-02"
    assert ytd["start"] == "2024-12-31" and ytd["period"] == "ytd"
    assert everything["twr"] == pytest.approx(0.10) and ytd["twr"] == pytest.approx(0.10)
    # Money-weighted: the year-to-date window starts with the value then, not a deposit.
    assert ytd["xirr"] is not None


def test_risk_numbers_over_a_longer_history(owner: TestClient, db: Session) -> None:
    alpha = instrument(db, "ALPHA", A)
    beta_ = instrument(db, "BETA", B)
    bench = instrument(db, "SXRV", "IE00B53SZB19")
    start, end = date(2024, 6, 3), date(2025, 2, 28)
    days = performance.weekdays(start, end)
    # The same up-and-down path for all three (+1%, -1.2%, ...): everything moves together.
    level = D(100)
    path: dict[date, str] = {}
    for i, day in enumerate(days):
        level = level * (D("1.01") if i % 2 else D("0.988"))
        path[day] = str(level)
    for row in (alpha, beta_, bench):
        prices(db, row, start, end, path)
    buy(owner, "2024-06-03", A, "10", "100")
    buy(owner, "2024-06-03", B, "10", "100")
    body = get(owner)
    assert body["observations"] > 150
    assert body["volatility"] is not None and 0.05 < body["volatility"] < 0.5
    assert body["benchmark_volatility"] == pytest.approx(body["volatility"], rel=0.05)
    assert body["beta"] == pytest.approx(1.0, abs=0.05)
    assert 0 < body["max_drawdown"] < 0.3
    assert body["drawdown_peak"] < body["drawdown_low"]
    assert body["var95"] is not None and body["var95"] > 0
    assert body["cvar95"] >= body["var95"] - 1e-9
    corr = body["correlation"]
    assert corr is not None and set(corr["isins"]) == {A, B}
    assert corr["values"][0][1] == pytest.approx(1.0, abs=1e-6)
    assert corr["values"][0][0] == 1.0


def test_foreign_currency_prices_are_converted(owner: TestClient, db: Session) -> None:
    from quant.models import FxRate

    usd = instrument(db, "USDCO", A, currency="USD")
    start, end = date(2025, 1, 6), date(2025, 1, 31)
    for day in performance.weekdays(start, end):
        db.add(PriceEOD(instrument_id=usd.id, date=day, close=D("110"), currency="USD", source="t"))
        db.add(FxRate(quote="USD", date=day, rate=D("1.10"), source="t"))
    db.commit()
    buy(owner, "2025-01-06", A, "10", "100")
    body = get(owner)
    assert D(body["current_value"]) == D("1000.00")  # 10 x 110 USD / 1.10
    assert body["estimated_days"] > 0 or body["twr"] is not None


def test_no_history_and_other_users_are_empty(
    owner: TestClient, client: TestClient, db: Session
) -> None:
    alpha = instrument(db, "ALPHA", A)
    prices(db, alpha, date(2025, 1, 6), date(2025, 2, 28), {date(2025, 1, 6): "100"})
    buy(owner, "2025-01-06", A, "10", "100")
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    empty = get(client)
    assert empty["has_history"] is False and empty["series"] == [] and empty["twr"] is None
    assert D(empty["current_value"]) == 0


def test_bad_parameters_and_the_disclaimer_gate(
    client: TestClient, db: Session, admin: User
) -> None:
    assert login(client, admin.email) == 200
    assert client.get("/api/performance", params={"period": "5y"}).status_code == 422
    assert client.get("/api/performance", params={"benchmark": "XYZ"}).status_code == 422
    make_user(db, "new@example.com", accepted=False)
    client.cookies.clear()
    assert login(client, "new@example.com") == 200
    assert client.get("/api/performance").status_code == 403


def test_the_series_is_thinned(owner: TestClient, db: Session) -> None:
    alpha = instrument(db, "ALPHA", A)
    start, end = date(2019, 1, 7), date(2025, 2, 28)
    prices(db, alpha, start, end, {start: "100"})
    buy(owner, "2019-01-07", A, "1", "100")
    body = get(owner)
    assert len(body["series"]) <= performance.MAX_SERIES_POINTS + 1
    assert body["series"][-1]["day"] == "2025-02-28"

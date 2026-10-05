from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant.models import FxRate, Instrument, PriceEOD, User
from quant.providers.base import Bar, PriceSeries, ProviderError

from .conftest import login, make_user
from .test_holdings_pnl_api import BTC, SPIN, A, by_name, history, import_history, upload


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    import_history(client, history())
    return client


def add_instrument(
    db: Session,
    isin: str,
    currency: str = "EUR",
    source: str | None = "yahoo",
    active: bool = True,
    symbol: str | None = "SYM",
) -> Instrument:
    inst = Instrument(
        code=isin, isin=isin, name=isin, asset_class="stock", currency=currency,
        symbols={"yahoo": symbol} if symbol else {}, active=active, mapping_source=source,
    )  # fmt: skip
    db.add(inst)
    db.commit()
    return inst


def add_price(db: Session, inst: Instrument, day: date, close: str, currency: str = "EUR") -> None:
    db.add(
        PriceEOD(instrument_id=inst.id, date=day, close=D(close), currency=currency, source="yahoo")
    )
    db.commit()


def add_fx(db: Session, quote: str, day: date, rate: str) -> None:
    db.add(FxRate(quote=quote, date=day, rate=D(rate), source="ecb"))
    db.commit()


# --- valuation -----------------------------------------------------------------------------


def test_a_stored_euro_price_values_the_position(owner: TestClient, db: Session) -> None:
    add_price(db, add_instrument(db, A), date(2026, 10, 2), "120")
    body = owner.get("/api/holdings").json()
    alpha = by_name(body)["Alpha Corp"]
    assert (alpha["price"], alpha["price_as_of"]) == ("120", "2026-10-02")
    assert (alpha["market_value"], alpha["unrealised_pnl"]) == (
        "720.00",
        "119.40",
    )  # 6 x 120 - 600.60
    # Alpha is priced; Spin Co and the two coins are not.
    assert body["review"]["unpriced"] == 3


def test_a_foreign_price_is_converted_at_the_rate_of_its_own_day(
    owner: TestClient, db: Session
) -> None:
    add_price(db, add_instrument(db, A, "USD"), date(2026, 10, 2), "150", "USD")
    add_fx(db, "USD", date(2026, 9, 25), "1.1")  # older: not used
    add_fx(db, "USD", date(2026, 10, 1), "1.25")  # the latest on or before the price day
    add_fx(db, "USD", date(2026, 10, 3), "2")  # after the price day: not used
    alpha = by_name(owner.get("/api/holdings").json())["Alpha Corp"]
    assert (alpha["price"], alpha["market_value"]) == ("120", "720.00")  # 150 / 1.25


def test_a_foreign_price_without_a_rate_gives_no_value(owner: TestClient, db: Session) -> None:
    add_price(db, add_instrument(db, A, "USD"), date(2026, 10, 2), "150", "USD")
    alpha = by_name(owner.get("/api/holdings").json())["Alpha Corp"]
    assert alpha["market_value"] is None and alpha["price"] is None  # better blank than wrong


def test_the_newest_stored_price_is_used_and_inactive_instruments_are_ignored(
    owner: TestClient, db: Session
) -> None:
    inst = add_instrument(db, A)
    add_price(db, inst, date(2026, 9, 30), "100")
    add_price(db, inst, date(2026, 10, 2), "120")
    add_price(db, inst, date(2026, 10, 1), "999")
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["price"] == "120"
    inst.active = False
    db.commit()
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["price"] is None


def test_a_newer_stored_price_beats_the_statement_price_and_an_older_one_does_not(
    owner: TestClient, db: Session
) -> None:
    upload(owner)  # the statement of 2026-09-27 prices Bitcoin at 50,000
    btc = add_instrument(db, BTC, source=None)
    add_price(db, btc, date(2026, 9, 1), "40000")
    assert by_name(owner.get("/api/holdings").json())["Bitcoin"]["price"] == "50000"
    add_price(db, btc, date(2026, 10, 2), "60000")
    bitcoin = by_name(owner.get("/api/holdings").json())["Bitcoin"]
    assert (bitcoin["price"], bitcoin["price_as_of"], bitcoin["market_value"]) == (
        "60000",
        "2026-10-02",
        "6000.00",
    )


# --- the Prices page -----------------------------------------------------------------------


def statuses(client: TestClient) -> dict[str, str]:
    return {i["isin"]: i["status"] for i in client.get("/api/prices").json()["items"]}


def test_each_open_position_has_a_price_status(owner: TestClient, db: Session) -> None:
    assert statuses(owner)[A] == "not_checked" and statuses(owner)[SPIN] == "not_checked"
    assert statuses(owner)[BTC] == "unsupported"  # crypto is priced from the statement
    add_instrument(db, SPIN, source="none", active=False, symbol=None)
    assert statuses(owner)[SPIN] == "unmapped"
    add_instrument(db, A)
    assert statuses(owner)[A] == "waiting"
    add_price(db, db.query(Instrument).filter_by(isin=A).one(), date(2026, 10, 2), "120")
    assert statuses(owner)[A] == "priced"
    items = {i["isin"]: i for i in owner.get("/api/prices").json()["items"]}
    assert (items[A]["symbol"], items[A]["last_date"], items[A]["last_close"]) == (
        "SYM",
        "2026-10-02",
        "120",
    )
    order = [i["status"] for i in owner.get("/api/prices").json()["items"]]
    assert order[0] == "unmapped"  # what needs a hand comes first


class FakeProvider:
    name = "yahoo"

    def __init__(self, series: PriceSeries | None, fail: bool = False) -> None:
        self.series = series
        self.fail = fail

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        if self.fail:
            raise ProviderError("yahoo: down")
        return self.series or PriceSeries(None, [])


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    from quant.api import prices

    monkeypatch.setattr(prices, "make_fetcher", lambda db, settings: None)
    monkeypatch.setattr(prices, "price_providers", lambda names, fetcher: [provider])


def test_an_admin_can_enter_a_symbol_and_gets_prices_at_once(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    series = PriceSeries("USD", [Bar(date=date(2026, 10, 2), close=D("150"))])
    use_provider(monkeypatch, FakeProvider(series))
    response = owner.put(f"/api/prices/{A}", json={"symbol": "ALPH"})
    assert response.status_code == 200
    item = {i["isin"]: i for i in response.json()["items"]}[A]
    assert (item["status"], item["symbol"], item["mapping_source"], item["currency"]) == (
        "priced",
        "ALPH",
        "manual",
        "USD",  # the provider's currency replaces the placeholder
    )


def test_a_symbol_is_kept_when_no_price_comes_back(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(None, fail=True))
    response = owner.put(f"/api/prices/{A}", json={"symbol": "ALPH"})
    assert response.status_code == 502 and "no price came back" in response.json()["detail"]
    assert statuses(owner)[A] == "waiting"  # the symbol is saved, the price will follow


def test_symbol_entry_is_for_admins_and_checks_its_input(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(None))
    assert owner.put(f"/api/prices/{A}", json={"symbol": "bad symbol"}).status_code == 422
    assert owner.put(f"/api/prices/{A}", json={"symbol": ""}).status_code == 422
    assert owner.put("/api/prices/not-an-isin", json={"symbol": "X"}).status_code == 422
    # Not an open position of this user.
    assert owner.put("/api/prices/US0000000999", json={"symbol": "X"}).status_code == 404
    make_user(db, "sister@example.com")
    other = TestClient(owner.app)
    login(other, "sister@example.com")
    assert other.put(f"/api/prices/{A}", json={"symbol": "X"}).status_code == 403
    assert other.get("/api/prices").json() == {"items": []}  # her own (empty) holdings only


def test_prices_need_a_login(client: TestClient) -> None:
    assert client.get("/api/prices").status_code == 401
    assert client.put(f"/api/prices/{A}", json={"symbol": "X"}).status_code == 401

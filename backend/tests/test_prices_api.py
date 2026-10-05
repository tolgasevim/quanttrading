from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant.models import FxRate, Instrument, PriceEOD, User
from quant.providers.base import Bar, PriceSeries, ProviderError

from .conftest import login, make_user
from .test_holdings_pnl_api import BTC, SPIN, A, by_name, history, import_history, upload


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prices are dated in October 2026; the age test must not drift with the real date."""
    from quant.portfolio import service

    monkeypatch.setattr(service, "today", lambda: date(2026, 10, 5))


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
    assert statuses(owner)[BTC] == "not_checked"  # a coin is looked up like a share
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
    def __init__(self, series: PriceSeries | None, fail: bool = False, name: str = "yahoo") -> None:
        self.name = name
        self.series = series
        self.fail = fail

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        if self.fail:
            raise ProviderError("yahoo: down")
        return self.series or PriceSeries(None, [])


class Closable:
    """Stands in for the HTTP fetcher; remembers whether the request released it."""

    closed = 0

    def close(self) -> None:
        Closable.closed += 1


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    from quant.api import prices

    monkeypatch.setattr(prices, "make_fetcher", lambda db, settings: Closable())
    monkeypatch.setattr(prices, "price_providers", lambda names, fetcher, key=None: [provider])


def test_an_admin_can_enter_a_symbol_and_gets_prices_at_once(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    series = PriceSeries("USD", [Bar(date=date(2026, 10, 2), close=D("150"))])
    use_provider(monkeypatch, FakeProvider(series))
    add_fx(db, "USD", date(2026, 10, 1), "1.25")
    response = owner.put(f"/api/prices/{A}", json={"symbol": "ALPH"})
    assert response.status_code == 200
    item = {i["isin"]: i for i in response.json()["items"]}[A]
    assert (item["status"], item["symbol"], item["mapping_source"], item["currency"]) == (
        "priced",
        "ALPH",
        "manual",
        "USD",  # the provider's currency replaces the placeholder
    )


def test_a_new_ticker_that_returns_no_price_changes_nothing(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(None, fail=True))
    response = owner.put(f"/api/prices/{A}", json={"symbol": "ALPH"})
    assert response.status_code == 502 and "nothing was changed" in response.json()["detail"]
    assert statuses(owner)[A] == "not_checked"  # no row was saved


def test_a_typo_cannot_wipe_the_prices_of_a_working_ticker(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = add_instrument(db, A, symbol="GOOD")
    add_price(db, inst, date(2026, 10, 2), "120")
    use_provider(monkeypatch, FakeProvider(PriceSeries(None, [])))  # the typo has no data
    assert owner.put(f"/api/prices/{A}", json={"symbol": "GOOOD"}).status_code == 502
    db.refresh(inst)
    assert inst.symbols == {"yahoo": "GOOD"} and inst.mapping_source == "yahoo"
    assert db.query(PriceEOD).filter_by(instrument_id=inst.id).count() == 1  # history intact
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["price"] == "120"


def test_the_same_ticker_can_be_fetched_again_when_it_has_no_price_yet(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_instrument(db, A, symbol="ALPH")  # a ticker was found, no price came yet
    use_provider(monkeypatch, FakeProvider(None, fail=True))
    response = owner.put(f"/api/prices/{A}", json={"symbol": "ALPH"})
    assert response.status_code == 502 and "Saved the symbol" in response.json()["detail"]
    assert statuses(owner)[A] == "waiting"


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


def test_a_new_ticker_replaces_the_stored_prices_of_the_old_one(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = add_instrument(db, A, "USD", symbol="OLD")
    add_price(db, inst, date(2026, 9, 1), "999", "USD")  # a price of the old ticker
    series = PriceSeries("EUR", [Bar(date=date(2026, 10, 2), close=D("120"))])
    use_provider(monkeypatch, FakeProvider(series))
    assert owner.put(f"/api/prices/{A}", json={"symbol": "NEW.DE"}).status_code == 200
    rows = db.query(PriceEOD).filter_by(instrument_id=inst.id).all()
    assert [(r.date, r.close, r.currency) for r in rows] == [(date(2026, 10, 2), D("120"), "EUR")]


def test_the_same_ticker_keeps_its_prices_when_a_retry_fails(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = add_instrument(db, A, symbol="SAME")
    add_price(db, inst, date(2026, 9, 1), "100")
    use_provider(monkeypatch, FakeProvider(None, fail=True))
    assert owner.put(f"/api/prices/{A}", json={"symbol": "SAME"}).status_code == 502
    assert db.query(PriceEOD).filter_by(instrument_id=inst.id).count() == 1


def test_the_request_releases_its_http_connections(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_instrument(db, A)
    Closable.closed = 0
    use_provider(monkeypatch, FakeProvider(PriceSeries("EUR", [Bar(date(2026, 10, 2), D("1"))])))
    owner.put(f"/api/prices/{A}", json={"symbol": "SYM"})
    use_provider(monkeypatch, FakeProvider(None, fail=True))
    owner.put(f"/api/prices/{A}", json={"symbol": "SYM"})  # a failed fetch releases it too
    assert Closable.closed == 2


def test_a_new_ticker_drops_the_symbols_other_providers_had_for_the_old_one(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = Instrument(code=A, isin=A, name="Alpha", asset_class="stock", currency="EUR",
                      symbols={"yahoo": "OLD", "stooq": "old.us"})  # fmt: skip
    db.add(inst)
    db.commit()
    use_provider(monkeypatch, FakeProvider(None, fail=True))
    owner.put(f"/api/prices/{A}", json={"symbol": "OLD"})  # same ticker: stooq stays
    db.refresh(inst)
    assert inst.symbols == {"yahoo": "OLD", "stooq": "old.us"}
    series = PriceSeries("EUR", [Bar(date=date(2026, 10, 2), close=D("120"))])
    use_provider(monkeypatch, FakeProvider(series))
    owner.put(f"/api/prices/{A}", json={"symbol": "NEW"})
    db.refresh(inst)
    assert inst.symbols == {"yahoo": "NEW"}  # the old security's stooq symbol is gone


def test_a_price_far_older_than_the_newest_one_is_not_used(owner: TestClient, db: Session) -> None:
    stale = add_instrument(db, A)
    add_price(db, stale, date(2026, 9, 1), "100")
    fresh = add_instrument(db, SPIN, symbol="OTHER")
    add_price(db, fresh, date(2026, 10, 2), "5")
    body = owner.get("/api/holdings").json()
    assert by_name(body)["Alpha Corp"]["market_value"] is None  # a month old
    assert by_name(body)["Spin Co"]["market_value"] == "30.00"
    assert body["review"]["unpriced"] == 3  # Alpha and the two coins
    assert statuses(owner)[A] == "stale" and statuses(owner)[SPIN] == "priced"
    add_price(db, stale, date(2026, 9, 28), "110")  # within ten days of the newest
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["price"] == "110"
    assert statuses(owner)[A] == "priced"


def test_when_the_price_job_stops_every_price_ages_out(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.portfolio import service

    add_price(db, add_instrument(db, A), date(2026, 10, 2), "120")
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["price"] == "120"
    monkeypatch.setattr(service, "today", lambda: date(2026, 11, 30))  # weeks without a new price
    body = owner.get("/api/holdings").json()
    assert by_name(body)["Alpha Corp"]["market_value"] is None
    assert statuses(owner)[A] == "stale"


def test_the_prices_page_never_says_priced_when_holdings_shows_no_value(
    owner: TestClient, db: Session
) -> None:
    usd = add_instrument(db, A, "USD")
    add_price(db, usd, date(2026, 10, 2), "150", "USD")  # no ECB rate for USD stored
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["market_value"] is None
    assert statuses(owner)[A] == "no_rate"
    add_fx(db, "USD", date(2026, 10, 1), "1.25")
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["market_value"] == "720.00"
    assert statuses(owner)[A] == "priced"
    usd.active = False
    db.commit()
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["market_value"] is None
    assert statuses(owner)[A] == "inactive"


def test_a_rate_that_is_far_older_than_the_price_is_not_used(
    owner: TestClient, db: Session
) -> None:
    add_price(db, add_instrument(db, A, "RUB"), date(2026, 10, 2), "9000", "RUB")
    add_fx(db, "RUB", date(2022, 2, 28), "100")  # the last rate the ECB ever published
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["market_value"] is None
    assert statuses(owner)[A] == "no_rate"
    add_fx(db, "RUB", date(2026, 9, 25), "90")  # seven days before the price day: still usable
    assert by_name(owner.get("/api/holdings").json())["Alpha Corp"]["price"] == "100"  # 9000 / 90


def test_a_fund_row_keeps_its_class_and_a_coin_row_keeps_its_symbols(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(None, fail=True))
    seeded = Instrument(code="BTC", isin=BTC, name="Bitcoin", asset_class="crypto", currency="EUR",
                        symbols={"yahoo": "BTC-EUR"})  # fmt: skip
    db.add(seeded)
    db.commit()
    # A Yahoo-style ticker is not a coin id, and a failed fetch changes nothing.
    assert owner.put(f"/api/prices/{BTC}", json={"symbol": "BTC-EUR"}).status_code == 422
    assert owner.put(f"/api/prices/{BTC}", json={"symbol": "bitcoin"}).status_code == 502
    db.refresh(seeded)
    assert (seeded.asset_class, seeded.symbols, seeded.mapping_source) == (
        "crypto",
        {"yahoo": "BTC-EUR"},
        None,
    )  # untouched
    etf = add_instrument(db, A)
    etf.asset_class = "etf"
    db.commit()
    owner.put(f"/api/prices/{A}", json={"symbol": "SYM"})  # the position is a STOCK
    db.refresh(etf)
    assert etf.asset_class == "etf"  # a fund row stays a fund row


COIN_SERIES = PriceSeries("EUR", [Bar(date=date(2026, 10, 2), close=D("50000"))])


def test_an_admin_can_enter_a_coingecko_id_for_a_coin(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(COIN_SERIES, name="coingecko"))
    response = owner.put(f"/api/prices/{BTC}", json={"symbol": "bitcoin"})
    assert response.status_code == 200, response.text
    item = {i["isin"]: i for i in response.json()["items"]}[BTC]
    assert (item["status"], item["symbol"], item["mapping_source"], item["currency"]) == (
        "priced",
        "bitcoin",
        "manual",
        "EUR",
    )
    inst = db.query(Instrument).filter_by(isin=BTC).one()
    assert inst.symbols == {"coingecko": "bitcoin"} and inst.asset_class == "crypto"
    # 0.1 BTC at 50,000 EUR with no exchange rate involved; it cost 4,000.
    bitcoin = by_name(owner.get("/api/holdings").json())["Bitcoin"]
    assert (bitcoin["market_value"], bitcoin["unrealised_pnl"]) == ("5000.00", "1000.00")


def test_a_coin_id_is_checked_before_anything_is_fetched(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(COIN_SERIES, name="coingecko"))
    for bad in ["Bitcoin", "bit coin", "../x", "BTC-EUR"]:
        response = owner.put(f"/api/prices/{BTC}", json={"symbol": bad})
        assert response.status_code == 422 and "coin id" in response.json()["detail"]
    assert statuses(owner)[BTC] == "not_checked"


def test_a_coin_ticker_needs_coingecko_among_the_price_providers(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.api import prices
    from quant.config import Settings

    monkeypatch.setattr(prices, "get_settings", lambda: Settings(price_providers=["yahoo"]))
    response = owner.put(f"/api/prices/{BTC}", json={"symbol": "bitcoin"})
    assert response.status_code == 409 and "coingecko" in response.json()["detail"]
    assert statuses(owner)[BTC] == "unsupported"  # coins are off: the statement prices them


def test_the_unpriced_count_covers_coins_only_while_coingecko_is_on(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.api import holdings
    from quant.config import Settings

    # Alpha and Spin Co are shares without a price; the two coins have none either.
    assert owner.get("/api/holdings").json()["review"]["unpriced"] == 4
    monkeypatch.setattr(
        holdings, "get_settings", lambda: Settings(price_providers=["yahoo", "stooq"])
    )
    assert owner.get("/api/holdings").json()["review"]["unpriced"] == 2  # coins are off


def test_a_manual_ticker_needs_yahoo_among_the_price_providers(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.api import prices
    from quant.config import Settings

    monkeypatch.setattr(prices, "get_settings", lambda: Settings(price_providers=["stooq"]))
    response = owner.put(f"/api/prices/{A}", json={"symbol": "ALPH"})
    assert response.status_code == 409 and "QT_PRICE_PROVIDERS" in response.json()["detail"]
    assert statuses(owner)[A] == "not_checked"  # nothing was changed


def test_a_save_that_loses_a_race_with_the_mapping_job_gets_a_clear_conflict(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.api import prices
    from quant.db import get_sessionmaker
    from quant.ingest.prices import fetch_with_fallback

    series = PriceSeries("EUR", [Bar(date=date(2026, 10, 2), close=D("120"))])
    use_provider(monkeypatch, FakeProvider(series))
    real = fetch_with_fallback

    def probe_then_the_job_creates_the_row(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = real(*args, **kwargs)
        with get_sessionmaker()() as other:  # the mapping job saves the same instrument now
            other.add(Instrument(code=A, isin=A, name="Alpha", asset_class="stock",
                                 currency="EUR", symbols={"yahoo": "JOB"}, mapping_source="yahoo"))  # fmt: skip
            other.commit()
        return result

    monkeypatch.setattr(prices, "fetch_with_fallback", probe_then_the_job_creates_the_row)
    response = owner.put(f"/api/prices/{A}", json={"symbol": "ALPH"})
    assert response.status_code == 409 and "try again" in response.json()["detail"]


class RecordingProvider(FakeProvider):
    def __init__(self, series: PriceSeries) -> None:
        super().__init__(series)
        self.calls: list[tuple[str, date, date]] = []

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        self.calls.append((symbol, start, end))
        return super().fetch_eod(symbol, start, end)


def test_a_new_ticker_is_checked_against_its_whole_history_with_one_fetch(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = add_instrument(db, A, "USD", symbol="OLD")
    add_price(db, inst, date(2026, 9, 1), "999", "USD")
    # An illiquid fund: its last price is months old, which a 10-day probe would have refused.
    series = PriceSeries(
        "EUR",
        [Bar(date=date(2026, 3, 2), close=D("50")), Bar(date=date(2026, 3, 3), close=D("51"))],
    )
    provider = RecordingProvider(series)
    use_provider(monkeypatch, provider)
    from quant.config import get_settings

    assert owner.put(f"/api/prices/{A}", json={"symbol": "FUND.DE"}).status_code == 200
    assert len(provider.calls) == 1  # one fetch: the old prices are swapped, not fetched again
    symbol, start, end = provider.calls[0]
    assert symbol == "FUND.DE" and (end - start).days == get_settings().backfill_days
    rows = db.query(PriceEOD).filter_by(instrument_id=inst.id).order_by(PriceEOD.date).all()
    assert [(r.date, r.close, r.currency) for r in rows] == [
        (date(2026, 3, 2), D("50"), "EUR"),
        (date(2026, 3, 3), D("51"), "EUR"),
    ]
    db.refresh(inst)
    assert (inst.currency, inst.symbols, inst.mapping_source) == (
        "EUR",
        {"yahoo": "FUND.DE"},
        "manual",
    )


def test_a_new_ticker_without_a_currency_changes_nothing(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = add_instrument(db, A, "USD", symbol="OLD")
    add_price(db, inst, date(2026, 10, 2), "150", "USD")
    use_provider(monkeypatch, FakeProvider(PriceSeries(None, [Bar(date(2026, 10, 2), D("9"))])))
    response = owner.put(f"/api/prices/{A}", json={"symbol": "NEW"})
    assert response.status_code == 502 and "did not report a currency" in response.json()["detail"]
    db.refresh(inst)
    assert inst.symbols == {"yahoo": "OLD"} and inst.currency == "USD"
    assert db.query(PriceEOD).filter_by(instrument_id=inst.id).one().close == D("150")


def test_a_row_without_a_yahoo_ticker_keeps_prices_in_the_same_currency_and_drops_other_currencies(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = Instrument(code=A, isin=A, name="Alpha", asset_class="stock", currency="EUR",
                      symbols={"stooq": "a.de"})  # fmt: skip
    db.add(inst)
    db.commit()
    add_price(db, inst, date(2025, 1, 2), "90")  # EUR, before the new history starts
    series = PriceSeries("EUR", [Bar(date=date(2026, 10, 2), close=D("120"))])
    use_provider(monkeypatch, FakeProvider(series))
    assert owner.put(f"/api/prices/{A}", json={"symbol": "ALPH.DE"}).status_code == 200
    db.refresh(inst)
    assert inst.symbols == {"stooq": "a.de", "yahoo": "ALPH.DE"}  # the same security: kept
    assert db.query(PriceEOD).filter_by(instrument_id=inst.id).count() == 2

    other = Instrument(code="OTHER", isin=SPIN, name="Spin", asset_class="stock", currency="EUR",
                       symbols={"stooq": "s.us"})  # fmt: skip
    db.add(other)
    db.commit()
    add_price(db, other, date(2025, 1, 2), "5")  # EUR history of an instrument now quoted in USD
    use_provider(monkeypatch, FakeProvider(PriceSeries("USD", [Bar(date(2026, 10, 2), D("7"))])))
    add_fx(db, "USD", date(2026, 10, 1), "1.25")
    # The EUR rows must not survive a switch to USD.
    owner.put(f"/api/prices/{SPIN}", json={"symbol": "SPN"})
    db.refresh(other)
    rows = db.query(PriceEOD).filter_by(instrument_id=other.id).all()
    assert {r.currency for r in rows} == {"USD"} and other.symbols == {"yahoo": "SPN"}


def test_a_mapped_coin_is_unsupported_on_both_pages_once_coingecko_is_off(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.api import holdings, prices
    from quant.config import Settings

    inst = add_instrument(db, BTC)
    inst.asset_class, inst.symbols, inst.mapping_source = (
        "crypto",
        {"coingecko": "bitcoin"},
        "coingecko",
    )
    db.commit()
    assert statuses(owner)[BTC] == "waiting"  # mapped, no price yet
    off = lambda: Settings(price_providers=["yahoo", "stooq"])  # noqa: E731
    monkeypatch.setattr(prices, "get_settings", off)
    monkeypatch.setattr(holdings, "get_settings", off)
    assert statuses(owner)[BTC] == "unsupported"
    assert owner.get("/api/holdings").json()["review"]["unpriced"] == 2  # Alpha and Spin Co only


def test_a_coin_with_a_usable_stored_price_stays_priced_when_coingecko_goes_off(
    owner: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.api import holdings, prices
    from quant.config import Settings

    inst = add_instrument(db, BTC)
    inst.asset_class, inst.symbols = "crypto", {"coingecko": "bitcoin"}
    db.commit()
    add_price(db, inst, date(2026, 10, 3), "50000")
    off = lambda: Settings(price_providers=["yahoo", "stooq"])  # noqa: E731
    monkeypatch.setattr(prices, "get_settings", off)
    monkeypatch.setattr(holdings, "get_settings", off)
    assert statuses(owner)[BTC] == "priced"  # the value on Holdings is still there
    assert by_name(owner.get("/api/holdings").json())["Bitcoin"]["market_value"] == "5000.00"
    monkeypatch.setattr(
        "quant.portfolio.service.today", lambda: date(2026, 11, 1)
    )  # the price has aged out
    assert statuses(owner)[BTC] == "unsupported"


def test_a_long_coin_id_is_accepted(owner: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, FakeProvider(COIN_SERIES, name="coingecko"))
    long_id = "a-" + "very-" * 12 + "long-coin"  # 69 characters, still a valid id
    response = owner.put(f"/api/prices/{BTC}", json={"symbol": long_id})
    assert response.status_code == 200, response.text
    assert {i["isin"]: i for i in response.json()["items"]}[BTC]["symbol"] == long_id

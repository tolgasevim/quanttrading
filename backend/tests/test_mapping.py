from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ingest.mapping import HeldIsin, held_isins, map_isins
from quant.ingest.prices import ingest_prices
from quant.models import Instrument, PriceEOD, User
from quant.providers.base import Bar, PriceSeries, ProviderError
from quant.providers.resolvers import Listing

from .conftest import login
from .test_holdings_pnl_api import BTC, ETH, SPIN, A, B, history, import_history, row

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
FUND = "IE0000000030"
GAMMA = "US0000000040"


class FakeResolver:
    def __init__(self, name: str, answers: dict[str, Listing | None], fail: bool = False) -> None:
        self.name = name
        self.answers = answers
        self.fail = fail
        self.asked: list[str] = []

    def resolve(self, isin: str) -> Listing | None:
        self.asked.append(isin)
        if self.fail:
            raise ProviderError(f"{self.name}: down")
        return self.answers.get(isin)


def listing(symbol: str, source: str = "fake", name: str | None = None) -> Listing:
    return Listing(symbol=symbol, name=name, exchange=None, source=source)


@pytest.fixture
def held(client: TestClient, admin, db: Session) -> list[HeldIsin]:  # type: ignore[no-untyped-def]
    login(client, admin.email)
    fund_buy = row(
        70, "2025-02-01", "TRADING", "BUY", "FUND", "World ETF", FUND,
        shares="3", price="80", amount="-240", fee="-1",
    )  # fmt: skip
    gamma_buy = row(
        71, "2025-02-02", "TRADING", "BUY", "STOCK", "Gamma Inc", GAMMA,
        shares="2", price="50", amount="-100", fee="-1",
    )  # fmt: skip
    import_history(client, history([fund_buy, gamma_buy]))
    rls.bypass(db)
    return held_isins(db)


def test_only_priceable_isins_that_are_still_held_are_listed(held: list[HeldIsin]) -> None:
    by_isin = {h.isin: h for h in held}
    # The two coins are held too, and priced; not the cash-only ISIN, and not Beta, sold in full.
    assert set(by_isin) == {A, GAMMA, SPIN, FUND, BTC, ETH}
    assert B not in by_isin
    assert by_isin[A].asset_class == "stock" and by_isin[FUND].asset_class == "etf"
    assert by_isin[BTC].asset_class == "crypto" and by_isin[BTC].name == "Bitcoin"
    assert by_isin[A].name == "Alpha Corp"


def test_found_isins_become_active_instruments_with_a_symbol(
    db: Session, held: list[HeldIsin]
) -> None:
    resolver = FakeResolver(
        "yahoo",
        {A: listing("ALPH", "yahoo", "Alpha Corporation"), FUND: listing("WRLD.DE", "yahoo")},
    )
    result = map_isins(db, [resolver], NOW, isins=held)
    assert result.rows_written == 4 and result.attempted == 4
    alpha = db.scalar(select(Instrument).where(Instrument.isin == A))
    assert alpha is not None
    assert (alpha.code, alpha.name, alpha.symbols, alpha.active) == (
        A,
        "Alpha Corporation",
        {"yahoo": "ALPH"},
        True,
    )
    assert (alpha.mapping_source, alpha.asset_class, alpha.mapped_at) == ("yahoo", "stock", NOW)
    fund = db.scalar(select(Instrument).where(Instrument.isin == FUND))
    assert fund is not None and fund.asset_class == "etf" and fund.name == "World ETF"


def test_unknown_isins_become_inactive_rows_that_are_not_asked_again_soon(
    db: Session, held: list[HeldIsin]
) -> None:
    resolver = FakeResolver("yahoo", {})
    result = map_isins(db, [resolver], NOW, isins=held)
    assert set(result.warnings) == {A, GAMMA, SPIN, FUND}
    unknown = db.scalar(select(Instrument).where(Instrument.isin == SPIN))
    assert unknown is not None
    assert (unknown.active, unknown.mapping_source, unknown.symbols) == (False, "none", {})
    resolver.asked.clear()
    map_isins(db, [resolver], NOW + timedelta(days=5), isins=held)
    assert resolver.asked == []  # asked recently
    map_isins(db, [resolver], NOW + timedelta(days=31), isins=held)
    assert sorted(resolver.asked) == sorted([A, GAMMA, SPIN, FUND])  # a month on, ask again


def test_a_later_answer_replaces_the_none_row(db: Session, held: list[HeldIsin]) -> None:
    map_isins(db, [FakeResolver("yahoo", {})], NOW, isins=held)
    found = FakeResolver("yahoo", {SPIN: listing("SPN", "yahoo")})
    map_isins(db, [found], NOW + timedelta(days=40), isins=held)
    spin = db.scalar(select(Instrument).where(Instrument.isin == SPIN))
    assert spin is not None and (spin.active, spin.symbols) == (True, {"yahoo": "SPN"})
    assert db.scalar(select(Instrument).where(Instrument.code == SPIN)) is spin  # no duplicate


def test_the_next_resolver_is_tried_when_one_fails(db: Session, held: list[HeldIsin]) -> None:
    down = FakeResolver("yahoo", {}, fail=True)
    figi = FakeResolver("openfigi", {A: listing("ALPH", "openfigi")})
    result = map_isins(db, [down, figi], NOW, isins=held)
    alpha = db.scalar(select(Instrument).where(Instrument.isin == A))
    assert alpha is not None and alpha.mapping_source == "openfigi"
    assert A not in result.errors  # a fallback answered, nothing to report


def test_a_partial_answer_shows_no_ticker_now_and_is_asked_again_after_a_day(
    db: Session, held: list[HeldIsin]
) -> None:
    down = FakeResolver("yahoo", {}, fail=True)
    figi = FakeResolver("openfigi", {A: listing("ALPH", "openfigi")})
    result = map_isins(db, [down, figi], NOW, isins=held)
    # OpenFIGI said "not found" for GAMMA and Yahoo could not be asked: say "no ticker found" so
    # the Prices page shows it, but remember that the answer was incomplete.
    assert GAMMA not in result.errors and "yahoo: down" in result.warnings[GAMMA]
    other = db.scalar(select(Instrument).where(Instrument.isin == GAMMA))
    assert other is not None and other.mapping_source == "none" and not other.active
    figi.asked.clear()
    map_isins(db, [down, figi], NOW + timedelta(hours=12), isins=held)
    assert figi.asked == []  # not yet: half a day
    map_isins(db, [FakeResolver("yahoo", {}), figi], NOW + timedelta(days=2), isins=held)
    assert GAMMA in figi.asked  # a day on: asked again, not after 30 days
    # A complete "not found" waits the full 30 days.
    full = FakeResolver("yahoo", {})
    map_isins(db, [full, figi], NOW + timedelta(days=2), isins=held)
    full.asked.clear()
    map_isins(db, [full, figi], NOW + timedelta(days=5), isins=held)
    assert full.asked == []


def test_when_nobody_can_be_asked_nothing_is_remembered(db: Session, held: list[HeldIsin]) -> None:
    result = map_isins(db, [FakeResolver("yahoo", {}, fail=True)], NOW, isins=held)
    assert set(result.errors) == {A, GAMMA, SPIN, FUND}
    assert db.scalars(select(Instrument)).all() == []  # so the next run simply tries again


def test_seeded_and_manual_instruments_are_left_alone(db: Session, held: list[HeldIsin]) -> None:
    db.add(Instrument(code="ALPHA", isin=A, name="Seeded", asset_class="stock", currency="EUR",
                      symbols={"yahoo": "A.DE"}))  # fmt: skip
    db.add(Instrument(code=GAMMA, isin=GAMMA, name="Typed", asset_class="stock", currency="EUR",
                      symbols={"yahoo": "GAMMA.DE"}, mapping_source="manual"))  # fmt: skip
    db.commit()
    resolver = FakeResolver("yahoo", {A: listing("OTHER"), GAMMA: listing("OTHER")})
    map_isins(db, [resolver], NOW, isins=held)
    assert A not in resolver.asked and GAMMA not in resolver.asked
    seeded = db.scalar(select(Instrument).where(Instrument.isin == A))
    assert seeded is not None and seeded.symbols == {"yahoo": "A.DE"}


class OnePrice:
    name = "yahoo"

    def __init__(self, currency: str | None) -> None:
        self.currency = currency

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        return PriceSeries(self.currency, [Bar(date=end, close=D("12.5"))])


def test_a_mapped_instrument_takes_the_currency_the_provider_reports(
    db: Session, held: list[HeldIsin]
) -> None:
    mapped = Instrument(code=A, isin=A, name="Alpha", asset_class="stock", currency="EUR",
                        symbols={"yahoo": "ALPH"}, mapping_source="yahoo")  # fmt: skip
    seeded = Instrument(code="SEEDED", isin=B, name="Seeded", asset_class="stock", currency="EUR",
                        symbols={"yahoo": "SEED"})  # fmt: skip
    db.add_all([mapped, seeded])
    db.commit()
    result = ingest_prices(db, [OnePrice("USD")], date(2026, 10, 6), 30)
    db.refresh(mapped)
    db.refresh(seeded)
    assert mapped.currency == "USD"  # the placeholder is replaced
    assert seeded.currency == "EUR" and "SEEDED" in result.warnings  # a seeded one is only warned


def test_a_ticker_saved_during_a_lookup_is_not_overwritten(
    db: Session, held: list[HeldIsin]
) -> None:
    from quant.db import get_sessionmaker

    map_isins(db, [FakeResolver("yahoo", {})], NOW, isins=held)  # every ISIN is now "none"

    class SavesWhileAsking(FakeResolver):
        def resolve(self, isin: str) -> Listing | None:
            if isin == A:  # an admin enters the ticker by hand while the job is running
                with get_sessionmaker()() as other:
                    row = other.scalar(select(Instrument).where(Instrument.isin == A))
                    assert row is not None
                    row.symbols = {"yahoo": "HAND"}
                    row.mapping_source = "manual"
                    row.active = True
                    other.commit()
            return None

    map_isins(db, [SavesWhileAsking("yahoo", {})], NOW + timedelta(days=40), isins=held)
    alpha = db.scalar(select(Instrument).where(Instrument.isin == A))
    assert alpha is not None
    assert (alpha.symbols, alpha.mapping_source, alpha.active) == (
        {"yahoo": "HAND"},
        "manual",
        True,
    )


def test_a_mapped_instrument_without_a_reported_currency_is_not_priced_blindly(
    db: Session, held: list[HeldIsin]
) -> None:
    mapped = Instrument(code=A, isin=A, name="Alpha", asset_class="stock", currency="EUR",
                        symbols={"yahoo": "ALPH"}, mapping_source="yahoo")  # fmt: skip
    db.add(mapped)
    db.commit()
    # First fetch: no currency in the answer, and the stored one is only a placeholder.
    first = ingest_prices(db, [OnePrice(None)], date(2026, 10, 6), 30)
    assert "did not report a currency" in first.errors[A]
    assert db.query(PriceEOD).count() == 0  # nothing stored in a guessed currency
    # Once a price has confirmed the currency, a later answer without one may rely on it.
    ingest_prices(db, [OnePrice("USD")], date(2026, 10, 6), 30)
    later = ingest_prices(db, [OnePrice(None)], date(2026, 10, 7), 30)
    assert A not in later.errors
    assert {r.currency for r in db.query(PriceEOD).all()} == {"USD"}


def test_held_isins_follow_the_position_engine_when_a_row_has_no_class(
    client: TestClient, admin: User, db: Session
) -> None:
    login(client, admin.email)
    delta, echo = "US0000000050", "US0000000051"
    rows = [
        # Delta: a share bought, then sold in full by a row that carries no class.
        row(80, "2025-01-10", "TRADING", "BUY", "STOCK", "Delta Inc", delta, shares="4", price="10", amount="-40", fee="-1"),
        row(81, "2025-02-10", "TRADING", "SELL", "", "Delta Inc", delta, shares="-4", price="12", amount="48", fee="-1"),
        # Echo: bought by a row with no class, partly sold by a share row: 3 units are held.
        row(82, "2025-01-11", "TRADING", "BUY", "", "Echo Inc", echo, shares="5", price="10", amount="-50", fee="-1"),
        row(83, "2025-02-11", "TRADING", "SELL", "STOCK", "Echo Inc", echo, shares="-2", price="12", amount="24", fee="-1"),
    ]  # fmt: skip
    import_history(client, history(rows))
    rls.bypass(db)
    isins = {h.isin: h for h in held_isins(db)}
    assert delta not in isins  # nothing left to price
    assert echo in isins and isins[echo].asset_class == "stock"


class Recording:
    name = "yahoo"

    def __init__(self) -> None:
        self.symbols: list[str] = []

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        self.symbols.append(symbol)
        return PriceSeries("EUR", [Bar(date=end, close=D("1"))])


def test_the_price_job_skips_instruments_nobody_holds_any_more(
    db: Session, held: list[HeldIsin]
) -> None:
    for isin, symbol in ((A, "ALPH"), (B, "BETA")):  # B was sold in full
        db.add(Instrument(code=isin, isin=isin, name=isin, asset_class="stock", currency="EUR",
                          symbols={"yahoo": symbol}, mapping_source="yahoo"))  # fmt: skip
    db.add(Instrument(code="NDX", name="Index", asset_class="index", currency="USD",
                      symbols={"yahoo": "^NDX"}))  # fmt: skip
    db.commit()
    provider = Recording()
    ingest_prices(db, [provider], date(2026, 10, 6), 30)
    assert sorted(provider.symbols) == ["ALPH", "^NDX"]  # a seeded one is always fetched
    only_b = Recording()
    ingest_prices(db, [only_b], date(2026, 10, 6), 30, codes=[B])
    assert only_b.symbols == ["BETA"]  # asked for by name: fetched


class FakeCoins:
    name = "coingecko"

    def __init__(self, answers: dict[str, str], fail: bool = False) -> None:
        self.answers = answers
        self.fail = fail
        self.asked: list[tuple[str, str | None]] = []

    def resolve(self, isin: str, name: str | None) -> Listing | None:
        self.asked.append((isin, name))
        if self.fail:
            raise ProviderError("coingecko: down")
        coin = self.answers.get(name or "")
        if coin is None:
            return None
        return Listing(coin, name, None, "coingecko", provider="coingecko")


def test_a_coin_is_looked_up_by_name_and_priced_from_coingecko(
    db: Session, held: list[HeldIsin]
) -> None:
    shares = FakeResolver("yahoo", {A: listing("ALPH", "yahoo")})
    coins = FakeCoins({"Bitcoin": "bitcoin"})
    result = map_isins(db, [shares], NOW, isins=held, crypto=[coins])
    btc = db.scalar(select(Instrument).where(Instrument.isin == BTC))
    assert btc is not None
    assert (btc.symbols, btc.asset_class, btc.currency, btc.mapping_source, btc.active) == (
        {"coingecko": "bitcoin"},
        "crypto",
        "EUR",
        "coingecko",
        True,
    )
    assert coins.asked == [(BTC, "Bitcoin"), (ETH, "Ethereum")]  # coins only, by name
    assert BTC not in shares.asked and ETH not in shares.asked  # a coin is not an ISIN lookup
    assert set(result.warnings) >= {ETH}  # Ethereum was not found
    eth = db.scalar(select(Instrument).where(Instrument.isin == ETH))
    assert eth is not None and (eth.active, eth.mapping_source) == (False, "none")


def test_coins_are_left_alone_when_coin_prices_are_off(db: Session, held: list[HeldIsin]) -> None:
    shares = FakeResolver("yahoo", {})
    result = map_isins(db, [shares], NOW, isins=held)  # no coin resolvers
    assert db.scalar(select(Instrument).where(Instrument.isin == BTC)) is None
    assert result.attempted == 4  # the shares and the fund only


def test_a_coin_lookup_that_fails_is_remembered_nowhere(db: Session, held: list[HeldIsin]) -> None:
    coins = FakeCoins({}, fail=True)
    result = map_isins(db, [FakeResolver("yahoo", {})], NOW, isins=held, crypto=[coins])
    assert db.scalar(select(Instrument).where(Instrument.isin == BTC)) is None
    assert "coingecko: down" in result.errors[BTC]


def test_the_price_job_stores_euro_prices_for_a_mapped_coin(
    db: Session, held: list[HeldIsin]
) -> None:
    map_isins(
        db, [FakeResolver("yahoo", {})], NOW, isins=held, crypto=[FakeCoins({"Bitcoin": "bitcoin"})]
    )

    class Gecko:
        name = "coingecko"

        def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
            assert symbol == "bitcoin"
            return PriceSeries("EUR", [Bar(date=date(2026, 10, 5), close=D("55000.5"))])

    result = ingest_prices(db, [Gecko()], date(2026, 10, 5), 400, codes=[BTC])
    assert result.errors == {} and result.rows_written == 1
    price = db.scalar(select(PriceEOD))
    assert price is not None and (price.close, price.currency, price.source) == (
        D("55000.5"),
        "EUR",
        "coingecko",
    )

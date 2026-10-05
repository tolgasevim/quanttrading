from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ingest.mapping import HeldIsin, held_isins, map_isins
from quant.ingest.prices import ingest_prices
from quant.models import Instrument, PriceEOD
from quant.providers.base import Bar, PriceSeries, ProviderError
from quant.providers.resolvers import Listing

from .conftest import login
from .test_holdings_pnl_api import SPIN, A, B, history, import_history, row

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
    # Not the coins, not the cash-only ISIN, and not Beta, which was sold in full.
    assert set(by_isin) == {A, GAMMA, SPIN, FUND}
    assert B not in by_isin
    assert by_isin[A].asset_class == "stock" and by_isin[FUND].asset_class == "etf"
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


def test_not_found_counts_only_when_every_resolver_could_answer(
    db: Session, held: list[HeldIsin]
) -> None:
    down = FakeResolver("yahoo", {}, fail=True)
    figi = FakeResolver("openfigi", {A: listing("ALPH", "openfigi")})
    result = map_isins(db, [down, figi], NOW, isins=held)
    # OpenFIGI said "not found" for GAMMA, but Yahoo could not be asked: that proves nothing.
    assert GAMMA in result.errors
    assert db.scalar(select(Instrument).where(Instrument.isin == GAMMA)) is None
    # With both able to answer, "not found" is remembered.
    answered = map_isins(db, [FakeResolver("yahoo", {}), figi], NOW, isins=held)
    other = db.scalar(select(Instrument).where(Instrument.isin == GAMMA))
    assert other is not None and other.mapping_source == "none" and GAMMA in answered.warnings


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


def test_a_mapped_instrument_takes_the_currency_the_provider_reports(db: Session) -> None:
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
    db: Session,
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

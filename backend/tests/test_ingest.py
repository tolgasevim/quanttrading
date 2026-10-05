from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quant.ingest.fx import ingest_fx
from quant.ingest.jobs import JobResult, run_job
from quant.ingest.prices import ingest_prices
from quant.models import FxRate, Instrument, JobRun, JobStatus, PriceEOD
from quant.providers import ecb
from quant.providers.base import Bar, FxObservation, PriceSeries, ProviderError
from quant.seed import load_instruments, seed_instruments

from .conftest import BACKEND, FIXTURES

TODAY = date(2026, 9, 25)


class FakePriceProvider:
    def __init__(self, name: str, data: dict[str, PriceSeries] | None = None, fail: bool = False):
        self.name = name
        self.data = data or {}
        self.fail = fail
        self.calls: list[tuple[str, date, date]] = []

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        self.calls.append((symbol, start, end))
        if self.fail:
            raise ProviderError(f"{self.name}: down")
        return self.data.get(symbol, PriceSeries(currency=None, bars=[]))


def series(currency: str | None, *closes: tuple[date, str]) -> PriceSeries:
    return PriceSeries(currency, [Bar(date=d, close=Decimal(c)) for d, c in closes])


def add_instrument(db: Session, code: str, currency: str, **symbols: str) -> Instrument:
    inst = Instrument(code=code, name=code, asset_class="stock", currency=currency, symbols=symbols)
    db.add(inst)
    db.commit()
    return inst


def test_seed_file_is_valid_and_idempotent(db: Session) -> None:
    items = load_instruments(BACKEND / "seed" / "instruments.yaml")
    assert seed_instruments(db, items) == len(items)
    seed_instruments(db, items)
    assert db.scalar(select(func.count()).select_from(Instrument)) == len(items)
    codes = {i["code"] for i in items}
    assert {"SXRV", "NDX"} <= codes  # Nasdaq-100 benchmark (D35)


def test_prices_fallback_chain_and_idempotent_upsert(db: Session) -> None:
    add_instrument(db, "NVDA", "USD", yahoo="NVDA", stooq="nvda.us")
    add_instrument(db, "SAP", "EUR", yahoo="SAP.DE")
    yahoo = FakePriceProvider("yahoo", fail=True)
    stooq = FakePriceProvider(
        "stooq",
        {"nvda.us": series(None, (date(2026, 9, 24), "179.9"), (date(2026, 9, 25), "185.43"))},
    )

    result = ingest_prices(db, [yahoo, stooq], TODAY, backfill_days=30)
    # NVDA falls back to Stooq; SAP has no Stooq symbol, so it fails and the run is partial.
    assert result.status == JobStatus.PARTIAL
    assert result.rows_written == 2 and "SAP" in result.errors
    row = db.scalar(select(PriceEOD).where(PriceEOD.date == date(2026, 9, 25)))
    assert row is not None and row.source == "stooq" and row.currency == "USD"

    # Re-running updates in place instead of duplicating.
    stooq.data["nvda.us"] = series(None, (date(2026, 9, 25), "186.00"))
    ingest_prices(db, [yahoo, stooq], TODAY, backfill_days=30)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(PriceEOD)) == 2
    assert db.scalar(select(PriceEOD.close).where(PriceEOD.date == date(2026, 9, 25))) == Decimal(
        "186.00"
    )


def test_incremental_fetch_starts_shortly_before_last_stored_date(db: Session) -> None:
    add_instrument(db, "NVDA", "USD", yahoo="NVDA")
    provider = FakePriceProvider("yahoo", {"NVDA": series("USD", (date(2026, 9, 20), "1"))})
    ingest_prices(db, [provider], TODAY, backfill_days=400)
    assert provider.calls[0][1] == date(2025, 8, 21)  # first run: full backfill
    ingest_prices(db, [provider], TODAY, backfill_days=400)
    assert provider.calls[1][1] == date(2026, 9, 15)  # then: last date minus overlap


def test_currency_mismatch_is_flagged(db: Session) -> None:
    add_instrument(db, "X", "EUR", yahoo="X")
    provider = FakePriceProvider("yahoo", {"X": series("USD", (TODAY, "10"))})
    result = ingest_prices(db, [provider], TODAY, backfill_days=10)
    assert "X" in result.warnings


def test_fx_ingest_from_ecb_fixture(db: Session) -> None:
    class FixtureEcb:
        name = "ecb"

        def fetch_rates(self, quotes: list[str], start: date, end: date) -> list[FxObservation]:
            return ecb.parse_csv((FIXTURES / "ecb_exr.csv").read_text())

    result = ingest_fx(db, FixtureEcb(), TODAY, backfill_days=10, quotes=["USD", "GBP", "CHF"])
    assert result.status == JobStatus.SUCCESS and result.rows_written == 4
    assert result.warnings["missing_quotes"] == "CHF"
    ingest_fx(db, FixtureEcb(), TODAY, backfill_days=10, quotes=["USD", "GBP"])
    assert db.scalar(select(func.count()).select_from(FxRate)) == 4


def test_run_job_records_success_and_crashes(db: Session) -> None:
    ok = run_job(db, "demo", lambda s: JobResult(rows_written=3, attempted=1))
    assert ok.status == JobStatus.SUCCESS and ok.rows_written == 3 and ok.finished_at

    def boom(_: Session) -> JobResult:
        raise RuntimeError("provider exploded")

    crashed = run_job(db, "demo", boom)
    assert crashed.status == JobStatus.FAILED and "exploded" in str(crashed.details["error"])


def test_all_instruments_failing_marks_job_failed(db: Session) -> None:
    add_instrument(db, "A", "USD", yahoo="A")
    result = ingest_prices(db, [FakePriceProvider("yahoo", fail=True)], TODAY, backfill_days=5)
    assert result.status == JobStatus.FAILED


def test_a_bad_provider_setting_is_a_failed_run_not_a_crash(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant import worker
    from quant.config import Settings

    bad = Settings(isin_resolvers=["yahoo", "openfigy"], price_providers=["yahu"], fx_provider="x")
    monkeypatch.setattr(worker, "get_settings", lambda: bad)
    worker.run_mapping()  # these used to raise ValueError before the job was recorded
    worker.run_prices()
    worker.run_fx()
    runs = {r.job: r for r in db.query(JobRun).all()}
    assert {worker.MAP_JOB, worker.PRICES_JOB, worker.FX_JOB} == set(runs)
    for run in runs.values():
        assert run.status == JobStatus.FAILED and "unknown" in str(run.details["error"])

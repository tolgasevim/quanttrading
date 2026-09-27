"""Daily ECB FX reference-rate ingestion (FR-25)."""

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.models import FxRate
from quant.providers.base import FxProvider, ProviderError

# Currencies seen in the owner's history and target universe. ECB has no TWD; Taiwanese
# listings are covered through USD ADRs until a paid provider arrives (D15).
DEFAULT_QUOTES = ["USD", "GBP", "CHF", "JPY", "HKD", "DKK", "SEK", "NOK", "KRW", "CAD", "TRY"]
OVERLAP_DAYS = 5


def ingest_fx(
    session: Session,
    provider: FxProvider,
    today: date,
    backfill_days: int,
    quotes: list[str] | None = None,
) -> JobResult:
    quotes = quotes or DEFAULT_QUOTES
    result = JobResult(attempted=1)
    last = session.scalar(select(func.max(FxRate.date)).where(FxRate.source == provider.name))
    start = last - timedelta(days=OVERLAP_DAYS) if last else today - timedelta(days=backfill_days)
    try:
        observations = provider.fetch_rates(quotes, start, today)
    except ProviderError as exc:
        result.errors[provider.name] = str(exc)
        return result
    missing = sorted(set(quotes) - {o.quote for o in observations})
    if missing and observations:
        result.warnings["missing_quotes"] = ", ".join(missing)
    if not observations:
        return result
    rows = [
        {"quote": o.quote, "date": o.date, "rate": o.rate, "source": provider.name}
        for o in observations
    ]
    stmt = insert(FxRate).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_fx_rates_quote_date",
        set_={"rate": stmt.excluded.rate, "source": stmt.excluded.source},
    )
    session.execute(stmt)
    session.commit()
    result.rows_written = len(rows)
    return result

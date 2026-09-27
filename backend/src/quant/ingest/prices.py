"""Daily EOD price ingestion with a provider fallback chain."""

import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.models import Instrument, PriceEOD
from quant.providers.base import PriceProvider, PriceSeries, ProviderError, normalise_minor_units

log = logging.getLogger(__name__)

# Re-fetch a few days already stored, so late corrections by the provider are picked up.
OVERLAP_DAYS = 5


def fetch_with_fallback(
    providers: list[PriceProvider], instrument: Instrument, start: date, end: date
) -> tuple[str, PriceSeries]:
    """Try each provider that has a symbol for the instrument; the first non-empty series wins."""
    failures = []
    for provider in providers:
        symbol = instrument.symbols.get(provider.name)
        if not symbol:
            continue
        try:
            series = normalise_minor_units(provider.fetch_eod(symbol, start, end))
        except ProviderError as exc:
            failures.append(str(exc))
            continue
        if series.bars:
            return provider.name, series
        failures.append(f"{provider.name}: no data for {symbol}")
    if not failures:
        raise ProviderError("no configured provider has a symbol for this instrument")
    raise ProviderError("; ".join(failures))


def ingest_prices(
    session: Session,
    providers: list[PriceProvider],
    today: date,
    backfill_days: int,
    codes: list[str] | None = None,
) -> JobResult:
    result = JobResult()
    query = select(Instrument).where(Instrument.active.is_(True)).order_by(Instrument.code)
    if codes:
        query = query.where(Instrument.code.in_(codes))
    instruments = session.scalars(query).all()
    last_dates = dict(
        session.execute(
            select(PriceEOD.instrument_id, func.max(PriceEOD.date)).group_by(PriceEOD.instrument_id)
        ).all()
    )

    for instrument in instruments:
        result.attempted += 1
        last = last_dates.get(instrument.id)
        start = (
            last - timedelta(days=OVERLAP_DAYS) if last else today - timedelta(days=backfill_days)
        )
        try:
            source, series = fetch_with_fallback(providers, instrument, start, today)
        except ProviderError as exc:
            result.errors[instrument.code] = str(exc)
            log.warning("prices %s: %s", instrument.code, exc)
            continue

        currency = series.currency or instrument.currency
        if currency != instrument.currency:
            result.warnings[instrument.code] = (
                f"{source} reports {currency}, instrument is set to {instrument.currency}"
            )
        rows = [
            {
                "instrument_id": instrument.id,
                "date": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "adj_close": bar.adj_close,
                "volume": bar.volume,
                "currency": currency,
                "source": source,
            }
            for bar in series.bars
        ]
        stmt = insert(PriceEOD).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_prices_eod_instr_date",
            set_={
                c: stmt.excluded[c]
                for c in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "adj_close",
                    "volume",
                    "currency",
                    "source",
                )
            }
            | {"fetched_at": func.now()},
        )
        session.execute(stmt)
        session.commit()
        result.rows_written += len(rows)
    return result

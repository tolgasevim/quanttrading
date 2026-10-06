"""Fill in the sector of the shares people hold (FR-20, FR-23).

A ticker found by the mapping job usually brings its sector with it. This job covers the rest:
shares mapped before sectors were stored, and shares an ISIN lookup found through a source that
gives no sector. A source that answers without a sector (a fund, a holding company) is not asked
again until `retry_days` have passed; a source that could not be reached is asked again at once.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.ingest.mapping import NO_TICKER, HeldIsin, held_isins
from quant.models import Instrument
from quant.providers.base import ProviderError
from quant.providers.resolvers import IsinResolver

log = logging.getLogger(__name__)


def fill_sectors(
    session: Session,
    resolvers: list[IsinResolver],
    now: datetime,
    retry_days: int = 30,
    isins: list[HeldIsin] | None = None,
) -> JobResult:
    result = JobResult()
    if not resolvers:
        return result
    shares = {
        h.isin
        for h in (isins if isins is not None else held_isins(session))
        if h.asset_class == "stock"
    }
    if not shares:
        return result
    stale = now - timedelta(days=retry_days)
    todo = session.scalars(
        select(Instrument)
        .where(
            Instrument.isin.in_(shares),
            Instrument.active.is_(True),
            Instrument.sector.is_(None),
            # Seeded instruments have no mapping source; "!=" alone would leave them out.
            or_(Instrument.mapping_source.is_(None), Instrument.mapping_source != NO_TICKER),
        )
        .order_by(Instrument.code)
    ).all()
    for instrument in todo:
        if instrument.sector_checked_at and instrument.sector_checked_at > stale:
            continue
        isin = instrument.isin
        if not isin:
            continue
        result.attempted += 1
        found = None
        failures: list[str] = []
        for resolver in resolvers:
            try:
                listing = resolver.resolve(isin)
            except ProviderError as exc:
                failures.append(str(exc))
                continue
            if listing is not None and listing.sector:
                found = listing
                break
        if found is None and failures:
            # A source could not be asked: it may know, so try again next run and say why.
            result.warnings[isin] = f"sector not looked up: {'; '.join(failures)}"
            continue
        current = session.scalar(
            select(Instrument)
            .where(Instrument.id == instrument.id)
            .execution_options(populate_existing=True)
        )
        if current is None or current.sector is not None:
            continue
        current.sector_checked_at = now
        if found is not None:
            current.sector, current.industry = found.sector, found.industry
        session.add(current)
        session.commit()
        if found is not None:
            result.rows_written += 1  # a share checked and found to have none writes no sector
    return result

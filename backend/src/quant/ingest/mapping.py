"""Map the ISINs people hold to tickers, so they can be priced (FR-12).

Runs across all users (the instrument list is shared market data, not personal data), so it
reads the transactions table with row-level security bypassed. Each ISIN gets an `Instrument`
row whose `symbols` the price job then uses. An ISIN no resolver knows becomes an inactive row
marked `none`, shown on the Prices page, where an admin can enter the symbol by hand.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.models import Instrument, Transaction
from quant.portfolio.positions import QUANTITY_CATEGORIES
from quant.providers.base import ProviderError
from quant.providers.resolvers import IsinResolver, Listing

log = logging.getLogger(__name__)

# TR's asset classes that have a listed price, and the instrument class they become. Crypto is
# priced from the broker statement (and later CoinGecko); bonds, private funds and knock-outs
# have no usable ticker.
PRICEABLE = {"STOCK": "stock", "FUND": "etf"}
NO_TICKER = "none"
MANUAL = "manual"


@dataclass(frozen=True)
class HeldIsin:
    isin: str
    name: str | None
    asset_class: str  # the instrument class: "stock" or "etf"


def held_isins(session: Session) -> list[HeldIsin]:
    """Every priceable ISIN anyone has traded, whether or not it is still held."""
    rows = session.execute(
        select(Transaction.isin, func.max(Transaction.name), func.max(Transaction.asset_class))
        .where(
            Transaction.isin.is_not(None),
            Transaction.shares.is_not(None),
            Transaction.category.in_(QUANTITY_CATEGORIES),
            Transaction.asset_class.in_(PRICEABLE),
        )
        .group_by(Transaction.isin)
        .order_by(Transaction.isin)
    ).all()
    return [HeldIsin(isin, name, PRICEABLE[cls]) for isin, name, cls in rows if isin and cls]


def _resolve(resolvers: list[IsinResolver], isin: str) -> tuple[Listing | None, list[str]]:
    """The first listing any resolver finds, plus the errors of resolvers that failed."""
    errors: list[str] = []
    for resolver in resolvers:
        try:
            listing = resolver.resolve(isin)
        except ProviderError as exc:
            errors.append(str(exc))
            continue
        if listing is not None:
            return listing, errors
    return None, errors


def map_isins(
    session: Session,
    resolvers: list[IsinResolver],
    now: datetime,
    retry_days: int = 30,
    isins: list[HeldIsin] | None = None,
) -> JobResult:
    result = JobResult()
    existing = {i.isin: i for i in session.scalars(select(Instrument)) if i.isin}
    for held in isins if isins is not None else held_isins(session):
        known = existing.get(held.isin)
        if known is not None:
            if known.mapping_source != NO_TICKER:
                continue  # seeded, found, or entered by hand
            if known.mapped_at and known.mapped_at > now - timedelta(days=retry_days):
                continue  # asked recently, still unknown
        result.attempted += 1
        listing, errors = _resolve(resolvers, held.isin)
        if listing is None and len(errors) == len(resolvers):
            # Nobody could be asked (network, rate limit): try again next run, remember nothing.
            result.errors[held.isin] = "; ".join(errors)
            continue
        instrument = known or Instrument(code=held.isin, isin=held.isin, currency="EUR")
        instrument.name = (listing.name if listing else None) or held.name or held.isin
        instrument.asset_class = held.asset_class
        instrument.mapped_at = now
        if listing is not None:
            instrument.symbols = {"yahoo": listing.symbol}
            instrument.mapping_source = listing.source
            instrument.active = True
        else:
            instrument.symbols = {}
            instrument.mapping_source = NO_TICKER
            instrument.active = False
            result.warnings[held.isin] = "no ticker found"
        session.add(instrument)
        session.commit()
        result.rows_written += 1
    return result

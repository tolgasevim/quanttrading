"""Map the ISINs people hold to tickers, so they can be priced (FR-12).

Runs across all users (the instrument list is shared market data, not personal data), so it
reads the transactions table with row-level security bypassed. Each ISIN gets an `Instrument`
row whose `symbols` the price job then uses. An ISIN no resolver knows becomes an inactive row
marked `none`, shown on the Prices page, where an admin can enter the symbol by hand.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.models import Instrument, Transaction
from quant.portfolio.positions import DUST, QUANTITY_CATEGORIES
from quant.providers.base import ProviderError
from quant.providers.resolvers import CryptoResolver, IsinResolver, Listing

log = logging.getLogger(__name__)

# TR's asset classes that have a market price, and the instrument class they become. Shares and
# funds get a Yahoo ticker from their ISIN; a coin gets a CoinGecko id from its name. Bonds,
# private funds and knock-outs have no usable ticker.
PRICEABLE = {"STOCK": "stock", "FUND": "etf", "CRYPTO": "crypto"}
NO_TICKER = "none"
# After a partial answer (one resolver errored) an ISIN is asked about again after this long.
PARTIAL_RETRY_DAYS = 1
MANUAL = "manual"


@dataclass(frozen=True)
class HeldIsin:
    isin: str
    name: str | None
    asset_class: str  # the instrument class: "stock", "etf" or "crypto"


def held_isins(session: Session) -> list[HeldIsin]:
    """Every priceable ISIN that at least one user still holds. A fully sold position needs no
    price, so it gets no ticker lookup, and the price job skips it too (see `ingest_prices`).

    "Still held" is worked out like the position engine does it: all quantity rows of the ISIN
    count, whatever class the row itself carries (some rows have none), and a non-zero net is
    held. The class is the one any of the rows carries."""
    movement = and_(
        Transaction.isin.is_not(None),
        Transaction.shares.is_not(None),
        Transaction.category.in_(QUANTITY_CATEGORIES),
    )
    still_held = (
        select(Transaction.isin)
        .where(movement)
        .group_by(Transaction.user_id, Transaction.isin)
        .having(func.abs(func.sum(Transaction.shares)) > DUST)
    )
    rows = session.execute(
        select(Transaction.isin, func.max(Transaction.name), func.max(Transaction.asset_class))
        .where(movement, Transaction.isin.in_(still_held))
        .group_by(Transaction.isin)
        .order_by(Transaction.isin)
    ).all()
    return [
        HeldIsin(isin, name, PRICEABLE[cls])
        for isin, name, cls in rows
        if isin and cls in PRICEABLE
    ]


def _resolve(
    resolvers: Sequence[IsinResolver] | Sequence[CryptoResolver], held: HeldIsin
) -> tuple[Listing | None, list[str]]:
    """The first listing any resolver finds, plus the errors of resolvers that failed. Without a
    listing, an empty error list means every resolver answered "not found". A coin is looked up
    by the name the broker gives it."""
    errors: list[str] = []
    for resolver in resolvers:
        try:
            if held.asset_class == "crypto":
                listing = cast(CryptoResolver, resolver).resolve(held.isin, held.name)
            else:
                listing = cast(IsinResolver, resolver).resolve(held.isin)
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
    crypto: Sequence[CryptoResolver] = (),
) -> JobResult:
    if not resolvers:
        # Without a resolver "every resolver failed" would be true for every ISIN: say what is
        # wrong instead of recording empty errors.
        raise ValueError("no ISIN resolvers configured (QT_ISIN_RESOLVERS is empty)")
    result = JobResult()
    existing = {i.isin: i for i in session.scalars(select(Instrument)) if i.isin}
    for held in isins if isins is not None else held_isins(session):
        is_coin = held.asset_class == "crypto"
        pool: Sequence[IsinResolver] | Sequence[CryptoResolver] = crypto if is_coin else resolvers
        if not pool:
            continue  # coin prices are switched off: leave the position to the statement
        known = existing.get(held.isin)
        if known is not None:
            if known.mapping_source != NO_TICKER:
                continue  # seeded, found, or entered by hand
            if known.mapped_at and known.mapped_at > now - timedelta(days=retry_days):
                continue  # asked recently, still unknown
        result.attempted += 1
        listing, errors = _resolve(pool, held)
        if listing is None and len(errors) == len(pool):
            # Nobody could be asked (network, rate limit): remember nothing, ask again next run.
            result.errors[held.isin] = "; ".join(errors)
            continue
        # The lookup can take a while: read the row again, so a ticker an admin saved meanwhile
        # is never overwritten with this older answer.
        current = session.scalar(
            select(Instrument)
            .where(Instrument.isin == held.isin)
            .execution_options(populate_existing=True)
        )
        if current is not None and current.mapping_source != NO_TICKER:
            continue
        instrument = current or Instrument(code=held.isin, isin=held.isin, currency="EUR")
        instrument.name = (listing.name if listing else None) or held.name or held.isin
        instrument.asset_class = held.asset_class
        instrument.mapped_at = now
        if listing is not None:
            instrument.symbols = {listing.provider: listing.symbol}
            instrument.mapping_source = listing.source
            instrument.active = True
        else:
            instrument.symbols = {}
            instrument.mapping_source = NO_TICKER
            instrument.active = False
            result.warnings[held.isin] = "no ticker found"
            if errors:
                # Only some resolvers could answer. Say "no ticker found" now, so the Prices page
                # shows it and an admin can enter one, but ask again after a day, not a month.
                instrument.mapped_at = now - timedelta(days=retry_days - PARTIAL_RETRY_DAYS)
                result.warnings[held.isin] = f"no ticker found; {'; '.join(errors)}"
        session.add(instrument)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()  # another writer created the row first: theirs wins
            continue
        result.rows_written += 1
    return result

"""Which of the user's holdings have a market price, and the manual ticker entry (FR-12).

The mapping and the prices themselves are shared market data; what is personal is which ISINs
a user holds, so the list is built from the signed-in user's own positions.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from quant.api.deps import AdminUser, CurrentUser, UserDb
from quant.config import get_settings
from quant.ingest.mapping import MANUAL, NO_TICKER, PRICEABLE
from quant.ingest.prices import ingest_prices
from quant.ingest.runtime import make_fetcher, today_local
from quant.models import Instrument, PriceEOD
from quant.portfolio import service
from quant.portfolio.positions import Position, compute_positions, open_positions
from quant.providers.registry import price_providers

router = APIRouter(prefix="/api/prices", tags=["prices"])

ISIN = re.compile(r"^[A-Z0-9]{12}$")
SYMBOL = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.\-=^]{0,29}$")


class PriceItem(BaseModel):
    isin: str
    name: str | None
    asset_class: str | None
    # priced | stale (the last price is too old to use) | waiting (mapped, no price yet) |
    # unmapped (no ticker found) | not_checked (the mapping has not run yet) | unsupported (no
    # ticker expected: crypto, bonds, funds without one)
    status: str
    symbol: str | None
    mapping_source: str | None
    currency: str | None
    last_date: str | None
    last_close: Decimal | None  # in the instrument's own currency


class PricesOut(BaseModel):
    items: list[PriceItem]


class SymbolIn(BaseModel):
    symbol: Annotated[str, Field(min_length=1, max_length=30)]


def _trim(value: Decimal) -> Decimal:
    """120.000000 -> 120 (the database keeps six decimals)."""
    return Decimal(format(value.normalize(), "f"))


def _open_positions(db: UserDb, user_id: uuid.UUID) -> list[Position]:
    return open_positions(compute_positions(service.load_movements(db, user_id)))


def _items(db: UserDb, user_id: uuid.UUID) -> list[PriceItem]:
    positions = _open_positions(db, user_id)
    instruments = {
        i.isin: i for i in db.scalars(select(Instrument).where(Instrument.isin.is_not(None)))
    }
    latest = (
        select(PriceEOD.instrument_id, func.max(PriceEOD.date).label("d"))
        .group_by(PriceEOD.instrument_id)
        .subquery()
    )
    newest = {
        instrument_id: (day, close)
        for instrument_id, day, close in db.execute(
            select(PriceEOD.instrument_id, PriceEOD.date, PriceEOD.close).join(
                latest,
                (latest.c.instrument_id == PriceEOD.instrument_id) & (latest.c.d == PriceEOD.date),
            )
        ).all()
    }
    newest_anywhere = db.scalar(select(func.max(PriceEOD.date)))
    items = []
    for p in positions:
        inst = instruments.get(p.isin)
        last = newest.get(inst.id) if inst else None
        if p.asset_class not in PRICEABLE and inst is None:
            state = "unsupported"
        elif inst is None:
            state = "not_checked"
        elif inst.mapping_source == NO_TICKER:
            state = "unmapped"
        elif last is None:
            state = "waiting"
        elif newest_anywhere is not None and last[0] < newest_anywhere - timedelta(
            days=service.MAX_PRICE_AGE_DAYS
        ):
            state = "stale"
        else:
            state = "priced"
        items.append(
            PriceItem(
                isin=p.isin,
                name=p.name,
                asset_class=p.asset_class,
                status=state,
                symbol=(inst.symbols.get("yahoo") if inst else None),
                mapping_source=inst.mapping_source if inst else None,
                currency=inst.currency if inst else None,
                last_date=last[0].isoformat() if last else None,
                last_close=_trim(last[1]) if last else None,
            )
        )
    order = {
        "unmapped": 0,
        "stale": 1,
        "waiting": 2,
        "not_checked": 3,
        "priced": 4,
        "unsupported": 5,
    }
    return sorted(items, key=lambda i: (order[i.status], i.name or i.isin))


@router.get("", response_model=PricesOut)
def list_prices(user: CurrentUser, db: UserDb) -> PricesOut:
    return PricesOut(items=_items(db, user.id))


@router.put("/{isin}", response_model=PricesOut)
def set_symbol(isin: str, body: SymbolIn, user: AdminUser, db: UserDb) -> PricesOut:
    """Enter the Yahoo symbol of an instrument by hand, then fetch its prices at once."""
    isin = isin.upper()
    symbol = body.symbol.strip()
    if not ISIN.match(isin):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "not an ISIN")
    if not SYMBOL.match(symbol):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "not a ticker symbol")
    held = {
        p.isin: p for p in open_positions(compute_positions(service.load_movements(db, user.id)))
    }
    if isin not in held:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such open position in your history")
    position = held[isin]
    inst = db.scalar(select(Instrument).where(Instrument.isin == isin))
    if inst is None:
        inst = Instrument(code=isin, isin=isin, currency="EUR")
    changed = inst.id is not None and inst.symbols.get("yahoo") not in (None, symbol)
    if changed:
        # A different ticker: the stored prices belong to the old one (maybe in another
        # currency), so drop them and fetch the whole history again.
        db.execute(delete(PriceEOD).where(PriceEOD.instrument_id == inst.id))
    inst.name = inst.name if inst.name and inst.name != isin else (position.name or isin)
    inst.asset_class = PRICEABLE.get(position.asset_class or "", "stock")
    if changed:
        inst.symbols = {"yahoo": symbol}  # the other providers' symbols belong to the old ticker
    else:
        inst.symbols = {**(inst.symbols or {}), "yahoo": symbol}
    inst.mapping_source = MANUAL
    inst.mapped_at = datetime.now(UTC)
    inst.active = True
    db.add(inst)
    db.commit()
    settings = get_settings()
    fetcher = make_fetcher(db, settings)
    try:
        providers = price_providers(settings.price_providers, fetcher)
        result = ingest_prices(
            db, providers, today_local(settings).date(), settings.backfill_days, codes=[inst.code]
        )
    finally:
        fetcher.close()
    if result.errors:
        # Keep the symbol (it may be right and the provider briefly down) and say what failed.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Saved the symbol, but no price came back: {'; '.join(result.errors.values())}",
        )
    return PricesOut(items=_items(db, user.id))

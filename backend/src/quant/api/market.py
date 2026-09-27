"""Read-only market data: ingestion status per instrument and latest FX rates."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from quant.api.deps import CurrentUser, DbSession
from quant.models import FxRate, Instrument, PriceEOD

router = APIRouter(prefix="/api/market", tags=["market"])


class InstrumentStatus(BaseModel):
    code: str
    name: str
    asset_class: str
    currency: str
    last_date: date | None
    last_close: Decimal | None
    source: str | None
    rows: int


class FxOut(BaseModel):
    quote: str
    date: date
    rate: Decimal


@router.get("/status", response_model=list[InstrumentStatus])
def price_status(_: CurrentUser, db: DbSession) -> list[InstrumentStatus]:
    stats = (
        select(
            PriceEOD.instrument_id,
            func.max(PriceEOD.date).label("last_date"),
            func.count().label("rows"),
        )
        .group_by(PriceEOD.instrument_id)
        .subquery()
    )
    latest = (
        select(Instrument, stats.c.last_date, stats.c.rows, PriceEOD.close, PriceEOD.source)
        .outerjoin(stats, stats.c.instrument_id == Instrument.id)
        .outerjoin(
            PriceEOD,
            (PriceEOD.instrument_id == Instrument.id) & (PriceEOD.date == stats.c.last_date),
        )
        .where(Instrument.active.is_(True))
        .order_by(Instrument.code)
    )
    return [
        InstrumentStatus(
            code=inst.code,
            name=inst.name,
            asset_class=inst.asset_class,
            currency=inst.currency,
            last_date=last_date,
            last_close=close,
            source=source,
            rows=rows or 0,
        )
        for inst, last_date, rows, close, source in db.execute(latest).all()
    ]


@router.get("/fx", response_model=list[FxOut])
def latest_fx(_: CurrentUser, db: DbSession) -> list[FxOut]:
    last = select(FxRate.quote, func.max(FxRate.date).label("d")).group_by(FxRate.quote).subquery()
    rows = db.scalars(
        select(FxRate)
        .join(last, (FxRate.quote == last.c.quote) & (FxRate.date == last.c.d))
        .order_by(FxRate.quote)
    )
    return [FxOut(quote=r.quote, date=r.date, rate=r.rate) for r in rows]

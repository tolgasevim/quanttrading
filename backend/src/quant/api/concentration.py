"""Concentration, ETF look-through and instrument types (FR-23, FR-24, FR-27)."""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from quant.api.deps import AdminUser, CurrentUser, UserDb
from quant.models import EtfHolding, Instrument
from quant.portfolio import lookthrough, service

router = APIRouter(prefix="/api", tags=["concentration"])

PCT = Decimal("0.01")
MAX_FILE_BYTES = 2_000_000
TOP_UNDERLYING = 30
ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


class SliceOut(BaseModel):
    label: str
    value: Decimal
    pct: Decimal


class PositionOut(BaseModel):
    isin: str
    name: str
    type: str
    flags: list[str]
    value: Decimal
    pct: Decimal
    trading_book: bool
    crypto_linked: bool
    looked_through: bool | None


class ViaOut(BaseModel):
    fund: str
    pct: Decimal


class UnderlyingOut(BaseModel):
    key: str
    name: str
    isin: str | None
    type: str
    sector: str
    country: str
    direct_pct: Decimal
    indirect_pct: Decimal
    total_pct: Decimal
    via: list[ViaOut]


class ConcentrationOut(BaseModel):
    total: Decimal
    unvalued: int
    covered_pct: Decimal | None  # of the fund value, how much was looked through
    crypto_pct: Decimal  # coins plus crypto-linked shares, direct and through funds
    positions: list[PositionOut]
    underlying: list[UnderlyingOut]
    by_class: list[SliceOut]
    by_sector: list[SliceOut]
    by_country: list[SliceOut]
    by_currency: list[SliceOut]
    unlooked: list[str]
    warnings: list[str]


def _pct(value: Decimal, total: Decimal) -> Decimal:
    return (value / total * 100).quantize(PCT) if total > 0 else Decimal("0.00")


def _slices(bucket: dict[str, Decimal], total: Decimal) -> list[SliceOut]:
    return [
        SliceOut(label=label, value=value.quantize(PCT), pct=_pct(value, total))
        for label, value in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
    ]


@router.get("/concentration", response_model=ConcentrationOut)
def get_concentration(user: CurrentUser, db: UserDb) -> ConcentrationOut:
    held = service.build_holdings(db, user.id)
    c = lookthrough.concentration(db, held)
    total = c.total
    warnings: list[str] = []
    if c.unlooked:
        warnings.append(
            f"{len(c.unlooked)} fund(s) are not looked through: {', '.join(c.unlooked[:5])}. "
            "Their share of your sectors, countries and shares is unknown until an admin "
            "uploads the issuer's holdings file."
        )
    if c.unvalued:
        warnings.append(f"{c.unvalued} position(s) have no price and are left out.")
    return ConcentrationOut(
        total=total.quantize(PCT),
        unvalued=c.unvalued,
        covered_pct=None if c.covered_pct is None else c.covered_pct.quantize(PCT),
        crypto_pct=_pct(c.crypto_value, total),
        positions=[
            PositionOut(
                isin=p.isin,
                name=p.name,
                type=p.type,
                flags=p.flags,
                value=p.value.quantize(PCT),
                pct=_pct(p.value, total),
                trading_book=p.trading_book,
                crypto_linked=p.crypto_linked,
                looked_through=p.looked_through,
            )
            for p in c.positions
        ],
        underlying=[
            UnderlyingOut(
                key=u.key,
                name=u.name,
                isin=u.isin,
                type=u.type,
                sector=u.sector,
                country=u.country,
                direct_pct=_pct(u.direct, total),
                indirect_pct=_pct(u.indirect, total),
                total_pct=_pct(u.total, total),
                via=[
                    ViaOut(fund=f, pct=_pct(v, total))
                    for f, v in sorted(u.via.items(), key=lambda kv: kv[1], reverse=True)
                ],
            )
            for u in c.underlying[:TOP_UNDERLYING]
        ],
        by_class=_slices(c.by_class, total),
        by_sector=_slices(c.by_sector, total),
        by_country=_slices(c.by_country, total),
        by_currency=_slices(c.by_currency, total),
        unlooked=c.unlooked,
        warnings=warnings,
    )


class FundFileOut(BaseModel):
    etf_isin: str
    name: str | None
    rows: int
    total_weight: Decimal
    as_of: date
    source: str


@router.get("/lookthrough", response_model=list[FundFileOut])
def list_files(user: CurrentUser, db: UserDb) -> list[FundFileOut]:
    """The funds that have a holdings file."""
    rows = db.execute(
        select(
            EtfHolding.etf_isin,
            func.count(),
            func.sum(EtfHolding.weight),
            func.max(EtfHolding.as_of),
            func.max(EtfHolding.source),
        )
        .group_by(EtfHolding.etf_isin)
        .order_by(EtfHolding.etf_isin)
    ).all()
    names = {
        i.isin: i.name
        for i in db.scalars(
            select(Instrument).where(Instrument.isin.in_([r[0] for r in rows] or [""]))
        )
    }
    return [
        FundFileOut(
            etf_isin=isin,
            name=names.get(isin),
            rows=count,
            total_weight=Decimal(total).quantize(PCT),
            as_of=as_of,
            source=source,
        )
        for isin, count, total, as_of, source in rows
    ]


@router.post("/lookthrough/{isin}", response_model=FundFileOut, status_code=201)
async def upload_file(
    isin: str,
    admin: AdminUser,
    db: UserDb,
    file: Annotated[UploadFile, File()],
    as_of: Annotated[date | None, Form()] = None,
    source: Annotated[str, Form()] = "upload",
) -> FundFileOut:
    """Store an issuer's holdings file for a fund (admin only: the data is shared)."""
    isin = isin.strip().upper()
    if not ISIN.fullmatch(isin):
        raise HTTPException(422, "not a valid ISIN")
    raw = await file.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(413, "the file is too large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        parsed = lookthrough.parse_holdings(text)
    except lookthrough.HoldingsFormatError as error:
        raise HTTPException(422, str(error)) from error
    day = as_of or service.today()
    lookthrough.replace_holdings(db, isin, parsed, source.strip() or "upload", day)
    name = db.scalar(select(Instrument.name).where(Instrument.isin == isin))
    return FundFileOut(
        etf_isin=isin,
        name=name,
        rows=len(parsed),
        total_weight=sum((p.weight for p in parsed), Decimal(0)).quantize(PCT),
        as_of=day,
        source=source.strip() or "upload",
    )


@router.delete("/lookthrough/{isin}", status_code=204)
def delete_file(isin: str, admin: AdminUser, db: UserDb) -> None:
    db.execute(delete(EtfHolding).where(EtfHolding.etf_isin == isin.strip().upper()))
    db.commit()

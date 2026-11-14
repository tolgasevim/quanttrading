"""Crypto: per-lot tax-free dates and the Freigrenze (FR-29)."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel

from quant.api.deps import CurrentUser, UserDb
from quant.portfolio import allocation, crypto

router = APIRouter(prefix="/api/crypto", tags=["crypto"])
CENT = Decimal("0.01")
UNIT = Decimal("0.00000001")


class LotOut(BaseModel):
    acquired: date
    quantity: Decimal
    cost: Decimal
    value: Decimal | None
    gain: Decimal | None
    tax_free_from: date
    free: bool
    days_left: int
    origin: str


class CoinOut(BaseModel):
    isin: str
    name: str
    quantity: Decimal
    price: Decimal | None
    value: Decimal | None
    free_quantity: Decimal
    free_share_pct: Decimal
    next_free_date: date | None
    next_free_quantity: Decimal
    taxable_gain_if_sold: Decimal | None
    lots: list[LotOut]


class CryptoOut(BaseModel):
    year: int
    coins: list[CoinOut]
    value: Decimal
    free_value: Decimal
    taxable_gain_if_sold: Decimal
    realised_taxable: Decimal
    realised_free: Decimal
    freigrenze: Decimal
    headroom: Decimal
    over_limit: bool
    exposure_pct: Decimal
    cap_pct: Decimal
    note: str


def _c(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def _co(value: Decimal | None) -> Decimal | None:
    return None if value is None else _c(value)


@router.get("", response_model=CryptoOut)
def get_crypto(user: CurrentUser, db: UserDb) -> CryptoOut:
    view = crypto.analyse(db, user.id)
    cap = allocation.load_settings(db, user.id).guardrails.max_crypto
    return CryptoOut(
        year=view.year,
        coins=[
            CoinOut(
                isin=c.isin,
                name=c.name,
                quantity=c.quantity.quantize(UNIT),
                price=_co(c.price),
                value=_co(c.value),
                free_quantity=c.free_quantity.quantize(UNIT),
                free_share_pct=_c(c.free_share_pct),
                next_free_date=c.next_free_date,
                next_free_quantity=c.next_free_quantity.quantize(UNIT),
                taxable_gain_if_sold=_co(c.taxable_gain_if_sold),
                lots=[
                    LotOut(
                        acquired=lot.acquired,
                        quantity=lot.quantity.quantize(UNIT),
                        cost=_c(lot.cost),
                        value=_co(lot.value),
                        gain=_co(lot.gain),
                        tax_free_from=lot.tax_free_from,
                        free=lot.free,
                        days_left=lot.days_left,
                        origin=lot.origin,
                    )
                    for lot in c.lots
                ],
            )
            for c in view.coins
        ],
        value=_c(view.value),
        free_value=_c(view.free_value),
        taxable_gain_if_sold=_c(view.taxable_gain_if_sold),
        realised_taxable=_c(view.realised_taxable),
        realised_free=_c(view.realised_free),
        freigrenze=view.freigrenze,
        headroom=_c(view.headroom),
        over_limit=view.over_limit,
        exposure_pct=_c(view.exposure_pct),
        cap_pct=cap,
        note=view.note,
    )

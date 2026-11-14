"""Commodities, key drivers, the regime, the hedge check and tradable ETCs
(FR-60 to FR-64)."""

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from quant.api.deps import AdminUser, CurrentUser, UserDb
from quant.models import CommodityProduct
from quant.portfolio import markets, performance, service

router = APIRouter(prefix="/api/markets", tags=["markets"])
CENT = Decimal("0.01")

RISKS = {
    "physical": "Backed by metal in a vault. Issuer and custodian risk remain.",
    "futures": (
        "Follows futures. Contango and the cost of rolling can lose money even when the price "
        "is flat."
    ),
    "synthetic": "Follows the price through a swap, so there is counterparty risk.",
    "other": "Check how it tracks the price and who stands behind it.",
}
COMMODITY_CODES = ("gold", "silver", "wti", "brent", "copper", "natgas")


class RowOut(BaseModel):
    code: str
    name: str
    unit: str
    usd: Decimal | None
    eur: Decimal | None
    as_of: date | None
    changes: dict[str, Decimal | None]
    low_52w: Decimal | None
    high_52w: Decimal | None
    range_pos: Decimal | None
    stale: bool


class ItemOut(BaseModel):
    label: str
    value: Decimal | None
    unit: str
    detail: str
    available: bool


class GroupOut(BaseModel):
    title: str
    items: list[ItemOut]


class SignalOut(BaseModel):
    name: str
    vote: int
    available: bool
    detail: str


class RegimeOut(BaseModel):
    label: str
    score: int
    signals: list[SignalOut]
    implication: str
    disclaimer: str


class WindowOut(BaseModel):
    days: int
    observations: int
    correlation: float | None


class HedgeOut(BaseModel):
    available: bool
    windows: list[WindowOut]
    verdict: str


class ProductOut(BaseModel):
    id: int
    commodity: str
    isin: str
    name: str
    issuer: str | None
    kind: str
    ter: Decimal | None
    tr_available: bool | None
    notes: str | None
    risk: str


class MarketsOut(BaseModel):
    today: date
    commodities: list[RowOut]
    drivers: list[GroupOut]
    regime: RegimeOut
    hedge: HedgeOut
    products: list[ProductOut]
    risks: dict[str, str]
    note: str


def _products(db: UserDb) -> list[ProductOut]:
    rows = db.scalars(
        select(CommodityProduct).order_by(CommodityProduct.commodity, CommodityProduct.name)
    )
    return [
        ProductOut(
            id=p.id,
            commodity=p.commodity,
            isin=p.isin,
            name=p.name,
            issuer=p.issuer,
            kind=p.kind,
            ter=p.ter,
            tr_available=p.tr_available,
            notes=p.notes,
            risk=RISKS.get(p.kind, RISKS["other"]),
        )
        for p in rows
    ]


@router.get("", response_model=MarketsOut)
def get_markets(user: CurrentUser, db: UserDb) -> MarketsOut:
    today = service.today()
    panel = markets.commodity_panel(db, today)
    groups = markets.drivers(db, today)
    regime = markets.regime(db, today)
    daily = performance.analyse(db, user.id, today=today).daily
    hedge = markets.hedge_correlation(db, today, daily)
    return MarketsOut(
        today=today,
        commodities=[
            RowOut(
                code=r.code,
                name=r.name,
                unit=r.unit,
                usd=r.usd,
                eur=None if r.eur is None else r.eur.quantize(CENT),
                as_of=r.as_of,
                changes={k: None if v is None else v.quantize(CENT) for k, v in r.changes.items()},
                low_52w=r.low_52w,
                high_52w=r.high_52w,
                range_pos=None if r.range_pos is None else r.range_pos.quantize(CENT),
                stale=r.stale,
            )
            for r in panel
        ],
        drivers=[
            GroupOut(
                title=g.title,
                items=[
                    ItemOut(
                        label=i.label,
                        value=i.value,
                        unit=i.unit,
                        detail=i.detail,
                        available=i.available,
                    )
                    for i in g.items
                ],
            )
            for g in groups
        ],
        regime=RegimeOut(
            label=regime.label,
            score=regime.score,
            signals=[
                SignalOut(name=s.name, vote=s.vote, available=s.available, detail=s.detail)
                for s in regime.signals
            ],
            implication=regime.implication,
            disclaimer=regime.disclaimer,
        ),
        hedge=HedgeOut(
            available=hedge.available,
            windows=[
                WindowOut(
                    days=w.days,
                    observations=w.observations,
                    correlation=None if w.correlation is None else round(w.correlation, 3),
                )
                for w in hedge.windows
            ],
            verdict=hedge.verdict,
        ),
        products=_products(db),
        risks=RISKS,
        note=(
            "Information only. The app places no trade and offers no futures (D5). Whether a "
            "broker offers a product is not checked: confirm it there."
        ),
    )


# --- tradable products (FR-62) --------------------------------------------------------------------


class ProductIn(BaseModel):
    commodity: Literal["gold", "silver", "wti", "brent", "copper", "natgas"]
    isin: str = Field(min_length=12, max_length=12, pattern=r"^[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]$")
    name: str = Field(min_length=1, max_length=200)
    issuer: str | None = Field(None, max_length=100)
    kind: Literal["physical", "synthetic", "futures", "other"]
    ter: Decimal | None = Field(None, ge=0, le=20)
    tr_available: bool | None = None
    notes: str | None = Field(None, max_length=300)


@router.post("/products", response_model=list[ProductOut], status_code=201)
def add_product(body: ProductIn, admin: AdminUser, db: UserDb) -> list[ProductOut]:
    db.add(
        CommodityProduct(
            commodity=body.commodity,
            isin=body.isin.upper(),
            name=" ".join(body.name.split()),
            issuer=body.issuer,
            kind=body.kind,
            ter=body.ter,
            tr_available=body.tr_available,
            notes=body.notes,
        )
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "this product is already listed for that commodity") from error
    return _products(db)


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: int, admin: AdminUser, db: UserDb) -> Response:
    row = db.get(CommodityProduct, product_id)
    if row is None:
        raise HTTPException(404, "no such product")
    db.delete(row)
    db.commit()
    return Response(status_code=204)

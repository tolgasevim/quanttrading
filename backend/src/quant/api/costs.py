"""Cost per unit for instruments whose cost the broker does not give (FR-20).

Spin-offs, rights issues and units transferred in arrive without a price. The owner enters what
one unit cost, and the cost basis, realised gains and the tax estimate use it from then on.
"""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from quant.api.deps import CurrentUser, UserDb
from quant.models import UnitCost
from quant.portfolio import service

router = APIRouter(prefix="/api/costs", tags=["costs"])

ISIN = re.compile(r"^[A-Z0-9]{12}$")
MAX_UNIT_COST = Decimal(10) ** 12  # a trillion euros per unit; the column holds 18 whole digits


class CostItemOut(BaseModel):
    isin: str
    name: str | None
    asset_class: str | None
    open_units: Decimal  # still held with no known cost
    sold_units: Decimal  # already sold with no known cost
    sales: int
    unit_cost: Decimal | None  # entered by the owner
    note: str | None


class CostsOut(BaseModel):
    items: list[CostItemOut]


class CostIn(BaseModel):
    unit_cost: Annotated[Decimal, Field(ge=0, le=MAX_UNIT_COST, max_digits=28, decimal_places=10)]
    note: Annotated[str | None, Field(max_length=200)] = None


def _trim(value: Decimal | None) -> Decimal | None:
    """12.5000000000 -> 12.5 (the database keeps ten decimals)."""
    return None if value is None else Decimal(format(value.normalize(), "f"))


def _out(items: list[service.CostItem]) -> CostsOut:
    return CostsOut(
        items=[
            CostItemOut(
                isin=i.isin,
                name=i.name,
                asset_class=i.asset_class,
                open_units=_trim(i.open_units) or Decimal(0),
                sold_units=_trim(i.sold_units) or Decimal(0),
                sales=i.sales,
                unit_cost=_trim(i.unit_cost),
                note=i.note,
            )
            for i in items
        ]
    )


@router.get("", response_model=CostsOut)
def list_costs(user: CurrentUser, db: UserDb) -> CostsOut:
    return _out(service.build_cost_items(db, user.id))


def _entry(db: UserDb, user_id: uuid.UUID, isin: str) -> UnitCost | None:
    return db.scalar(select(UnitCost).where(UnitCost.user_id == user_id, UnitCost.isin == isin))


@router.put("/{isin}", response_model=CostsOut)
def set_cost(isin: str, body: CostIn, user: CurrentUser, db: UserDb) -> CostsOut:
    isin = isin.upper()
    if not ISIN.match(isin):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "not an ISIN")
    entry = _entry(db, user.id, isin)
    if entry is None:
        if isin not in service.known_isins(db, user.id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such instrument in your history")
        db.add(UnitCost(user_id=user.id, isin=isin, unit_cost=body.unit_cost, note=body.note))
        try:
            db.commit()
        except IntegrityError:
            # A second save of the same new instrument got there first (a double click, two
            # tabs): change that entry instead.
            db.rollback()
            entry = _entry(db, user.id, isin)
            if entry is None:
                raise
    if entry is not None:
        entry.unit_cost = body.unit_cost
        entry.note = body.note
        entry.updated_at = datetime.now(UTC)
        db.commit()
    return _out(service.build_cost_items(db, user.id))


@router.delete("/{isin}", status_code=status.HTTP_204_NO_CONTENT)
def clear_cost(isin: str, user: CurrentUser, db: UserDb) -> Response:
    entry = _entry(db, user.id, isin.upper())
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no cost entered for this instrument")
    db.delete(entry)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

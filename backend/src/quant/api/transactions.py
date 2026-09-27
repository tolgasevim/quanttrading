"""Read access to the signed-in user's imported transactions."""

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from quant.api.deps import CurrentUser, UserDb
from quant.models import Transaction

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


class TransactionOut(BaseModel):
    id: int
    executed_at: datetime
    date: date
    kind: str
    type: str
    asset_class: str | None
    isin: str | None
    name: str | None
    shares: Decimal | None
    price: Decimal | None
    amount: Decimal | None
    fee: Decimal | None
    tax: Decimal | None
    currency: str | None
    savings_plan: bool


class TransactionsOut(BaseModel):
    counts: dict[str, int]
    cash_balance: Decimal  # history-derived, for the FR-28 cross-check
    items: list[TransactionOut]


@router.get("", response_model=TransactionsOut)
def list_transactions(
    user: CurrentUser,
    db: UserDb,
    kind: str | None = None,
    include_card: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> TransactionsOut:
    # Filtered explicitly *and* by row-level security (FR-3): belt and braces.
    mine = Transaction.user_id == user.id
    counts: dict[str, int] = {
        kind: n
        for kind, n in db.execute(
            select(Transaction.kind, func.count()).where(mine).group_by(Transaction.kind)
        ).all()
    }
    cash = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(Transaction.amount, 0)
                    + func.coalesce(Transaction.fee, 0)
                    + func.coalesce(Transaction.tax, 0)
                ),
                0,
            )
        ).where(mine)
    )
    query = select(Transaction).where(mine).order_by(Transaction.executed_at.desc()).limit(limit)
    if kind:
        query = query.where(Transaction.kind == kind)
    elif not include_card:
        query = query.where(Transaction.kind != "card")
    items = [TransactionOut.model_validate(t, from_attributes=True) for t in db.scalars(query)]
    return TransactionsOut(counts=counts, cash_balance=Decimal(cash or 0), items=items)

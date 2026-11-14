"""Read access to the signed-in user's imported transactions."""

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from quant.api.deps import CurrentUser, UserDb
from quant.models import Transaction, TransactionAudit
from quant.portfolio import manual

Day = date  # the new models have a field called `date`, which would hide the type

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
    source: str  # "tr" for an imported row, "manual" for one typed in
    editable: bool


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
    items = [_out(t) for t in db.scalars(query)]
    return TransactionsOut(counts=counts, cash_balance=Decimal(cash or 0), items=items)


def _out(t: Transaction) -> TransactionOut:
    return TransactionOut(
        id=t.id,
        executed_at=t.executed_at,
        date=t.date,
        kind=t.kind,
        type=t.type,
        asset_class=t.asset_class,
        isin=t.isin,
        name=t.name,
        shares=t.shares,
        price=t.price,
        amount=t.amount,
        fee=t.fee,
        tax=t.tax,
        currency=t.currency,
        savings_plan=t.savings_plan,
        source=t.broker,
        editable=t.broker == manual.BROKER or t.kind in manual.EDITABLE_IMPORTED,
    )


class ManualIn(BaseModel):
    """A transaction typed in by hand. Every number is positive: the side says which way."""

    side: manual.Side
    date: Day
    time: datetime | None = Field(None, description="Exact time; the date's noon (UTC) if empty")
    isin: str | None = Field(None, max_length=12)
    name: str | None = Field(None, max_length=200)
    asset_class: str | None = Field(None, max_length=20)
    shares: Decimal | None = Field(None, ge=0)
    price: Decimal | None = Field(None, ge=0)
    amount: Decimal | None = Field(None, ge=0)
    fee: Decimal | None = Field(None, ge=0)
    tax: Decimal | None = Field(None, ge=0)
    currency: str = Field("EUR", min_length=3, max_length=3)


class ManualPatch(BaseModel):
    """Only the fields that are sent change."""

    side: manual.Side | None = None
    date: Day | None = None
    time: datetime | None = None
    isin: str | None = Field(None, max_length=12)
    name: str | None = Field(None, max_length=200)
    asset_class: str | None = Field(None, max_length=20)
    shares: Decimal | None = Field(None, ge=0)
    price: Decimal | None = Field(None, ge=0)
    amount: Decimal | None = Field(None, ge=0)
    fee: Decimal | None = Field(None, ge=0)
    tax: Decimal | None = Field(None, ge=0)
    currency: str | None = Field(None, min_length=3, max_length=3)


def _mine(db: UserDb, user: CurrentUser, tx_id: int) -> Transaction:
    tx = db.scalar(
        select(Transaction).where(Transaction.id == tx_id, Transaction.user_id == user.id)
    )
    if tx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such transaction")
    return tx


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
def add_transaction(body: ManualIn, user: CurrentUser, db: UserDb) -> TransactionOut:
    try:
        tx = manual.add(
            db,
            user.id,
            body.side,
            body.date,
            body.time,
            isin=body.isin,
            name=body.name,
            shares=body.shares,
            price=body.price,
            amount=body.amount,
            fee=body.fee,
            tax=body.tax,
            asset_class=body.asset_class,
            currency=body.currency,
        )
    except manual.ManualError as error:
        raise HTTPException(422, str(error)) from error
    return _out(tx)


@router.patch("/{tx_id}", response_model=TransactionOut)
def edit_transaction(
    tx_id: int, body: ManualPatch, user: CurrentUser, db: UserDb
) -> TransactionOut:
    tx = _mine(db, user, tx_id)
    sent = body.model_dump(exclude_unset=True)
    side, day, at = sent.pop("side", None), sent.pop("date", None), sent.pop("time", None)
    try:
        tx = manual.edit(db, tx, side=side, day=day, at=at, fields=sent)
    except manual.ManualError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return _out(tx)


@router.delete("/{tx_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction(tx_id: int, user: CurrentUser, db: UserDb) -> Response:
    manual.delete(db, _mine(db, user, tx_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class AuditOut(BaseModel):
    id: int
    at: datetime
    action: str
    source: str
    external_id: str
    before: dict[str, object] | None
    after: dict[str, object] | None


@router.get("/audit", response_model=list[AuditOut])
def audit_trail(
    user: CurrentUser, db: UserDb, limit: int = Query(100, ge=1, le=500)
) -> list[AuditOut]:
    rows = db.scalars(
        select(TransactionAudit)
        .where(TransactionAudit.user_id == user.id)
        .order_by(TransactionAudit.id.desc())
        .limit(limit)
    )
    return [
        AuditOut(
            id=a.id,
            at=a.at,
            action=a.action,
            source=a.broker,
            external_id=a.external_id,
            before=a.before,
            after=a.after,
        )
        for a in rows
    ]

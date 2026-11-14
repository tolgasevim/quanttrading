"""Cash, earmarked reserves and the opportunity unlock (FR-28, FR-28a, FR-28b)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from quant.api.deps import CurrentUser, UserDb
from quant.models import CashBalance, Reserve
from quant.portfolio import cash, service

router = APIRouter(prefix="/api/cash", tags=["cash"])


class BalanceIn(BaseModel):
    amount: Decimal = Field(max_digits=20, decimal_places=2)
    as_of: date | None = None
    note: str | None = Field(None, max_length=200)


class BalanceOut(BaseModel):
    id: int
    as_of: date
    amount: Decimal
    note: str | None


class ReserveIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    target_date: date | None = None
    note: str | None = Field(None, max_length=200)


class ReservePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    kind: str | None = None
    amount: Decimal | None = Field(None, gt=0, max_digits=20, decimal_places=2)
    target_date: date | None = None
    clear_target_date: bool = False
    note: str | None = Field(None, max_length=200)


class ReserveOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    amount: Decimal
    loaned: Decimal  # unlocked and not yet put back
    refill_per_month: Decimal | None  # to have the loan back by the target date
    target_date: date | None
    note: str | None


class StatusOut(BaseModel):
    entered: Decimal | None
    entered_as_of: date | None
    history: Decimal
    difference: Decimal | None
    difference_pct: Decimal | None
    mismatch: bool
    cash: Decimal
    cash_source: str
    reserves_total: Decimal
    shortfall: Decimal
    investable: Decimal


class BandOut(BaseModel):
    pct: Decimal
    status: str
    target_pct: Decimal
    low_pct: Decimal
    high_pct: Decimal
    excess: Decimal


class EventOut(BaseModel):
    id: int
    at: datetime
    reserve_id: uuid.UUID
    action: str
    name: str
    amount_before: Decimal | None
    amount_after: Decimal | None
    detail: dict[str, object] | None


class CashOut(BaseModel):
    status: StatusOut
    securities_value: Decimal  # the valued holdings, in EUR
    net_worth: Decimal  # securities plus all cash, reserves included
    band: BandOut | None
    reserves: list[ReserveOut]
    balances: list[BalanceOut]  # the entries the user made, newest first
    events: list[EventOut]
    warnings: list[str]


def _reserve(r: Reserve) -> ReserveOut:
    return ReserveOut(
        id=r.id,
        name=r.name,
        kind=r.kind,
        amount=r.amount,
        loaned=r.loaned,
        refill_per_month=cash.refill_per_month(r, service.today()),
        target_date=r.target_date,
        note=r.note,
    )


def _view(db: UserDb, user: CurrentUser) -> CashOut:
    state = cash.cash_status(db, user.id)
    holdings = service.build_holdings(db, user.id)
    securities = service.weights(holdings)[1]
    band = cash.cash_band(securities, state.investable)
    warnings: list[str] = []
    if state.shortfall > 0:
        warnings.append(
            f"Your cash is {state.shortfall:.2f} EUR below your reserves. "
            "Reserved money was spent or invested."
        )
    if state.mismatch and state.difference is not None and state.difference_pct is not None:
        warnings.append(
            f"The balance you entered differs from the one rebuilt from your history by "
            f"{state.difference:.2f} EUR ({state.difference_pct:.1f}%). "
            "Check for a missing import, pending card payments or interest."
        )
    if state.cash_source == "history":
        warnings.append(
            "You have not entered your cash balance. The figure shown is rebuilt from your history."
        )
    balances = db.scalars(
        select(CashBalance)
        .where(CashBalance.user_id == user.id)
        .order_by(CashBalance.as_of.desc(), CashBalance.id.desc())
        .limit(10)
    )
    return CashOut(
        status=StatusOut(**state.__dict__),
        securities_value=securities.quantize(Decimal("0.01")),
        net_worth=(securities + state.cash).quantize(Decimal("0.01")),
        band=None if band is None else BandOut(**band.__dict__),
        reserves=[_reserve(r) for r in cash.active_reserves(db, user.id)],
        balances=[
            BalanceOut(id=b.id, as_of=b.as_of, amount=b.amount, note=b.note) for b in balances
        ],
        events=[
            EventOut(
                id=e.id,
                at=e.at,
                reserve_id=e.reserve_id,
                action=e.action,
                name=e.name,
                amount_before=e.amount_before,
                amount_after=e.amount_after,
                detail=e.detail,
            )
            for e in cash.events(db, user.id)
        ],
        warnings=warnings,
    )


def _mine(db: UserDb, user: CurrentUser, reserve_id: uuid.UUID) -> Reserve:
    reserve = db.scalar(select(Reserve).where(Reserve.id == reserve_id, Reserve.user_id == user.id))
    if reserve is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such reserve")
    return reserve


@router.get("", response_model=CashOut)
def get_cash(user: CurrentUser, db: UserDb) -> CashOut:
    return _view(db, user)


@router.post("/balance", response_model=CashOut, status_code=status.HTTP_201_CREATED)
def enter_balance(body: BalanceIn, user: CurrentUser, db: UserDb) -> CashOut:
    day = body.as_of or service.today()
    if day > service.today():
        raise HTTPException(422, "the day cannot be in the future")
    cash.enter_balance(db, user.id, body.amount, day, body.note)
    return _view(db, user)


@router.post("/reserves", response_model=CashOut, status_code=status.HTTP_201_CREATED)
def add_reserve(body: ReserveIn, user: CurrentUser, db: UserDb) -> CashOut:
    try:
        cash.create_reserve(
            db, user.id, body.name, body.kind, body.amount, body.target_date, body.note
        )
    except cash.CashError as error:
        raise HTTPException(422, str(error)) from error
    return _view(db, user)


@router.patch("/reserves/{reserve_id}", response_model=CashOut)
def change_reserve(
    reserve_id: uuid.UUID, body: ReservePatch, user: CurrentUser, db: UserDb
) -> CashOut:
    reserve = _mine(db, user, reserve_id)
    try:
        cash.change_reserve(
            db,
            reserve,
            name=body.name,
            kind=body.kind,
            amount=body.amount,
            target_date=body.target_date,
            clear_target_date=body.clear_target_date,
            note=body.note,
        )
    except cash.CashError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return _view(db, user)


@router.post("/reserves/{reserve_id}/release", response_model=CashOut)
def release_reserve(reserve_id: uuid.UUID, user: CurrentUser, db: UserDb) -> CashOut:
    reserve = _mine(db, user, reserve_id)
    try:
        cash.release_reserve(db, reserve)
    except cash.CashError as error:
        raise HTTPException(422, str(error)) from error
    return _view(db, user)


class RefillIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)


@router.post("/reserves/{reserve_id}/refill", response_model=CashOut)
def refill(reserve_id: uuid.UUID, body: RefillIn, user: CurrentUser, db: UserDb) -> CashOut:
    """Put money back into a reserve that lent some, out of the investable cash."""
    reserve = _mine(db, user, reserve_id)
    try:
        cash.refill_reserve(db, user.id, reserve, body.amount)
    except cash.CashError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return _view(db, user)


class UnlockIn(BaseModel):
    isin: str = Field(min_length=1, max_length=12)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    manual: bool = False  # the user flags this as an opportunity


class CheckOut(BaseModel):
    key: str
    title: str
    passed: bool | None
    detail: str
    blocking: bool


class UnlockOut(BaseModel):
    allowed: bool
    triggers: list[str]
    checks: list[CheckOut]


def _unlock_out(result: cash.Unlock) -> UnlockOut:
    return UnlockOut(
        allowed=result.allowed,
        triggers=result.triggers,
        checks=[CheckOut(**c.__dict__) for c in result.checks],
    )


@router.post("/reserves/{reserve_id}/unlock-check", response_model=UnlockOut)
def unlock_check(reserve_id: uuid.UUID, body: UnlockIn, user: CurrentUser, db: UserDb) -> UnlockOut:
    """Show the FR-28b checks for an investment, without changing anything."""
    reserve = _mine(db, user, reserve_id)
    result = cash.unlock_checks(
        db, user.id, reserve, body.isin, body.amount, body.manual, service.today()
    )
    return _unlock_out(result)


@router.post("/reserves/{reserve_id}/unlock", response_model=CashOut)
def unlock(reserve_id: uuid.UUID, body: UnlockIn, user: CurrentUser, db: UserDb) -> CashOut:
    """Confirm the unlock. It only moves money inside the app (the reserve shrinks); the user
    places the trade at the broker."""
    reserve = _mine(db, user, reserve_id)
    try:
        cash.unlock(db, user.id, reserve, body.isin, body.amount, body.manual, service.today())
    except cash.CashError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return _view(db, user)

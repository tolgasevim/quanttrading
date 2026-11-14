"""Manual transactions: add, edit and delete, with an audit trail (FR-14).

A manual row looks like an imported one, so positions, lots, tax and the cash balance need no
special case. It has broker `manual` and a generated external id. The user types plain numbers
(a buy is "10 shares at 50"); this module turns them into the signed values the engines expect.

Every change writes one `TransactionAudit` row. A deleted imported row stays in the audit trail,
and a later import skips it (`deleted_external_ids`), so the file you imported again does not
bring it back.
"""

import enum
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import Transaction, TransactionAudit

BROKER = "manual"
CENT = Decimal("0.000001")


class Side(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    INTEREST = "interest"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    FEE = "fee"


# side -> (category, type, kind)
SIDE_ROWS: dict[Side, tuple[str, str, str]] = {
    Side.BUY: ("TRADING", "BUY", "trade"),
    Side.SELL: ("TRADING", "SELL", "trade"),
    Side.DIVIDEND: ("CASH", "DIVIDEND", "income"),
    Side.INTEREST: ("CASH", "INTEREST_PAYMENT", "interest"),
    Side.DEPOSIT: ("CASH", "CUSTOMER_INBOUND", "transfer"),
    Side.WITHDRAWAL: ("CASH", "CUSTOMER_OUTBOUND_REQUEST", "transfer"),
    Side.FEE: ("CASH", "FEE", "fee"),
}
NEEDS_ISIN = {Side.BUY, Side.SELL, Side.DIVIDEND}
NEEDS_SHARES = {Side.BUY, Side.SELL}
NEEDS_AMOUNT = {Side.DIVIDEND, Side.INTEREST, Side.DEPOSIT, Side.WITHDRAWAL, Side.FEE}
# Imported rows of these kinds can have their numbers corrected. The others (corporate actions,
# deliveries, card spending, ...) can only be deleted: one number is not enough to fix them.
EDITABLE_IMPORTED = {"trade", "income", "interest", "transfer", "fee"}
AUDIT_FIELDS = (
    "executed_at",
    "kind",
    "category",
    "type",
    "asset_class",
    "isin",
    "name",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "currency",
)


class ManualError(ValueError):
    """The input does not make a valid transaction. The text says why, in plain words."""


def snapshot(tx: Transaction) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in AUDIT_FIELDS:
        value = getattr(tx, key)
        out[key] = (
            value.isoformat()
            if isinstance(value, datetime)
            else (str(value) if isinstance(value, Decimal) else value)
        )
    return out


def _q(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(CENT)


def moment(day: date, at: datetime | None) -> datetime:
    """The time stamp of a row. With no time given, noon UTC: it sorts after the day's imported
    rows that happened in the morning and keeps the same calendar day in Berlin."""
    if at is not None:
        return at if at.tzinfo else at.replace(tzinfo=UTC)
    return datetime.combine(day, time(12, 0), tzinfo=UTC)


def build(
    side: Side,
    *,
    isin: str | None,
    name: str | None,
    shares: Decimal | None,
    price: Decimal | None,
    amount: Decimal | None,
    fee: Decimal | None,
    tax: Decimal | None,
    asset_class: str | None,
    currency: str,
) -> dict[str, Any]:
    """The column values of a manual row. The user's numbers are all positive."""
    if side in NEEDS_ISIN and not isin:
        raise ManualError("an ISIN is needed for a trade or a dividend")
    if side in NEEDS_SHARES:
        if shares is None or shares <= 0:
            raise ManualError("shares must be more than zero")
        if price is None or price < 0:
            raise ManualError("the price must be zero or more")
    if side in NEEDS_AMOUNT and (amount is None or amount <= 0):
        raise ManualError("the amount must be more than zero")
    for label, value in (("fee", fee), ("tax", tax)):
        if value is not None and value < 0:
            raise ManualError(f"the {label} must be zero or more")
    category, type_, kind = SIDE_ROWS[side]
    if side in NEEDS_SHARES:
        assert shares is not None and price is not None
        gross = shares * price
        paid = amount if amount is not None and amount > 0 else gross
        signed_amount = -paid if side is Side.BUY else paid
        signed_shares: Decimal | None = shares if side is Side.BUY else -shares
    else:
        assert amount is not None
        out = side in {Side.WITHDRAWAL, Side.FEE}
        signed_amount = -amount if out else amount
        signed_shares = None
    return {
        "category": category,
        "type": type_,
        "kind": kind,
        "isin": isin.upper() if isin else None,
        "name": name or None,
        "asset_class": (asset_class or ("stock" if side in NEEDS_SHARES else None)),
        "shares": _q(signed_shares),
        "price": _q(price) if side in NEEDS_SHARES else None,
        "amount": _q(signed_amount),
        "fee": _q(-fee) if fee else None,
        "tax": _q(-tax) if tax else None,
        "currency": currency.upper(),
    }


def add(
    session: Session,
    user_id: uuid.UUID,
    side: Side,
    day: date,
    at: datetime | None,
    **values: Any,
) -> Transaction:
    columns = build(side, **values)
    when = moment(day, at)
    tx = Transaction(
        user_id=user_id,
        broker=BROKER,
        external_id=f"manual-{uuid.uuid4()}",
        executed_at=when,
        date=day,
        savings_plan=False,
        **columns,
    )
    session.add(tx)
    session.flush()
    session.add(
        TransactionAudit(
            user_id=user_id,
            action="add",
            broker=BROKER,
            external_id=tx.external_id,
            before=None,
            after=snapshot(tx),
        )
    )
    session.commit()
    return tx


def side_of(tx: Transaction) -> Side | None:
    for side, (category, type_, _) in SIDE_ROWS.items():
        if tx.category == category and tx.type == type_:
            return side
    return None


def edit(
    session: Session,
    tx: Transaction,
    *,
    side: Side | None,
    day: date | None,
    at: datetime | None,
    fields: dict[str, Any],
) -> Transaction:
    """Change a row. `fields` holds only the values the user sent (positive numbers, text)."""
    before = snapshot(tx)
    if tx.broker == BROKER:
        current = side_of(tx)
        assert current is not None
        new_side = side or current
        merged = {
            "isin": tx.isin,
            "name": tx.name,
            "shares": abs(tx.shares) if tx.shares is not None else None,
            "price": tx.price,
            "amount": abs(tx.amount) if tx.amount is not None else None,
            "fee": abs(tx.fee) if tx.fee is not None else None,
            "tax": abs(tx.tax) if tx.tax is not None else None,
            "asset_class": tx.asset_class,
            "currency": tx.currency or "EUR",
        }
        # New shares or a new price: the old amount is stale, so it is worked out again (unless
        # the user sends an amount too).
        if new_side in NEEDS_SHARES and "amount" not in fields:
            if "shares" in fields or "price" in fields:
                merged["amount"] = None
        elif new_side not in NEEDS_SHARES and current in NEEDS_SHARES and "amount" not in fields:
            merged["amount"] = None  # a trade turned into a cash entry: the user must say how much
        merged.update(fields)
        for key, value in build(new_side, **merged).items():
            setattr(tx, key, value)
    else:
        if tx.kind not in EDITABLE_IMPORTED:
            raise ManualError(f"a row of kind {tx.kind} can be deleted but not edited")
        if side is not None and side is not side_of(tx):
            raise ManualError("the kind of an imported row cannot change")
        _edit_imported(tx, fields)
    if day is not None or at is not None:
        new_day = day or tx.date
        tx.date = new_day
        tx.executed_at = (
            moment(new_day, at) if at or tx.broker == BROKER else _same_time(tx, new_day)
        )
    session.flush()
    session.add(
        TransactionAudit(
            user_id=tx.user_id,
            action="edit",
            broker=tx.broker,
            external_id=tx.external_id,
            before=before,
            after=snapshot(tx),
        )
    )
    session.commit()
    return tx


def _same_time(tx: Transaction, day: date) -> datetime:
    """An imported row keeps its time of day when only its date is corrected."""
    old = tx.executed_at
    return datetime.combine(day, old.timetz())


def _edit_imported(tx: Transaction, fields: dict[str, Any]) -> None:
    """Numbers of an imported row, typed as positive values: each keeps the sign it has."""
    for key in ("shares", "amount"):
        if key in fields and fields[key] is not None:
            old = getattr(tx, key)
            negative = old is not None and old < 0
            value = abs(Decimal(fields[key]))
            setattr(tx, key, _q(-value if negative else value))
    if "price" in fields:
        tx.price = _q(fields["price"])
    for key in ("fee", "tax"):
        if key in fields:
            value = fields[key]
            old = getattr(tx, key)
            keep_positive = old is not None and old > 0  # a refund
            setattr(tx, key, None if not value else _q(value if keep_positive else -abs(value)))
    for key in ("name",):
        if key in fields:
            setattr(tx, key, fields[key] or None)


def delete(session: Session, tx: Transaction) -> None:
    session.add(
        TransactionAudit(
            user_id=tx.user_id,
            action="delete",
            broker=tx.broker,
            external_id=tx.external_id,
            before=snapshot(tx),
            after=None,
        )
    )
    session.delete(tx)
    session.commit()


def deleted_external_ids(session: Session, user_id: uuid.UUID, broker: str) -> set[str]:
    """Imported rows the user deleted: a new import must not put them back."""
    return set(
        session.scalars(
            select(TransactionAudit.external_id).where(
                TransactionAudit.user_id == user_id,
                TransactionAudit.broker == broker,
                TransactionAudit.action == "delete",
            )
        )
    )

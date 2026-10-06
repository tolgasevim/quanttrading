"""The notification centre and the alert settings (FR-70, FR-73).

Every query takes the user's own session (row-level security) and also filters by `user_id`.
"""

import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from quant.api.deps import CurrentUser, UserDb
from quant.models import AlertSettings, Notification

router = APIRouter(prefix="/api", tags=["notifications"])

MAX_LIMIT = 100
Pct = Annotated[Decimal, Field(gt=0, le=100, max_digits=5, decimal_places=2)]


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    severity: str
    title: str
    body: str
    isin: str | None
    created_at: datetime
    read: bool


class NotificationsOut(BaseModel):
    items: list[NotificationOut]
    unread: int  # all unread alerts of the user, not only the ones in `items`


class SettingsOut(BaseModel):
    daily_moves_enabled: bool
    move_stock_pct: Decimal
    move_fund_pct: Decimal
    move_crypto_pct: Decimal
    quiet_start: str | None  # "HH:MM", Europe/Berlin like the schedules
    quiet_end: str | None


class SettingsIn(BaseModel):
    daily_moves_enabled: bool
    move_stock_pct: Pct
    move_fund_pct: Pct
    move_crypto_pct: Pct
    quiet_start: str | None = None
    quiet_end: str | None = None

    @model_validator(mode="after")
    def _quiet_hours_are_a_pair(self) -> "SettingsIn":
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("set both quiet hours times, or neither")
        for value in (self.quiet_start, self.quiet_end):
            if value is not None:
                _parse_time(value)
        return self


def _parse_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("a time is HH:MM") from exc
    if len(value) != 5 or parsed.second or parsed.microsecond:
        raise ValueError("a time is HH:MM")
    return parsed


def _plain(value: Decimal) -> Decimal:
    """7.50 -> 7.5 and 100.00 -> 100, never 1E+2 (Decimal.normalize alone gives that)."""
    return Decimal(format(value.normalize(), "f"))


def _hhmm(value: time | None) -> str | None:
    return None if value is None else value.strftime("%H:%M")


def _out(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        kind=n.kind,
        severity=n.severity,
        title=n.title,
        body=n.body,
        isin=n.isin,
        created_at=n.created_at,
        read=n.read_at is not None,
    )


def _unread(db: UserDb, user_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


@router.get("/notifications", response_model=NotificationsOut)
def list_notifications(
    user: CurrentUser,
    db: UserDb,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    unread_only: bool = False,
) -> NotificationsOut:
    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    rows = db.scalars(
        query.order_by(Notification.created_at.desc(), Notification.id).limit(limit)
    ).all()
    return NotificationsOut(items=[_out(n) for n in rows], unread=_unread(db, user.id))


@router.post("/notifications/read-all", response_model=NotificationsOut)
def read_all(user: CurrentUser, db: UserDb) -> NotificationsOut:
    db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    db.commit()
    return list_notifications(user, db)


@router.post("/notifications/{notification_id}/read", response_model=NotificationsOut)
def read_one(notification_id: uuid.UUID, user: CurrentUser, db: UserDb) -> NotificationsOut:
    found = db.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user.id
        )
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such notification")
    if found.read_at is None:
        found.read_at = datetime.now(UTC)
        db.commit()
    return list_notifications(user, db)


def _settings_out(row: AlertSettings | None) -> SettingsOut:
    if row is None:
        return SettingsOut(
            daily_moves_enabled=True,
            move_stock_pct=Decimal(5),
            move_fund_pct=Decimal(3),
            move_crypto_pct=Decimal(10),
            quiet_start=None,
            quiet_end=None,
        )
    return SettingsOut(
        daily_moves_enabled=row.daily_moves_enabled,
        move_stock_pct=_plain(row.move_stock_pct),
        move_fund_pct=_plain(row.move_fund_pct),
        move_crypto_pct=_plain(row.move_crypto_pct),
        quiet_start=_hhmm(row.quiet_start),
        quiet_end=_hhmm(row.quiet_end),
    )


@router.get("/alerts/settings", response_model=SettingsOut)
def get_alert_settings(user: CurrentUser, db: UserDb) -> SettingsOut:
    row = db.scalar(select(AlertSettings).where(AlertSettings.user_id == user.id))
    return _settings_out(row)


@router.put("/alerts/settings", response_model=SettingsOut)
def put_alert_settings(body: SettingsIn, user: CurrentUser, db: UserDb) -> SettingsOut:
    values = {
        "daily_moves_enabled": body.daily_moves_enabled,
        "move_stock_pct": body.move_stock_pct,
        "move_fund_pct": body.move_fund_pct,
        "move_crypto_pct": body.move_crypto_pct,
        "quiet_start": _parse_time(body.quiet_start) if body.quiet_start else None,
        "quiet_end": _parse_time(body.quiet_end) if body.quiet_end else None,
    }
    stmt = insert(AlertSettings).values(user_id=user.id, **values)
    # Two saves at the same moment must not fail on the primary key: the last one wins.
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[AlertSettings.user_id],
            set_={**values, "updated_at": func.now()},
        )
    )
    db.commit()
    return get_alert_settings(user, db)

"""The notification centre and the alert settings (FR-70, FR-73).

Every query takes the user's own session (row-level security) and also filters by `user_id`.
"""

import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from quant.api.deps import CurrentUser, UserDb
from quant.models import (
    DEFAULT_CRYPTO_PCT,
    DEFAULT_DAILY_MOVES,
    DEFAULT_FUND_PCT,
    DEFAULT_STOCK_PCT,
    AlertSettings,
    Notification,
)
from quant.portfolio.alerts import plain

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
    quiet_start: time | None = None
    quiet_end: time | None = None

    @field_validator("quiet_start", "quiet_end", mode="before")
    @classmethod
    def _a_time_is_hh_mm(cls, value: object) -> object:
        """ "22:00" is a time. A cleared time input sends "", which means no time."""
        if value is None or isinstance(value, time):
            return value
        if not isinstance(value, str):
            raise ValueError("a time is HH:MM")
        if not value.strip():
            return None
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("a time is HH:MM") from exc
        # Some browsers send "22:00:00"; seconds are accepted when zero and are not kept. A time
        # with a zone ("22:00+01") is not a wall-clock time and is refused.
        if len(value) not in (5, 8) or parsed.second or parsed.microsecond or parsed.tzinfo:
            raise ValueError("a time is HH:MM")
        return parsed

    @model_validator(mode="after")
    def _quiet_hours_are_a_pair(self) -> "SettingsIn":
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("set both quiet hours times, or neither")
        return self


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
    offset: Annotated[int, Query(ge=0)] = 0,
    unread_only: bool = False,
) -> NotificationsOut:
    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    rows = db.scalars(
        query.order_by(Notification.created_at.desc(), Notification.id).limit(limit).offset(offset)
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
            daily_moves_enabled=DEFAULT_DAILY_MOVES,
            move_stock_pct=DEFAULT_STOCK_PCT,
            move_fund_pct=DEFAULT_FUND_PCT,
            move_crypto_pct=DEFAULT_CRYPTO_PCT,
            quiet_start=None,
            quiet_end=None,
        )
    return SettingsOut(
        daily_moves_enabled=row.daily_moves_enabled,
        move_stock_pct=Decimal(plain(row.move_stock_pct)),
        move_fund_pct=Decimal(plain(row.move_fund_pct)),
        move_crypto_pct=Decimal(plain(row.move_crypto_pct)),
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
        "quiet_start": body.quiet_start,
        "quiet_end": body.quiet_end,
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

"""Create notifications from fresh data: daily price moves for each user's holdings, and failed
jobs for the admins (FR-70, FR-72).

Runs for all users, so it reads with row-level security bypassed (like the mapping job) and
always writes the user explicitly. Every alert has a dedupe key, so a second run, or a catch-up
after the Mac mini was off, never stores the same alert twice.
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.models import (
    AlertSettings,
    Instrument,
    JobRun,
    JobStatus,
    Notification,
    PriceEOD,
    Role,
    User,
)
from quant.portfolio import service
from quant.portfolio.alerts import (
    MOVE_CLASSES,
    breaches,
    daily_move,
    move_text,
    threshold_for,
)
from quant.portfolio.positions import compute_positions, open_positions

log = logging.getLogger(__name__)

FAILED_JOB_WINDOW_HOURS = 48
DEFAULTS = AlertSettings(
    daily_moves_enabled=True,
    move_stock_pct=Decimal(5),
    move_fund_pct=Decimal(3),
    move_crypto_pct=Decimal(10),
)


def _store(session: Session, rows: list[dict[str, object]]) -> int:
    """Insert alerts that are not stored yet. Returns how many were new."""
    if not rows:
        return 0
    stmt = (
        insert(Notification)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_notifications_user_key")
    )
    return len(session.execute(stmt.returning(Notification.id)).all())


def _newest_bars(
    session: Session, instrument_ids: list[int]
) -> dict[int, list[tuple[date, Decimal]]]:
    """The two newest closes of each instrument, newest first."""
    if not instrument_ids:
        return {}
    ranked = (
        select(
            PriceEOD.instrument_id,
            PriceEOD.date,
            PriceEOD.close,
            func.row_number()
            .over(partition_by=PriceEOD.instrument_id, order_by=PriceEOD.date.desc())
            .label("rn"),
        )
        .where(PriceEOD.instrument_id.in_(instrument_ids))
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.instrument_id, ranked.c.date, ranked.c.close)
        .where(ranked.c.rn <= 2)
        .order_by(ranked.c.instrument_id, ranked.c.date.desc())
    )
    bars: dict[int, list[tuple[date, Decimal]]] = {}
    for instrument_id, day, close in rows:
        bars.setdefault(instrument_id, []).append((day, close))
    return bars


def _user_moves(
    session: Session,
    user: User,
    settings: AlertSettings,
    instruments: dict[str, Instrument],
    bars: dict[int, list[tuple[date, Decimal]]],
    today: date,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    positions = open_positions(compute_positions(service.load_movements(session, user.id)))
    for position in positions:
        limit = threshold_for(
            position.asset_class,
            settings.move_stock_pct,
            settings.move_fund_pct,
            settings.move_crypto_pct,
        )
        instrument = instruments.get(position.isin)
        if limit is None or instrument is None or position.asset_class not in MOVE_CLASSES:
            continue
        move = daily_move(bars.get(instrument.id, []), today)
        if move is None or not breaches(move, limit):
            continue
        name = position.name or instrument.name or position.isin
        title, body = move_text(name, position.asset_class or "", move, limit, instrument.currency)
        rows.append(
            {
                "user_id": user.id,
                "kind": "daily_move",
                "severity": "warning" if abs(move.pct) >= 2 * limit else "info",
                "title": title[:200],
                "body": body[:1000],
                "isin": position.isin,
                "dedupe_key": f"daily_move:{position.isin}:{move.day.isoformat()}",
            }
        )
    return rows


def _failed_jobs(session: Session, admin: User, now: datetime) -> list[dict[str, object]]:
    runs = session.scalars(
        select(JobRun).where(
            JobRun.status == JobStatus.FAILED,
            JobRun.finished_at.is_not(None),
            JobRun.finished_at >= now - timedelta(hours=FAILED_JOB_WINDOW_HOURS),
        )
    )
    rows: list[dict[str, object]] = []
    for run in runs:
        details = run.details or {}
        reason = str(details.get("error") or "")
        if not reason and isinstance(details.get("errors"), dict):
            errors = details["errors"]
            assert isinstance(errors, dict)
            reason = f"{len(errors)} items failed, for example: " + "; ".join(
                f"{k}: {v}" for k, v in list(errors.items())[:2]
            )
        rows.append(
            {
                "user_id": admin.id,
                "kind": "job_failed",
                "severity": "warning",
                "title": f"Job {run.job} failed",
                "body": (reason or "The run failed with no message.")[:900],
                "isin": None,
                "dedupe_key": f"job_failed:{run.id}",
            }
        )
    return rows


def create_alerts(session: Session, today: date, now: datetime) -> JobResult:
    result = JobResult()
    users = list(session.scalars(select(User).order_by(User.created_at)))
    settings = {s.user_id: s for s in session.scalars(select(AlertSettings))}
    instruments = {
        i.isin: i
        for i in session.scalars(
            select(Instrument).where(Instrument.isin.is_not(None), Instrument.active.is_(True))
        )
        if i.isin
    }
    bars = _newest_bars(session, [i.id for i in instruments.values()])
    for user in users:
        result.attempted += 1
        mine = settings.get(user.id, DEFAULTS)
        try:
            rows: list[dict[str, object]] = []
            if mine.daily_moves_enabled:
                rows += _user_moves(session, user, mine, instruments, bars, today)
            if user.role == Role.ADMIN:
                rows += _failed_jobs(session, user, now)
            result.rows_written += _store(session, rows)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one user's failure must not stop the others
            session.rollback()
            log.exception("alerts for user %s failed", user.id)
            result.errors[str(user.id)] = f"{type(exc).__name__}: {exc}"
    return result

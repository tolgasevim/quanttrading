"""Create notifications from fresh data: daily price moves for each user's holdings, and failed
jobs for the admins (FR-70, FR-72).

Runs for all users, so it reads with row-level security bypassed (like the mapping job) and
always writes the user explicitly. Every alert has a dedupe key, so a second run, or a catch-up
after the Mac mini was off, never stores the same alert twice. An alert is a record of an event:
once stored it is not rewritten, even if the user then changes a limit or the day's price is
corrected.
"""

import hashlib
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.config import get_settings
from quant.ingest.jobs import JobResult
from quant.models import (
    DEFAULT_CRYPTO_PCT,
    DEFAULT_FUND_PCT,
    DEFAULT_STOCK_PCT,
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
    looks_like_split,
    move_text,
    threshold_for,
)
from quant.portfolio.positions import Position, compute_positions, open_positions

log = logging.getLogger(__name__)

FAILED_JOB_WINDOW_HOURS = 48
STUCK_AFTER_HOURS = 6  # no job here runs this long
DEFAULTS = AlertSettings(
    daily_moves_enabled=True,
    move_stock_pct=DEFAULT_STOCK_PCT,
    move_fund_pct=DEFAULT_FUND_PCT,
    move_crypto_pct=DEFAULT_CRYPTO_PCT,
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


def _open_positions(session: Session, user: User) -> list[Position]:
    return open_positions(compute_positions(service.load_movements(session, user.id)))


def _user_moves(
    user: User,
    positions: list[Position],
    settings: AlertSettings,
    instruments: dict[str, Instrument],
    bars: dict[int, list[tuple[date, Decimal]]],
    today: date,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
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
        split = position.asset_class == "STOCK" and looks_like_split(move)
        name = position.name or instrument.name or position.isin
        title, body = move_text(
            name, position.asset_class or "", move, limit, instrument.currency, split
        )
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
    """Runs that failed, finished with errors, or look stuck, in the last two days and after the
    admin's account existed (a new admin gets no alerts about the time before). A run that a later
    successful run of the same job has made good is not news any more."""
    since = max(now - timedelta(hours=FAILED_JOB_WINDOW_HOURS), admin.created_at)
    last_success = dict(
        session.execute(
            select(JobRun.job, func.max(JobRun.finished_at))
            .where(JobRun.status == JobStatus.SUCCESS)
            .group_by(JobRun.job)
        ).all()
    )
    tz = ZoneInfo(get_settings().timezone)
    runs = session.scalars(
        select(JobRun).where(
            or_(
                and_(
                    JobRun.status.in_([JobStatus.FAILED, JobStatus.PARTIAL]),
                    JobRun.finished_at.is_not(None),
                    JobRun.finished_at >= since,
                ),
                # A run the worker never finished (killed, power cut) stays RUNNING for ever.
                and_(
                    JobRun.status == JobStatus.RUNNING,
                    JobRun.started_at >= since,
                    JobRun.started_at <= now - timedelta(hours=STUCK_AFTER_HOURS),
                ),
            )
        )
    )
    rows: list[dict[str, object]] = []
    for run in runs:
        happened = run.finished_at or run.started_at
        fixed = last_success.get(run.job)
        if fixed is not None and fixed > happened:
            continue
        stuck = run.status == JobStatus.RUNNING
        details = run.details or {}
        reason = str(details.get("error") or "")
        errors = details.get("errors")
        if not reason and isinstance(errors, dict) and errors:
            reason = f"{len(errors)} items failed, for example: " + "; ".join(
                f"{k}: {v}" for k, v in list(errors.items())[:2]
            )
        if stuck:
            reason = f"The run started at {run.started_at:%Y-%m-%d %H:%M} UTC and never finished."
        elif run.status == JobStatus.PARTIAL and not reason:
            continue  # nothing went wrong that a person could act on
        what = (
            "seems stuck"
            if stuck
            else "failed"
            if run.status == JobStatus.FAILED
            else "finished with errors"
        )
        # One alert per job, outcome, local day and reason: a nightly repeat is one a day, a
        # second and different failure the same day still gets its own.
        digest = hashlib.sha256(reason.encode()).hexdigest()[:8]
        day = happened.astimezone(tz).date().isoformat()
        rows.append(
            {
                "user_id": admin.id,
                "kind": "job_failed",
                "severity": "info" if run.status == JobStatus.PARTIAL else "warning",
                "title": f"Job {run.job} {what}",
                "body": (reason or "The run failed with no message.")[:900],
                "isin": None,
                "dedupe_key": f"job_failed:{run.job}:{run.status.value}:{day}:{digest}",
            }
        )
    return rows


def create_alerts(session: Session, today: date, now: datetime) -> JobResult:
    result = JobResult()
    users = list(session.scalars(select(User).order_by(User.created_at)))
    settings = {s.user_id: s for s in session.scalars(select(AlertSettings))}
    # Each user's open positions, read once. Only what somebody holds needs prices.
    positions: dict[uuid.UUID, list[Position]] = {}
    for user in users:
        if settings.get(user.id, DEFAULTS).daily_moves_enabled:
            try:
                positions[user.id] = _open_positions(session, user)
            except Exception as exc:  # noqa: BLE001 - one user's failure must not stop the others
                session.rollback()
                log.exception("positions of user %s failed", user.id)
                result.errors[user.email] = f"{type(exc).__name__}: {exc}"
    held = {p.isin for mine in positions.values() for p in mine if p.asset_class in MOVE_CLASSES}
    instruments = {
        i.isin: i
        for i in session.scalars(
            select(Instrument).where(Instrument.isin.in_(held), Instrument.active.is_(True))
        )
        if i.isin
    }
    bars = _newest_bars(session, [i.id for i in instruments.values()])
    wanted = {
        u.id for u in users if u.id in positions or u.role == Role.ADMIN or u.email in result.errors
    }
    result.attempted = len(wanted)  # the users that have something to check
    for user in users:
        if user.id not in wanted:
            continue
        mine = settings.get(user.id, DEFAULTS)
        try:
            rows: list[dict[str, object]] = []
            if user.id in positions:
                rows += _user_moves(user, positions[user.id], mine, instruments, bars, today)
            if user.role == Role.ADMIN:
                rows += _failed_jobs(session, user, now)
            result.rows_written += _store(session, rows)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one user's failure must not stop the others
            session.rollback()
            log.exception("alerts for user %s failed", user.id)
            result.errors[user.email] = f"{type(exc).__name__}: {exc}"
    return result

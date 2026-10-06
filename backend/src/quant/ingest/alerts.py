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
import re
import uuid
from dataclasses import dataclass
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
    DEFAULT_DAILY_MOVES,
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
    MAX_GAP_DAYS,
    MAX_PRICE_AGE_DAYS,
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
ALERT_JOB_NAME = "create_alerts"  # the job that runs this module
STUCK_AFTER_HOURS = 6  # no job here runs this long
PARTIAL_MIN_FAILURES = 5
PARTIAL_MIN_SHARE = 0.25


@dataclass(frozen=True)
class Person:
    """The few fields of a user the job needs, copied out once: the session commits after every
    user, which would otherwise expire the ORM rows and re-read them at each access."""

    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime


@dataclass(frozen=True)
class Effective:
    """What a user's alert settings come to: their saved row, or the defaults when there is none."""

    daily_moves_enabled: bool
    move_stock_pct: Decimal
    move_fund_pct: Decimal
    move_crypto_pct: Decimal


DEFAULTS = Effective(DEFAULT_DAILY_MOVES, DEFAULT_STOCK_PCT, DEFAULT_FUND_PCT, DEFAULT_CRYPTO_PCT)


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
    session: Session, instrument_ids: list[int], since: date
) -> dict[int, list[tuple[date, Decimal]]]:
    """The two newest closes of each instrument since `since`, newest first."""
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
        .where(PriceEOD.instrument_id.in_(instrument_ids), PriceEOD.date >= since)
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


def _open_positions(session: Session, user: Person) -> list[Position]:
    return open_positions(compute_positions(service.load_movements(session, user.id)))


def _user_moves(
    user: Person,
    positions: list[Position],
    settings: Effective,
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
        if limit is None or instrument is None:
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


def _normalise(reason: str) -> str:
    """The reason without the parts that change from run to run (counts, ids, ports), so the same
    outage hashes the same."""
    return re.sub(r"[0-9a-fA-F]{6,}|\d+", "#", reason)


def _job_events(session: Session, now: datetime) -> list[tuple[datetime, dict[str, object]]]:
    """What went wrong in the jobs in the last two days, as alert rows without a user, each with
    the time it happened. Runs that failed, finished with errors, or look stuck. A run that a later
    successful run of the same job has made good is not news any more. Read once for all admins."""
    since = now - timedelta(hours=FAILED_JOB_WINDOW_HOURS)

    def last_finished(*statuses: JobStatus) -> dict[str, datetime]:
        rows = session.execute(
            select(JobRun.job, func.max(JobRun.finished_at))
            .where(JobRun.status.in_(statuses))
            .group_by(JobRun.job)
        )
        return {job: when for job, when in rows if when is not None}

    # The scheduler counts a partial run as healthy, so a later partial run makes good a failure
    # or a stuck run; only a clean run makes good a run that finished with errors.
    healthy = last_finished(JobStatus.SUCCESS, JobStatus.PARTIAL)
    clean = last_finished(JobStatus.SUCCESS)
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
    events: list[tuple[datetime, dict[str, object]]] = []
    for run in runs:
        happened = run.finished_at or run.started_at
        fixed = (clean if run.status == JobStatus.PARTIAL else healthy).get(run.job)
        if fixed is not None and fixed > happened:
            continue
        stuck = run.status == JobStatus.RUNNING
        details = run.details or {}
        errors = details.get("errors")
        failures = len(errors) if isinstance(errors, dict) else 0
        if stuck:
            reason = f"The run started at {run.started_at:%Y-%m-%d %H:%M} UTC and never finished."
        elif details.get("error"):
            reason = str(details["error"])
        elif isinstance(errors, dict) and errors:
            # The alerts job names people in its errors: say what failed, not who it failed for.
            named = run.job != ALERT_JOB_NAME
            examples = "; ".join(
                f"{k}: {v}" if named else str(v) for k, v in list(errors.items())[:2]
            )
            reason = f"{failures} items failed, for example: {examples}"
        else:
            reason = ""
        if run.status == JobStatus.PARTIAL:
            if not reason:
                continue  # nothing went wrong that a person could act on
            # One delisted ticker is a Prices-page matter, not an alert every night: a partial run
            # alerts when a few items failed, or a quarter of them.
            attempted = details.get("attempted")
            share = failures / attempted if isinstance(attempted, int) and attempted else 1
            if failures < PARTIAL_MIN_FAILURES and share < PARTIAL_MIN_SHARE:
                continue
        what = (
            "seems stuck"
            if stuck
            else "failed"
            if run.status == JobStatus.FAILED
            else "finished with errors"
        )
        # One alert per job, outcome and local day. A failed or stuck run adds its reason, so a
        # different failure the same day still arrives; a partial run's text changes with the
        # count and must not make a second alert.
        digest = (
            ""
            if run.status == JobStatus.PARTIAL
            else hashlib.sha256(_normalise(reason).encode()).hexdigest()[:8]
        )
        day = happened.astimezone(tz).date().isoformat()
        events.append(
            (
                happened,
                {
                    "kind": "job_failed",
                    "severity": "info" if run.status == JobStatus.PARTIAL else "warning",
                    "title": f"Job {run.job} {what}",
                    "body": (reason or "The run failed with no message.")[:900],
                    "isin": None,
                    "dedupe_key": f"job_failed:{run.job}:{run.status.value}:{day}:{digest}".rstrip(
                        ":"
                    ),
                },
            )
        )
    return events


def create_alerts(session: Session, today: date, now: datetime) -> JobResult:
    result = JobResult()
    users = [
        Person(u.id, u.email, u.role == Role.ADMIN, u.created_at)
        for u in session.scalars(select(User).order_by(User.created_at))
    ]
    settings: dict[uuid.UUID, Effective] = {
        s.user_id: Effective(
            s.daily_moves_enabled, s.move_stock_pct, s.move_fund_pct, s.move_crypto_pct
        )
        for s in session.scalars(select(AlertSettings))
    }
    # Each user's open positions, read once. Only what somebody holds needs prices.
    positions: dict[uuid.UUID, list[Position]] = {}
    for user in users:
        if settings.get(user.id, DEFAULTS).daily_moves_enabled:
            try:
                positions[user.id] = _open_positions(session, user)
            except Exception as exc:  # noqa: BLE001 - one user's failure must not stop the others
                session.rollback()
                log.exception("positions of user %s failed", user.id)
                result.errors[str(user.id)] = f"{type(exc).__name__}: {exc}"
    held = {p.isin for mine in positions.values() for p in mine if p.asset_class in MOVE_CLASSES}
    instruments = {
        i.isin: i
        for i in session.scalars(
            select(Instrument).where(Instrument.isin.in_(held), Instrument.active.is_(True))
        )
        if i.isin
    }
    # A move needs a fresh bar and one at most MAX_GAP_DAYS before it: nothing older is read.
    window = today - timedelta(days=MAX_PRICE_AGE_DAYS + MAX_GAP_DAYS + 1)
    bars = _newest_bars(session, [i.id for i in instruments.values()], window)
    job_events = _job_events(session, now) if any(u.is_admin for u in users) else []
    wanted = {u.id for u in users if u.id in positions or u.is_admin or str(u.id) in result.errors}
    result.attempted = len(wanted)  # the users that have something to check
    for user in users:
        if user.id not in wanted:
            continue
        mine = settings.get(user.id, DEFAULTS)
        try:
            rows: list[dict[str, object]] = []
            if user.id in positions:
                rows += _user_moves(user, positions[user.id], mine, instruments, bars, today)
            if user.is_admin:
                # Nothing about the time before the account existed.
                rows += [
                    {**event, "user_id": user.id}
                    for happened, event in job_events
                    if happened >= user.created_at
                ]
            result.rows_written += _store(session, rows)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one user's failure must not stop the others
            session.rollback()
            log.exception("alerts for user %s failed", user.id)
            result.errors[str(user.id)] = f"{type(exc).__name__}: {exc}"
    return result

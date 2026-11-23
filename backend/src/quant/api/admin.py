"""Admin endpoints: invites (FR-1) and the admin panel: users, last import, data-job health and
AI spend (FR-5)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from quant import rls
from quant.ai import budget
from quant.api.deps import AdminUser, DbSession
from quant.config import get_settings
from quant.db import get_sessionmaker
from quant.models import (
    Import,
    ImportStatus,
    Invite,
    JobRun,
    JobStatus,
    LlmUsage,
    Passkey,
    User,
)
from quant.security import hash_token, new_token

router = APIRouter(prefix="/api/admin", tags=["admin"])


class InviteIn(BaseModel):
    email: EmailStr


class InviteOut(BaseModel):
    email: str
    token: str  # shown once; only its hash is stored
    expires_at: datetime


class JobRunOut(BaseModel):
    id: int
    job: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    rows_written: int
    details: dict[str, object]


@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
def create_invite(body: InviteIn, admin: AdminUser, db: DbSession) -> InviteOut:
    if db.scalar(select(User).where(func.lower(User.email) == body.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")
    token = new_token()
    expires = datetime.now(UTC) + timedelta(hours=get_settings().invite_ttl_hours)
    db.add(
        Invite(
            token_hash=hash_token(token),
            email=body.email.lower(),
            created_by=admin.id,
            expires_at=expires,
        )
    )
    db.commit()
    return InviteOut(email=body.email.lower(), token=token, expires_at=expires)


@router.get("/jobs", response_model=list[JobRunOut])
def job_runs(_: AdminUser, db: DbSession, limit: int = 50) -> list[JobRunOut]:
    runs = db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(min(limit, 200)))
    return [
        JobRunOut(
            id=r.id,
            job=r.job,
            status=r.status,
            started_at=r.started_at,
            finished_at=r.finished_at,
            rows_written=r.rows_written,
            details=r.details,
        )
        for r in runs
    ]


# --- the admin panel (FR-5) ------------------------------------------------------------------

# Every scheduled job, and the longest a healthy one may go without a good run. The prices run on
# weekdays only, so a weekend is 3 days; the weekly commentary runs on Sundays.
EXPECTED_JOBS: dict[str, int] = {
    "ingest_prices": 80,
    "ingest_fx": 80,
    "map_isins": 80,
    "score_picks": 80,
    "score_factors": 80,
    "scan_swing": 80,
    "push_alerts": 12,  # every 15 minutes from 06:00 to 23:45
    "send_digests": 36,  # hourly from 07:05 to 21:05, and only sends when one is due
    "create_alerts": 80,
    "weekly_commentary": 8 * 24,
}
FAILURE_WINDOW_DAYS = 7


class UserRow(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    totp_enabled: bool
    passkeys: int
    created_at: datetime
    last_login_at: datetime | None
    locked: bool
    last_import_at: datetime | None
    last_import_source: str | None
    ai_spend_eur: Decimal


class JobHealth(BaseModel):
    job: str
    healthy: bool
    problem: str | None
    last_status: str | None
    last_started: datetime | None
    last_finished: datetime | None
    last_success: datetime | None
    rows_written: int
    failures_7d: int
    error_count: int  # failed items in the last run (a symbol, a share), counted, not named


class LlmSpend(BaseModel):
    month: str
    total_eur: Decimal
    total_cap_eur: Decimal
    user_cap_eur: Decimal
    by_purpose: dict[str, Decimal]


class Overview(BaseModel):
    now: datetime
    users: list[UserRow]
    jobs: list[JobHealth]
    llm: LlmSpend
    pending_invites: int


def _job_health(runs: list[JobRun], job: str, now: datetime) -> JobHealth:
    mine = sorted((r for r in runs if r.job == job), key=lambda r: r.started_at)
    last = mine[-1] if mine else None
    good = [r for r in mine if r.status in (JobStatus.SUCCESS, JobStatus.PARTIAL)]
    last_success = good[-1].finished_at or good[-1].started_at if good else None
    window = now - timedelta(days=FAILURE_WINDOW_DAYS)
    failures = sum(1 for r in mine if r.status == JobStatus.FAILED and r.started_at >= window)
    errors = last.details.get("errors") if last is not None else None
    problem: str | None = None
    if last is None:
        problem = "it has never run"
    elif last.status == JobStatus.FAILED:
        problem = "the last run failed"
    elif last_success is None or now - last_success > timedelta(hours=EXPECTED_JOBS[job]):
        problem = "no good run for too long"
    elif last.status == JobStatus.RUNNING and now - last.started_at > timedelta(hours=3):
        problem = "the last run has been running for over 3 hours"
    return JobHealth(
        job=job,
        healthy=problem is None,
        problem=problem,
        last_status=str(last.status) if last else None,
        last_started=last.started_at if last else None,
        last_finished=last.finished_at if last else None,
        last_success=last_success,
        rows_written=last.rows_written if last else 0,
        failures_7d=failures,
        error_count=len(errors) if isinstance(errors, dict) else 0,
    )


@router.get("/overview", response_model=Overview)
def overview(admin: AdminUser) -> Overview:
    """Users, their last import, the health of every job, and the AI spend of the month. The
    admin sees counts and times, never anyone's holdings or amounts. Per-user rows are read with
    the row-level-security bypass the background jobs use (FR-3), and only these aggregates leave
    this function."""
    now = datetime.now(UTC)
    start, month = budget.month_bounds(now)
    settings = get_settings()
    with get_sessionmaker()() as db:
        rls.bypass(db)
        users = db.scalars(select(User).order_by(User.created_at)).all()
        spend = dict(
            db.execute(
                select(LlmUsage.user_id, func.sum(LlmUsage.cost_eur))
                .where(LlmUsage.created_at >= start)
                .group_by(LlmUsage.user_id)
            ).all()
        )
        passkeys_of = dict(
            db.execute(select(Passkey.user_id, func.count()).group_by(Passkey.user_id)).all()
        )
        latest = {
            uid: (at, source)
            for uid, at, source in db.execute(
                select(Import.user_id, Import.committed_at, Import.source)
                .where(Import.status == ImportStatus.COMMITTED, Import.committed_at.is_not(None))
                .order_by(Import.committed_at)
            ).all()
        }  # oldest first, so the newest wins
        purposes = {
            p: Decimal(c or 0)
            for p, c in db.execute(
                select(LlmUsage.purpose, func.sum(LlmUsage.cost_eur))
                .where(LlmUsage.created_at >= start)
                .group_by(LlmUsage.purpose)
            ).all()
        }
        runs = list(
            db.scalars(select(JobRun).where(JobRun.started_at >= now - timedelta(days=30))).all()
        )
        pending = db.scalar(
            select(func.count())
            .select_from(Invite)
            .where(Invite.used_at.is_(None), Invite.expires_at > now)
        )
        rows = [
            UserRow(
                id=str(u.id),
                email=u.email,
                display_name=u.display_name,
                role=str(u.role),
                totp_enabled=u.totp_enabled,
                passkeys=passkeys_of.get(u.id, 0),
                created_at=u.created_at,
                last_login_at=u.last_login_at,
                locked=bool(u.locked_until and u.locked_until > now),
                last_import_at=latest[u.id][0] if u.id in latest else None,
                last_import_source=latest[u.id][1] if u.id in latest else None,
                ai_spend_eur=Decimal(spend.get(u.id) or 0),
            )
            for u in users
        ]
    return Overview(
        now=now,
        users=rows,
        jobs=[_job_health(runs, job, now) for job in EXPECTED_JOBS],
        llm=LlmSpend(
            month=month,
            total_eur=sum(spend.values(), Decimal(0)),
            total_cap_eur=settings.llm_monthly_cap_eur,
            user_cap_eur=settings.llm_user_monthly_cap_eur,
            by_purpose=purposes,
        ),
        pending_invites=pending or 0,
    )

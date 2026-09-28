"""Admin endpoints: invites (FR-1) and data-job health (FR-5)."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from quant.api.deps import AdminUser, DbSession
from quant.config import get_settings
from quant.models import Invite, JobRun, User
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

"""Sign-in, sign-out, invite registration and TOTP (FR-1, FR-2).

Passkeys (WebAuthn, FR-2's primary method) follow in Phase 1. Password plus TOTP is the fallback
the PRD requires anyway, so it comes first.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select

from quant import disclaimer
from quant.api.deps import SESSION_COOKIE, CurrentUser, DbSession, ScopedDb
from quant.config import get_settings
from quant.models import AuthSession, Invite, Role, User
from quant.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    hash_token,
    new_token,
    new_totp_secret,
    totp_uri,
    verify_password,
    verify_totp,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Verified against when the email is unknown, so response time doesn't reveal which emails exist.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class LoginIn(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: Role
    totp_enabled: bool
    # Whether the user accepted the current disclaimer (FR-4). Only /me looks it up, so it is
    # None (unknown) in the other answers; the page sends the user to /disclaimer unless it is
    # true. The data endpoints refuse a user who has not accepted (403) whatever the page does.
    disclaimer_accepted: bool | None = None

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            totp_enabled=user.totp_enabled,
        )


class RegisterIn(BaseModel):
    token: str
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class InviteInfo(BaseModel):
    email: str


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class TotpCodeIn(BaseModel):
    code: str


def _start_session(db: DbSession, response: Response, user: User) -> None:
    settings = get_settings()
    token = new_token()
    now = datetime.now(UTC)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
        )
    )
    # Housekeeping: drop this user's expired sessions.
    db.execute(
        delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at <= now)
    )
    user.last_login_at = now
    db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, db: DbSession, response: Response) -> UserOut:
    settings = get_settings()
    now = datetime.now(UTC)
    user = db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if user is None:
        verify_password(_DUMMY_HASH, body.password)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if user.locked_until and user.locked_until > now:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many failed attempts, try later"
        )

    def fail() -> HTTPException:
        # Wrong passwords and wrong TOTP codes share one counter and one lockout.
        user.failed_logins += 1
        if user.failed_logins >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_logins = 0
        db.commit()
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    if not verify_password(user.password_hash, body.password):
        raise fail()

    if user.totp_enabled:
        if not body.totp_code:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "totp_required")
        if not user.totp_secret or not verify_totp(user.totp_secret, body.totp_code):
            raise fail()

    user.failed_logins = 0
    user.locked_until = None
    _start_session(db, response, user)
    return UserOut.of(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    db: DbSession, response: Response, qt_session: Annotated[str | None, Cookie()] = None
) -> None:
    if qt_session:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(qt_session)))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser, db: ScopedDb) -> UserOut:
    out = UserOut.of(user)
    out.disclaimer_accepted = disclaimer.accepted(db, user.id)
    return out


class DisclaimerOut(BaseModel):
    text: str
    version: int
    accepted: bool


@router.get("/disclaimer", response_model=DisclaimerOut)
def get_disclaimer(user: CurrentUser, db: ScopedDb) -> DisclaimerOut:
    return DisclaimerOut(
        text=disclaimer.TEXT, version=disclaimer.VERSION, accepted=disclaimer.accepted(db, user.id)
    )


class AcceptIn(BaseModel):
    version: int  # the version of the text the user was shown


@router.post("/disclaimer", response_model=DisclaimerOut)
def accept_disclaimer(body: AcceptIn, user: CurrentUser, db: ScopedDb) -> DisclaimerOut:
    if body.version != disclaimer.VERSION:
        # The text changed while the page was open: show the new one before accepting it.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the disclaimer changed, please read it again"
        )
    disclaimer.accept(db, user.id)
    return DisclaimerOut(text=disclaimer.TEXT, version=disclaimer.VERSION, accepted=True)


def _valid_invite(db: DbSession, token: str) -> Invite:
    invite = db.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if invite is None or invite.used_at is not None or invite.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite is invalid or expired")
    return invite


@router.get("/invite/{token}", response_model=InviteInfo)
def invite_info(token: str, db: DbSession) -> InviteInfo:
    return InviteInfo(email=_valid_invite(db, token).email)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, db: DbSession, response: Response) -> UserOut:
    invite = _valid_invite(db, body.token)
    if db.scalar(select(User).where(func.lower(User.email) == invite.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")
    user = User(
        email=invite.email,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        role=Role.MEMBER,
    )
    db.add(user)
    invite.used_at = datetime.now(UTC)
    db.flush()
    _start_session(db, response, user)
    return UserOut.of(user)


@router.post("/totp/setup", response_model=TotpSetupOut)
def totp_setup(user: CurrentUser, db: DbSession) -> TotpSetupOut:
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "TOTP is already enabled")
    user.totp_secret = new_totp_secret()
    db.commit()
    return TotpSetupOut(
        secret=user.totp_secret,
        otpauth_uri=totp_uri(user.totp_secret, user.email, get_settings().totp_issuer),
    )


@router.post("/totp/enable", response_model=UserOut)
def totp_enable(body: TotpCodeIn, user: CurrentUser, db: DbSession) -> UserOut:
    if not user.totp_secret or not verify_totp(user.totp_secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code")
    user.totp_enabled = True
    db.commit()
    return UserOut.of(user)

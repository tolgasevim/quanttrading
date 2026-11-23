"""Passkeys: add, list, remove, and sign in with one (FR-2).

The password with TOTP stays as the fallback (`api/auth.py`). A ceremony has two steps: the
server hands out options with a challenge, the browser signs it, and the server checks the answer.
The challenge is stored for five minutes and works once, whatever the outcome.
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from quant import passkeys
from quant.api.auth import UserOut, _start_session
from quant.api.deps import CurrentUser, DbSession
from quant.models import Passkey, User, WebauthnChallenge

router = APIRouter(prefix="/api/auth", tags=["auth"])

CHALLENGE_MINUTES = 5
MAX_PASSKEYS = 10


class OptionsOut(BaseModel):
    challenge_id: str
    options: dict[str, Any]


class RegisterIn(BaseModel):
    challenge_id: uuid.UUID
    credential: dict[str, Any]
    name: str = Field(default="My device", min_length=1, max_length=60)


class LoginIn(BaseModel):
    challenge_id: uuid.UUID
    credential: dict[str, Any]


class PasskeyOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None


def _out(p: Passkey) -> PasskeyOut:
    return PasskeyOut(
        id=str(p.id), name=p.name, created_at=p.created_at, last_used_at=p.last_used_at
    )


def _store(db: DbSession, challenge: bytes, purpose: str, user_id: uuid.UUID | None) -> str:
    now = datetime.now(UTC)
    db.execute(delete(WebauthnChallenge).where(WebauthnChallenge.expires_at <= now))  # housekeeping
    row = WebauthnChallenge(
        challenge=challenge,
        purpose=purpose,
        user_id=user_id,
        expires_at=now + timedelta(minutes=CHALLENGE_MINUTES),
    )
    db.add(row)
    db.commit()
    return str(row.id)


def _take(db: DbSession, challenge_id: uuid.UUID, purpose: str) -> WebauthnChallenge:
    """The stored challenge, removed so it cannot be used twice."""
    row = db.get(WebauthnChallenge, challenge_id)
    if row is None or row.purpose != purpose:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the passkey request is not valid")
    expired = row.expires_at <= datetime.now(UTC)
    db.delete(row)
    db.commit()
    if expired:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the passkey request has expired")
    return row


@router.post("/passkey/register/options", response_model=OptionsOut)
def register_options(user: CurrentUser, db: DbSession) -> OptionsOut:
    held = list(db.scalars(select(Passkey.credential_id).where(Passkey.user_id == user.id)))
    if len(held) >= MAX_PASSKEYS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"at most {MAX_PASSKEYS} passkeys")
    options, challenge = passkeys.registration_options(user, held)
    return OptionsOut(challenge_id=_store(db, challenge, "register", user.id), options=options)


@router.post("/passkey/register", response_model=PasskeyOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, user: CurrentUser, db: DbSession) -> PasskeyOut:
    row = _take(db, body.challenge_id, "register")
    if row.user_id != user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the passkey request is not valid")
    try:
        new = passkeys.verify_registration(body.credential, row.challenge)
    except passkeys.PasskeyError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "the passkey could not be verified"
        ) from exc
    if db.scalar(select(Passkey.id).where(Passkey.credential_id == new.credential_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "this passkey is already registered")
    passkey = Passkey(
        user_id=user.id,
        credential_id=new.credential_id,
        public_key=new.public_key,
        sign_count=new.sign_count,
        name=body.name.strip() or "My device",
    )
    db.add(passkey)
    db.commit()
    return _out(passkey)


@router.get("/passkeys", response_model=list[PasskeyOut])
def list_passkeys(user: CurrentUser, db: DbSession) -> list[PasskeyOut]:
    rows = db.scalars(
        select(Passkey).where(Passkey.user_id == user.id).order_by(Passkey.created_at)
    )
    return [_out(p) for p in rows]


@router.delete("/passkeys/{passkey_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_passkey(passkey_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    row = db.get(Passkey, passkey_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such passkey")
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/passkey/login/options", response_model=OptionsOut)
def login_options(db: DbSession) -> OptionsOut:
    options, challenge = passkeys.login_options()
    return OptionsOut(challenge_id=_store(db, challenge, "login", None), options=options)


@router.post("/passkey/login", response_model=UserOut)
def login(body: LoginIn, db: DbSession, response: Response) -> UserOut:
    row = _take(db, body.challenge_id, "login")
    fail = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    raw_id = body.credential.get("rawId") or body.credential.get("id")
    try:
        credential_id = _b64url(str(raw_id))
    except ValueError as exc:
        raise fail from exc
    passkey = db.scalar(select(Passkey).where(Passkey.credential_id == credential_id))
    if passkey is None:
        raise fail
    user = db.get(User, passkey.user_id)
    if user is None:
        raise fail
    now = datetime.now(UTC)
    if user.locked_until and user.locked_until > now:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many failed attempts, try later"
        )
    try:
        new_count = passkeys.verify_login(
            body.credential, row.challenge, passkey.public_key, passkey.sign_count
        )
    except passkeys.PasskeyError as exc:
        raise fail from exc
    passkey.sign_count = new_count
    passkey.last_used_at = now
    user.failed_logins = 0
    _start_session(db, response, user)
    return UserOut.of(user)


def _b64url(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)

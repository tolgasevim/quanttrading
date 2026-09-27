"""Request dependencies: DB session and the signed-in user."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.db import get_db
from quant.models import AuthSession, Role, User
from quant.security import hash_token

SESSION_COOKIE = "qt_session"

DbSession = Annotated[Session, Depends(get_db)]


def current_user(db: DbSession, qt_session: Annotated[str | None, Cookie()] = None) -> User:
    if not qt_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    auth = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(qt_session)))
    if auth is None or auth.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    return auth.user


CurrentUser = Annotated[User, Depends(current_user)]


def admin_user(user: CurrentUser) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user


AdminUser = Annotated[User, Depends(admin_user)]


def user_db(db: DbSession, user: CurrentUser) -> Session:
    """A session that can only see the signed-in user's rows (FR-3, row-level security)."""
    return rls.scope_to_user(db, user.id)


UserDb = Annotated[Session, Depends(user_db)]

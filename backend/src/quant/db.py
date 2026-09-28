"""Database engine and session helpers."""

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from quant import rls  # noqa: F401  (registers the per-transaction RLS listener)
from quant.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    engine = create_engine(settings.db_url(), pool_pre_ping=True)
    role = settings.db_app_role
    if role:
        if not role.isidentifier():
            raise ValueError(f"invalid db_app_role: {role!r}")

        @event.listens_for(engine, "connect")
        def _set_role(dbapi_connection: Any, _: Any) -> None:
            # Session-level and permanent for the pooled connection: the app never acts as the
            # table owner, so RLS policies always apply.
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f"SET ROLE {role}")
            dbapi_connection.commit()

    return engine


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed by the handler."""
    with get_sessionmaker()() as session:
        yield session

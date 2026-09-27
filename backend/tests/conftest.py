"""Test setup: a real Postgres database migrated with Alembic, truncated between tests."""

import os

os.environ.setdefault(
    "QT_DATABASE_URL", "postgresql+psycopg://quant:quant@localhost:5432/quant_test"
)
os.environ["QT_COOKIE_SECURE"] = "false"  # TestClient talks plain http

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from quant.config import get_settings  # noqa: E402
from quant.db import get_sessionmaker  # noqa: E402
from quant.main import app  # noqa: E402
from quant.models import Base, Role, User  # noqa: E402
from quant.security import hash_password  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    yield
    names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    # As the owner: the app role may not TRUNCATE.
    owner = create_engine(get_settings().database_url)
    with owner.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    owner.dispose()


@pytest.fixture
def db() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def make_user(db: Session, email: str, role: Role = Role.MEMBER) -> User:
    user = User(
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(PASSWORD),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def admin(db: Session) -> User:
    return make_user(db, "owner@example.com", Role.ADMIN)


def login(client: TestClient, email: str, password: str = PASSWORD, totp: str | None = None) -> int:
    body = {"email": email, "password": password}
    if totp:
        body["totp_code"] = totp
    status: int = client.post("/api/auth/login", json=body).status_code
    return status

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from quant import rls
from quant.config import get_settings
from quant.models import Import, Transaction, User

from .conftest import FIXTURES, login, make_user
from .test_tr_csv import PII, SYNTHETIC


def upload(client: TestClient, content: str = SYNTHETIC) -> dict:  # type: ignore[type-arg]
    response = client.post(
        "/api/imports", files={"file": ("transactions.csv", content.encode(), "text/csv")}
    )
    assert response.status_code == 201, response.text
    body: dict = response.json()  # type: ignore[type-arg]
    return body


@pytest.fixture
def owner_client(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    return client


def test_preview_then_commit(owner_client: TestClient) -> None:
    preview = upload(owner_client)
    assert preview["status"] == "preview"
    assert preview["summary"]["new"] == 12 and preview["summary"]["already_imported"] == 0
    # Nothing is written before confirmation (FR-15).
    assert owner_client.get("/api/transactions").json()["counts"] == {}

    committed = owner_client.post(f"/api/imports/{preview['id']}/commit").json()
    assert committed["status"] == "committed" and committed["rows_inserted"] == 12
    listing = owner_client.get("/api/transactions").json()
    assert sum(listing["counts"].values()) == 12
    assert all(item["kind"] != "card" for item in listing["items"])  # hidden by default
    assert listing["cash_balance"] == "6293.990000"

    again = owner_client.post(f"/api/imports/{preview['id']}/commit")
    assert again.status_code == 409


def test_reimport_is_idempotent(owner_client: TestClient) -> None:
    first = upload(owner_client)
    owner_client.post(f"/api/imports/{first['id']}/commit")
    second = upload(owner_client)
    assert second["summary"]["already_imported"] == 12 and second["summary"]["new"] == 0
    assert owner_client.post(f"/api/imports/{second['id']}/commit").json()["rows_inserted"] == 0
    assert sum(owner_client.get("/api/transactions").json()["counts"].values()) == 12


def test_discard(owner_client: TestClient, db: Session) -> None:
    preview = upload(owner_client)
    assert owner_client.delete(f"/api/imports/{preview['id']}").json()["status"] == "discarded"
    rls.bypass(db)
    record = db.scalar(select(Import))
    assert record is not None and record.staged is None


def test_rejects_non_tr_files_and_oversized_uploads(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv = (FIXTURES / "stooq_nvda.csv").read_bytes()
    bad = owner_client.post("/api/imports", files={"file": ("x.csv", csv, "text/csv")})
    assert bad.status_code == 422 and "Trade Republic" in bad.json()["detail"]

    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
    big = owner_client.post("/api/imports", files={"file": ("x.csv", b"x", "text/csv")})
    assert big.status_code == 413


def test_personal_data_is_not_stored(owner_client: TestClient, db: Session) -> None:
    preview = upload(owner_client)
    owner_client.post(f"/api/imports/{preview['id']}/commit")
    rls.bypass(db)
    stored = json.dumps(
        [
            [str(v) for v in row]
            for row in [
                *db.execute(text("SELECT * FROM transactions")).all(),
                *db.execute(text("SELECT * FROM imports")).all(),
            ]
        ]
    )
    for secret in PII:
        assert secret not in stored, secret


def test_users_cannot_see_each_others_data(owner_client: TestClient, db: Session) -> None:
    preview = upload(owner_client)
    owner_client.post(f"/api/imports/{preview['id']}/commit")

    make_user(db, "sister@example.com")
    other = TestClient(owner_client.app)
    login(other, "sister@example.com")
    assert other.get("/api/imports").json() == []
    assert other.get(f"/api/imports/{preview['id']}").status_code == 404
    assert other.delete(f"/api/imports/{preview['id']}").status_code == 404
    listing = other.get("/api/transactions").json()
    assert listing["counts"] == {} and listing["items"] == []


def test_row_level_security_is_enforced_by_the_database(
    owner_client: TestClient, admin: User, db: Session
) -> None:
    """Even a query that forgets `WHERE user_id = ...` only sees the scoped user's rows (FR-3)."""
    preview = upload(owner_client)
    owner_client.post(f"/api/imports/{preview['id']}/commit")
    sister = make_user(db, "sister@example.com")

    # No user in the session: fails closed.
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0

    rls.scope_to_user(db, sister.id)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0
    # ...and she cannot write rows in someone else's name.
    with pytest.raises(ProgrammingError, match="row-level security"):
        db.execute(
            text(
                "INSERT INTO imports (id, user_id, source, status, summary, rows_inserted) "
                "VALUES (gen_random_uuid(), :uid, 'x', 'preview', '{}', 0)"
            ),
            {"uid": str(admin.id)},
        )
    db.rollback()

    rls.scope_to_user(db, admin.id)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Transaction)) == 12

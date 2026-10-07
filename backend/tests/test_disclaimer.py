from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import disclaimer, rls
from quant.models import DisclaimerAcceptance, User

from .conftest import login, make_user


def test_a_new_user_has_not_accepted_the_disclaimer(owner: TestClient) -> None:
    assert owner.get("/api/auth/me").json()["disclaimer_accepted"] is False
    body = owner.get("/api/auth/disclaimer").json()
    assert body["accepted"] is False and body["version"] == disclaimer.VERSION
    for words in ("not investment advice", "AI may be wrong", "never places trades"):
        assert words in body["text"]


def test_accepting_it_is_remembered(owner: TestClient) -> None:
    body = owner.post("/api/auth/disclaimer").json()
    assert body["accepted"] is True
    assert owner.get("/api/auth/me").json()["disclaimer_accepted"] is True
    assert owner.get("/api/auth/disclaimer").json()["accepted"] is True


def test_a_new_version_asks_everybody_again(owner: TestClient, db: Session, admin: User) -> None:
    owner.post("/api/auth/disclaimer")
    rls.bypass(db)
    row = db.scalars(select(DisclaimerAcceptance)).one()
    row.version = disclaimer.VERSION - 1  # the text has changed since they accepted
    db.commit()
    assert owner.get("/api/auth/me").json()["disclaimer_accepted"] is False
    owner.post("/api/auth/disclaimer")
    assert owner.get("/api/auth/me").json()["disclaimer_accepted"] is True
    db.expire_all()
    assert db.scalars(select(DisclaimerAcceptance)).one().version == disclaimer.VERSION


def test_accepting_twice_keeps_the_first_time(owner: TestClient, db: Session) -> None:
    owner.post("/api/auth/disclaimer")
    rls.bypass(db)
    first = db.scalars(select(DisclaimerAcceptance)).one().accepted_at
    db.expire_all()
    owner.post("/api/auth/disclaimer")
    rls.bypass(db)
    assert db.scalars(select(DisclaimerAcceptance)).one().accepted_at == first


def test_acceptance_is_per_user_and_private(client: TestClient, db: Session, admin: User) -> None:
    login(client, admin.email)
    client.post("/api/auth/disclaimer")
    other = make_user(db, "other@example.com")
    guest = TestClient(client.app)
    login(guest, other.email)
    assert guest.get("/api/auth/me").json()["disclaimer_accepted"] is False
    assert client.get("/api/auth/me").json()["disclaimer_accepted"] is True


def test_the_disclaimer_needs_a_login(client: TestClient) -> None:
    assert client.get("/api/auth/disclaimer").status_code == 401
    assert client.post("/api/auth/disclaimer").status_code == 401

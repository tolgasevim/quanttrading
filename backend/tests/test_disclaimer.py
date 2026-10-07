import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import disclaimer, rls
from quant.models import DisclaimerAcceptance, Role, User

from .conftest import login, make_user

# The text of each version of the disclaimer. Changing `disclaimer.TEXT` without raising
# `disclaimer.VERSION` (FR-4: ask again after every change) fails the test below.
PINNED = {1: "672aa44ca117c14cf86af51c6a3445500fbc7cad96d6f65a6510859990535e6a"}


ACCEPT = {"version": disclaimer.VERSION}


@pytest.fixture
def newbie(db: Session) -> User:
    """A user who has not accepted the disclaimer yet."""
    return make_user(db, "new@example.com", Role.MEMBER, accepted=False)


@pytest.fixture
def fresh(client: TestClient, newbie: User) -> TestClient:
    login(client, newbie.email)
    return client


def test_the_text_and_its_version_move_together() -> None:
    digest = hashlib.sha256(disclaimer.TEXT.encode()).hexdigest()
    assert PINNED.get(disclaimer.VERSION) == digest, (
        f"the disclaimer text changed: raise VERSION and pin this digest for it: {digest}"
    )


def test_a_new_user_has_not_accepted_the_disclaimer(fresh: TestClient) -> None:
    assert fresh.get("/api/auth/me").json()["disclaimer_accepted"] is False
    body = fresh.get("/api/auth/disclaimer").json()
    assert body["accepted"] is False and body["version"] == disclaimer.VERSION
    for words in ("not investment advice", "AI may be wrong", "never places trades"):
        assert words in body["text"]


def test_data_endpoints_refuse_a_user_who_has_not_accepted(fresh: TestClient) -> None:
    for path in ("/api/holdings", "/api/notifications", "/api/ai/status", "/api/tax", "/api/costs"):
        r = fresh.get(path)
        assert r.status_code == 403 and "disclaimer" in r.json()["detail"], path
    assert fresh.post("/api/ai/ask", json={"question": "Hello there"}).status_code == 403
    # Only what is needed to accept stays open.
    assert fresh.get("/api/auth/me").status_code == 200
    assert fresh.get("/api/auth/disclaimer").status_code == 200


def test_accepting_it_opens_the_endpoints_and_is_remembered(fresh: TestClient) -> None:
    assert fresh.post("/api/auth/disclaimer", json=ACCEPT).json()["accepted"] is True
    assert fresh.get("/api/auth/me").json()["disclaimer_accepted"] is True
    assert fresh.get("/api/auth/disclaimer").json()["accepted"] is True
    assert fresh.get("/api/notifications").status_code == 200


def test_the_login_answer_does_not_claim_acceptance(client: TestClient, newbie: User) -> None:
    r = client.post(
        "/api/auth/login", json={"email": newbie.email, "password": "correct horse battery staple"}
    )
    assert r.status_code == 200 and r.json()["disclaimer_accepted"] is None  # unknown, not true


def test_a_new_version_asks_everybody_again(owner: TestClient, db: Session, admin: User) -> None:
    assert owner.get("/api/notifications").status_code == 200  # make_user accepted it
    rls.bypass(db)
    row = db.scalars(select(DisclaimerAcceptance)).one()
    row.version = disclaimer.VERSION - 1  # the text has changed since they accepted
    db.commit()
    assert owner.get("/api/auth/me").json()["disclaimer_accepted"] is False
    assert owner.get("/api/notifications").status_code == 403
    owner.post("/api/auth/disclaimer", json=ACCEPT)
    assert owner.get("/api/notifications").status_code == 200
    db.expire_all()
    assert db.scalars(select(DisclaimerAcceptance)).one().version == disclaimer.VERSION


def test_accepting_twice_keeps_the_first_time(fresh: TestClient, db: Session) -> None:
    fresh.post("/api/auth/disclaimer", json=ACCEPT)
    rls.bypass(db)
    first = db.scalars(select(DisclaimerAcceptance)).one().accepted_at
    db.expire_all()
    fresh.post("/api/auth/disclaimer", json=ACCEPT)
    fresh.post("/api/auth/disclaimer", json=ACCEPT)
    rls.bypass(db)
    assert db.scalars(select(DisclaimerAcceptance)).one().accepted_at == first


def test_acceptance_is_per_user_and_private(
    client: TestClient, db: Session, admin: User, newbie: User
) -> None:
    login(client, admin.email)
    guest = TestClient(client.app)
    login(guest, newbie.email)
    assert guest.get("/api/auth/me").json()["disclaimer_accepted"] is False
    assert client.get("/api/auth/me").json()["disclaimer_accepted"] is True


def test_the_disclaimer_needs_a_login(client: TestClient) -> None:
    assert client.get("/api/auth/disclaimer").status_code == 401
    assert client.post("/api/auth/disclaimer", json=ACCEPT).status_code == 401


def test_accepting_an_old_version_is_refused(fresh: TestClient) -> None:
    # The page showed version 0 and the server has moved on: the user must read the new text.
    r = fresh.post("/api/auth/disclaimer", json={"version": disclaimer.VERSION - 1})
    assert r.status_code == 409
    assert fresh.get("/api/auth/me").json()["disclaimer_accepted"] is False
    assert fresh.post("/api/auth/disclaimer", json={}).status_code == 422


def test_a_newer_acceptance_is_never_lowered(db: Session, newbie: User) -> None:
    rls.bypass(db)
    db.add(DisclaimerAcceptance(user_id=newbie.id, version=disclaimer.VERSION + 1, accepted_at=datetime.now(UTC)))  # fmt: skip
    db.commit()
    rls.scope_to_user(db, newbie.id)
    disclaimer.accept(db, newbie.id)  # e.g. after rolling a deploy back
    rls.bypass(db)
    db.expire_all()
    assert db.scalars(select(DisclaimerAcceptance)).one().version == disclaimer.VERSION + 1

from datetime import UTC, datetime, timedelta

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import AuthSession, Invite, User

from .conftest import PASSWORD, login, make_user


def test_login_me_logout(client: TestClient, admin: User) -> None:
    assert client.get("/api/auth/me").status_code == 401
    assert login(client, "OWNER@example.com") == 200  # email is case-insensitive
    me = client.get("/api/auth/me").json()
    assert me["email"] == "owner@example.com" and me["role"] == "admin"

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_session_cookie_is_hardened(client: TestClient, admin: User) -> None:
    response = client.post("/api/auth/login", json={"email": admin.email, "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_only_token_hash_is_stored(client: TestClient, admin: User, db: Session) -> None:
    login(client, admin.email)
    token = client.cookies["qt_session"]
    stored = db.scalars(select(AuthSession.token_hash)).all()
    assert token not in stored and len(stored) == 1


def test_wrong_password_and_unknown_user_look_the_same(client: TestClient, admin: User) -> None:
    wrong = client.post(
        "/api/auth/login", json={"email": admin.email, "password": "nope-nope-nope"}
    )
    unknown = client.post(
        "/api/auth/login", json={"email": "x@example.com", "password": "nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_lockout_after_repeated_failures(client: TestClient, admin: User) -> None:
    for _ in range(5):
        assert login(client, admin.email, password="wrong-password") == 401
    # Locked: even the right password is refused until the lockout expires.
    assert login(client, admin.email) == 429


def test_expired_session_is_rejected(client: TestClient, admin: User, db: Session) -> None:
    login(client, admin.email)
    session = db.scalar(select(AuthSession))
    assert session is not None
    session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_totp_enrolment_and_login(client: TestClient, admin: User) -> None:
    login(client, admin.email)
    secret = client.post("/api/auth/totp/setup").json()["secret"]
    assert client.post("/api/auth/totp/enable", json={"code": "000000"}).status_code == 400
    code = pyotp.TOTP(secret).now()
    assert client.post("/api/auth/totp/enable", json={"code": code}).json()["totp_enabled"]

    fresh = TestClient(client.app)
    response = fresh.post("/api/auth/login", json={"email": admin.email, "password": PASSWORD})
    assert response.status_code == 401 and response.json()["detail"] == "totp_required"
    assert login(fresh, admin.email, totp="123456") == 401
    assert login(fresh, admin.email, totp=pyotp.TOTP(secret).now()) == 200


def test_wrong_totp_codes_lock_the_account(client: TestClient, admin: User, db: Session) -> None:
    admin.totp_secret = pyotp.random_base32()
    admin.totp_enabled = True
    db.commit()
    for _ in range(5):
        assert login(client, admin.email, totp="000000") == 401
    assert login(client, admin.email, totp=pyotp.TOTP(admin.totp_secret).now()) == 429


def test_invite_flow(client: TestClient, admin: User, db: Session) -> None:
    login(client, admin.email)
    invite = client.post("/api/admin/invites", json={"email": "Sister@Example.com"}).json()
    token = invite["token"]
    assert invite["email"] == "sister@example.com"

    guest = TestClient(client.app)
    assert guest.get(f"/api/auth/invite/{token}").json() == {"email": "sister@example.com"}
    short = guest.post(
        "/api/auth/register", json={"token": token, "display_name": "S", "password": "short"}
    )
    assert short.status_code == 422
    created = guest.post(
        "/api/auth/register", json={"token": token, "display_name": "Sister", "password": PASSWORD}
    )
    assert created.status_code == 201 and created.json()["role"] == "member"
    assert guest.get("/api/auth/me").status_code == 200  # signed in right away

    # Single use.
    again = guest.post(
        "/api/auth/register", json={"token": token, "display_name": "X", "password": PASSWORD}
    )
    assert again.status_code == 404
    stored = db.scalar(select(Invite))
    assert stored is not None and stored.token_hash != token


def test_expired_invite_is_rejected(client: TestClient, admin: User, db: Session) -> None:
    login(client, admin.email)
    token = client.post("/api/admin/invites", json={"email": "a@example.com"}).json()["token"]
    invite = db.scalar(select(Invite))
    assert invite is not None
    invite.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert client.get(f"/api/auth/invite/{token}").status_code == 404


def test_members_cannot_use_admin_endpoints(client: TestClient, db: Session) -> None:
    make_user(db, "member@example.com")
    login(client, "member@example.com")
    assert client.post("/api/admin/invites", json={"email": "b@example.com"}).status_code == 403
    assert client.get("/api/admin/jobs").status_code == 403

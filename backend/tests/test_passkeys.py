"""Passkeys (FR-2): the ceremonies, the one-time challenges and the sign-in.

No test can hold a real authenticator, so the two functions that check the browser's answer are
replaced; the options the server hands out are the real ones from the `webauthn` package."""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import passkeys
from quant.models import Passkey, User, WebauthnChallenge

from .conftest import login, make_user

CRED = b"credential-one"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def verify(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the two checks. `state["ok"]` flips them to failing; the calls are recorded."""
    state: dict[str, Any] = {"ok": True, "cred": CRED, "reg": [], "login": [], "count": 7}

    def fake_register(credential: dict[str, Any], challenge: bytes) -> passkeys.NewCredential:
        state["reg"].append((credential, challenge))
        if not state["ok"]:
            raise passkeys.PasskeyError("InvalidRegistrationResponse")
        return passkeys.NewCredential(state["cred"], b"public-key", 0)

    def fake_login(
        credential: dict[str, Any], challenge: bytes, public_key: bytes, sign_count: int
    ) -> int:
        state["login"].append((credential, challenge, public_key, sign_count))
        if not state["ok"]:
            raise passkeys.PasskeyError("InvalidAuthenticationResponse")
        return int(state["count"])

    monkeypatch.setattr(passkeys, "verify_registration", fake_register)
    monkeypatch.setattr(passkeys, "verify_login", fake_login)
    return state


def add(client: TestClient, name: str = "Phone") -> dict[str, Any]:
    opts = client.post("/api/auth/passkey/register/options")
    assert opts.status_code == 200, opts.text
    done = client.post(
        "/api/auth/passkey/register",
        json={
            "challenge_id": opts.json()["challenge_id"],
            "credential": {"id": b64(CRED)},
            "name": name,
        },
    )
    assert done.status_code == 201, done.text
    out: dict[str, Any] = done.json()
    return out


def test_the_registration_options_ask_for_a_verified_discoverable_passkey(
    owner: TestClient, db: Session
) -> None:
    out = owner.post("/api/auth/passkey/register/options").json()
    o = out["options"]
    assert o["rp"]["id"] == "localhost" and o["rp"]["name"] == "QuantTrading"
    assert o["user"]["name"] == "owner@example.com" and o["challenge"]
    assert o["authenticatorSelection"]["userVerification"] == "required"
    assert o["authenticatorSelection"]["residentKey"] == "required"
    assert o.get("excludeCredentials", []) == []
    [row] = db.scalars(select(WebauthnChallenge)).all()
    assert str(row.id) == out["challenge_id"] and row.purpose == "register"
    assert row.expires_at > datetime.now(UTC) and len(row.challenge) >= 16


def test_adding_a_passkey_stores_the_public_key_and_lists_it(
    owner: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    made = add(owner, "My phone")
    assert made["name"] == "My phone" and made["last_used_at"] is None
    [row] = db.scalars(select(Passkey)).all()
    assert row.credential_id == CRED and row.public_key == b"public-key" and row.sign_count == 0
    assert [p["name"] for p in owner.get("/api/auth/passkeys").json()] == ["My phone"]
    credential, challenge = verify["reg"][0]
    assert credential == {"id": b64(CRED)} and len(challenge) >= 16
    # The challenge was used up: the same answer cannot be sent twice.
    assert db.scalars(select(WebauthnChallenge)).all() == []


def test_a_second_ceremony_excludes_the_passkeys_already_held(
    owner: TestClient, verify: dict[str, Any]
) -> None:
    add(owner)
    out = owner.post("/api/auth/passkey/register/options").json()["options"]
    assert [c["id"] for c in out["excludeCredentials"]] == [b64(CRED)]


def test_a_failed_check_adds_nothing_and_still_uses_up_the_challenge(
    owner: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    verify["ok"] = False
    cid = owner.post("/api/auth/passkey/register/options").json()["challenge_id"]
    body = {"challenge_id": cid, "credential": {}, "name": "x"}
    bad = owner.post("/api/auth/passkey/register", json=body)
    assert bad.status_code == 400 and "could not be verified" in bad.text
    assert db.scalars(select(Passkey)).all() == []
    again = owner.post("/api/auth/passkey/register", json=body)
    assert again.status_code == 400 and "not valid" in again.text  # the challenge is gone


def test_a_challenge_expires_and_belongs_to_its_user(
    owner: TestClient, client: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    cid = owner.post("/api/auth/passkey/register/options").json()["challenge_id"]
    row = db.scalars(select(WebauthnChallenge)).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    late = owner.post("/api/auth/passkey/register", json={"challenge_id": cid, "credential": {}})
    assert late.status_code == 400 and "expired" in late.text

    mine = owner.post("/api/auth/passkey/register/options").json()["challenge_id"]
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    stolen = client.post(
        "/api/auth/passkey/register", json={"challenge_id": mine, "credential": {}}
    )
    assert stolen.status_code == 400 and verify["reg"] == []  # never even checked


def test_a_login_challenge_cannot_register_and_the_reverse(
    owner: TestClient, verify: dict[str, Any]
) -> None:
    login_id = owner.post("/api/auth/passkey/login/options").json()["challenge_id"]
    wrong = owner.post(
        "/api/auth/passkey/register", json={"challenge_id": login_id, "credential": {}}
    )
    assert wrong.status_code == 400
    reg_id = owner.post("/api/auth/passkey/register/options").json()["challenge_id"]
    wrong = owner.post("/api/auth/passkey/login", json={"challenge_id": reg_id, "credential": {}})
    assert wrong.status_code == 400


def test_the_same_credential_cannot_be_added_twice_and_ten_is_the_limit(
    owner: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    add(owner)
    cid = owner.post("/api/auth/passkey/register/options").json()["challenge_id"]
    dup = owner.post("/api/auth/passkey/register", json={"challenge_id": cid, "credential": {}})
    assert dup.status_code == 409
    for i in range(9):
        verify["cred"] = f"other-{i}".encode()
        add(owner, f"Device {i}")
    assert owner.post("/api/auth/passkey/register/options").status_code == 409


def test_a_passkey_is_removed_by_its_owner_only(
    owner: TestClient, client: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    made = add(owner)
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert client.get("/api/auth/passkeys").json() == []
    assert client.delete(f"/api/auth/passkeys/{made['id']}").status_code == 404
    client.cookies.clear()
    assert login(client, "owner@example.com") == 200
    assert client.delete(f"/api/auth/passkeys/{made['id']}").status_code == 204
    assert client.get("/api/auth/passkeys").json() == []
    assert client.delete(f"/api/auth/passkeys/{made['id']}").status_code == 404


def sign_in(client: TestClient, cred: bytes = CRED) -> Any:
    opts = client.post("/api/auth/passkey/login/options")
    assert opts.status_code == 200
    assert opts.json()["options"]["userVerification"] == "required"
    assert opts.json()["options"].get("allowCredentials", []) == []  # the device offers its own
    return client.post(
        "/api/auth/passkey/login",
        json={
            "challenge_id": opts.json()["challenge_id"],
            "credential": {"id": b64(cred), "rawId": b64(cred)},
        },
    )


def test_a_passkey_signs_the_user_in_without_a_password(
    owner: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    add(owner)
    owner.post("/api/auth/logout")
    owner.cookies.clear()
    assert owner.get("/api/auth/me").status_code == 401
    answer = sign_in(owner)
    assert answer.status_code == 200, answer.text
    assert answer.json()["email"] == "owner@example.com"
    assert owner.get("/api/auth/me").status_code == 200  # the session cookie works
    _, challenge, public_key, before = verify["login"][0]
    assert public_key == b"public-key" and before == 0 and len(challenge) >= 16
    db.expire_all()
    row = db.scalars(select(Passkey)).one()
    assert row.sign_count == 7 and row.last_used_at is not None
    user = db.scalars(select(User)).one()
    assert user.last_login_at is not None


def test_a_passkey_needs_no_totp_code_and_clears_failed_attempts(
    owner: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    add(owner)
    user = db.scalars(select(User)).one()
    user.totp_enabled, user.totp_secret, user.failed_logins = True, "JBSWY3DPEHPK3PXP", 3
    db.commit()
    owner.cookies.clear()
    assert sign_in(owner).status_code == 200
    db.expire_all()
    assert db.scalars(select(User)).one().failed_logins == 0


def test_a_bad_unknown_or_replayed_sign_in_is_refused(
    owner: TestClient, client: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    add(owner)
    owner.cookies.clear()
    assert sign_in(owner, b"never-registered").status_code == 401
    verify["ok"] = False
    refused = sign_in(owner)
    assert refused.status_code == 401 and refused.json()["detail"] == "invalid credentials"
    assert owner.get("/api/auth/me").status_code == 401
    verify["ok"] = True
    opts = owner.post("/api/auth/passkey/login/options").json()
    body = {"challenge_id": opts["challenge_id"], "credential": {"rawId": b64(CRED)}}
    assert owner.post("/api/auth/passkey/login", json=body).status_code == 200
    owner.cookies.clear()
    assert owner.post("/api/auth/passkey/login", json=body).status_code == 400  # a replay
    junk = {"challenge_id": opts["challenge_id"], "credential": {}}
    assert owner.post("/api/auth/passkey/login", json=junk).status_code == 400


def test_a_locked_account_cannot_use_a_passkey(
    owner: TestClient, db: Session, verify: dict[str, Any]
) -> None:
    add(owner)
    user = db.scalars(select(User)).one()
    user.locked_until = datetime.now(UTC) + timedelta(minutes=10)
    db.commit()
    owner.cookies.clear()
    assert sign_in(owner).status_code == 429
    assert verify["login"] == []  # refused before the check


def test_the_password_sign_in_still_works_beside_a_passkey(
    owner: TestClient, client: TestClient, verify: dict[str, Any]
) -> None:
    add(owner)
    client.cookies.clear()
    assert login(client, "owner@example.com") == 200


def test_the_settings_name_the_address_the_browser_must_match() -> None:
    from quant.config import Settings

    s = Settings()
    assert s.webauthn_rp_id == "localhost" and s.webauthn_origin.startswith("http")
    assert s.webauthn_rp_name == "QuantTrading"

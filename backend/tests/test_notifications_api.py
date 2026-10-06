import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant import rls
from quant.models import AlertSettings, Notification, User

from .conftest import login, make_user


def add(
    db: Session, user: User, n: int, *, read: bool = False, kind: str = "daily_move"
) -> Notification:
    rls.bypass(db)
    row = Notification(
        user_id=user.id, kind=kind, severity="info", title=f"Title {n}", body=f"Body {n}",
        isin="US0000000001", dedupe_key=f"k{n}",
        created_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=n),
        read_at=datetime(2026, 10, 2, tzinfo=UTC) if read else None,
    )  # fmt: skip
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    return client


def test_the_centre_lists_the_newest_first_with_the_unread_count(
    owner: TestClient, db: Session, admin: User
) -> None:
    for n in range(1, 4):
        add(db, admin, n, read=(n == 1))
    body = owner.get("/api/notifications").json()
    assert [i["title"] for i in body["items"]] == ["Title 3", "Title 2", "Title 1"]
    assert body["unread"] == 2
    assert [i["read"] for i in body["items"]] == [False, False, True]
    first = body["items"][0]
    assert set(first) == {"id", "kind", "severity", "title", "body", "isin", "created_at", "read"}


def test_a_limit_cuts_the_list_but_not_the_unread_count(
    owner: TestClient, db: Session, admin: User
) -> None:
    for n in range(1, 6):
        add(db, admin, n)
    body = owner.get("/api/notifications?limit=2").json()
    assert len(body["items"]) == 2 and body["unread"] == 5
    only = owner.get("/api/notifications?unread_only=true&limit=100").json()
    assert len(only["items"]) == 5
    for bad in ("0", "101", "x"):
        assert owner.get(f"/api/notifications?limit={bad}").status_code == 422


def test_one_alert_can_be_marked_read(owner: TestClient, db: Session, admin: User) -> None:
    first, second = add(db, admin, 1), add(db, admin, 2)
    body = owner.post(f"/api/notifications/{first.id}/read").json()
    assert body["unread"] == 1
    assert {i["title"]: i["read"] for i in body["items"]} == {"Title 1": True, "Title 2": False}
    assert (
        owner.post(f"/api/notifications/{first.id}/read").json()["unread"] == 1
    )  # again: no change
    assert second.read_at is None


def test_all_alerts_can_be_marked_read_at_once(owner: TestClient, db: Session, admin: User) -> None:
    for n in range(1, 4):
        add(db, admin, n)
    body = owner.post("/api/notifications/read-all").json()
    assert body["unread"] == 0 and all(i["read"] for i in body["items"])


def test_users_never_see_or_change_each_others_alerts(
    client: TestClient, db: Session, admin: User
) -> None:
    other = make_user(db, "member@example.com")
    mine = add(db, admin, 1)
    theirs = add(db, other, 2)
    login(client, admin.email)
    body = client.get("/api/notifications").json()
    assert [i["title"] for i in body["items"]] == ["Title 1"] and body["unread"] == 1
    assert client.post(f"/api/notifications/{theirs.id}/read").status_code == 404
    assert client.post(f"/api/notifications/{uuid.uuid4()}/read").status_code == 404
    client.post("/api/notifications/read-all")
    rls.bypass(db)
    db.expire_all()
    assert db.get(Notification, theirs.id).read_at is None  # type: ignore[union-attr]
    assert db.get(Notification, mine.id).read_at is not None  # type: ignore[union-attr]


def test_notifications_need_a_login(client: TestClient) -> None:
    for method, path in [
        ("get", "/api/notifications"),
        ("post", "/api/notifications/read-all"),
        ("post", f"/api/notifications/{uuid.uuid4()}/read"),
        ("get", "/api/alerts/settings"),
        ("put", "/api/alerts/settings"),
    ]:
        assert getattr(client, method)(path).status_code == 401


# --- settings ------------------------------------------------------------------------------

GOOD = {
    "daily_moves_enabled": True,
    "move_stock_pct": "7.5",
    "move_fund_pct": 2,
    "move_crypto_pct": "12.25",
    "quiet_start": "22:00",
    "quiet_end": "07:30",
}


def test_without_a_saved_row_the_settings_are_the_defaults_of_the_prd(owner: TestClient) -> None:
    assert owner.get("/api/alerts/settings").json() == {
        "daily_moves_enabled": True,
        "move_stock_pct": "5",
        "move_fund_pct": "3",
        "move_crypto_pct": "10",
        "quiet_start": None,
        "quiet_end": None,
    }


def test_settings_are_saved_and_read_back(owner: TestClient) -> None:
    saved = owner.put("/api/alerts/settings", json=GOOD).json()
    assert saved == {
        "daily_moves_enabled": True,
        "move_stock_pct": "7.5",
        "move_fund_pct": "2",
        "move_crypto_pct": "12.25",
        "quiet_start": "22:00",
        "quiet_end": "07:30",
    }
    assert owner.get("/api/alerts/settings").json() == saved


def test_saving_twice_updates_the_same_row(owner: TestClient, db: Session, admin: User) -> None:
    owner.put("/api/alerts/settings", json=GOOD)
    off = {**GOOD, "daily_moves_enabled": False, "quiet_start": None, "quiet_end": None}
    again = owner.put("/api/alerts/settings", json=off).json()
    assert again["daily_moves_enabled"] is False and again["quiet_start"] is None
    rls.bypass(db)
    assert db.query(AlertSettings).count() == 1


@pytest.mark.parametrize(
    "change",
    [
        {"move_stock_pct": 0},
        {"move_stock_pct": "-1"},
        {"move_fund_pct": 100.01},
        {"move_crypto_pct": "1.234"},
        {"move_crypto_pct": "abc"},
        {"daily_moves_enabled": "maybe"},
        {"quiet_start": "22:00", "quiet_end": None},
        {"quiet_start": None, "quiet_end": "07:00"},
        {"quiet_start": "7:00", "quiet_end": "08:00"},
        {"quiet_start": "24:00", "quiet_end": "08:00"},
        {"quiet_start": "22:00:30", "quiet_end": "08:00"},
        {"quiet_start": "22:00:00:00", "quiet_end": "08:00"},
        {"quiet_start": "22:00+01", "quiet_end": "08:00"},
        {"quiet_start": "22:00", "quiet_end": "08:00Z"},
        {"quiet_start": "late", "quiet_end": "08:00"},
    ],
)
def test_bad_settings_are_refused_and_change_nothing(owner: TestClient, change: dict) -> None:  # type: ignore[type-arg]
    owner.put("/api/alerts/settings", json=GOOD)
    assert owner.put("/api/alerts/settings", json={**GOOD, **change}).status_code == 422
    assert owner.get("/api/alerts/settings").json()["move_stock_pct"] == "7.5"


def test_a_limit_of_one_hundred_percent_and_midnight_are_allowed(owner: TestClient) -> None:
    ok = {**GOOD, "move_stock_pct": 100, "quiet_start": "00:00", "quiet_end": "06:00"}
    body = owner.put("/api/alerts/settings", json=ok).json()
    assert body["move_stock_pct"] == "100" and body["quiet_start"] == "00:00"


def test_settings_belong_to_one_user(client: TestClient, db: Session, admin: User) -> None:
    make_user(db, "member@example.com")
    login(client, admin.email)
    client.put("/api/alerts/settings", json=GOOD)
    client.post("/api/auth/logout")
    login(client, "member@example.com")
    assert client.get("/api/alerts/settings").json()["move_stock_pct"] == "5"  # their own default


def test_a_cleared_time_means_no_time_but_one_time_alone_is_refused(owner: TestClient) -> None:
    both_blank = owner.put(
        "/api/alerts/settings", json={**GOOD, "quiet_start": "", "quiet_end": " "}
    )
    assert both_blank.status_code == 200 and both_blank.json()["quiet_start"] is None
    one = owner.put("/api/alerts/settings", json={**GOOD, "quiet_start": "22:00", "quiet_end": ""})
    assert one.status_code == 422


def test_a_new_users_settings_equal_the_defaults_the_job_uses(owner: TestClient) -> None:
    from quant.ingest.alerts import DEFAULTS

    shown = owner.get("/api/alerts/settings").json()
    assert (shown["move_stock_pct"], shown["move_fund_pct"], shown["move_crypto_pct"]) == (
        str(DEFAULTS.move_stock_pct),
        str(DEFAULTS.move_fund_pct),
        str(DEFAULTS.move_crypto_pct),
    )


def test_a_browser_that_sends_seconds_is_understood(owner: TestClient) -> None:
    body = owner.put(
        "/api/alerts/settings", json={**GOOD, "quiet_start": "22:00:00", "quiet_end": "07:00:00"}
    ).json()
    assert (body["quiet_start"], body["quiet_end"]) == ("22:00", "07:00")


def test_older_alerts_can_be_reached_with_an_offset(
    owner: TestClient, db: Session, admin: User
) -> None:
    for n in range(1, 8):
        add(db, admin, n)
    first = owner.get("/api/notifications?limit=3").json()["items"]
    second = owner.get("/api/notifications?limit=3&offset=3").json()["items"]
    third = owner.get("/api/notifications?limit=3&offset=6").json()["items"]
    titles = [i["title"] for i in first + second + third]
    assert titles == [f"Title {n}" for n in range(7, 0, -1)]  # newest first, no gaps or repeats
    assert owner.get("/api/notifications?offset=-1").status_code == 422


def test_read_all_up_to_a_moment_leaves_newer_alerts_unread(
    owner: TestClient, db: Session, admin: User
) -> None:
    rows = [add(db, admin, n) for n in range(1, 5)]  # created one hour apart
    seen = rows[1].created_at  # the page showed alerts 1 and 2
    body = owner.post("/api/notifications/read-all", json={"up_to": seen.isoformat()}).json()
    assert {i["title"]: i["read"] for i in body["items"]} == {
        "Title 4": False,
        "Title 3": False,
        "Title 2": True,
        "Title 1": True,
    }
    assert body["unread"] == 2
    assert owner.post("/api/notifications/read-all").json()["unread"] == 0  # no body: all
    assert owner.post("/api/notifications/read-all", json={"up_to": "yesterday"}).status_code == 422

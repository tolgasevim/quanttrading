"""Email digests, Telegram alerts and quiet hours (FR-71, FR-72, FR-73).

Both channels are replaced by fakes: no test sends a real mail or message. The two small
transport classes are tested on their own against a fake SMTP class and a mock HTTP transport."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.api import delivery as delivery_api
from quant.config import Settings
from quant.delivery import service
from quant.delivery.transports import (
    DeliveryError,
    HttpTelegram,
    SmtpSender,
    Update,
    make_email_sender,
    make_telegram,
)
from quant.main import app
from quant.models import (
    AiCommentary,
    AlertSettings,
    DeliverySettings,
    Notification,
    TelegramState,
    User,
)
from quant.security import hash_token

from .conftest import login, make_user

TZ = "Europe/Berlin"
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)  # a Monday, 10:00 in Berlin


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    def send(self, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise DeliveryError("the mail was not sent (OSError)")
        self.sent.append((to, subject, body))


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.inbox: list[Update] = []
        self.offsets: list[int] = []
        self.fail = False

    def send(self, chat_id: int, text: str) -> None:
        if self.fail:
            raise DeliveryError("Telegram did not answer (ConnectError)")
        self.sent.append((chat_id, text))

    def updates(self, offset: int) -> list[Update]:
        self.offsets.append(offset)
        return [u for u in self.inbox if u.update_id >= offset]


@pytest.fixture
def channels() -> Iterator[tuple[FakeEmail, FakeTelegram]]:
    email, tg = FakeEmail(), FakeTelegram()
    app.dependency_overrides[delivery_api.email_sender] = lambda: email
    app.dependency_overrides[delivery_api.telegram_api] = lambda: tg
    yield email, tg
    app.dependency_overrides.pop(delivery_api.email_sender, None)
    app.dependency_overrides.pop(delivery_api.telegram_api, None)


def note(
    db: Session, user: User, title: str, created: datetime, body: str = "It moved.", key: str = ""
) -> Notification:
    n = Notification(
        user_id=user.id,
        kind="daily_move",
        severity="info",
        title=title,
        body=body,
        dedupe_key=key or title,
        created_at=created,
    )
    db.add(n)
    db.commit()
    return n


def link(db: Session, user: User, chat: int = 111, at: datetime = NOW - timedelta(days=1)) -> None:
    db.add(DeliverySettings(user_id=user.id, telegram_chat_id=chat, telegram_linked_at=at))
    db.commit()


def quiet(db: Session, user: User, start: time, end: time) -> None:
    db.add(AlertSettings(user_id=user.id, quiet_start=start, quiet_end=end))
    db.commit()


# --- pushing alerts to Telegram ---------------------------------------------------------------


def test_a_new_alert_is_pushed_once_and_marked(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    link(db, user, 111)
    n = note(db, user, "Apple +6.0%", NOW - timedelta(hours=2), "Apple closed higher.")
    tg = FakeTelegram()
    first = service.push_alerts(db, tg, NOW, TZ)
    assert first.rows_written == 1 and first.errors == {}
    assert tg.sent == [(111, "Apple +6.0%\nApple closed higher.")]
    db.refresh(n)
    assert n.pushed_at is not None
    assert service.push_alerts(db, tg, NOW, TZ).rows_written == 0 and len(tg.sent) == 1


def test_only_alerts_after_linking_and_within_two_days_are_pushed(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    link(db, user, 111, at=NOW - timedelta(hours=5))
    note(db, user, "before linking", NOW - timedelta(hours=6))
    note(db, user, "after linking", NOW - timedelta(hours=1))
    stale_user = make_user(db, "b@example.com")
    link(db, stale_user, 222, at=NOW - timedelta(days=30))
    note(db, stale_user, "stale news", NOW - timedelta(days=3))
    tg = FakeTelegram()
    service.push_alerts(db, tg, NOW, TZ)
    assert tg.sent == [(111, "after linking\nIt moved.")]


def test_a_user_without_a_linked_chat_gets_nothing_and_no_bot_means_no_push(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    note(db, user, "Apple +6.0%", NOW - timedelta(hours=1))
    db.add(DeliverySettings(user_id=user.id, email_mode="weekly"))
    db.commit()
    tg = FakeTelegram()
    assert service.push_alerts(db, tg, NOW, TZ).attempted == 0 and tg.sent == []
    link_user = make_user(db, "b@example.com")
    link(db, link_user)
    note(db, link_user, "x", NOW - timedelta(hours=1))
    assert service.push_alerts(db, None, NOW, TZ).rows_written == 0  # the bot is not set up


def test_quiet_hours_hold_an_alert_until_they_end(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    link(db, user, 111, at=NOW - timedelta(days=1))
    quiet(db, user, time(22, 0), time(7, 0))  # across midnight, Berlin time
    note(db, user, "late move", NOW + timedelta(hours=12, minutes=30))  # 21:30 UTC, 22:30 Berlin
    tg = FakeTelegram()
    at_night = NOW + timedelta(hours=12, minutes=45)
    assert service.push_alerts(db, tg, at_night, TZ).attempted == 0 and tg.sent == []
    at_morning = datetime(2026, 11, 24, 6, 30, tzinfo=UTC)  # 07:30 Berlin: the window is over
    out = service.push_alerts(db, tg, at_morning, TZ)
    assert out.rows_written == 1 and tg.sent == [(111, "late move\nIt moved.")]


def test_quiet_hours_use_berlin_time_in_summer_too(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    link(db, user, 111, at=datetime(2026, 7, 1, tzinfo=UTC))
    quiet(db, user, time(22, 0), time(7, 0))
    note(db, user, "summer", datetime(2026, 7, 10, 19, 30, tzinfo=UTC))  # 21:30 UTC is 23:30 CEST
    tg = FakeTelegram()
    assert service.push_alerts(db, tg, datetime(2026, 7, 10, 20, 0, tzinfo=UTC), TZ).attempted == 0
    assert (
        service.push_alerts(db, tg, datetime(2026, 7, 11, 5, 30, tzinfo=UTC), TZ).rows_written == 1
    )  # 07:30 CEST


def test_a_flood_is_sent_as_ten_and_one_summary(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    link(db, user, 111)
    for i in range(13):
        note(db, user, f"alert {i:02d}", NOW - timedelta(hours=3, minutes=-i), key=f"k{i}")
    tg = FakeTelegram()
    service.push_alerts(db, tg, NOW, TZ)
    assert len(tg.sent) == 11 and tg.sent[0][1].startswith("alert 00")
    assert "and 3 more alerts" in tg.sent[-1][1]
    assert all(n.pushed_at is not None for n in db.scalars(select(Notification)))


def test_a_failed_send_is_retried_in_the_next_run(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    link(db, user, 111)
    n = note(db, user, "Apple +6.0%", NOW - timedelta(hours=1))
    tg = FakeTelegram()
    tg.fail = True
    out = service.push_alerts(db, tg, NOW, TZ)
    assert str(user.id) in out.errors and out.rows_written == 0
    db.refresh(n)
    assert n.pushed_at is None
    tg.fail = False
    assert service.push_alerts(db, tg, NOW + timedelta(minutes=15), TZ).rows_written == 1


# --- linking a chat -----------------------------------------------------------------------------


def test_the_link_code_is_kept_only_as_a_hash(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    code, expires = service.start_link(db, user.id, NOW)
    row = db.get(DeliverySettings, user.id)
    assert row is not None and row.link_code_hash == hash_token(code)
    assert len(code) == service.CODE_LENGTH and set(code) <= set(service.CODE_ALPHABET)
    assert expires == NOW + timedelta(minutes=service.LINK_MINUTES)
    assert code not in repr(row.__dict__)


def test_a_chat_that_sends_the_code_is_linked_and_told(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    code, _ = service.start_link(db, user.id, NOW)
    tg = FakeTelegram()
    tg.inbox = [Update(7, 555, True, f"/start {code.lower()}")]
    assert service.process_updates(db, tg, NOW + timedelta(minutes=1)) == 1
    row = db.get(DeliverySettings, user.id)
    assert row is not None and row.telegram_chat_id == 555 and row.link_code_hash is None
    assert row.telegram_linked_at is not None
    assert tg.sent[0][0] == 555 and tg.sent[0][1].startswith("Linked.")
    assert "places no trades" in tg.sent[0][1]
    state = db.get(TelegramState, 1)
    assert state is not None and state.next_offset == 8


def test_updates_are_not_read_twice(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    code, _ = service.start_link(db, user.id, NOW)
    tg = FakeTelegram()
    tg.inbox = [Update(7, 555, True, code)]
    service.process_updates(db, tg, NOW)
    sent = len(tg.sent)
    assert service.process_updates(db, tg, NOW) == 0
    assert tg.offsets == [0, 8] and len(tg.sent) == sent


@pytest.mark.parametrize(
    ("text", "private", "words"),
    [
        ("hello", True, "not valid"),
        ("/start", True, "one-time code"),
        ("ABCDEFGH", True, "not valid"),  # well formed, but nobody's code
    ],
)
def test_other_messages_get_a_short_answer(
    db: Session, text: str, private: bool, words: str
) -> None:
    rls.bypass(db)
    tg = FakeTelegram()
    tg.inbox = [Update(1, 9, private, text)]
    assert service.process_updates(db, tg, NOW) == 0
    assert words in tg.sent[0][1]


def test_a_group_chat_is_ignored_and_an_expired_code_does_not_link(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    code, _ = service.start_link(db, user.id, NOW)
    tg = FakeTelegram()
    tg.inbox = [Update(1, -100, False, code)]  # a group
    assert service.process_updates(db, tg, NOW) == 0 and tg.sent == []
    tg.inbox = [Update(2, 555, True, code)]
    late = NOW + timedelta(minutes=service.LINK_MINUTES + 1)
    assert service.process_updates(db, tg, late) == 0
    assert "not valid" in tg.sent[0][1]
    row = db.get(DeliverySettings, user.id)
    assert row is not None and row.telegram_chat_id is None


def test_one_chat_cannot_be_linked_to_two_accounts(db: Session) -> None:
    rls.bypass(db)
    first, second = make_user(db, "a@example.com"), make_user(db, "b@example.com")
    link(db, first, chat=555)
    code, _ = service.start_link(db, second.id, NOW)
    tg = FakeTelegram()
    tg.inbox = [Update(1, 555, True, code)]
    assert service.process_updates(db, tg, NOW) == 0
    assert "already linked" in tg.sent[0][1]
    row = db.get(DeliverySettings, second.id)
    assert row is not None and row.telegram_chat_id is None


def test_a_failed_reply_does_not_undo_the_link(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    code, _ = service.start_link(db, user.id, NOW)
    tg = FakeTelegram()
    tg.fail = True
    tg.inbox = [Update(1, 555, True, code)]
    assert service.process_updates(db, tg, NOW) == 1
    row = db.get(DeliverySettings, user.id)
    assert row is not None and row.telegram_chat_id == 555


def test_unlinking_clears_the_chat_and_any_code(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    link(db, user, 555)
    service.start_link(db, user.id, NOW)
    service.unlink(db, user.id)
    row = db.get(DeliverySettings, user.id)
    assert row is not None
    assert (row.telegram_chat_id, row.telegram_linked_at, row.link_code_hash) == (None, None, None)


# --- the email digest -------------------------------------------------------------------------------


def settings_row(db: Session, user: User, mode: str, to: str | None = None) -> None:
    db.add(DeliverySettings(user_id=user.id, email_mode=mode, email_to=to))
    db.commit()


def test_a_weekly_digest_goes_out_on_monday_once(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    settings_row(db, user, "weekly")
    note(db, user, "Apple +6.0%", NOW - timedelta(days=2), "Apple closed higher.")
    note(db, user, "too old", NOW - timedelta(days=9))
    email = FakeEmail()
    out = service.send_digests(db, email, NOW, TZ)
    assert out.rows_written == 1 and out.errors == {}
    [(to, subject, body)] = email.sent
    assert to == "a@example.com" and subject == "QuantTrading weekly digest, 2026-11-23"
    assert "Alerts (1)" in body and "- Apple +6.0%: Apple closed higher." in body
    assert "too old" not in body and "places no trades" in body and "not investment advice" in body
    assert service.send_digests(db, email, NOW + timedelta(hours=2), TZ).attempted == 0
    assert len(email.sent) == 1


def test_a_weekly_digest_waits_for_monday_and_a_daily_one_does_not(db: Session) -> None:
    rls.bypass(db)
    weekly, daily = make_user(db, "w@example.com"), make_user(db, "d@example.com")
    settings_row(db, weekly, "weekly")
    settings_row(db, daily, "daily")
    tuesday = NOW + timedelta(days=1)
    note(db, weekly, "w", tuesday - timedelta(hours=3), key="w")
    note(db, daily, "d", tuesday - timedelta(hours=3), key="d")
    note(db, daily, "d-old", tuesday - timedelta(days=2), key="d2")  # outside the day's window
    email = FakeEmail()
    service.send_digests(db, email, tuesday, TZ)
    assert [to for to, _, _ in email.sent] == ["d@example.com"]
    assert "daily digest" in email.sent[0][1] and "d-old" not in email.sent[0][2]


def test_the_weekly_digest_carries_the_commentary_and_the_position_prompts(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    settings_row(db, user, "weekly")
    db.add(
        AiCommentary(
            user_id=user.id, week_start=date(2026, 11, 16), kind="template",
            text="Template summary.", created_at=NOW - timedelta(days=1),
        )
    )  # fmt: skip
    db.add(
        AiCommentary(
            user_id=user.id, week_start=date(2026, 11, 16), kind="ai",
            text="The AI wrote this.", created_at=NOW - timedelta(days=1),
        )
    )  # fmt: skip
    db.commit()
    email = FakeEmail()
    service.send_digests(db, email, NOW, TZ, lambda s, u, t: ["Merge A into B: same company."])
    body = email.sent[0][2]
    assert "Weekly commentary (written by the AI)" in body and "The AI wrote this." in body
    assert "Template summary." not in body  # one commentary, the AI one first
    assert "Positions to look at (1)" in body and "- Merge A into B: same company." in body


def test_a_digest_with_nothing_in_it_is_not_sent_but_counts_for_the_day(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    settings_row(db, user, "weekly")
    email = FakeEmail()
    out = service.send_digests(db, email, NOW, TZ)
    assert email.sent == [] and out.attempted == 1 and out.rows_written == 0
    row = db.get(DeliverySettings, user.id)
    assert row is not None and row.digest_sent_on == date(2026, 11, 23)


def test_quiet_hours_hold_the_digest_until_the_window_ends(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    settings_row(db, user, "daily")
    quiet(db, user, time(8, 0), time(12, 0))
    note(db, user, "x", NOW - timedelta(hours=2))
    email = FakeEmail()
    assert service.send_digests(db, email, NOW, TZ).attempted == 0 and email.sent == []  # 10:00
    after = NOW + timedelta(hours=3)  # 13:00 Berlin
    assert service.send_digests(db, email, after, TZ).rows_written == 1


def test_the_digest_goes_to_the_chosen_address_and_a_failure_is_retried(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    settings_row(db, user, "weekly", to="family@example.com")
    note(db, user, "x", NOW - timedelta(hours=2))
    email = FakeEmail()
    email.fail = True
    out = service.send_digests(db, email, NOW, TZ)
    assert str(user.id) in out.errors
    row = db.get(DeliverySettings, user.id)
    assert row is not None and row.digest_sent_on is None
    email.fail = False
    service.send_digests(db, email, NOW + timedelta(hours=1), TZ)
    assert email.sent[0][0] == "family@example.com"


def test_off_and_no_mail_server_send_nothing(db: Session) -> None:
    rls.bypass(db)
    user = make_user(db, "a@example.com")
    settings_row(db, user, "off")
    note(db, user, "x", NOW - timedelta(hours=1))
    email = FakeEmail()
    assert service.send_digests(db, email, NOW, TZ).attempted == 0
    assert service.send_digests(db, None, NOW, TZ).attempted == 0


# --- the API ----------------------------------------------------------------------------------------------


def test_the_settings_start_with_everything_off(owner: TestClient) -> None:
    out = owner.get("/api/delivery").json()
    assert out["email_mode"] == "off" and out["telegram_linked"] is False
    assert out["email_available"] is False and out["telegram_available"] is False  # not set up
    assert out["account_email"] == "owner@example.com" and "Telegram sees" in out["privacy"]


def test_a_digest_needs_a_mail_server_and_the_choice_is_stored(
    owner: TestClient, channels: tuple[FakeEmail, FakeTelegram]
) -> None:
    out = owner.put("/api/delivery/email", json={"mode": "weekly", "to": "Family@Example.com"})
    assert out.status_code == 200, out.text
    assert out.json()["email_mode"] == "weekly" and out.json()["email_to"] == "family@example.com"
    assert owner.get("/api/delivery").json()["email_available"] is True
    assert owner.put("/api/delivery/email", json={"mode": "hourly"}).status_code == 422
    assert owner.put("/api/delivery/email", json={"mode": "daily", "to": "nope"}).status_code == 422
    off = owner.put("/api/delivery/email", json={"mode": "off"})
    assert off.json()["email_mode"] == "off" and off.json()["email_to"] is None


def test_without_a_mail_server_only_off_is_accepted(owner: TestClient) -> None:
    assert owner.put("/api/delivery/email", json={"mode": "off"}).status_code == 200
    refused = owner.put("/api/delivery/email", json={"mode": "weekly"})
    assert refused.status_code == 409 and "mail server" in refused.text


def test_linking_telegram_from_the_page(
    owner: TestClient, db: Session, channels: tuple[FakeEmail, FakeTelegram]
) -> None:
    _, tg = channels
    link_out = owner.post("/api/delivery/telegram/link")
    assert link_out.status_code == 200, link_out.text
    code = link_out.json()["code"]
    assert owner.get("/api/delivery").json()["link_pending"] is True
    assert owner.post("/api/delivery/telegram/check").json() == {
        "linked": False
    }  # nothing sent yet
    tg.inbox.append(Update(1, 777, True, f"/start {code}"))
    assert owner.post("/api/delivery/telegram/check").json() == {"linked": True}
    state = owner.get("/api/delivery").json()
    assert state["telegram_linked"] is True and state["link_pending"] is False
    assert owner.post("/api/delivery/test", json={"channel": "telegram"}).status_code == 204
    assert tg.sent[-1][0] == 777 and "test from QuantTrading" in tg.sent[-1][1]
    assert owner.delete("/api/delivery/telegram").status_code == 204
    assert owner.get("/api/delivery").json()["telegram_linked"] is False
    assert owner.post("/api/delivery/test", json={"channel": "telegram"}).status_code == 409


def test_the_check_reads_telegram_only_while_a_code_waits(
    owner: TestClient, channels: tuple[FakeEmail, FakeTelegram]
) -> None:
    _, tg = channels
    assert owner.post("/api/delivery/telegram/check").json() == {"linked": False}
    assert tg.offsets == []  # no code, no call to Telegram
    owner.post("/api/delivery/telegram/link")
    owner.post("/api/delivery/telegram/check")
    assert len(tg.offsets) == 1


def test_linking_needs_a_bot(owner: TestClient) -> None:
    out = owner.post("/api/delivery/telegram/link")
    assert out.status_code == 409 and "Telegram bot" in out.text


def test_the_test_mail_goes_to_the_chosen_address(
    owner: TestClient, channels: tuple[FakeEmail, FakeTelegram]
) -> None:
    email, _ = channels
    assert owner.post("/api/delivery/test", json={"channel": "email"}).status_code == 204
    assert email.sent[0][0] == "owner@example.com"
    owner.put("/api/delivery/email", json={"mode": "weekly", "to": "family@example.com"})
    owner.post("/api/delivery/test", json={"channel": "email"})
    assert email.sent[1][0] == "family@example.com"
    email.fail = True
    bad = owner.post("/api/delivery/test", json={"channel": "email"})
    assert bad.status_code == 502 and "not sent" in bad.text


def test_one_users_link_is_not_anothers(
    owner: TestClient, client: TestClient, db: Session, channels: tuple[FakeEmail, FakeTelegram]
) -> None:
    _, tg = channels
    code = owner.post("/api/delivery/telegram/link").json()["code"]
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert client.get("/api/delivery").json()["link_pending"] is False
    tg.inbox.append(Update(1, 777, True, code))
    assert client.post("/api/delivery/telegram/check").json() == {"linked": False}
    assert tg.offsets == []  # the friend has no code waiting: nothing is read for them


def test_the_delivery_routes_need_the_disclaimer(client: TestClient, db: Session) -> None:
    make_user(db, "new@example.com", accepted=False)
    assert login(client, "new@example.com") == 200
    assert client.get("/api/delivery").status_code == 403


def test_the_delivery_table_is_row_level_secured() -> None:
    assert "delivery_settings" in rls.USER_TABLES


# --- the transports ---------------------------------------------------------------------------------------


class FakeSMTP:
    calls: list[tuple[str, Any]] = []
    fail = False

    def __init__(self, host: str, port: int, timeout: float) -> None:
        if FakeSMTP.fail:
            raise OSError("secret-host refused")
        FakeSMTP.calls.append(("connect", (host, port)))

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self) -> None:
        FakeSMTP.calls.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        FakeSMTP.calls.append(("login", (user, password)))

    def send_message(self, message: Any) -> None:
        FakeSMTP.calls.append(("send", (message["To"], message["Subject"], message.get_content())))


def test_the_smtp_sender_connects_secures_logs_in_and_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quant.delivery.transports.smtplib.SMTP", FakeSMTP)
    FakeSMTP.calls, FakeSMTP.fail = [], False
    settings = Settings(
        smtp_host="mail.example.com", smtp_user="u", smtp_password="p", smtp_from="q@x.org"
    )
    sender = make_email_sender(settings)
    assert isinstance(sender, SmtpSender)
    sender.send("a@example.com", "Hi", "Body text")
    kinds = [k for k, _ in FakeSMTP.calls]
    assert kinds == ["connect", "starttls", "login", "send"]
    assert FakeSMTP.calls[0][1] == ("mail.example.com", 587)
    assert (
        FakeSMTP.calls[-1][1][:2] == ("a@example.com", "Hi")
        and "Body text" in FakeSMTP.calls[-1][1][2]
    )


def test_a_mail_failure_names_the_kind_of_error_not_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quant.delivery.transports.smtplib.SMTP", FakeSMTP)
    FakeSMTP.fail = True
    sender = SmtpSender(Settings(smtp_host="mail.example.com"))
    with pytest.raises(DeliveryError) as err:
        sender.send("a@example.com", "Hi", "x")
    assert "OSError" in str(err.value) and "secret-host" not in str(err.value)
    FakeSMTP.fail = False


def test_a_channel_with_no_setting_is_off() -> None:
    assert make_email_sender(Settings(smtp_host=None)) is None
    assert make_telegram(Settings(telegram_bot_token=None)) is None
    with pytest.raises(DeliveryError):
        SmtpSender(Settings(smtp_host=None))


def telegram_with(handler: Any) -> HttpTelegram:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpTelegram(Settings(telegram_bot_token="SECRET-TOKEN"), client)


def test_a_telegram_message_is_posted_to_the_bot_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    telegram_with(handler).send(42, "hello")
    assert seen[0].url.path == "/botSECRET-TOKEN/sendMessage"
    assert json.loads(seen[0].read()) == {"chat_id": 42, "text": "hello"}


def test_a_long_message_is_cut_to_the_limit() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(200, json={"ok": True, "result": {}})

    telegram_with(handler).send(1, "x" * 9000)
    assert len(json.loads(bodies[0])["text"]) == 4000


def test_telegram_errors_never_hold_the_token() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False})

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom SECRET-TOKEN")

    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    for handler, words in (
        (refused, "401"),
        (broken, "ConnectError"),
        (not_json, "JSONDecodeError"),
    ):
        with pytest.raises(DeliveryError) as err:
            telegram_with(handler).send(1, "x")
        assert words in str(err.value) and "SECRET-TOKEN" not in str(err.value)


def test_updates_are_read_and_odd_ones_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 5,
                        "message": {"chat": {"id": 9, "type": "private"}, "text": "/start AB"},
                    },
                    {
                        "update_id": 6,
                        "message": {"chat": {"id": -1, "type": "group"}, "text": "hi"},
                    },
                    {"update_id": 7, "message": {"text": "no chat"}},
                    {"update_id": 8},
                ],
            },
        )

    got = telegram_with(handler).updates(5)
    assert got == [Update(5, 9, True, "/start AB"), Update(6, -1, False, "hi")]

"""Pushing alerts to Telegram, sending the email digests, and linking a Telegram chat
(FR-71, FR-72, FR-73).

The in-app list (FR-70) always keeps every alert. These are extra channels, and they obey the
user's quiet hours (FR-73): a push held back by quiet hours is not lost, it goes out in the first
run after the window ends. Nothing is sent to a user who has not set a channel up.

What leaves the server is the text of alerts and the weekly commentary, so the digest holds no
amounts of money beyond what the alert text itself says, and the settings page tells the user
which outside service sees it.
"""

import contextlib
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from quant.delivery.transports import DeliveryError, EmailSender, TelegramApi
from quant.ingest.jobs import JobResult
from quant.models import (
    AiCommentary,
    AlertSettings,
    DeliverySettings,
    Notification,
    TelegramState,
    User,
)
from quant.portfolio.alerts import in_quiet_hours
from quant.security import hash_token

MAX_PUSH_PER_RUN = 10  # a flood is summed up in one line
PUSH_WINDOW_HOURS = 48  # an alert older than this is stale news: it stays in the app only
LINK_MINUTES = 15
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to mistype
CODE_LENGTH = 8
DIGEST_ALERTS = 25
ALERT_CHARS = 220
COMMENTARY_CHARS = 3000
EMAIL_MODES = ("off", "weekly", "daily")
FOOTER = (
    "Information only, not investment advice. QuantTrading places no trades.\n"
    "Change or stop these messages in the app: Settings, Delivery."
)
LOCK_KEY = 7311  # one reader of the Telegram updates at a time

# Lists the consolidation prompts (FR-45) of a user for the weekly digest.
PromptSource = Callable[[Session, User, date], list[str]]


def local_now(now: datetime, tz: str) -> datetime:
    return now.astimezone(ZoneInfo(tz))


def _quiet(settings: AlertSettings | None, local: datetime) -> bool:
    if settings is None:
        return False
    return in_quiet_hours(
        settings.quiet_start, settings.quiet_end, local.time().replace(tzinfo=None)
    )


# --- Telegram: push -----------------------------------------------------------------------


def push_alerts(
    session: Session, telegram: TelegramApi | None, now: datetime, tz: str
) -> JobResult:
    """Send the alerts not yet pushed to every user with a linked chat who is not in quiet hours."""
    result = JobResult()
    if telegram is None:
        return result
    local = local_now(now, tz)
    quiet = {a.user_id: a for a in session.scalars(select(AlertSettings))}
    linked = session.scalars(
        select(DeliverySettings).where(DeliverySettings.telegram_chat_id.is_not(None))
    ).all()
    for ds in linked:
        if _quiet(quiet.get(ds.user_id), local) or ds.telegram_chat_id is None:
            continue
        result.attempted += 1
        floor = max(ds.telegram_linked_at or now, now - timedelta(hours=PUSH_WINDOW_HOURS))
        rows = session.scalars(
            select(Notification)
            .where(
                Notification.user_id == ds.user_id,
                Notification.pushed_at.is_(None),
                Notification.created_at >= floor,
            )
            .order_by(Notification.created_at)
        ).all()
        if not rows:
            continue
        send, rest = rows[:MAX_PUSH_PER_RUN], rows[MAX_PUSH_PER_RUN:]
        try:
            for n in send:
                telegram.send(ds.telegram_chat_id, f"{n.title}\n{n.body}")
                n.pushed_at = now
                result.rows_written += 1
                session.commit()
            if rest:
                telegram.send(
                    ds.telegram_chat_id, f"... and {len(rest)} more alerts. They are in the app."
                )
                for n in rest:
                    n.pushed_at = now
                session.commit()
        except DeliveryError as exc:
            session.rollback()
            result.errors[str(ds.user_id)] = str(exc)  # retried in the next run
    return result


# --- Telegram: linking a chat ----------------------------------------------------------------


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _settings_of(session: Session, user_id: uuid.UUID) -> DeliverySettings:
    row = session.get(DeliverySettings, user_id)
    if row is None:
        row = DeliverySettings(user_id=user_id, email_mode="off")
        session.add(row)
    return row


def start_link(session: Session, user_id: uuid.UUID, now: datetime) -> tuple[str, datetime]:
    """A one-time code for the user to send to the bot. Only its hash is kept."""
    code = new_code()
    expires = now + timedelta(minutes=LINK_MINUTES)
    row = _settings_of(session, user_id)
    row.link_code_hash = hash_token(code)
    row.link_expires_at = expires
    session.commit()
    return code, expires


def unlink(session: Session, user_id: uuid.UUID) -> None:
    row = session.get(DeliverySettings, user_id)
    if row is not None:
        row.telegram_chat_id = None
        row.telegram_linked_at = None
        row.link_code_hash = None
        row.link_expires_at = None
        session.commit()


LINKED_TEXT = (
    "Linked. QuantTrading will send your alerts here, outside your quiet hours. "
    "Information only, not investment advice: the app places no trades. "
    "To stop, unlink in the app (Settings, Delivery)."
)
BAD_CODE_TEXT = (
    "That code is not valid or has expired. Make a new one in the app (Settings, Delivery) "
    "and send it here."
)
TAKEN_TEXT = "This chat is already linked to an account. Unlink it in the app first."


def _code_of(message: str) -> str | None:
    """The code in "/start CODE" or "CODE": the last word, upper-cased, of the right length."""
    words = message.strip().split()
    if not words:
        return None
    word = words[-1].strip().upper()
    return word if re.fullmatch(rf"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}", word) else None


def process_updates(session: Session, telegram: TelegramApi, now: datetime) -> int:
    """Read what was sent to the bot and link the chats that sent a valid code. Returns how many
    were linked. Only private chats link; a group is ignored. The read position is kept, and one
    reader at a time takes the lock."""
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
    state = session.get(TelegramState, 1)
    if state is None:
        state = TelegramState(id=1, next_offset=0)
        session.add(state)
    linked = 0
    for update in telegram.updates(state.next_offset):
        state.next_offset = max(state.next_offset, update.update_id + 1)
        if not update.private:
            continue
        code = _code_of(update.text)
        reply = BAD_CODE_TEXT
        if code is not None:
            row = session.scalar(
                select(DeliverySettings).where(DeliverySettings.link_code_hash == hash_token(code))
            )
            if row is not None and row.link_expires_at is not None and row.link_expires_at > now:
                taken = session.scalar(
                    select(DeliverySettings.user_id).where(
                        DeliverySettings.telegram_chat_id == update.chat_id,
                        DeliverySettings.user_id != row.user_id,
                    )
                )
                if taken is not None:
                    reply = TAKEN_TEXT
                else:
                    row.telegram_chat_id = update.chat_id
                    row.telegram_linked_at = now
                    row.link_code_hash = None
                    row.link_expires_at = None
                    reply = LINKED_TEXT
                    linked += 1
        elif update.text.strip().lower().startswith("/start"):
            reply = "Send the one-time code from the app (Settings, Delivery) to link this chat."
        session.commit()  # the position and the link are saved before the reply goes out
        with contextlib.suppress(DeliveryError):  # the link stands; the reply is a courtesy
            telegram.send(update.chat_id, reply)
    session.commit()
    return linked


# --- the email digest -----------------------------------------------------------------------


@dataclass(frozen=True)
class Digest:
    subject: str
    body: str
    items: int  # alerts, commentary and prompts together: 0 means nothing to say


def digest_due(mode: str, today: date) -> bool:
    """A daily digest goes out every day; a weekly one on Monday morning, after the Sunday
    commentary."""
    return mode == "daily" or (mode == "weekly" and today.weekday() == 0)


def _clip(value: str, size: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= size else value[: size - 3].rstrip() + "..."


def build_digest(
    session: Session, user: User, mode: str, now: datetime, today: date, prompts: list[str]
) -> Digest:
    window = timedelta(days=7 if mode == "weekly" else 1)
    alerts = session.scalars(
        select(Notification)
        .where(Notification.user_id == user.id, Notification.created_at >= now - window)
        .order_by(Notification.created_at.desc())
    ).all()
    lines = [f"QuantTrading {mode} digest, {today.isoformat()}", ""]
    items = 0
    if alerts:
        items += len(alerts)
        lines.append(f"Alerts ({len(alerts)})")
        for n in alerts[:DIGEST_ALERTS]:
            lines.append(f"- {n.title}: {_clip(n.body, ALERT_CHARS)}")
        if len(alerts) > DIGEST_ALERTS:
            lines.append(f"... and {len(alerts) - DIGEST_ALERTS} more in the app.")
        lines.append("")
    if mode == "weekly":
        commentary = session.scalar(
            select(AiCommentary)
            .where(
                AiCommentary.user_id == user.id,
                AiCommentary.created_at >= now - timedelta(days=8),
            )
            .order_by(
                AiCommentary.kind, AiCommentary.created_at.desc()
            )  # "ai" sorts before "template"
            .limit(1)
        )
        if commentary is not None:
            items += 1
            label = (
                "Weekly commentary (written by the AI)"
                if commentary.kind == "ai"
                else "Weekly summary"
            )
            lines += [label, _clip(commentary.text, COMMENTARY_CHARS), ""]
        if prompts:
            items += len(prompts)
            lines.append(f"Positions to look at ({len(prompts)})")
            lines += [f"- {_clip(p, ALERT_CHARS)}" for p in prompts]
            lines.append("")
    lines.append(FOOTER)
    return Digest(
        subject=f"QuantTrading {mode} digest, {today.isoformat()}",
        body="\n".join(lines),
        items=items,
    )


def send_digests(
    session: Session,
    sender: EmailSender | None,
    now: datetime,
    tz: str,
    prompts_of: PromptSource | None = None,
) -> JobResult:
    """Send each due digest once a day, outside quiet hours. A digest with nothing in it is not
    sent, but counts as done for the day."""
    result = JobResult()
    if sender is None:
        return result
    local = local_now(now, tz)
    today = local.date()
    quiet = {a.user_id: a for a in session.scalars(select(AlertSettings))}
    rows = session.scalars(
        select(DeliverySettings).where(DeliverySettings.email_mode.in_(("weekly", "daily")))
    ).all()
    for ds in rows:
        if ds.digest_sent_on == today or not digest_due(ds.email_mode, today):
            continue
        if _quiet(quiet.get(ds.user_id), local):
            continue  # the next run after the quiet hours sends it
        user = session.get(User, ds.user_id)
        if user is None:
            continue
        result.attempted += 1
        try:
            prompts = (
                prompts_of(session, user, today) if prompts_of and ds.email_mode == "weekly" else []
            )
            digest = build_digest(session, user, ds.email_mode, now, today, prompts)
            if digest.items:
                sender.send(ds.email_to or user.email, digest.subject, digest.body)
                result.rows_written += 1
            ds.digest_sent_on = today
            session.commit()
        except DeliveryError as exc:
            session.rollback()
            result.errors[str(ds.user_id)] = str(exc)  # tried again in the next run
    return result


def consolidation_prompts(session: Session, user: User, today: date) -> list[str]:
    """The weekly prompts of the positions view, as plain sentences. A failure gives none: the
    digest is worth sending without them."""
    from quant.portfolio import (  # noqa: PLC0415
        allocation,
        book,
        consolidation,
        lookthrough,
        service,
    )

    try:
        held = service.build_holdings(session, user.id)
        conc = lookthrough.concentration(session, held)
        c = consolidation.analyse(
            session,
            held,
            conc,
            book.load_tags(session, user.id),
            book.load_book_settings(session, user.id),
            allocation.savings_plans(session, user.id, today),
            today,
        )
    except Exception:  # noqa: BLE001 - a broken view must not stop the digest
        session.rollback()
        return []
    out = [p.text for p in c.prompts]
    if c.held_back:
        out.append(f"{c.held_back} more are waiting in the app.")
    return out


# --- the settings, used by the API -----------------------------------------------------------


def resolve_email(session: Session, user: User) -> str:
    ds = session.get(DeliverySettings, user.id)
    return ds.email_to if ds and ds.email_to else user.email

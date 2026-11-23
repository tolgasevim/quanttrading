"""The two ways out of the app: SMTP mail and the Telegram Bot API (FR-71, FR-72).

Each is a small class behind a protocol, so the delivery logic can be tested with a fake, and a
different mail relay or bot library is a change in one place. An empty setting (no SMTP host, no
bot token) means the channel is off and the factory returns None.

A message leaves the home server here. Mail goes to the relay the owner chose; Telegram sees the
text of every alert it is asked to deliver. The settings page says so. The bot token is a secret:
it is part of the request address, so errors name only the kind of failure, never the address.
"""

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from quant.config import Settings

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4000  # the API allows 4096


class DeliveryError(Exception):
    """A message could not be sent. The text is safe to log: it never holds a secret."""


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class SmtpSender:
    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host:
            raise DeliveryError("no SMTP host is set")
        self._s = settings

    def send(self, to: str, subject: str, body: str) -> None:
        s = self._s
        message = EmailMessage()
        message["From"] = s.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(
                s.smtp_host or "", s.smtp_port, timeout=s.http_timeout_seconds
            ) as smtp:
                if s.smtp_starttls:
                    smtp.starttls()
                if s.smtp_user and s.smtp_password:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise DeliveryError(f"the mail was not sent ({type(exc).__name__})") from exc


@dataclass(frozen=True)
class Update:
    """One message sent to the bot."""

    update_id: int
    chat_id: int
    private: bool
    text: str


class TelegramApi(Protocol):
    def send(self, chat_id: int, text: str) -> None: ...

    def updates(self, offset: int) -> list[Update]: ...


class HttpTelegram:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.telegram_bot_token:
            raise DeliveryError("no Telegram bot token is set")
        self._base = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}"
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = self._client.post(f"{self._base}/{method}", json=payload)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DeliveryError(f"Telegram did not answer ({type(exc).__name__})") from exc
        if response.status_code != 200 or not body.get("ok"):
            raise DeliveryError(f"Telegram refused the request ({response.status_code})")
        return body.get("result")

    def send(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text[:TELEGRAM_MAX_CHARS]})

    def updates(self, offset: int) -> list[Update]:
        result = self._call(
            "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]}
        )
        out: list[Update] = []
        for item in result or []:
            message = item.get("message") or {}
            chat = message.get("chat") or {}
            if "update_id" not in item or "id" not in chat:
                continue
            out.append(
                Update(
                    update_id=int(item["update_id"]),
                    chat_id=int(chat["id"]),
                    private=chat.get("type") == "private",
                    text=str(message.get("text") or ""),
                )
            )
        return out


def make_email_sender(settings: Settings) -> EmailSender | None:
    return SmtpSender(settings) if settings.smtp_host else None


def make_telegram(settings: Settings) -> TelegramApi | None:
    return HttpTelegram(settings) if settings.telegram_bot_token else None

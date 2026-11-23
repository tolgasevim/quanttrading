"""Delivery outside the app: the email digest and Telegram (FR-71, FR-72).

The user chooses here; the quiet hours are on the Alerts page (FR-73). The in-app list is always
on. Linking a Telegram chat works with a one-time code the user sends to the bot: the page asks
`/telegram/check` while it waits, and that call reads the bot's messages once.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from quant import rls
from quant.api.deps import CurrentUser, UserDb
from quant.config import get_settings
from quant.db import get_sessionmaker
from quant.delivery import service
from quant.delivery.transports import (
    DeliveryError,
    EmailSender,
    TelegramApi,
    make_email_sender,
    make_telegram,
)
from quant.models import DeliverySettings

router = APIRouter(prefix="/api/delivery", tags=["delivery"])

PRIVACY = (
    "Mail goes through the mail server the owner set up. Telegram sees the text of every alert it "
    "delivers. Only alert texts and the weekly commentary are sent, never your files."
)


def email_sender() -> EmailSender | None:
    return make_email_sender(get_settings())


def telegram_api() -> TelegramApi | None:
    return make_telegram(get_settings())


class DeliveryOut(BaseModel):
    email_available: bool  # the owner set up a mail server
    email_mode: str
    email_to: str | None
    account_email: str
    telegram_available: bool  # the owner set up a bot
    telegram_linked: bool
    telegram_bot_name: str | None
    link_pending: bool
    privacy: str


class EmailIn(BaseModel):
    mode: Literal["off", "weekly", "daily"]
    to: EmailStr | None = None


class LinkOut(BaseModel):
    code: str
    expires_at: datetime
    bot_name: str | None


class CheckOut(BaseModel):
    linked: bool


class TestIn(BaseModel):
    channel: Literal["email", "telegram"]


def _out(
    db: UserDb, user: CurrentUser, email: EmailSender | None, tg: TelegramApi | None
) -> DeliveryOut:
    ds = db.scalar(select(DeliverySettings).where(DeliverySettings.user_id == user.id))
    now = datetime.now(UTC)
    return DeliveryOut(
        email_available=email is not None,
        email_mode=ds.email_mode if ds else "off",
        email_to=ds.email_to if ds else None,
        account_email=user.email,
        telegram_available=tg is not None,
        telegram_linked=bool(ds and ds.telegram_chat_id is not None),
        telegram_bot_name=get_settings().telegram_bot_name,
        link_pending=bool(
            ds and ds.link_expires_at and ds.link_expires_at > now and ds.link_code_hash
        ),
        privacy=PRIVACY,
    )


@router.get("", response_model=DeliveryOut)
def get_delivery(
    user: CurrentUser,
    db: UserDb,
    email: EmailSender | None = Depends(email_sender),
    tg: TelegramApi | None = Depends(telegram_api),
) -> DeliveryOut:
    return _out(db, user, email, tg)


@router.put("/email", response_model=DeliveryOut)
def put_email(
    body: EmailIn,
    user: CurrentUser,
    db: UserDb,
    email: EmailSender | None = Depends(email_sender),
    tg: TelegramApi | None = Depends(telegram_api),
) -> DeliveryOut:
    if body.mode != "off" and email is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the owner has not set up a mail server")
    row = service._settings_of(db, user.id)
    row.email_mode = body.mode
    row.email_to = str(body.to).lower() if body.to else None
    db.commit()
    return _out(db, user, email, tg)


@router.post("/telegram/link", response_model=LinkOut)
def link_telegram(
    user: CurrentUser, db: UserDb, tg: TelegramApi | None = Depends(telegram_api)
) -> LinkOut:
    if tg is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the owner has not set up a Telegram bot")
    code, expires = service.start_link(db, user.id, datetime.now(UTC))
    return LinkOut(code=code, expires_at=expires, bot_name=get_settings().telegram_bot_name)


@router.post("/telegram/check", response_model=CheckOut)
def check_telegram(
    user: CurrentUser, db: UserDb, tg: TelegramApi | None = Depends(telegram_api)
) -> CheckOut:
    """Read the bot's messages once, and say whether this user's chat is linked now. It only
    reads while this user has a code waiting, so it cannot be used to poll Telegram for nothing."""
    ds = db.scalar(select(DeliverySettings).where(DeliverySettings.user_id == user.id))
    if ds is None:
        return CheckOut(linked=False)
    if ds.telegram_chat_id is not None:
        return CheckOut(linked=True)
    now = datetime.now(UTC)
    if tg is None or not ds.link_code_hash or not ds.link_expires_at or ds.link_expires_at <= now:
        return CheckOut(linked=False)
    db.commit()  # give the connection back before the other session works
    try:
        with get_sessionmaker()() as other:
            rls.bypass(other)  # the message may link anyone's code: one reader reads for all
            service.process_updates(other, tg, now)
    except DeliveryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    db.expire_all()
    fresh = db.scalar(select(DeliverySettings).where(DeliverySettings.user_id == user.id))
    return CheckOut(linked=bool(fresh and fresh.telegram_chat_id is not None))


@router.delete("/telegram", status_code=status.HTTP_204_NO_CONTENT)
def unlink_telegram(user: CurrentUser, db: UserDb) -> Response:
    service.unlink(db, user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/test", status_code=status.HTTP_204_NO_CONTENT)
def send_test(
    body: TestIn,
    user: CurrentUser,
    db: UserDb,
    email: EmailSender | None = Depends(email_sender),
    tg: TelegramApi | None = Depends(telegram_api),
) -> Response:
    """Send one test message now, whatever the quiet hours say."""
    text = "This is a test from QuantTrading. If you can read it, delivery works."
    try:
        if body.channel == "email":
            if email is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "no mail server is set up")
            email.send(service.resolve_email(db, user), "QuantTrading test", text)
        else:
            ds = db.scalar(select(DeliverySettings).where(DeliverySettings.user_id == user.id))
            if tg is None or ds is None or ds.telegram_chat_id is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "Telegram is not linked")
            tg.send(ds.telegram_chat_id, text)
    except DeliveryError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""Shared helpers for running provider jobs, from the worker and from admin requests."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from quant.config import Settings
from quant.models import RawResponse
from quant.providers.base import Fetcher


def make_fetcher(session: Session, settings: Settings) -> Fetcher:
    def record(provider: str, key: str, status: int, body: str) -> None:
        session.add(RawResponse(provider=provider, request_key=key, status_code=status, body=body))

    client = httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True)
    return Fetcher(client, recorder=record, retries=settings.http_retries)


def today_local(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))

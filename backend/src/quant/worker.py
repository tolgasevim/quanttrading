"""Scheduler process: runs the ingestion jobs on their cron schedules (PRD §8).

On start it catches up on any job without a successful run in the last 24 hours, so a Mac mini
that was asleep or rebooted during the scheduled time still gets its data.
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.config import Settings, get_settings
from quant.db import get_sessionmaker
from quant.ingest.fx import ingest_fx
from quant.ingest.jobs import run_job
from quant.ingest.prices import ingest_prices
from quant.models import JobRun, JobStatus, RawResponse
from quant.providers.base import Fetcher
from quant.providers.registry import fx_provider, price_providers

log = logging.getLogger(__name__)

PRICES_JOB = "ingest_prices"
FX_JOB = "ingest_fx"


def make_fetcher(session: Session, settings: Settings) -> Fetcher:
    def record(provider: str, key: str, status: int, body: str) -> None:
        session.add(RawResponse(provider=provider, request_key=key, status_code=status, body=body))

    client = httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True)
    return Fetcher(client, recorder=record, retries=settings.http_retries)


def today_local(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def run_prices() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        providers = price_providers(settings.price_providers, make_fetcher(session, settings))
        run_job(
            session,
            PRICES_JOB,
            lambda s: ingest_prices(
                s, providers, today_local(settings).date(), settings.backfill_days
            ),
        )


def run_fx() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        provider = fx_provider(settings.fx_provider, make_fetcher(session, settings))
        run_job(
            session,
            FX_JOB,
            lambda s: ingest_fx(s, provider, today_local(settings).date(), settings.backfill_days),
        )


def needs_catch_up(session: Session, job: str, now: datetime) -> bool:
    last_ok = session.scalar(
        select(JobRun.finished_at)
        .where(JobRun.job == job, JobRun.status.in_([JobStatus.SUCCESS, JobStatus.PARTIAL]))
        .order_by(JobRun.finished_at.desc())
        .limit(1)
    )
    return last_ok is None or last_ok < now - timedelta(hours=24)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)

    with get_sessionmaker()() as session:
        now = datetime.now(UTC)
        for job, fn in ((FX_JOB, run_fx), (PRICES_JOB, run_prices)):
            if needs_catch_up(session, job, now):
                log.info("catching up on %s", job)
                fn()

    scheduler = BlockingScheduler(timezone=tz)
    common = {"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1}
    scheduler.add_job(
        run_fx, CronTrigger.from_crontab(settings.fx_cron, timezone=tz), id=FX_JOB, **common
    )
    scheduler.add_job(
        run_prices,
        CronTrigger.from_crontab(settings.prices_cron, timezone=tz),
        id=PRICES_JOB,
        **common,
    )
    log.info("scheduler started: fx=%r prices=%r (%s)", settings.fx_cron, settings.prices_cron, tz)
    scheduler.start()


if __name__ == "__main__":
    main()

"""Scheduler process: runs the ingestion jobs on their cron schedules (PRD §8).

On start it catches up on any job without a successful run in the last 24 hours, so a Mac mini
that was asleep or rebooted during the scheduled time still gets its data.
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.config import get_settings
from quant.db import get_sessionmaker
from quant.ingest.fx import ingest_fx
from quant.ingest.jobs import run_job
from quant.ingest.mapping import map_isins
from quant.ingest.prices import ingest_prices
from quant.ingest.runtime import make_fetcher, today_local
from quant.models import JobRun, JobStatus
from quant.providers.registry import fx_provider, isin_resolvers, price_providers

log = logging.getLogger(__name__)

PRICES_JOB = "ingest_prices"
FX_JOB = "ingest_fx"
MAP_JOB = "map_isins"


def run_mapping() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Shared market data across users: read every user's transactions (FR-3 bypass).
        rls.bypass(session)
        fetcher = make_fetcher(session, settings)
        try:
            # Built inside the job, so a bad setting (an unknown name) is recorded in job_runs
            # as a failed run instead of crashing the worker at start.
            run_job(
                session,
                MAP_JOB,
                lambda s: map_isins(
                    s,
                    isin_resolvers(settings.isin_resolvers, fetcher, settings.openfigi_api_key),
                    datetime.now(UTC),
                    settings.isin_retry_days,
                ),
            )
        finally:
            fetcher.close()


def run_prices() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Which instruments to fetch depends on what every user holds (FR-3 bypass).
        rls.bypass(session)
        fetcher = make_fetcher(session, settings)
        try:
            run_job(
                session,
                PRICES_JOB,
                lambda s: ingest_prices(
                    s,
                    price_providers(settings.price_providers, fetcher),
                    today_local(settings).date(),
                    settings.backfill_days,
                ),
            )
        finally:
            fetcher.close()


def run_fx() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        fetcher = make_fetcher(session, settings)
        try:
            run_job(
                session,
                FX_JOB,
                lambda s: ingest_fx(
                    s,
                    fx_provider(settings.fx_provider, fetcher),
                    today_local(settings).date(),
                    settings.backfill_days,
                ),
            )
        finally:
            fetcher.close()


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
        for job, fn in ((FX_JOB, run_fx), (MAP_JOB, run_mapping), (PRICES_JOB, run_prices)):
            if needs_catch_up(session, job, now):
                log.info("catching up on %s", job)
                try:
                    fn()
                except Exception:  # noqa: BLE001 - one broken job must not stop the scheduler
                    log.exception("catch-up of %s failed", job)

    scheduler = BlockingScheduler(timezone=tz)
    common = {"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1}
    scheduler.add_job(
        run_fx, CronTrigger.from_crontab(settings.fx_cron, timezone=tz), id=FX_JOB, **common
    )
    scheduler.add_job(
        run_mapping,
        CronTrigger.from_crontab(settings.mapping_cron, timezone=tz),
        id=MAP_JOB,
        **common,
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

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
from quant.config import Settings, get_settings
from quant.db import get_sessionmaker
from quant.ingest.alerts import create_alerts
from quant.ingest.fx import ingest_fx
from quant.ingest.jobs import JobResult, run_job
from quant.ingest.mapping import held_isins, map_isins
from quant.ingest.prices import ingest_prices
from quant.ingest.runtime import make_fetcher, today_local
from quant.ingest.sectors import fill_sectors
from quant.models import JobRun, JobStatus
from quant.providers.base import Fetcher
from quant.providers.registry import crypto_resolvers, fx_provider, isin_resolvers, price_providers

log = logging.getLogger(__name__)

PRICES_JOB = "ingest_prices"
FX_JOB = "ingest_fx"
MAP_JOB = "map_isins"
ALERT_JOB = "create_alerts"


def _map_and_fill(session: Session, settings: Settings, fetcher: Fetcher) -> JobResult:
    """Find tickers for the ISINs people hold, then the sector of their shares."""
    # An empty list is allowed when coins are priced: shares then get an error each.
    shares = (
        isin_resolvers(settings.isin_resolvers, fetcher, settings.openfigi_api_key)
        if settings.isin_resolvers
        else []
    )
    now = datetime.now(UTC)
    held = held_isins(session)
    result = map_isins(
        session,
        shares,
        now,
        settings.isin_retry_days,
        isins=held,
        crypto=crypto_resolvers(settings.price_providers, fetcher, settings.coingecko_api_key),
    )
    # A failed sector lookup is a warning, never a failed run: the tickers are what matter, and
    # they are already saved.
    try:
        sectors = fill_sectors(session, shares, now, settings.isin_retry_days, isins=held)
    except Exception as exc:  # noqa: BLE001 - whatever it was, the mapping above must stand
        session.rollback()
        log.exception("sector lookup failed")
        result.warnings["sectors"] = f"sector lookup failed: {type(exc).__name__}: {exc}"
        return result
    result.rows_written += sectors.rows_written
    result.warnings.update({k: v for k, v in sectors.warnings.items() if k not in result.warnings})
    return result


def run_mapping() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Shared market data across users: read every user's transactions (FR-3 bypass).
        rls.bypass(session)
        fetcher = make_fetcher(session, settings)
        try:
            # Built inside the job, so a bad setting (an unknown name) is recorded in job_runs
            # as a failed run instead of crashing the worker at start.
            run_job(session, MAP_JOB, lambda s: _map_and_fill(s, settings, fetcher))
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
                    price_providers(settings.price_providers, fetcher, settings.coingecko_api_key),
                    today_local(settings).date(),
                    settings.backfill_days,
                ),
            )
        finally:
            fetcher.close()


def run_evening() -> None:
    """The evening run: prices, then the alerts that need them. Chained, so a slow or late price
    run can never leave the alerts to work on yesterday's bars."""
    try:
        run_prices()
    finally:
        run_alerts()


def run_alerts() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Alerts are made for every user from the shared prices (FR-3 bypass); each row names its
        # user.
        rls.bypass(session)
        run_job(
            session,
            ALERT_JOB,
            lambda s: create_alerts(s, today_local(settings).date(), datetime.now(UTC)),
        )


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
        for job, fn in (
            (FX_JOB, run_fx),
            (MAP_JOB, run_mapping),
            (PRICES_JOB, run_prices),
            (ALERT_JOB, run_alerts),
        ):
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
        run_evening,
        CronTrigger.from_crontab(settings.prices_cron, timezone=tz),
        id=PRICES_JOB,
        **common,
    )
    log.info("scheduler started: fx=%r prices=%r (%s)", settings.fx_cron, settings.prices_cron, tz)
    scheduler.start()


if __name__ == "__main__":
    main()

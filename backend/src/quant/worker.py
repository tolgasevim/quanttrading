"""Scheduler process: runs the ingestion jobs on their cron schedules (PRD §8).

On start it catches up on any job without a successful run in the last 24 hours, so a Mac mini
that was asleep or rebooted during the scheduled time still gets its data.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ai.commentary import run_weekly
from quant.ai.scoring import score_picks
from quant.config import Settings, get_settings
from quant.db import get_sessionmaker
from quant.delivery import service as delivery
from quant.delivery.transports import make_email_sender, make_telegram
from quant.ingest.alerts import ALERT_JOB_NAME, create_alerts
from quant.ingest.fx import ingest_fx
from quant.ingest.jobs import JobResult, run_job
from quant.ingest.mapping import held_isins, map_isins
from quant.ingest.prices import ingest_prices
from quant.ingest.runtime import make_fetcher, today_local
from quant.ingest.sectors import fill_sectors
from quant.models import JobRun, JobStatus
from quant.portfolio.factors import compute_scores
from quant.providers.base import Fetcher
from quant.providers.registry import crypto_resolvers, fx_provider, isin_resolvers, price_providers

log = logging.getLogger(__name__)

PRICES_JOB = "ingest_prices"
FX_JOB = "ingest_fx"
MAP_JOB = "map_isins"
SCORE_JOB = "score_picks"
FACTOR_JOB = "score_factors"
PUSH_JOB = "push_alerts"
DIGEST_JOB = "send_digests"
WEEKLY_JOB = "weekly_commentary"


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


def _record_crash(job: str, exc: Exception) -> None:
    """A job that crashes before `run_job` can write its row (a bad setting, a database error at
    start) leaves nothing in job_runs, and the admin alerts are built from job_runs. Leave a failed
    row, so they hear about it. Only the kind of error is kept."""
    try:
        with get_sessionmaker()() as session:
            session.add(
                JobRun(
                    job=job,
                    status=JobStatus.FAILED,
                    finished_at=datetime.now(UTC),
                    details={"error": type(exc).__name__},
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - the log already has the crash
        log.exception("could not record the crash of %s", job)


def run_evening() -> None:
    """The evening run: prices, then the alerts that need them. Chained, so a slow or late price
    run can never leave the alerts to work on yesterday's bars. A failed price run is recorded
    in job_runs (and logged here if it fails before it can be); the alerts still run, so the
    admins hear about it."""
    try:
        run_prices()
    except Exception as exc:  # noqa: BLE001 - the alerts must still run
        log.exception("the price run failed")
        _record_crash(PRICES_JOB, exc)
    try:
        run_scoring()  # after the prices, so a window that ended yesterday is scored today
    except Exception as exc:  # noqa: BLE001 - the alerts must still run
        log.exception("the pick scoring failed")
        _record_crash(SCORE_JOB, exc)
    try:
        run_factor_scores()  # after the prices: momentum and risk need tonight's bars
    except Exception as exc:  # noqa: BLE001 - the alerts must still run
        log.exception("the factor scoring failed")
        _record_crash(FACTOR_JOB, exc)
    run_alerts()  # last, so it can tell the admins about any job that failed tonight
    try:
        run_push()  # the alerts just made go out now, unless quiet hours hold them
    except Exception as exc:  # noqa: BLE001 - nothing else depends on it
        log.exception("the alert push failed")
        _record_crash(PUSH_JOB, exc)


def run_alerts() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Alerts are made for every user from the shared prices (FR-3 bypass); each row names its
        # user.
        rls.bypass(session)
        run_job(
            session,
            ALERT_JOB_NAME,
            lambda s: create_alerts(s, today_local(settings).date(), datetime.now(UTC)),
        )


def run_scoring() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Scores every user's picks from the shared prices (FR-3 bypass).
        rls.bypass(session)
        run_job(session, SCORE_JOB, lambda s: score_picks(s, today_local(settings).date()))


def run_factor_scores() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Scores the shared universe (FR-30, FR-31). The scores are public data: no user rows.
        rls.bypass(session)
        run_job(session, FACTOR_JOB, lambda s: compute_scores(s, today_local(settings).date()))


def run_push() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Pushes every user's new alerts to their linked Telegram chat (FR-72, FR-3 bypass).
        rls.bypass(session)
        run_job(
            session,
            PUSH_JOB,
            lambda s: delivery.push_alerts(
                s, make_telegram(settings), datetime.now(UTC), settings.timezone
            ),
        )


def run_digests() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Sends each user's email digest (FR-71, FR-3 bypass).
        rls.bypass(session)
        run_job(
            session,
            DIGEST_JOB,
            lambda s: delivery.send_digests(
                s,
                make_email_sender(settings),
                datetime.now(UTC),
                settings.timezone,
                delivery.consolidation_prompts,
            ),
        )


def run_weekly_commentary() -> None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        # Only the job's own bookkeeping runs here; each user's commentary has its own scoped
        # session (see `run_weekly`).
        run_job(session, WEEKLY_JOB, lambda s: run_weekly(today_local(settings).date()))


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
        for job, fn in ((FX_JOB, run_fx), (MAP_JOB, run_mapping)):
            if needs_catch_up(session, job, now):
                log.info("catching up on %s", job)
                try:
                    fn()
                except Exception:  # noqa: BLE001 - one broken job must not stop the scheduler
                    log.exception("catch-up of %s failed", job)
        # Prices and alerts go together, as in the evening run.
        late: Callable[[], None] | None = None
        if needs_catch_up(session, PRICES_JOB, now):
            late = run_evening
        elif needs_catch_up(session, ALERT_JOB_NAME, now):
            late = run_alerts
        if late is not None:
            log.info("catching up on %s", late.__name__)
            try:
                late()
            except Exception:  # noqa: BLE001 - the scheduler must still start
                log.exception("catch-up failed")

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
    scheduler.add_job(
        run_weekly_commentary,
        CronTrigger.from_crontab(settings.commentary_cron, timezone=tz),
        id=WEEKLY_JOB,
        **common,
    )
    scheduler.add_job(
        run_push, CronTrigger.from_crontab(settings.push_cron, timezone=tz), id=PUSH_JOB, **common
    )
    scheduler.add_job(
        run_digests,
        CronTrigger.from_crontab(settings.digest_cron, timezone=tz),
        id=DIGEST_JOB,
        **common,
    )
    log.info("scheduler started: fx=%r prices=%r (%s)", settings.fx_cron, settings.prices_cron, tz)
    scheduler.start()


if __name__ == "__main__":
    main()

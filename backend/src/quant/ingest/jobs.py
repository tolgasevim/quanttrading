"""Job bookkeeping: every run is recorded in `job_runs` so failures are visible (PRD §8)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from quant.models import JobRun, JobStatus

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    rows_written: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)
    attempted: int = 0

    @property
    def status(self) -> JobStatus:
        if not self.errors:
            return JobStatus.SUCCESS
        if len(self.errors) < max(self.attempted, 1):
            return JobStatus.PARTIAL
        return JobStatus.FAILED


def run_job(session: Session, name: str, work: Callable[[Session], JobResult]) -> JobRun:
    run = JobRun(job=name, status=JobStatus.RUNNING, details={})
    session.add(run)
    session.commit()
    try:
        result = work(session)
    except Exception as exc:  # noqa: BLE001 - any crash must land in job_runs, not vanish
        session.rollback()
        log.exception("job %s crashed", name)
        run.status = JobStatus.FAILED
        run.details = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        run.status = result.status
        run.rows_written = result.rows_written
        run.details = {
            "attempted": result.attempted,
            "errors": result.errors,
            "warnings": result.warnings,
        }
    run.finished_at = datetime.now(UTC)
    session.add(run)
    session.commit()
    log.info("job %s finished: %s (%s rows)", name, run.status, run.rows_written)
    return run

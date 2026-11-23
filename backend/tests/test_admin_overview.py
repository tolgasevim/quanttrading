"""The admin panel (FR-5): users, last import, data-job health and AI spend."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.api.admin import EXPECTED_JOBS
from quant.models import (
    Import,
    ImportStatus,
    JobRun,
    JobStatus,
    LlmUsage,
    Passkey,
    User,
)

from .conftest import login, make_user


def overview(client: TestClient) -> dict[str, Any]:
    out = client.get("/api/admin/overview")
    assert out.status_code == 200, out.text
    body: dict[str, Any] = out.json()
    return body


def job(db: Session, name: str, status: JobStatus, hours_ago: float = 1, **kw: Any) -> None:
    started = datetime.now(UTC) - timedelta(hours=hours_ago)
    db.add(
        JobRun(
            job=name,
            status=status,
            started_at=started,
            finished_at=started + timedelta(minutes=2),
            **kw,
        )
    )
    db.commit()


def jobs_of(client: TestClient) -> dict[str, dict[str, Any]]:
    return {j["job"]: j for j in overview(client)["jobs"]}


def test_only_an_admin_sees_the_panel(owner: TestClient, client: TestClient, db: Session) -> None:
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert client.get("/api/admin/overview").status_code == 401
    assert login(client, "friend@example.com") == 200
    assert client.get("/api/admin/overview").status_code == 403


def test_the_expected_jobs_are_the_ones_the_worker_runs() -> None:
    from quant import worker
    from quant.ingest.alerts import ALERT_JOB_NAME

    assert set(EXPECTED_JOBS) == {
        worker.PRICES_JOB,
        worker.FX_JOB,
        worker.MAP_JOB,
        worker.SCORE_JOB,
        worker.FACTOR_JOB,
        worker.PUSH_JOB,
        worker.SWING_JOB,
        worker.DIGEST_JOB,
        worker.WEEKLY_JOB,
        ALERT_JOB_NAME,
    }


def test_a_job_that_never_ran_is_not_healthy(owner: TestClient) -> None:
    jobs = jobs_of(owner)
    assert set(jobs) == set(EXPECTED_JOBS)
    prices = jobs["ingest_prices"]
    assert prices["healthy"] is False and prices["problem"] == "it has never run"
    assert prices["last_status"] is None and prices["last_success"] is None


def test_good_runs_make_a_job_healthy_and_the_weekly_one_has_a_longer_leash(
    owner: TestClient, db: Session
) -> None:
    for name in EXPECTED_JOBS:
        job(db, name, JobStatus.SUCCESS, hours_ago=2, rows_written=5)
    job(db, "weekly_commentary", JobStatus.SUCCESS, hours_ago=5 * 24)  # newer than the 8 days
    jobs = jobs_of(owner)
    assert all(j["healthy"] for j in jobs.values())
    assert jobs["ingest_prices"]["rows_written"] == 5
    assert jobs["ingest_prices"]["last_success"] is not None


def test_a_failed_last_run_a_stale_success_and_a_partial_run(
    owner: TestClient, db: Session
) -> None:
    job(db, "ingest_prices", JobStatus.SUCCESS, hours_ago=30)
    job(db, "ingest_prices", JobStatus.FAILED, hours_ago=2)
    job(db, "ingest_fx", JobStatus.SUCCESS, hours_ago=100)  # no good run for too long
    job(db, "map_isins", JobStatus.PARTIAL, hours_ago=2, details={"errors": {"A": "x", "B": "y"}})
    jobs = jobs_of(owner)
    assert jobs["ingest_prices"]["problem"] == "the last run failed"
    assert jobs["ingest_prices"]["failures_7d"] == 1
    assert jobs["ingest_prices"]["last_success"] is not None  # the older good run is remembered
    assert jobs["ingest_fx"]["problem"] == "no good run for too long"
    partial = jobs["map_isins"]
    assert partial["healthy"] is True and partial["error_count"] == 2  # counted, not named
    assert "A" not in str(partial)


def test_a_run_stuck_for_hours_is_a_problem_and_old_failures_are_not_counted(
    owner: TestClient, db: Session
) -> None:
    job(db, "score_factors", JobStatus.SUCCESS, hours_ago=20)
    job(db, "score_factors", JobStatus.RUNNING, hours_ago=5)
    job(db, "score_picks", JobStatus.SUCCESS, hours_ago=2)
    job(db, "score_picks", JobStatus.FAILED, hours_ago=24 * 9)
    jobs = jobs_of(owner)
    assert jobs["score_factors"]["problem"] == "the last run has been running for over 3 hours"
    assert jobs["score_picks"]["failures_7d"] == 0 and jobs["score_picks"]["healthy"] is True


def test_users_show_sign_in_methods_last_import_and_ai_spend(
    owner: TestClient, db: Session
) -> None:
    friend = make_user(db, "friend@example.com")
    admin = db.scalars(select(User).where(User.email == "owner@example.com")).one()
    rls.bypass(db)  # imports and the AI ledger are row-level secured
    db.add(Passkey(user_id=friend.id, credential_id=b"c", public_key=b"k", sign_count=0, name="p"))
    friend.last_login_at = datetime.now(UTC) - timedelta(days=1)
    friend.locked_until = datetime.now(UTC) + timedelta(minutes=5)
    now = datetime.now(UTC)
    db.add(
        Import(
            user_id=friend.id, source="tr_transactions_csv", status=ImportStatus.COMMITTED,
            summary={}, committed_at=now - timedelta(days=9), rows_inserted=10,
        )
    )  # fmt: skip
    db.add(
        Import(
            user_id=friend.id, source="tr_depot_pdf", status=ImportStatus.COMMITTED,
            summary={}, committed_at=now - timedelta(days=2), rows_inserted=0,
        )
    )  # fmt: skip
    db.add(
        Import(user_id=friend.id, source="draft", status=ImportStatus.PREVIEW, summary={})
    )  # not committed: not a last import
    for who, cost, purpose in ((friend.id, "1.5", "ask"), (admin.id, "2.25", "weekly")):
        db.add(
            LlmUsage(
                user_id=who, purpose=purpose, model="m", input_tokens=1, output_tokens=1,
                cost_usd=D(cost), cost_eur=D(cost),
            )
        )  # fmt: skip
    db.commit()
    body = overview(owner)
    rows = {u["email"]: u for u in body["users"]}
    f, a = rows["friend@example.com"], rows["owner@example.com"]
    assert (
        f["passkeys"] == 1 and a["passkeys"] == 0 and f["locked"] is True and a["locked"] is False
    )
    assert f["last_import_source"] == "tr_depot_pdf" and f["last_import_at"] is not None
    assert a["last_import_at"] is None
    assert D(f["ai_spend_eur"]) == D("1.5") and D(a["ai_spend_eur"]) == D("2.25")
    assert a["role"] == "admin" and f["role"] == "member"
    llm = body["llm"]
    assert D(llm["total_eur"]) == D("3.75")
    assert {k: D(v) for k, v in llm["by_purpose"].items()} == {"ask": D("1.5"), "weekly": D("2.25")}
    assert D(llm["total_cap_eur"]) > 0 and D(llm["user_cap_eur"]) > 0


def test_the_panel_shows_counts_and_times_never_holdings(owner: TestClient, db: Session) -> None:
    friend = make_user(db, "friend@example.com")
    rls.bypass(db)
    db.add(
        Import(
            user_id=friend.id, source="tr_transactions_csv", status=ImportStatus.COMMITTED,
            summary={"secret": "holding-detail"}, committed_at=datetime.now(UTC),
        )
    )  # fmt: skip
    db.commit()
    assert "holding-detail" not in owner.get("/api/admin/overview").text


def test_pending_invites_are_counted(owner: TestClient) -> None:
    assert overview(owner)["pending_invites"] == 0
    assert owner.post("/api/admin/invites", json={"email": "new@example.com"}).status_code == 201
    assert overview(owner)["pending_invites"] == 1


@pytest.mark.parametrize("hours", [0.5, 70])
def test_a_good_run_inside_the_window_keeps_a_job_healthy(
    owner: TestClient, db: Session, hours: float
) -> None:
    job(db, "ingest_prices", JobStatus.SUCCESS, hours_ago=hours)  # 70 h: over a weekend
    assert jobs_of(owner)["ingest_prices"]["healthy"] is True

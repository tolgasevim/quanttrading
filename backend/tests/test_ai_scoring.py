from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from quant import rls
from quant.ai import scoring
from quant.models import AiPick, AiPickScore, Instrument, JobStatus, PriceEOD, User
from quant.worker import run_scoring

from .conftest import login, make_user

ISIN = "US0000000001"
TODAY = date(2026, 10, 6)


def instrument(db: Session, code: str, isin: str | None = None) -> Instrument:
    row = Instrument(code=code, isin=isin, name=code, asset_class="stock", currency="EUR")
    db.add(row)
    db.flush()
    return row


def bars(db: Session, row: Instrument, closes: dict[date, str]) -> None:
    for day, close in closes.items():
        db.add(PriceEOD(instrument_id=row.id, date=day, close=D(close), currency="EUR", source="t"))
    db.commit()


def pick(
    db: Session, user: User, when: date, direction: str = "buy", isin: str | None = ISIN, **kw: str
) -> AiPick:
    rls.bypass(db)
    row = AiPick(
        user_id=user.id, created_at=datetime(when.year, when.month, when.day, 9, tzinfo=UTC),
        question="q", name="Alpha", isin=isin, direction=direction, horizon_months=12,
        rationale="r", held=False, **kw,
    )  # fmt: skip
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def market(db: Session) -> tuple[Instrument, Instrument]:
    alpha = instrument(db, "ALPHA", ISIN)
    bench = instrument(db, scoring.BENCHMARK_CODE, "IE00B53SZB19")
    start = date(2026, 5, 4)  # a Monday
    one = date(2026, 6, 4)
    three = date(2026, 8, 4)
    # Alpha +20 % after a month, +50 % after three; the benchmark +10 % and +12 %.
    bars(db, alpha, {start: "100", one: "120", three: "150"})
    bars(db, bench, {start: "200", one: "220", three: "224"})
    return alpha, bench


def test_months_are_added_by_the_calendar() -> None:
    assert scoring.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert scoring.add_months(date(2026, 10, 6), 12) == date(2027, 10, 6)
    assert scoring.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_a_hit_depends_on_the_direction() -> None:
    assert scoring.is_hit("buy", D("1")) and not scoring.is_hit("buy", D("-1"))
    assert scoring.is_hit("hold", D("1")) and not scoring.is_hit("hold", D("0"))
    assert scoring.is_hit("sell", D("-1")) and not scoring.is_hit("sell", D("1"))


def test_due_windows_are_scored_against_the_benchmark(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    row = pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    result = scoring.score_picks(db, TODAY)
    assert result.rows_written == 2 and result.errors == {}  # 1 and 3 months; 6 is not due
    scores = {s.window_months: s for s in db.scalars(select(AiPickScore))}
    assert set(scores) == {1, 3}
    one = scores[1]
    assert (one.pick_return_pct, one.benchmark_return_pct, one.excess_pct) == (
        D("20.0000"), D("10.0000"), D("10.0000"),
    )  # fmt: skip
    assert one.hit is True and one.pick_id == row.id and one.user_id == admin.id
    assert scores[3].pick_return_pct == D("50.0000") and scores[3].benchmark_return_pct == D(
        "12.0000"
    )


def test_a_sell_is_a_hit_when_the_instrument_trailed(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4), direction="sell")
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    assert all(s.hit is False for s in db.scalars(select(AiPickScore)))  # Alpha beat the market


def test_a_score_is_written_once(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    assert scoring.score_picks(db, TODAY).rows_written == 2
    assert scoring.score_picks(db, TODAY).rows_written == 0
    assert len(db.scalars(select(AiPickScore)).all()) == 2


def test_a_window_without_prices_waits_for_the_next_run(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    alpha, bench = market
    pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    # Six months are due on 4 Nov; the data ends in August, so nothing can be scored then.
    result = scoring.score_picks(db, date(2026, 11, 5))
    assert result.rows_written == 2 and "waiting" in result.warnings
    bars(db, alpha, {date(2026, 11, 4): "90"})
    bars(db, bench, {date(2026, 11, 4): "230"})
    rls.bypass(db)
    assert scoring.score_picks(db, date(2026, 11, 5)).rows_written == 1
    six = db.scalars(select(AiPickScore).where(AiPickScore.window_months == 6)).one()
    assert six.pick_return_pct == D("-10.0000") and six.hit is False


def test_a_pick_with_no_known_instrument_is_reported_not_scored(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4), isin="XX0000000000")
    rls.bypass(db)
    result = scoring.score_picks(db, TODAY)
    assert result.rows_written == 0 and "unscorable" in result.warnings


def test_a_ticker_finds_the_instrument_when_the_isin_is_unknown(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4), isin=None, ticker="alpha")
    rls.bypass(db)
    assert scoring.score_picks(db, TODAY).rows_written == 2


def test_without_the_benchmark_the_job_says_so(db: Session, admin: User) -> None:
    rls.bypass(db)
    assert "benchmark" in scoring.score_picks(db, TODAY).errors


def test_the_nightly_job_records_a_run(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    from quant.models import JobRun

    pick(db, admin, date(2026, 5, 4))
    run_scoring()
    rls.bypass(db)
    run = db.scalars(select(JobRun).where(JobRun.job == "score_picks")).one()
    assert run.status in (JobStatus.SUCCESS, JobStatus.PARTIAL)


def test_the_track_record_page_data(
    client: TestClient, db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4))
    pick(db, admin, date(2026, 10, 1))  # too young to score
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    login(client, admin.email)
    body = client.get("/api/ai/track-record").json()
    assert body["benchmark"] == "SXRV" and body["picks"] == 2 and body["unscored"] == 1
    w = {x["window_months"]: x for x in body["windows"]}
    assert w[1]["scored"] == 1 and w[1]["hit_rate_pct"] == "100.0"
    assert (
        w[1]["avg_excess_pct"] == "10.00" and w[6]["scored"] == 0 and w[6]["hit_rate_pct"] is None
    )
    scored = [i for i in body["items"] if i["scores"]]
    assert len(scored) == 1 and [s["window_months"] for s in scored[0]["scores"]] == [1, 3]


def test_track_records_are_private(
    client: TestClient, db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    other = make_user(db, "other@example.com")
    login(client, other.email)
    body = client.get("/api/ai/track-record").json()
    assert (
        body["picks"] == 0
        and body["items"] == []
        and all(w["scored"] == 0 for w in body["windows"])
    )
    assert TestClient(client.app).get("/api/ai/track-record").status_code == 401


def test_the_app_cannot_change_scores(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    for sql in ("DELETE FROM ai_pick_scores", "UPDATE ai_pick_scores SET hit = true"):
        with pytest.raises(ProgrammingError):
            db.execute(text(sql))
        db.rollback()

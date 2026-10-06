from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ingest.alerts import ALERT_JOB_NAME, Person, create_alerts
from quant.models import (
    AlertSettings,
    Instrument,
    JobRun,
    JobStatus,
    Notification,
    PriceEOD,
    Role,
    User,
)
from quant.portfolio.alerts import (
    Move,
    breaches,
    daily_move,
    in_quiet_hours,
    looks_like_split,
    move_text,
    threshold_for,
)

from .conftest import login, make_user
from .test_holdings_pnl_api import BTC, SPIN, A, B, history, import_history, row

TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)
FUND = "IE0000000030"


def next_berlin_midnight(after: datetime) -> datetime:
    """The first midnight in Europe/Berlin after `after`, in UTC. Alert days are Berlin days, so
    tests that put two runs on one day start from here and cannot straddle a day edge."""
    from zoneinfo import ZoneInfo

    local = after.astimezone(ZoneInfo("Europe/Berlin"))
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def bars(*closes: str, last: date = TODAY, step: int = 1) -> list[tuple[date, D]]:
    """Closes newest first, one bar `step` days apart."""
    return [(last - timedelta(days=step * i), D(c)) for i, c in enumerate(closes)]


# --- the rules -----------------------------------------------------------------------------


def test_a_move_is_the_change_between_the_two_newest_closes() -> None:
    move = daily_move(bars("108", "100"), TODAY)
    assert move is not None and move.pct == D(8) and move.day == TODAY
    assert (move.close, move.previous, move.previous_day) == (D(108), D(100), TODAY - timedelta(1))
    down = daily_move(bars("95", "100"), TODAY)
    assert down is not None and down.pct == D(-5)


def test_no_move_without_two_bars_or_with_stale_or_holey_data() -> None:
    assert daily_move([], TODAY) is None
    assert daily_move(bars("100"), TODAY) is None
    assert daily_move(bars("108", "100", last=TODAY - timedelta(days=5)), TODAY) is None  # stale
    assert daily_move(bars("108", "100", last=TODAY - timedelta(days=4)), TODAY) is not None
    assert daily_move(bars("108", "100", step=6), TODAY) is None  # a hole in the data
    assert daily_move(bars("108", "100", step=5), TODAY) is not None  # Easter is fine
    assert daily_move(bars("108", "0"), TODAY) is None  # no base to divide by
    assert daily_move(bars("108", "100", last=TODAY + timedelta(days=1)), TODAY) is None  # future
    same_day = [(TODAY, D(108)), (TODAY, D(100))]
    assert daily_move(same_day, TODAY) is None


def test_a_limit_is_breached_at_the_limit_and_beyond_it_in_both_directions() -> None:
    limit = D(5)
    assert breaches(Move(D(5), D(1), D(1), TODAY, TODAY), limit)
    assert breaches(Move(D(-5), D(1), D(1), TODAY, TODAY), limit)
    assert not breaches(Move(D("4.99"), D(1), D(1), TODAY, TODAY), limit)


def test_every_class_with_a_move_label_has_a_limit_and_no_other_class_does() -> None:
    from quant.portfolio.alerts import MOVE_CLASSES

    args = (D(5), D(3), D(10))
    assert {c for c in MOVE_CLASSES if threshold_for(c, *args) is not None} == set(MOVE_CLASSES)
    assert threshold_for("ETC", *args) is None


def test_each_asset_class_has_its_own_limit() -> None:
    args = (D(5), D(3), D(10))
    assert threshold_for("STOCK", *args) == D(5)
    assert threshold_for("FUND", *args) == D(3)
    assert threshold_for("CRYPTO", *args) == D(10)
    assert threshold_for("BOND", *args) is None and threshold_for(None, *args) is None


def test_quiet_hours_may_cross_midnight() -> None:
    assert not in_quiet_hours(None, None, time(3))
    assert not in_quiet_hours(time(22), time(22), time(22))  # an empty window
    assert in_quiet_hours(time(22), time(7), time(23, 30))
    assert in_quiet_hours(time(22), time(7), time(3))
    assert in_quiet_hours(time(22), time(7), time(22))  # the start is inside
    assert not in_quiet_hours(time(22), time(7), time(7))  # the end is outside
    assert not in_quiet_hours(time(22), time(7), time(12))
    assert in_quiet_hours(time(13), time(14), time(13, 30))
    assert not in_quiet_hours(time(13), time(14), time(14))


def test_the_text_names_the_instrument_the_move_and_the_limit() -> None:
    move = daily_move(bars("108.5", "100"), TODAY)
    assert move is not None
    title, body = move_text("Alpha Corp", "STOCK", move, D("5.00"), "USD")
    assert title == "Alpha Corp +8.5%"
    assert body == (
        "Alpha Corp closed at 108.5 USD on 2026-10-06, +8.5% from 100 USD on 2026-10-05. "
        "Your limit for a share is 5%."
    )
    down = daily_move(bars("90", "100"), TODAY)
    assert down is not None
    assert move_text("Bitcoin", "CRYPTO", down, D(10), None)[0] == "Bitcoin -10.0%"


# --- the job -------------------------------------------------------------------------------


def price(db: Session, isin: str, closes: tuple[str, ...], cls: str = "stock", **kw: object) -> int:
    inst = Instrument(
        code=isin, isin=isin, name=isin, asset_class=cls, currency="EUR", symbols={"yahoo": "S"}, **kw
    )  # fmt: skip
    db.add(inst)
    db.commit()
    for day, close in bars(*closes):
        db.add(PriceEOD(instrument_id=inst.id, date=day, close=close, currency="EUR", source="x"))
    db.commit()
    return inst.id


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    fund_buy = row(
        70, "2025-02-01", "TRADING", "BUY", "FUND", "World ETF", FUND,
        shares="3", price="80", amount="-240", fee="-1",
    )  # fmt: skip
    import_history(client, history([fund_buy]))
    return client


def alerts(db: Session) -> list[Notification]:
    rls.bypass(db)
    return list(db.scalars(select(Notification).order_by(Notification.dedupe_key)))


def run(db: Session, today: date = TODAY, now: datetime = NOW) -> int:
    rls.bypass(db)
    result = create_alerts(db, today, now)
    assert result.errors == {}
    return result.rows_written


def test_a_big_move_in_a_holding_becomes_a_notification(owner: TestClient, db: Session) -> None:
    price(db, A, ("108", "100"))  # +8% on a share: over 5%
    price(db, SPIN, ("101", "100"))  # +1%: under
    assert run(db) == 1
    [n] = alerts(db)
    assert (n.kind, n.isin, n.title, n.severity) == ("daily_move", A, "Alpha Corp +8.0%", "info")
    assert n.dedupe_key == f"daily_move:{A}:2026-10-06" and n.read_at is None
    assert "Your limit for a share is 5%" in n.body


def test_a_move_of_twice_the_limit_is_a_warning(owner: TestClient, db: Session) -> None:
    price(db, A, ("90", "100"))  # -10% against a 5% limit
    run(db)
    assert alerts(db)[0].severity == "warning"


def test_funds_and_coins_use_their_own_limits(owner: TestClient, db: Session) -> None:
    price(db, FUND, ("104", "100"), cls="etf")  # +4% on a fund: over 3%
    price(db, BTC, ("108", "100"), cls="crypto")  # +8% on a coin: under 10%
    run(db)
    assert [n.isin for n in alerts(db)] == [FUND]
    price(db, "XF000ETH0019", ("89", "100"), cls="crypto")  # -11% on a coin
    run(db)
    assert sorted(n.isin or "" for n in alerts(db)) == sorted([FUND, "XF000ETH0019"])


def test_the_same_move_is_stored_once_however_often_the_job_runs(
    owner: TestClient, db: Session
) -> None:
    price(db, A, ("108", "100"))
    assert run(db) == 1
    assert run(db) == 0
    assert len(alerts(db)) == 1


def test_a_new_day_with_a_new_move_is_a_new_alert(owner: TestClient, db: Session) -> None:
    inst = price(db, A, ("108", "100"))
    run(db)
    tomorrow = TODAY + timedelta(days=1)
    db.add(PriceEOD(instrument_id=inst, date=tomorrow, close=D("120"), currency="EUR", source="x"))
    db.commit()
    assert run(db, tomorrow) == 1
    assert len(alerts(db)) == 2


def test_users_set_their_own_limits_and_can_switch_daily_moves_off(
    owner: TestClient, db: Session, admin: User
) -> None:
    price(db, A, ("108", "100"))
    rls.bypass(db)
    db.add(AlertSettings(user_id=admin.id, daily_moves_enabled=True, move_stock_pct=D(10),
                         move_fund_pct=D(3), move_crypto_pct=D(10)))  # fmt: skip
    db.commit()
    assert run(db) == 0  # +8% is under this user's 10%
    rls.bypass(db)
    row_ = db.get(AlertSettings, admin.id)
    assert row_ is not None
    row_.move_stock_pct, row_.daily_moves_enabled = D(5), False
    db.commit()
    assert run(db) == 0  # switched off
    row_.daily_moves_enabled = True
    db.commit()
    assert run(db) == 1


def test_only_open_positions_with_a_usable_price_get_alerts(owner: TestClient, db: Session) -> None:
    price(db, B, ("150", "100"))  # Beta is sold in full
    price(db, SPIN, ("130", "100"), active=False)  # a switched-off instrument
    stale = price(db, A, ("108", "100"))
    db.query(PriceEOD).filter_by(instrument_id=stale).update({PriceEOD.date: PriceEOD.date - 30})
    db.commit()
    assert run(db) == 0  # a stale price is not today's move


def test_each_user_only_gets_alerts_for_their_own_holdings(
    owner: TestClient, db: Session, admin: User
) -> None:
    other = make_user(db, "member@example.com")
    price(db, A, ("108", "100"))
    run(db)
    assert {n.user_id for n in alerts(db)} == {admin.id}
    assert other.id not in {n.user_id for n in alerts(db)}


def test_failed_and_partly_failed_jobs_are_alerts_for_admins_only(
    owner: TestClient, db: Session, admin: User
) -> None:
    other = make_user(db, "member@example.com")
    now = admin.created_at + timedelta(hours=10)  # the runs below are after the admin's account
    done = now - timedelta(hours=1)
    db.add_all(
        [
            JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=done,
                   details={"error": "RuntimeError: boom"}),
            JobRun(job="map_isins", status=JobStatus.PARTIAL, finished_at=done,
                   details={"attempted": 4, "errors": {"US1": "yahoo: down", "US2": "yahoo: down", "US3": "x"}}),
            JobRun(job="clean_partial", status=JobStatus.PARTIAL, finished_at=done, details={}),
            JobRun(job="one_bad_ticker", status=JobStatus.PARTIAL, finished_at=done,
                   details={"attempted": 40, "errors": {"US9": "no data"}}),
            JobRun(job="old", status=JobStatus.FAILED, finished_at=now - timedelta(hours=60),
                   details={"error": "long ago"}),
            JobRun(job="running", status=JobStatus.RUNNING, started_at=now - timedelta(hours=1),
                   details={}),
            JobRun(job="fine", status=JobStatus.SUCCESS, finished_at=done, details={}),
        ]
    )  # fmt: skip
    db.commit()
    assert run(db, now=now) == 2
    rows = {n.title: n for n in alerts(db)}
    assert set(rows) == {"Job ingest_prices failed", "Job map_isins finished with errors"}
    assert (rows["Job ingest_prices failed"].severity, rows["Job ingest_prices failed"].body) == (
        "warning",
        "RuntimeError: boom",
    )
    partial = rows["Job map_isins finished with errors"]
    assert partial.severity == "info" and "3 items failed" in partial.body
    assert all(n.kind == "job_failed" and n.user_id == admin.id for n in rows.values())
    assert other.id not in {n.user_id for n in rows.values()}
    assert run(db, now=now) == 0  # once per run


def test_a_new_admin_gets_no_alerts_about_the_time_before_the_account(
    owner: TestClient, db: Session, admin: User
) -> None:
    db.add(
        JobRun(job="ingest_prices", status=JobStatus.FAILED,
               finished_at=admin.created_at - timedelta(hours=1), details={"error": "early"})
    )  # fmt: skip
    db.commit()
    assert run(db, now=admin.created_at + timedelta(hours=1)) == 0


def test_one_users_failure_does_not_stop_the_others(
    owner: TestClient, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.ingest import alerts as module

    member = make_user(db, "member@example.com")
    price(db, A, ("108", "100"))
    real = module._open_positions

    def flaky(session: Session, user: Person):  # type: ignore[no-untyped-def]
        if user.id == member.id:
            raise RuntimeError("bad data")
        return real(session, user)

    monkeypatch.setattr(module, "_open_positions", flaky)
    rls.bypass(db)
    result = create_alerts(db, TODAY, NOW)
    assert result.errors == {str(member.id): "RuntimeError"}  # the kind only, never the text
    assert len(alerts(db)) == 1  # the owner still got theirs


def test_a_move_that_is_a_split_ratio_is_still_an_alert_with_a_note_for_a_share(
    owner: TestClient, db: Session
) -> None:
    price(db, A, ("50", "100"))  # -50%: the size of a 2-for-1 split, but also of a crash
    price(db, SPIN, ("40", "100"))  # a fall of 60%: not a simple ratio
    run(db)
    rows = {n.isin: n for n in alerts(db)}
    assert set(rows) == {A, SPIN}
    assert rows[A].severity == "warning"  # its size, not the heuristic, sets the level
    assert "size of a share split" in rows[A].body
    assert rows[SPIN].severity == "warning" and "split" not in rows[SPIN].body


def test_a_fall_of_half_is_an_alert_for_a_fund_or_a_coin(owner: TestClient, db: Session) -> None:
    price(db, FUND, ("50", "100"), cls="etf")  # funds and coins have no splits
    price(db, BTC, ("50", "100"), cls="crypto")
    run(db)
    assert sorted(n.isin or "" for n in alerts(db)) == sorted([FUND, BTC])


@pytest.mark.parametrize(
    ("close", "previous", "split"),
    [
        ("50", "100", True),  # 2-for-1
        ("49.2", "100", True),  # within 2% of the ratio
        ("33.3", "100", True),  # 3-for-1
        ("1000", "100", False),  # a rise by ten: not flagged, it is a big gain far more often
        ("200", "100", False),  # a doubling is not a split
        ("10", "100", True),  # 10-for-1
        ("60", "100", False),
        ("55", "100", False),
        ("150", "100", False),
        ("108", "100", False),
    ],
)
def test_split_ratios_are_told_from_real_moves(close: str, previous: str, split: bool) -> None:
    move = daily_move([(TODAY, D(close)), (TODAY - timedelta(1), D(previous))], TODAY)
    assert move is not None and looks_like_split(move) is split


def test_the_defaults_are_one_set_for_the_model_the_api_and_the_job(db: Session) -> None:
    from quant.ingest.alerts import DEFAULTS

    rls.bypass(db)
    make = make_user(db, "x@example.com")
    db.add(AlertSettings(user_id=make.id))  # nothing given: the database fills the defaults
    db.commit()
    row_ = db.get(AlertSettings, make.id)
    assert row_ is not None
    db.refresh(row_)
    assert (
        row_.daily_moves_enabled,
        row_.move_stock_pct,
        row_.move_fund_pct,
        row_.move_crypto_pct,
    ) == (
        DEFAULTS.daily_moves_enabled,
        DEFAULTS.move_stock_pct,
        DEFAULTS.move_fund_pct,
        DEFAULTS.move_crypto_pct,
    )


def test_the_alert_job_is_recorded_like_the_others(owner: TestClient, db: Session) -> None:
    from quant import worker

    worker.run_alerts()
    run_ = db.query(JobRun).filter_by(job=ALERT_JOB_NAME).one()
    assert run_.status == JobStatus.SUCCESS and run_.rows_written == 0


def test_an_admin_whose_positions_cannot_be_read_still_gets_the_failed_job_alerts(
    owner: TestClient, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.ingest import alerts as module

    def broken(session: Session, user: Person):  # type: ignore[no-untyped-def]
        raise RuntimeError("bad ledger")

    monkeypatch.setattr(module, "_open_positions", broken)
    now = admin.created_at + timedelta(hours=10)
    db.add(JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=now - timedelta(hours=1),
                  details={"error": "boom"}))  # fmt: skip
    db.commit()
    rls.bypass(db)
    result = create_alerts(db, TODAY, now)
    assert result.errors[str(admin.id)] == "RuntimeError"  # the run is not clean...
    assert [n.title for n in alerts(db)] == ["Job ingest_prices failed"]  # ...but the news arrives


def test_the_evening_run_makes_the_alerts_after_the_prices_even_when_they_fail(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from quant import worker

    order: list[str] = []

    def prices_fail() -> None:
        order.append("prices")
        raise RuntimeError("provider down")

    monkeypatch.setattr(worker, "run_prices", prices_fail)
    monkeypatch.setattr(worker, "run_alerts", lambda: order.append("alerts"))
    monkeypatch.setattr(worker, "run_scoring", lambda: order.append("scoring"))
    worker.run_evening()  # the failure is logged, and the alerts still run
    assert order == ["prices", "alerts", "scoring"]
    assert "the price run failed" in caplog.text
    # A crash before the job could write its own row leaves a failed one, so the admins hear of it.
    crashed = db.query(JobRun).filter_by(job=worker.PRICES_JOB).one()
    assert crashed.status == JobStatus.FAILED and crashed.details == {"error": "RuntimeError"}
    order.clear()
    monkeypatch.setattr(worker, "run_prices", lambda: order.append("prices"))
    worker.run_evening()
    assert order == ["prices", "alerts", "scoring"]


def test_a_job_that_is_partial_every_night_is_one_alert_a_day(
    owner: TestClient, db: Session, admin: User
) -> None:
    day0 = next_berlin_midnight(admin.created_at)  # a fixed day edge, whatever the clock says
    now = day0 + timedelta(hours=30)
    for hours in (3, 5):  # two runs on the same day
        db.add(JobRun(job="ingest_prices", status=JobStatus.PARTIAL,
                      finished_at=day0 + timedelta(hours=hours),
                      details={"attempted": 3, "errors": {"US1": "no data", "US2": "x"}}))  # fmt: skip
    db.commit()
    assert run(db, now=now) == 1
    assert run(db, now=now) == 0


def test_users_with_nothing_to_check_are_not_counted_as_attempted(
    owner: TestClient, db: Session, admin: User
) -> None:
    for name in ("a", "b"):
        user = make_user(db, f"{name}@example.com")
        rls.bypass(db)
        db.add(AlertSettings(user_id=user.id, daily_moves_enabled=False))
        db.commit()
    rls.bypass(db)
    result = create_alerts(db, TODAY, NOW)
    # The admin holds shares (one user to check) and has job alerts (one step); the two members
    # switched alerts off and count for nothing.
    assert result.attempted == 2


def test_a_run_stuck_in_running_is_an_alert_once_it_is_old_enough(
    owner: TestClient, db: Session, admin: User
) -> None:
    now = admin.created_at + timedelta(days=1)
    db.add_all(
        [
            JobRun(job="ingest_prices", status=JobStatus.RUNNING, started_at=now - timedelta(hours=7),
                   details={}),
            JobRun(job="map_isins", status=JobStatus.RUNNING, started_at=now - timedelta(hours=1),
                   details={}),  # still young: just running
        ]
    )  # fmt: skip
    db.commit()
    assert run(db, now=now) == 1
    [n] = alerts(db)
    assert n.title == "Job ingest_prices seems stuck" and n.severity == "warning"
    assert "never finished" in n.body


def test_a_failure_that_a_later_success_made_good_is_not_news(
    owner: TestClient, db: Session, admin: User
) -> None:
    now = admin.created_at + timedelta(hours=30)
    db.add_all(
        [
            JobRun(job="ingest_prices", status=JobStatus.FAILED,
                   finished_at=admin.created_at + timedelta(hours=2), details={"error": "boom"}),
            JobRun(job="ingest_prices", status=JobStatus.SUCCESS,
                   finished_at=admin.created_at + timedelta(hours=20), details={}),
            JobRun(job="map_isins", status=JobStatus.FAILED,
                   finished_at=admin.created_at + timedelta(hours=25), details={"error": "bad"}),
            JobRun(job="map_isins", status=JobStatus.SUCCESS,
                   finished_at=admin.created_at + timedelta(hours=3), details={}),  # earlier
        ]
    )  # fmt: skip
    db.commit()
    assert run(db, now=now) == 1
    assert [n.title for n in alerts(db)] == ["Job map_isins failed"]


def test_a_second_and_different_failure_the_same_day_is_its_own_alert(
    owner: TestClient, db: Session, admin: User
) -> None:
    day0 = next_berlin_midnight(admin.created_at)  # a fixed day edge, whatever the clock says
    now = day0 + timedelta(hours=10)
    for hours, reason in ((2, "first reason"), (4, "second reason"), (5, "second reason")):
        db.add(JobRun(job="ingest_prices", status=JobStatus.FAILED,
                      finished_at=day0 + timedelta(hours=hours),
                      details={"error": reason}))  # fmt: skip
    db.commit()
    assert run(db, now=now) == 2
    assert sorted(n.body for n in alerts(db)) == ["first reason", "second reason"]


def test_a_partial_run_with_a_changing_count_is_still_one_alert_a_day(
    owner: TestClient, db: Session, admin: User
) -> None:
    day0 = next_berlin_midnight(admin.created_at)  # a fixed day edge, whatever the clock says
    now = day0 + timedelta(hours=30)
    for hours, count in ((3, 6), (5, 7)):  # 6, then 7 items failed the same day
        db.add(JobRun(job="ingest_prices", status=JobStatus.PARTIAL,
                      finished_at=day0 + timedelta(hours=hours),
                      details={"attempted": 50, "errors": {f"US{i}": "x" for i in range(count)}}))  # fmt: skip
    db.commit()
    assert run(db, now=now) == 1


def test_the_job_reads_no_bar_older_than_it_can_use(owner: TestClient, db: Session) -> None:
    from quant.ingest.alerts import _newest_bars

    inst = price(db, A, ("108", "100"))
    db.add(
        PriceEOD(
            instrument_id=inst,
            date=TODAY - timedelta(days=400),
            close=D(1),
            currency="EUR",
            source="x",
        )
    )
    db.commit()
    rls.bypass(db)
    found = _newest_bars(db, [inst], TODAY - timedelta(days=12))
    assert [d for d, _ in found[inst]] == [TODAY, TODAY - timedelta(1)]
    assert _newest_bars(db, [], TODAY) == {}


def test_the_same_outage_with_other_numbers_is_one_alert_a_day(
    owner: TestClient, db: Session, admin: User
) -> None:
    day0 = next_berlin_midnight(admin.created_at)  # a fixed day edge, whatever the clock says
    now = day0 + timedelta(hours=10)
    for hours, port in ((2, "8123"), (4, "9456")):
        db.add(JobRun(job="ingest_fx", status=JobStatus.FAILED,
                      finished_at=day0 + timedelta(hours=hours),
                      details={"error": f"ConnectError: port {port} refused (request 4f9ac2e1)"}))  # fmt: skip
    db.commit()
    assert run(db, now=now) == 1


def test_the_alerts_job_does_not_name_people_in_its_own_failure_alert(
    owner: TestClient, db: Session, admin: User
) -> None:
    now = admin.created_at + timedelta(hours=10)
    db.add(JobRun(job=ALERT_JOB_NAME, status=JobStatus.FAILED, finished_at=now - timedelta(hours=1),
                  details={"errors": {f"member{i}@example.com": f"RuntimeError: x{i}" for i in range(6)}}))  # fmt: skip
    db.commit()
    run(db, now=now)
    [n] = alerts(db)
    assert "6 items failed" in n.body and "RuntimeError" in n.body
    assert "@example.com" not in n.body  # what failed, not who it failed for


def test_two_admins_each_get_the_job_alerts_from_one_read(
    owner: TestClient, db: Session, admin: User
) -> None:
    second = make_user(db, "second@example.com", Role.ADMIN)
    now = max(admin.created_at, second.created_at) + timedelta(hours=10)
    db.add(JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=now - timedelta(hours=1),
                  details={"error": "boom"}))  # fmt: skip
    db.commit()
    assert run(db, now=now) == 2
    assert {n.user_id for n in alerts(db)} == {admin.id, second.id}


def test_a_later_partial_run_makes_good_a_failure_but_not_a_run_with_errors(
    owner: TestClient, db: Session, admin: User
) -> None:
    now = admin.created_at + timedelta(hours=30)
    t = admin.created_at
    db.add_all(
        [
            JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=t + timedelta(hours=2),
                   details={"error": "boom"}),
            JobRun(job="ingest_prices", status=JobStatus.PARTIAL, finished_at=t + timedelta(hours=20),
                   details={"attempted": 50, "errors": {"US1": "x"}}),  # healthy for the scheduler
            JobRun(job="map_isins", status=JobStatus.PARTIAL, finished_at=t + timedelta(hours=2),
                   details={"attempted": 4, "errors": {"US1": "a", "US2": "b"}}),
            JobRun(job="map_isins", status=JobStatus.PARTIAL, finished_at=t + timedelta(hours=20),
                   details={"attempted": 4, "errors": {"US1": "a"}}),  # still not clean
        ]
    )  # fmt: skip
    db.commit()
    run(db, now=now)
    titles = sorted(n.title for n in alerts(db))
    assert titles == ["Job map_isins finished with errors", "Job map_isins finished with errors"]


def test_http_codes_are_told_apart_but_ports_and_ids_are_not() -> None:
    from quant.ingest.alerts import _normalise

    assert _normalise("HTTP 429") != _normalise("HTTP 503")
    assert _normalise("refused on port 8123 (4f9ac2e1)") == _normalise(
        "refused on port 9456 (a1b2c3d4)"
    )


def test_a_crash_of_the_alerts_job_shows_only_the_kind_of_error_to_the_admins(
    owner: TestClient, db: Session, admin: User
) -> None:
    now = admin.created_at + timedelta(hours=10)
    db.add(JobRun(job=ALERT_JOB_NAME, status=JobStatus.FAILED, finished_at=now - timedelta(hours=1),
                  details={"error": "IntegrityError: duplicate key (user_id)=(secret-id) already exists"}))  # fmt: skip
    db.commit()
    run(db, now=now)
    [n] = alerts(db)
    assert n.body == "IntegrityError" and "secret" not in n.body


def test_ordinary_words_made_of_hex_letters_are_not_taken_for_ids() -> None:
    from quant.ingest.alerts import _normalise

    assert _normalise("defaced feed") == "defaced feed"  # words stay
    assert _normalise("decade") != _normalise("facade")
    assert _normalise("request 4f9ac2e1 failed") == "request # failed"  # an id has digits


def test_a_run_stuck_twice_the_same_day_is_one_alert(
    owner: TestClient, db: Session, admin: User
) -> None:
    day0 = next_berlin_midnight(admin.created_at)  # a fixed day edge, whatever the clock says
    now = day0 + timedelta(hours=40)
    for hours in (8, 12):  # started at different times, never finished
        db.add(JobRun(job="ingest_prices", status=JobStatus.RUNNING,
                      started_at=day0 + timedelta(hours=hours), details={}))  # fmt: skip
    db.commit()
    assert run(db, now=now) == 1


def test_users_holding_nothing_are_not_counted_as_attempted(
    owner: TestClient, db: Session, admin: User
) -> None:
    make_user(db, "empty@example.com")  # daily moves on by default, but nothing held
    rls.bypass(db)
    result = create_alerts(db, TODAY, NOW)
    assert result.attempted == 2  # the admin as a holder, and the job-alerts step


def test_a_partial_run_without_a_count_of_attempts_alerts_on_failures_alone(
    owner: TestClient, db: Session, admin: User
) -> None:
    now = admin.created_at + timedelta(hours=10)
    done = now - timedelta(hours=1)
    db.add_all(
        [
            JobRun(job="few", status=JobStatus.PARTIAL, finished_at=done,
                   details={"errors": {"US1": "x"}}),  # one failure, no count: not enough
            JobRun(job="many", status=JobStatus.PARTIAL, finished_at=done,
                   details={"errors": {f"US{i}": "x" for i in range(5)}}),
        ]
    )  # fmt: skip
    db.commit()
    run(db, now=now)
    assert [n.title for n in alerts(db)] == ["Job many finished with errors"]


def test_a_move_before_the_user_bought_is_not_their_alert(owner: TestClient, db: Session) -> None:
    # Alpha was first bought on 2024-02-01 in the test history. Bars from before that day are a move
    # the user never went through; a move after it is.
    early = date(2024, 1, 10)
    inst = price(db, A, ("108", "100"))
    db.query(PriceEOD).filter_by(instrument_id=inst).delete()
    for day, close in ((early, D("108")), (early - timedelta(1), D("100"))):
        db.add(PriceEOD(instrument_id=inst, date=day, close=close, currency="EUR", source="x"))
    db.commit()
    assert run(db, today=early) == 0
    db.add(
        PriceEOD(
            instrument_id=inst, date=date(2024, 2, 5), close=D("100"), currency="EUR", source="x"
        )
    )
    db.add(
        PriceEOD(
            instrument_id=inst, date=date(2024, 2, 6), close=D("110"), currency="EUR", source="x"
        )
    )
    db.commit()
    assert run(db, today=date(2024, 2, 6)) == 1


def test_the_job_alerts_arrive_even_when_the_price_read_breaks(
    owner: TestClient, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.ingest import alerts as module

    now = admin.created_at + timedelta(hours=10)
    db.add(JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=now - timedelta(hours=1),
                  details={"error": "boom"}))  # fmt: skip
    db.commit()

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("price read broke")

    monkeypatch.setattr(module, "_newest_bars", broken)
    rls.bypass(db)
    with pytest.raises(RuntimeError):  # the moves cannot be made: the job itself fails...
        create_alerts(db, TODAY, now)
    assert [n.title for n in alerts(db)] == ["Job ingest_prices failed"]  # ...the news was stored


def test_a_stuck_run_names_its_start_in_utc(owner: TestClient, db: Session, admin: User) -> None:
    now = admin.created_at + timedelta(days=1)
    started = (now - timedelta(hours=7)).astimezone(UTC)
    db.add(JobRun(job="ingest_prices", status=JobStatus.RUNNING, started_at=started, details={}))
    db.commit()
    run(db, now=now)
    assert f"{started:%Y-%m-%d %H:%M} UTC" in alerts(db)[0].body


def test_a_holding_bought_back_after_the_move_is_not_alerted_for_it(
    client: TestClient, admin: User, db: Session
) -> None:
    login(client, admin.email)
    rebuy = [
        row(80, "2025-05-01", "TRADING", "SELL", "STOCK", "Alpha Corp", A, shares="-6", price="1"),
        row(81, "2026-10-05", "TRADING", "BUY", "STOCK", "Alpha Corp", A, shares="3", price="1"),
    ]
    import_history(client, history(rebuy))  # bought 2024, sold in full 2025, bought again 5 Oct
    inst = price(db, A, ("100",))  # one bar; the others are added below

    def set_bars(*pairs: tuple[date, str]) -> None:
        db.query(PriceEOD).filter_by(instrument_id=inst).delete()
        for day, close in pairs:
            db.add(
                PriceEOD(instrument_id=inst, date=day, close=D(close), currency="EUR", source="x")
            )
        db.commit()

    # +8% from 2 to 3 October: before the buy-back. The first purchase was in 2024, so the first
    # date alone would have raised this alert for a holding that did not exist then.
    set_bars((date(2026, 10, 2), "100"), (date(2026, 10, 3), "108"))
    assert run(db, today=date(2026, 10, 3)) == 0
    # +8% from 5 to 6 October: the new holding began on 5 October, so it went through the move.
    set_bars((date(2026, 10, 5), "100"), (date(2026, 10, 6), "108"))
    assert run(db, today=date(2026, 10, 6)) == 1


def test_rows_are_counted_only_when_the_job_alerts_are_stored(
    owner: TestClient, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.ingest import alerts as module

    second = make_user(db, "second@example.com", Role.ADMIN)
    now = max(admin.created_at, second.created_at) + timedelta(hours=10)
    db.add(JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=now - timedelta(hours=1),
                  details={"error": "boom"}))  # fmt: skip
    db.commit()
    real, calls = module._store, []

    def second_fails(session: Session, rows: list[dict[str, object]]) -> int:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("insert failed")
        return real(session, rows)

    monkeypatch.setattr(module, "_store", second_fails)
    rls.bypass(db)
    result = create_alerts(db, TODAY, now)
    assert result.errors == {"job alerts": "RuntimeError"} and result.rows_written == 0
    # The owner holds shares (that part ran) and the job-alerts step failed: partial.
    assert result.status == JobStatus.PARTIAL
    assert alerts(db) == []


def test_a_failed_job_alert_step_makes_the_run_partial_when_other_work_succeeded(
    owner: TestClient, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.ingest import alerts as module

    price(db, A, ("108", "100"))  # the admin holds Alpha: a move to store
    now = admin.created_at + timedelta(hours=10)
    db.add(JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=now - timedelta(hours=1),
                  details={"error": "boom"}))  # fmt: skip
    db.commit()
    real, calls = module._store, []

    def first_fails(session: Session, rows: list[dict[str, object]]) -> int:
        calls.append(1)
        if len(calls) == 1:  # the job-alerts step stores first
            raise RuntimeError("insert failed")
        return real(session, rows)

    monkeypatch.setattr(module, "_store", first_fails)
    rls.bypass(db)
    result = create_alerts(db, TODAY, now)
    assert result.errors == {"job alerts": "RuntimeError"}
    assert result.rows_written == 1  # the price move was stored
    assert result.status == JobStatus.PARTIAL  # not a clean run, and not a failed one


def test_a_run_that_failed_on_every_item_is_one_alert_a_day_whichever_tickers_it_names(
    owner: TestClient, db: Session, admin: User
) -> None:
    day0 = next_berlin_midnight(admin.created_at)  # a fixed day edge, whatever the clock says
    now = day0 + timedelta(hours=10)
    for hours, tickers in ((2, ("US1", "US2")), (4, ("US7", "US9"))):
        db.add(JobRun(job="ingest_prices", status=JobStatus.FAILED,
                      finished_at=day0 + timedelta(hours=hours),
                      details={"attempted": 2, "errors": {t: "no data" for t in tickers}}))  # fmt: skip
    db.commit()
    assert run(db, now=now) == 1

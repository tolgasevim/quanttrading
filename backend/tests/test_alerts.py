from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ingest.alerts import create_alerts
from quant.models import (
    AlertSettings,
    Instrument,
    JobRun,
    JobStatus,
    Notification,
    PriceEOD,
    User,
)
from quant.portfolio.alerts import (
    Move,
    breaches,
    daily_move,
    in_quiet_hours,
    move_text,
    threshold_for,
)

from .conftest import login, make_user
from .test_holdings_pnl_api import BTC, SPIN, A, B, history, import_history, row

TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)
FUND = "IE0000000030"


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
    assert daily_move(bars("108", "100", step=8), TODAY) is None  # a hole in the data
    assert daily_move(bars("108", "100", step=7), TODAY) is not None  # a long weekend is fine
    assert daily_move(bars("108", "0"), TODAY) is None  # no base to divide by
    assert daily_move(bars("108", "100", last=TODAY + timedelta(days=1)), TODAY) is None  # future
    same_day = [(TODAY, D(108)), (TODAY, D(100))]
    assert daily_move(same_day, TODAY) is None


def test_a_limit_is_breached_at_the_limit_and_beyond_it_in_both_directions() -> None:
    limit = D(5)
    assert breaches(Move(D(5), D(1), D(1), TODAY, TODAY), limit)
    assert breaches(Move(D(-5), D(1), D(1), TODAY, TODAY), limit)
    assert not breaches(Move(D("4.99"), D(1), D(1), TODAY, TODAY), limit)


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


def run(db: Session, today: date = TODAY) -> int:
    rls.bypass(db)
    result = create_alerts(db, today, NOW)
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


def test_a_failed_job_is_an_alert_for_admins_only(owner: TestClient, db: Session) -> None:
    other = make_user(db, "member@example.com")
    now = NOW
    db.add_all(
        [
            JobRun(job="ingest_prices", status=JobStatus.FAILED, finished_at=now,
                   details={"error": "RuntimeError: boom"}),
            JobRun(job="map_isins", status=JobStatus.FAILED, finished_at=now,
                   details={"errors": {"US1": "yahoo: down", "US2": "yahoo: down", "US3": "x"}}),
            JobRun(job="old", status=JobStatus.FAILED, finished_at=now - timedelta(hours=60),
                   details={"error": "long ago"}),
            JobRun(job="running", status=JobStatus.RUNNING, details={}),
            JobRun(job="partial", status=JobStatus.PARTIAL, finished_at=now, details={}),
        ]
    )  # fmt: skip
    db.commit()
    assert run(db) == 2
    rows = alerts(db)
    assert {n.title for n in rows} == {"Job ingest_prices failed", "Job map_isins failed"}
    assert all(n.severity == "warning" and n.kind == "job_failed" for n in rows)
    bodies = {n.title: n.body for n in rows}
    assert bodies["Job ingest_prices failed"] == "RuntimeError: boom"
    assert "3 items failed" in bodies["Job map_isins failed"]
    assert other.id not in {n.user_id for n in rows}
    assert run(db) == 0  # once per failed run


def test_one_users_failure_does_not_stop_the_others(
    owner: TestClient, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.ingest import alerts as module

    make_user(db, "member@example.com")
    price(db, A, ("108", "100"))
    real = module._user_moves
    calls: list[str] = []

    def flaky(
        session: Session, user: User, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        calls.append(user.email)
        if user.email == "member@example.com":
            raise RuntimeError("bad data")
        return real(session, user, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "_user_moves", flaky)
    rls.bypass(db)
    result = create_alerts(db, TODAY, NOW)
    assert len(result.errors) == 1 and "bad data" in next(iter(result.errors.values()))
    assert len(alerts(db)) == 1 and len(calls) == 2  # the owner still got theirs


def test_the_alert_job_is_recorded_like_the_others(owner: TestClient, db: Session) -> None:
    from quant import worker

    worker.run_alerts()
    run_ = db.query(JobRun).filter_by(job=worker.ALERT_JOB).one()
    assert run_.status == JobStatus.SUCCESS and run_.rows_written == 0

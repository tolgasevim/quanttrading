from datetime import UTC, date, datetime
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from quant import rls
from quant.ai import scoring
from quant.models import AiPick, AiPickScore, FxRate, Instrument, JobStatus, PriceEOD, User

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
    db: Session, user: User, when: date, direction: str = "buy", isin: str | None = ISIN, **kw: Any
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
    start = date(2026, 5, 1)  # the Friday before the pick (Monday 4 May): no look-ahead
    one = date(2026, 6, 4)
    three = date(2026, 8, 4)
    # Alpha +20 % after a month, +50 % after three; the benchmark +10 % and +12 %.
    later = date(2026, 9, 1)  # the prices run past the end of the 3-month window
    bars(db, alpha, {start: "100", one: "120", three: "150", later: "160"})
    bars(db, bench, {start: "200", one: "220", three: "224", later: "230"})
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
    bars(db, alpha, {date(2026, 11, 4): "90", date(2026, 11, 5): "91"})
    bars(db, bench, {date(2026, 11, 4): "230", date(2026, 11, 5): "231"})
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
    from quant.worker import run_scoring  # imported here: see the note in conftest on loggers

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


def test_a_stale_close_is_not_locked_in_when_the_prices_stop_early(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    alpha, bench = market
    pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    # The 1-month window ends on 4 June. Remove every close after 2 June: an outage of the
    # price job. The nearest close is only 2 days old, but the prices do not run past the day.
    db.execute(text("DELETE FROM prices_eod WHERE date > '2026-06-02'"))
    db.add(
        PriceEOD(
            instrument_id=alpha.id,
            date=date(2026, 6, 2),
            close=D("119"),
            currency="EUR",
            source="t",
        )
    )
    db.add(PriceEOD(instrument_id=bench.id, date=date(2026, 6, 2), close=D("219"), currency="EUR", source="t"))  # fmt: skip
    db.commit()
    assert scoring.score_picks(db, TODAY).rows_written == 0
    assert db.scalars(select(AiPickScore)).all() == []


def test_one_instrument_scores_every_window_of_a_pick(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    alpha, _ = market
    # A second candidate (same ticker as the pick's) with other prices must not be mixed in.
    other = instrument(db, "ALPHA2")
    bars(db, other, {date(2026, 5, 1): "10", date(2026, 6, 4): "11", date(2026, 9, 1): "12"})
    pick(db, admin, date(2026, 5, 4), isin=ISIN, ticker="alpha2")
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    assert {s.pick_return_pct for s in db.scalars(select(AiPickScore))} == {
        D("20.0000"),
        D("50.0000"),
    }


def test_the_day_of_a_pick_is_the_berlin_day(db: Session, admin: User) -> None:
    rls.bypass(db)
    late = AiPick(
        user_id=admin.id, created_at=datetime(2026, 5, 3, 22, 30, tzinfo=UTC), question="q",
        name="n", direction="buy", horizon_months=1, rationale="r", held=False,
    )  # fmt: skip
    assert scoring.pick_day(late) == date(2026, 5, 4)  # 00:30 in Berlin


def test_a_skipped_duplicate_is_not_counted(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    row = pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    db.add(
        AiPickScore(
            user_id=admin.id, pick_id=row.id, window_months=1, start_date=date(2026, 5, 4),
            end_date=date(2026, 6, 4), pick_return_pct=D("1"), benchmark_return_pct=D("1"),
            excess_pct=D("0"), hit=False,
        )
    )  # fmt: skip
    db.commit()
    assert scoring.score_picks(db, TODAY).rows_written == 1  # only the 3-month window is new


def test_a_dollar_pick_is_judged_in_euros(db: Session, admin: User) -> None:
    usd = Instrument(
        code="USDCO", isin="US0000000077", name="Usd", asset_class="stock", currency="USD"
    )
    db.add(usd)
    bench = instrument(db, scoring.BENCHMARK_CODE, "IE00B53SZB19")
    db.flush()
    days = {date(2026, 5, 1): "100", date(2026, 6, 4): "110", date(2026, 6, 10): "110"}
    for day, close in days.items():
        db.add(PriceEOD(instrument_id=usd.id, date=day, close=D(close), currency="USD", source="t"))
        db.add(
            PriceEOD(instrument_id=bench.id, date=day, close=D("200"), currency="EUR", source="t")
        )
    # The dollar loses 10 % against the euro over the month: 1 EUR = 1.00 USD, then 1.10 USD.
    db.add(FxRate(quote="USD", date=date(2026, 5, 1), rate=D("1.0"), source="ecb"))
    db.add(FxRate(quote="USD", date=date(2026, 6, 4), rate=D("1.1"), source="ecb"))
    db.commit()
    pick(db, admin, date(2026, 5, 4), isin="US0000000077")
    rls.bypass(db)
    scoring.score_picks(db, date(2026, 6, 10))
    one = db.scalars(select(AiPickScore)).one()
    assert one.pick_return_pct == D("0.0000")  # +10 % in dollars is flat in euros
    assert one.hit is False and one.benchmark_return_pct == D("0.0000")


def test_a_day_without_an_exchange_rate_waits(db: Session, admin: User) -> None:
    usd = Instrument(
        code="USDCO", isin="US0000000077", name="Usd", asset_class="stock", currency="USD"
    )
    db.add(usd)
    bench = instrument(db, scoring.BENCHMARK_CODE, "IE00B53SZB19")
    db.flush()
    for day in (date(2026, 5, 1), date(2026, 6, 4), date(2026, 6, 10)):
        db.add(PriceEOD(instrument_id=usd.id, date=day, close=D("100"), currency="USD", source="t"))
        db.add(
            PriceEOD(instrument_id=bench.id, date=day, close=D("200"), currency="EUR", source="t")
        )
    db.commit()
    pick(db, admin, date(2026, 5, 4), isin="US0000000077")
    rls.bypass(db)
    assert scoring.score_picks(db, date(2026, 6, 10)).rows_written == 0


def test_the_price_stored_on_the_pick_is_the_start(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(
        db,
        admin,
        date(2026, 5, 4),
        price=D("80"),
        price_currency="EUR",
        price_date=date(2026, 5, 1),
    )
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    one = db.scalars(select(AiPickScore).where(AiPickScore.window_months == 1)).one()
    assert one.pick_return_pct == D("50.0000")  # 80 -> 120, not 100 -> 120
    assert one.start_date == date(2026, 5, 1)


def test_the_close_of_the_pick_day_is_not_the_start(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    alpha, _ = market
    bars(
        db, alpha, {date(2026, 5, 4): "130"}
    )  # the day of the pick: a jump the pick must not claim
    pick(db, admin, date(2026, 5, 4))
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    one = db.scalars(select(AiPickScore).where(AiPickScore.window_months == 1)).one()
    assert one.start_date == date(2026, 5, 1) and one.pick_return_pct == D("20.0000")


def test_the_nightly_price_job_tracks_what_the_ai_recommended(db: Session, admin: User) -> None:
    from quant.ingest.mapping import held_isins

    pick(db, admin, date(2026, 5, 4), isin="US67066G1040")
    pick(db, admin, date(2026, 5, 4), isin="XF000BTC0017")  # a coin's pseudo-ISIN: no lookup
    pick(db, admin, date(2026, 5, 4), isin="US0000000555")  # the check digit is wrong: invented
    rls.bypass(db)
    found = {h.isin: h for h in held_isins(db)}
    assert found["US67066G1040"].asset_class == "stock"
    assert "XF000BTC0017" not in found and "US0000000555" not in found


def test_valid_isins_have_the_right_check_digit() -> None:
    from quant.ingest.mapping import valid_isin

    for good in ("US67066G1040", "IE00B53SZB19", "DE0007164600"):
        assert valid_isin(good)
    for bad in ("US67066G1041", "US0000000001", "us67066g1040", "US67066G104", ""):
        assert not valid_isin(bad)


def test_an_instrument_with_no_prices_of_its_own_is_not_chosen(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    empty = instrument(db, "EMPTY", "US67066G1040")
    db.commit()
    # The pick names the empty instrument by ISIN and the priced one by ticker, and carries a price.
    pick(db, admin, date(2026, 5, 4), isin=empty.isin, ticker="alpha", price=D("100"),
         price_currency="EUR", price_date=date(2026, 5, 1))  # fmt: skip
    rls.bypass(db)
    assert scoring.score_picks(db, TODAY).rows_written == 2  # scored on ALPHA


def test_one_bad_pick_does_not_stop_the_others(
    db: Session, admin: User, market: tuple[Instrument, Instrument], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = pick(db, admin, date(2026, 5, 4))
    second = pick(db, admin, date(2026, 5, 4))
    real = scoring.outcome

    def flaky(session: Session, p: AiPick, *a: Any) -> Any:
        if p.id == first.id:
            raise RuntimeError("boom")
        return real(session, p, *a)

    monkeypatch.setattr(scoring, "outcome", flaky)
    rls.bypass(db)
    result = scoring.score_picks(db, TODAY)
    assert result.errors == {str(first.id): "RuntimeError"} and result.rows_written == 2
    assert {s.pick_id for s in db.scalars(select(AiPickScore))} == {second.id}


def test_an_absurd_return_is_a_data_error_not_a_score() -> None:
    assert scoring._pct(D("0.000001"), D("1000")) is None
    assert scoring._pct(D("100"), D("120")) == D("20.0000")


def test_the_benchmark_starts_with_the_pick(
    db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    alpha, bench = market
    bars(db, bench, {date(2026, 5, 4): "400", date(2026, 4, 30): "190"})  # 4 May must not be used
    pick(
        db,
        admin,
        date(2026, 5, 4),
        price=D("100"),
        price_currency="EUR",
        price_date=date(2026, 4, 30),
    )
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    one = db.scalars(select(AiPickScore).where(AiPickScore.window_months == 1)).one()
    assert one.start_date == date(2026, 4, 30)
    # From the 30 April close (190), the pick's own start day, to 220.
    assert one.benchmark_return_pct == D("15.7895")


def test_the_edge_flips_for_a_sell(
    client: TestClient, db: Session, admin: User, market: tuple[Instrument, Instrument]
) -> None:
    pick(db, admin, date(2026, 5, 4), direction="buy")
    pick(db, admin, date(2026, 5, 4), direction="sell")
    rls.bypass(db)
    scoring.score_picks(db, TODAY)
    login(client, admin.email)
    w = {x["window_months"]: x for x in client.get("/api/ai/track-record").json()["windows"]}
    # Alpha beat the benchmark by 10 points: +10 for the buy, -10 for the sell.
    assert w[1]["scored"] == 2 and w[1]["hits"] == 1 and w[1]["avg_excess_pct"] == "0.00"

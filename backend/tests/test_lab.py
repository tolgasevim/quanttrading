"""The backtesting lab and the swing scanner (Phase 3 and 4). The prices are synthetic, and the
expected numbers are worked out by hand in the comments."""

from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.models import FactorScore, Instrument, PriceEOD, SwingGate, SwingSignal
from quant.portfolio import backtest, swing
from quant.portfolio.performance import Prices

from .conftest import login, make_user
from .test_factors import make_isin

BASE = date(2025, 1, 1)


def day(i: int) -> date:
    return BASE + timedelta(days=i)


def share(db: Session, code: str, n: int = 0, asset_class: str = "stock") -> Instrument:
    inst = Instrument(
        code=code,
        isin=make_isin(n) if n else None,
        name=f"{code} Inc",
        asset_class=asset_class,
        currency="EUR",
        symbols={},
    )
    db.add(inst)
    db.commit()
    return inst


def prices_for(db: Session, inst: Instrument, closes: list[float]) -> None:
    for i, c in enumerate(closes):
        db.add(
            PriceEOD(
                instrument_id=inst.id,
                date=day(i),
                close=D(str(round(c, 4))),
                currency="EUR",
                source="test",
            )
        )
    db.commit()


def growth(n: int, daily: float, start: float = 100.0) -> list[float]:
    return [start * (1 + daily) ** i for i in range(n)]


def params(**kw: Any) -> backtest.Params:
    base: dict[str, Any] = dict(
        strategy="buy_hold", start=date(2025, 6, 1), end=date(2026, 1, 31), capital=D(10000),
        fee=D(1), tax_pct=D("26.375"), top_n=1, benchmark="BM", universe=("BM",),
    )  # fmt: skip
    base.update(kw)
    return backtest.Params(**base)


# --- the pure pieces -----------------------------------------------------------------------------


def test_month_ends_are_the_last_trading_day_of_each_month() -> None:
    days = [
        date(2025, 1, 30),
        date(2025, 1, 31),
        date(2025, 2, 3),
        date(2025, 2, 28),
        date(2025, 3, 3),
    ]
    assert backtest.month_ends(days) == [date(2025, 1, 31), date(2025, 2, 28), date(2025, 3, 3)]


def test_trend_weight_needs_200_closes_and_a_close_above_their_average() -> None:
    up, down = Prices(), Prices()
    for i, c in enumerate(growth(250, 0.001)):
        up.add("X", day(i), D(str(c)))
    for i, c in enumerate(growth(250, -0.001)):
        down.add("X", day(i), D(str(c)))
    assert backtest.trend_weight(up, "X", day(249)) == 1
    assert backtest.trend_weight(down, "X", day(249)) == 0
    assert backtest.trend_weight(up, "X", day(150)) == 0  # fewer than 200 closes: no view
    assert backtest.trend_weight(up, "NOPE", day(249)) == 0


def test_momentum_picks_the_best_twelve_one_month_return_and_skips_short_histories() -> None:
    p = Prices()
    for code, rate, length in (
        ("A", 0.003, 420),
        ("B", 0.001, 420),
        ("C", -0.001, 420),
        ("NEW", 0.01, 100),
    ):
        for i, c in enumerate(growth(length, rate)):
            p.add(code, day(420 - length + i), D(str(c)))
    d = day(419)
    assert backtest.momentum_pick(p, ("A", "B", "C", "NEW"), d, 1) == {"A": D(1)}
    assert backtest.momentum_pick(p, ("A", "B", "C", "NEW"), d, 2) == {"A": D("0.5"), "B": D("0.5")}
    assert backtest.momentum_pick(p, ("NEW",), d, 1) == {}  # under a year of prices


def test_score_pick_takes_the_stored_ranking_and_stays_in_cash_when_thin() -> None:
    s = backtest.Scores(by_day={date(2026, 1, 5): {"A": D(90), "B": D(70), "C": D(80)}})
    assert backtest.score_pick(s, date(2026, 1, 6), 2) == {"A": D("0.5"), "C": D("0.5")}
    assert backtest.score_pick(s, date(2026, 1, 6), 4) == {}  # fewer names than asked
    assert backtest.score_pick(s, date(2026, 2, 1), 1) == {}  # the newest score is too old
    assert backtest.score_pick(s, date(2026, 1, 1), 1) == {}  # no score yet that day


def test_the_curve_is_thinned_but_keeps_its_last_point() -> None:
    rows = [(day(i), float(i), float(i)) for i in range(1000)]
    thin = backtest._downsample(rows)
    assert len(thin) == backtest.CURVE_POINTS + 1 and thin[-1] == rows[-1] and thin[0] == rows[0]
    short = rows[:50]
    assert backtest._downsample(short) == short


# --- the book: fees, tax and the loss carry (worked by hand) ---------------------------------------


def book_with(*quotes: tuple[int, str]) -> tuple[backtest._Book, Prices]:
    p = Prices()
    for i, price in quotes:
        p.add("X", day(i), D(price))
    return backtest._Book(D(1000), D(1), D("26.375")), p


def test_a_buy_pays_the_fee_and_leaves_no_cash() -> None:
    book, p = book_with((1, "10"))
    book.rebalance(p, day(1), {"X": D(1)})
    # 1,000 less the 1 fee buys 999 of shares: 99.9 shares at 10.
    assert book.pos["X"].shares == D("99.9") and book.pos["X"].cost == D(999)
    assert book.cash == 0 and book.run.trades == 1 and book.run.fees == 1


def test_a_gain_is_taxed_when_the_shares_are_sold() -> None:
    book, p = book_with((1, "10"), (2, "20"))
    book.rebalance(p, day(1), {"X": D(1)})
    book.rebalance(p, day(2), {})
    # 99.9 shares sell for 1,998 (cost 999): a gain of 999, tax 26.375% = 263.48625, fee 1.
    assert book.run.tax == D("263.48625")
    assert book.cash == D(1998) - 1 - D("263.48625")
    assert book.pos["X"].shares == 0 and book.run.trades == 2 and book.run.fees == 2


def test_a_loss_is_carried_forward_against_a_later_gain() -> None:
    book, p = book_with((1, "10"), (2, "5"), (3, "5"), (4, "10"))
    book.rebalance(p, day(1), {"X": D(1)})  # 99.9 shares for 999
    book.rebalance(p, day(2), {})  # sold for 499.5: a loss of 499.5, no tax
    assert book.run.tax == 0 and book.loss_pot == D("499.5") and book.cash == D("498.5")
    book.rebalance(p, day(3), {"X": D(1)})  # 497.5 buys 99.5 shares at 5
    book.rebalance(p, day(4), {})  # sold for 995: a gain of 497.5, all covered by the pot
    assert book.run.tax == 0 and book.loss_pot == D("2.0")


def test_a_rebalance_with_nothing_to_change_costs_nothing() -> None:
    book, p = book_with((1, "10"), (2, "10.05"))
    book.rebalance(p, day(1), {"X": D(1)})
    fees = book.run.fees
    book.rebalance(p, day(2), {"X": D(1)})
    assert book.run.fees == fees and book.run.trades == 1 and book.run.rebalances == 2


def test_cash_that_cannot_cover_the_fee_buys_nothing() -> None:
    book = backtest._Book(D("0.5"), D(1), D(0))
    p = Prices()
    p.add("X", day(1), D(10))
    book.rebalance(p, day(1), {"X": D(1)})
    assert book.run.trades == 0 and book.cash == D("0.5")


# --- the engine reproduces a known strategy -----------------------------------------------------------


def test_buy_and_hold_reproduces_the_closed_form(db: Session) -> None:
    """The Phase 3 exit test: a known strategy comes out as it must, to the cent."""
    bm = share(db, "BM")
    closes = growth(400, 0.001)
    prices_for(db, bm, closes)
    result = backtest.run_backtest(db, params())
    first, last = date(2025, 6, 1), date(2026, 1, 31)
    p0 = D(str(round(closes[(first - BASE).days], 4)))
    pt = D(str(round(closes[(last - BASE).days], 4)))
    expected = (D(10000) - 1) / p0 * pt  # one fee, then held
    for stats in (result.strategy, result.benchmark):
        assert abs(stats.final_value - expected) < D("0.0001")
        assert stats.trades == 1 and stats.fees == 1 and stats.tax == 0
    assert result.first_day == first and result.last_day == last
    assert result.strategy.cagr_pct is not None and result.strategy.cagr_pct > 0
    assert result.strategy.max_drawdown_pct == 0  # a smooth climb never falls
    assert (
        result.curve[0][1] == pytest.approx(9999.0, abs=0.01)
        and result.curve[0][1] == result.curve[0][2]
    )
    assert any("Survivorship" in w for w in result.warnings)


def test_momentum_holds_the_leader_and_beats_a_slow_benchmark(db: Session) -> None:
    for code, rate in (("A", 0.003), ("B", 0.001), ("C", -0.001), ("BM", 0.0005)):
        prices_for(db, share(db, code), growth(480, rate))
    result = backtest.run_backtest(
        db,
        params(
            strategy="momentum",
            start=date(2026, 2, 1),
            end=date(2026, 4, 20),
            universe=("A", "B", "C"),
        ),
    )
    assert result.strategy.trades == 1  # bought A once, and a drift alone causes no trade
    assert result.strategy.final_value > result.benchmark.final_value
    assert result.universe_size == 3 and result.strategy.sharpe is not None


def test_trend_steps_aside_in_a_crash(db: Session) -> None:
    closes = growth(300, 0.002) + [growth(300, 0.002)[-1] * 0.99 ** (k + 1) for k in range(190)]
    prices_for(db, share(db, "X"), closes)
    result = backtest.run_backtest(
        db,
        params(
            strategy="trend",
            benchmark="X",
            universe=("X",),
            start=date(2025, 10, 1),
            end=date(2026, 3, 31),
        ),
    )
    assert result.strategy.trades == 2  # in at the end of October, out at the end of November
    assert result.strategy.final_value > result.benchmark.final_value * 2
    assert (
        result.strategy.max_drawdown_pct is not None
        and result.benchmark.max_drawdown_pct is not None
    )
    assert result.strategy.max_drawdown_pct < result.benchmark.max_drawdown_pct
    assert result.strategy.tax == 0  # it sold at a loss


def put_scores(
    db: Session, rows: dict[str, float], first: date, days: int, quality: str = "ok"
) -> None:
    for code, composite in rows.items():
        isin = db.scalars(select(Instrument.isin).where(Instrument.code == code)).one()
        for i in range(days):
            db.add(
                FactorScore(
                    isin=isin, day=first + timedelta(days=i), composite=D(str(composite)),
                    data_quality=quality, flags=[], peers=3,
                )
            )  # fmt: skip
    db.commit()


def test_the_score_rule_replays_the_stored_history_from_its_first_day(db: Session) -> None:
    rls.bypass(db)
    for n, (code, rate) in enumerate((("A", 0.003), ("B", 0.001), ("BM", 0.0005)), start=1):
        prices_for(db, share(db, code, n if code != "BM" else 0), growth(480, rate))
    put_scores(db, {"A": 90, "B": 60}, date(2026, 1, 1), 120)
    result = backtest.run_backtest(
        db,
        params(
            strategy="score", start=date(2025, 12, 1), end=date(2026, 4, 20), universe=("A", "B")
        ),
    )
    assert result.first_day == date(2026, 1, 1)
    assert any("first day with stored scores" in n for n in result.notes)
    assert (
        result.strategy.trades == 1 and result.strategy.final_value > result.benchmark.final_value
    )


def test_the_score_rule_refuses_missing_thin_or_poor_history(db: Session) -> None:
    rls.bypass(db)
    for n, code in enumerate(("A", "BM"), start=1):
        prices_for(db, share(db, code, n if code != "BM" else 0), growth(480, 0.001))
    ask = params(strategy="score", start=date(2026, 1, 1), end=date(2026, 4, 20), universe=("A",))
    with pytest.raises(backtest.BacktestError, match="no stored scores"):
        backtest.run_backtest(db, ask)
    put_scores(db, {"A": 80}, date(2026, 1, 1), 20)
    with pytest.raises(backtest.BacktestError, match="too short"):
        backtest.run_backtest(db, ask)
    db.query(FactorScore).update({"data_quality": "poor"})  # gaps rank nothing
    db.commit()
    with pytest.raises(backtest.BacktestError, match="no stored scores"):
        backtest.run_backtest(db, ask)


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"strategy": "magic"}, "the rule must be"),
        ({"end": date(2025, 6, 1)}, "end must be after"),
        ({"capital": D(0)}, "capital must be positive"),
        ({"fee": D(-1)}, "must not be negative"),
        ({"tax_pct": D(101)}, "at most 100"),
        ({"top_n": 0}, "at least 1"),
        ({"strategy": "momentum", "top_n": 5, "universe": ("A",)}, "fewer names"),
        ({"strategy": "trend", "universe": ("A", "B")}, "exactly one"),
    ],
)
def test_a_test_that_cannot_run_says_why(change: dict[str, object], words: str) -> None:
    with pytest.raises(backtest.BacktestError, match=words):
        backtest.check(params(**change))


def test_a_benchmark_with_too_little_history_is_refused(db: Session) -> None:
    prices_for(db, share(db, "BM"), growth(30, 0.001))
    with pytest.raises(backtest.BacktestError, match="only 30 prices"):
        backtest.run_backtest(db, params(start=date(2025, 1, 1), end=date(2025, 3, 1)))


# --- the API --------------------------------------------------------------------------------------------


def test_the_lab_lists_the_rules_and_what_it_can_use(owner: TestClient, db: Session) -> None:
    prices_for(db, share(db, "SXRV"), growth(100, 0.001))
    prices_for(db, share(db, "ACME", 1), growth(100, 0.001))
    out = owner.get("/api/lab").json()
    assert set(out["strategies"]) == {"buy_hold", "trend", "momentum", "score"}
    assert out["benchmarks"] == ["SXRV"] and out["universe"] == 2  # both are stocks here
    assert out["held"] == 0 and out["watchlist"] == 0 and out["score_first_day"] is None


def test_a_backtest_over_the_api(owner: TestClient, db: Session) -> None:
    prices_for(db, share(db, "SXRV"), growth(400, 0.001))
    out = owner.post(
        "/api/lab/backtest",
        json={"strategy": "buy_hold", "start": "2025-06-01", "end": "2026-01-31"},
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["strategy"]["trades"] == 1 and len(body["curve"]) > 100
    assert body["first_day"] == "2025-06-01" and any(
        "not a forecast" in w for w in body["warnings"]
    )
    assert float(body["strategy"]["final_value"]) == pytest.approx(
        float(body["benchmark"]["final_value"])
    )


@pytest.mark.parametrize(
    ("body", "words"),
    [
        ({"strategy": "trend"}, "pick the instrument"),
        ({"strategy": "trend", "instrument": "NOPE"}, "does not know"),
        ({"strategy": "momentum", "universe": "held"}, "fewer names"),
        ({"strategy": "momentum", "universe": "codes", "codes": ["GHOST"]}, "does not know"),
        ({"strategy": "buy_hold", "benchmark": "GHOST"}, "does not know"),
    ],
)
def test_a_bad_request_gets_a_plain_422(
    owner: TestClient, db: Session, body: dict[str, object], words: str
) -> None:
    prices_for(db, share(db, "SXRV"), growth(400, 0.001))
    out = owner.post("/api/lab/backtest", json={"start": "2025-06-01", "end": "2026-01-31", **body})
    assert out.status_code == 422 and words in out.text


def test_the_lab_needs_the_disclaimer_and_valid_numbers(
    owner: TestClient, client: TestClient, db: Session
) -> None:
    bad = owner.post(
        "/api/lab/backtest",
        json={"strategy": "buy_hold", "start": "2025-06-01", "end": "2026-01-31", "capital": 0},
    )
    assert bad.status_code == 422
    make_user(db, "new@example.com", accepted=False)
    client.cookies.clear()
    assert login(client, "new@example.com") == 200
    assert client.get("/api/lab").status_code == 403
    assert client.get("/api/lab/swing").status_code == 403


# --- the swing indicators and signals ------------------------------------------------------------------


def bars_of(closes: list[float], spread: float = 0.001) -> list[swing.Bar]:
    return [swing.Bar(day(i), c * (1 + spread), c * (1 - spread), c) for i, c in enumerate(closes)]


def test_the_indicators() -> None:
    assert swing.sma([1.0, 2.0, 3.0, 4.0], 2, 3) == 3.5 and swing.sma([1.0], 2, 0) is None
    flat = [swing.Bar(day(i), 11.0, 9.0, 10.0) for i in range(20)]
    assert swing.atr(flat, 19) == pytest.approx(2.0)  # a range of 2 every day, no gaps
    assert swing.atr(flat, 5) is None  # fewer than 14 bars
    assert swing.rsi([10.0, 11.0, 10.0], 2) == pytest.approx(50.0)
    assert swing.rsi([10.0, 9.0, 8.0], 2) == 0.0 and swing.rsi([8.0, 9.0, 10.0], 2) == 100.0
    assert swing.rsi([1.0, 2.0], 1) is None  # too early for two changes


def trend_series() -> list[float]:
    """Rising for 259 days, a dip on day 258 and a jump above the 50-day average on day 259."""
    closes = [100 + 0.5 * i for i in range(260)]
    closes[258], closes[259] = 150.0, 300.0
    return closes


def test_the_trend_signal_fires_on_the_cross_above_the_fast_average() -> None:
    closes = trend_series()
    assert swing.signal_at("trend", closes, 259) is True
    assert swing.signal_at("trend", closes, 258) is False  # still under the average
    assert swing.signal_at("trend", closes, 257) is False  # above it, but no cross
    assert swing.signal_at("trend", closes, 150) is False  # under 200 days of history


def test_the_reversion_signal_needs_a_short_dip_in_an_uptrend() -> None:
    closes = [100 + float(i) for i in range(260)] + [349.0, 339.0]  # two falls after a climb
    assert (
        swing.signal_at("reversion", closes, 261) is True
    )  # RSI(2) is 0, above the 200-day average
    assert swing.signal_at("reversion", closes, 259) is False  # RSI(2) is 100
    falling = [400 - float(i) for i in range(262)]
    assert swing.signal_at("reversion", falling, 261) is False  # RSI is low, but under the average
    with pytest.raises(ValueError, match="unknown signal"):
        swing.signal_at("magic", closes, 261)


def test_the_levels_are_worked_from_the_atr() -> None:
    assert swing.levels("trend", 100.0, 2.0) == (96.0, 106.0, 30)
    assert swing.levels("reversion", 100.0, 2.0) == (96.0, 103.0, 10)


def test_a_trade_that_reaches_its_target_is_counted_net_of_costs() -> None:
    closes = trend_series() + [300 + 5.0 * k for k in range(1, 41)]
    bars = bars_of(closes)
    a = swing.atr(bars, 259)
    assert a is not None
    entry = bars[260].close
    trades = swing.replay("trend", bars)
    assert len(trades) == 1 and trades[0].day == day(259)
    assert trades[0].ret == pytest.approx((entry + 3 * a) / entry - 1 - swing.COST)


def test_a_stop_is_taken_first_when_both_levels_are_in_one_bar() -> None:
    closes = trend_series() + [305.0, 305.0, 305.0]
    bars = bars_of(closes)
    a = swing.atr(bars, 259)
    assert a is not None
    entry = bars[260].close
    bars[261] = swing.Bar(day(261), entry + 500, entry - 500, 305.0)  # reaches both levels
    trades = swing.replay("trend", bars)
    assert len(trades) == 1
    assert trades[0].ret == pytest.approx((entry - 2 * a) / entry - 1 - swing.COST)


def test_a_trade_that_goes_nowhere_leaves_at_the_time_limit() -> None:
    flat = [300.0] * 45
    bars = bars_of(trend_series() + flat, spread=0.00001)
    trades = swing.replay("trend", bars)
    assert len(trades) == 1 and trades[0].ret == pytest.approx(-swing.COST, abs=1e-4)


def trades_of(rets: list[float]) -> list[swing.Trade]:
    return [swing.Trade(day(i), r) for i, r in enumerate(rets)]


def test_a_signal_type_passes_only_with_every_criterion() -> None:
    good = swing.gate_stats(trades_of([0.02] * 36 + [-0.01] * 4))
    assert good.passed and good.n_trades == 40 and good.profit_factor == pytest.approx(18.0)
    assert good.first_half is not None and good.second_half is not None
    assert swing.gate_stats(trades_of([0.03] * 20)).passed is False  # too few trades
    assert swing.gate_stats(trades_of([0.001] * 40)).passed is False  # profit too small
    drift = swing.gate_stats(trades_of([0.03] * 20 + [-0.005] * 20))
    assert drift.expectancy > 0.3 and drift.passed is False  # the second half loses
    assert drift.second_half is not None and drift.second_half < 0
    assert swing.gate_stats(trades_of([0.05, -0.05] * 20)).passed is False  # profit factor 1.0
    assert swing.gate_stats([]).passed is False and swing.gate_stats([]).n_trades == 0


def test_the_criteria_are_stated_in_words() -> None:
    assert "30 trades" in swing.CRITERIA_TEXT and "each half" in swing.CRITERIA_TEXT


# --- the nightly scan and the page --------------------------------------------------------------------------


TODAY = day(259)


def seed_scan(db: Session) -> Instrument:
    inst = share(db, "ACME", 1)
    for i, c in enumerate(trend_series()):
        db.add(
            PriceEOD(
                instrument_id=inst.id, date=day(i), close=D(str(c)), high=D(str(c * 1.001)),
                low=D(str(c * 0.999)), currency="EUR", source="test",
            )
        )  # fmt: skip
    db.commit()
    return inst


def test_the_scan_stores_the_gates_and_tonights_signal(db: Session) -> None:
    rls.bypass(db)
    seed_scan(db)
    short = share(db, "TINY", 2)
    prices_for(db, short, growth(50, 0.001))  # too little history: not replayed
    out = swing.scan(db, TODAY)
    assert out.rows_written == 3 and out.attempted == 2  # two gates and one signal
    gates = {g.signal: g for g in db.scalars(select(SwingGate))}
    assert set(gates) == {"trend", "reversion"} and gates["trend"].universe == 1
    assert gates["trend"].passed is False and gates["trend"].n_trades == 0
    [signal] = db.scalars(select(SwingSignal)).all()
    assert (signal.code, signal.signal, signal.day) == ("ACME", "trend", TODAY)
    assert signal.entry == D("300") and signal.stop < signal.entry < signal.target
    assert signal.max_hold == 30 and signal.atr > 0
    again = swing.scan(db, TODAY)  # a rerun the same day replaces, never doubles
    assert again.rows_written == 3
    assert (
        len(db.scalars(select(SwingGate)).all()) == 2
        and len(db.scalars(select(SwingSignal)).all()) == 1
    )


def test_an_old_bar_gives_no_signal(db: Session) -> None:
    rls.bypass(db)
    seed_scan(db)
    swing.scan(db, TODAY + timedelta(days=10))
    assert db.scalars(select(SwingSignal)).all() == []


def test_signals_of_a_failed_type_are_hidden_and_a_passed_type_shows_levels(
    owner: TestClient, db: Session
) -> None:
    rls.bypass(db)
    seed_scan(db)
    swing.scan(db, TODAY)
    watch = owner.post("/api/scores/watchlist", json={"isin": make_isin(1)})
    assert watch.status_code == 201
    out = owner.get("/api/lab/swing").json()
    assert out["signals"] == [] and out["hidden"] == 1  # the trend type has not passed
    assert {g["signal"] for g in out["gates"]} == {"trend", "reversion"}
    assert all(g["passed"] is False for g in out["gates"]) and "30 trades" in out["criteria"]
    assert any("not advice" in n for n in out["notes"])
    gate = db.scalars(select(SwingGate).where(SwingGate.signal == "trend")).one()
    gate.passed = True
    db.commit()
    shown = owner.get("/api/lab/swing").json()
    [sig] = shown["signals"]
    assert (sig["code"], sig["signal"], sig["watched"], sig["held"]) == (
        "ACME",
        "trend",
        True,
        False,
    )
    assert float(sig["risk_pct"]) > 0 and float(sig["reward_pct"]) > float(sig["risk_pct"])


def test_the_scanner_shows_signals_of_my_names_unless_asked_for_all(
    owner: TestClient, db: Session
) -> None:
    rls.bypass(db)
    seed_scan(db)
    swing.scan(db, TODAY)
    gate = db.scalars(select(SwingGate).where(SwingGate.signal == "trend")).one()
    gate.passed = True
    db.commit()
    assert owner.get("/api/lab/swing").json()["signals"] == []  # not on my watchlist or held
    assert len(owner.get("/api/lab/swing?scope=all").json()["signals"]) == 1


def test_before_the_first_scan_the_page_is_empty(owner: TestClient) -> None:
    out = owner.get("/api/lab/swing").json()
    assert out["scanned_on"] is None and out["gates"] == [] and out["signals"] == []

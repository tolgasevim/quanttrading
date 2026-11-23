"""The swing signal scanner (Phase 4, PRD §6.9), gated by backtests.

Two end-of-day signals, each with an entry, a stop and a target:

- `trend`: the close crosses above its 50-day average while the 50-day average is above the
  200-day one. Stop 2 ATR below the entry, target 3 ATR above, at most 30 days.
- `reversion`: RSI(2) under 10 while the close is above its 200-day average (a dip in an
  uptrend). Stop 2 ATR below, target 1.5 ATR above, at most 10 days.

**A signal type is shown only if its backtest passes** (`passes`): the same rule is replayed over
the stored history of every share and fund the app prices, one trade at a time (no overlapping
trades in one instrument), with costs and a slippage allowance, and it must show enough trades, a
positive expectancy after costs, a profit factor over 1.3 and a positive expectancy in *both*
halves of the history (a crude out-of-sample check). A type that fails is listed with its numbers
and its signals are hidden. The gates are recomputed every night, so a type can pass and later
fail.

Honest limits, repeated on the page: daily bars only. A stop and a target hit in the same bar
count as the stop, and a gap through a stop is filled at the stop, which is optimistic. The entry
is the close of the day after the signal. The history is what the app stores (survivorship bias).
A pass is evidence about the past, not a forecast. The scanner places no trade and the levels are
not advice.
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.models import Instrument, PriceEOD, SwingGate, SwingSignal

SIGNALS = ("trend", "reversion")
HISTORY_DAYS = 5 * 366
MIN_BARS = 260  # 200 for the long average, and a little more
MAX_SIGNAL_AGE_DAYS = 4  # a signal on an old bar is not today's
ATR_DAYS = 14
FAST, SLOW = 50, 200
RSI_DAYS = 2
RSI_LIMIT = 10.0

# (stop in ATR, target in ATR, longest holding in days)
LEVELS: dict[str, tuple[float, float, int]] = {
    "trend": (2.0, 3.0, 30),
    "reversion": (2.0, 1.5, 10),
}
COST = 0.004  # a round trip: 2 EUR on a 1,000 EUR trade, and 0.1% slippage each way

# The acceptance criteria. They are constants on purpose: nobody tunes them to let a signal in.
MIN_TRADES = 30
MIN_HALF_TRADES = 10
MIN_EXPECTANCY = 0.003  # after costs, per trade
MIN_PROFIT_FACTOR = 1.3

CRITERIA_TEXT = (
    f"at least {MIN_TRADES} trades, a profit after costs of at least {MIN_EXPECTANCY * 100:.1f}% "
    f"a trade, a profit factor of at least {MIN_PROFIT_FACTOR}, and a positive profit in each "
    f"half of the history (at least {MIN_HALF_TRADES} trades in each)"
)


@dataclass(frozen=True)
class Bar:
    day: date
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Trade:
    day: date  # the day of the signal
    ret: float  # net of costs, as a fraction


@dataclass(frozen=True)
class GateStats:
    n_trades: int
    win_rate: float  # percent
    expectancy: float  # percent a trade, net
    profit_factor: float | None  # None: no losing trade
    first_half: float | None  # expectancy in percent
    second_half: float | None
    passed: bool


# --- indicators -------------------------------------------------------------------------------


def sma(values: list[float], n: int, i: int) -> float | None:
    """The mean of the `n` values ending at index `i`."""
    if i + 1 < n:
        return None
    return sum(values[i + 1 - n : i + 1]) / n


def atr(bars: list[Bar], i: int, n: int = ATR_DAYS) -> float | None:
    """The average true range of the `n` bars ending at `i`."""
    if i < n:
        return None
    total = 0.0
    for j in range(i + 1 - n, i + 1):
        prev = bars[j - 1].close
        total += max(bars[j].high - bars[j].low, abs(bars[j].high - prev), abs(bars[j].low - prev))
    return total / n


def rsi(closes: list[float], i: int, n: int = RSI_DAYS) -> float | None:
    """The simple-average RSI over the last `n` changes."""
    if i < n:
        return None
    gains = losses = 0.0
    for j in range(i + 1 - n, i + 1):
        change = closes[j] - closes[j - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def signal_at(kind: str, closes: list[float], i: int) -> bool:
    """Whether the signal fires at the close of index `i`."""
    slow = sma(closes, SLOW, i)
    if slow is None or i < 1:
        return False
    if kind == "trend":
        fast, fast_before = sma(closes, FAST, i), sma(closes, FAST, i - 1)
        if fast is None or fast_before is None:
            return False
        return closes[i] > fast and closes[i - 1] <= fast_before and fast > slow
    if kind == "reversion":
        r = rsi(closes, i)
        return r is not None and r < RSI_LIMIT and closes[i] > slow
    raise ValueError(f"unknown signal {kind!r}")


def levels(kind: str, entry: float, atr_value: float) -> tuple[float, float, int]:
    """The stop, the target and the longest holding for an entry."""
    stop_atr, target_atr, hold = LEVELS[kind]
    return entry - stop_atr * atr_value, entry + target_atr * atr_value, hold


# --- replaying a signal over a history --------------------------------------------------------


def replay(kind: str, bars: list[Bar]) -> list[Trade]:
    """Every trade the rule would have made in this history, one at a time. The signal is read at
    the close of day i, the entry is the close of day i+1, and the stop and the target are checked
    on the bars after that. Both in one bar counts as the stop."""
    closes = [b.close for b in bars]
    trades: list[Trade] = []
    i = SLOW
    while i < len(bars) - 2:
        if not signal_at(kind, closes, i):
            i += 1
            continue
        a = atr(bars, i)
        entry = bars[i + 1].close
        if a is None or a <= 0 or entry <= 0:
            i += 1
            continue
        stop, target, hold = levels(kind, entry, a)
        last = min(i + 1 + hold, len(bars) - 1)
        exit_price, exit_index = bars[last].close, last
        for j in range(i + 2, last + 1):
            if bars[j].low <= stop:
                exit_price, exit_index = stop, j
                break
            if bars[j].high >= target:
                exit_price, exit_index = target, j
                break
        if exit_index > i + 1:
            trades.append(Trade(bars[i].day, exit_price / entry - 1 - COST))
        i = max(exit_index, i + 1)  # no new signal while the trade is open
    return trades


def _expectancy(trades: list[Trade]) -> float | None:
    return sum(t.ret for t in trades) / len(trades) * 100 if trades else None


def gate_stats(trades: list[Trade]) -> GateStats:
    """The acceptance test of a signal type over all its trades."""
    ordered = sorted(trades, key=lambda t: t.day)
    n = len(ordered)
    if n == 0:
        return GateStats(0, 0.0, 0.0, None, None, None, False)
    wins = [t.ret for t in ordered if t.ret > 0]
    losses = [-t.ret for t in ordered if t.ret <= 0]
    expectancy = _expectancy(ordered) or 0.0
    pf = sum(wins) / sum(losses) if losses and sum(losses) > 0 else None
    middle = ordered[n // 2].day
    first = [t for t in ordered if t.day < middle]
    second = [t for t in ordered if t.day >= middle]
    first_half, second_half = _expectancy(first), _expectancy(second)
    passed = (
        n >= MIN_TRADES
        and expectancy >= MIN_EXPECTANCY * 100
        and (pf is None or pf >= MIN_PROFIT_FACTOR)
        and len(first) >= MIN_HALF_TRADES
        and len(second) >= MIN_HALF_TRADES
        and first_half is not None
        and first_half > 0
        and second_half is not None
        and second_half > 0
    )
    return GateStats(n, len(wins) / n * 100, expectancy, pf, first_half, second_half, passed)


# --- the nightly scan -------------------------------------------------------------------------


def load_bars(
    session: Session, instrument_ids: list[int], since: date, until: date
) -> dict[int, list[Bar]]:
    """Daily bars by instrument. A bar with no high or low uses its close for them, which makes
    the range and the stops a little too tight, never wider than the data."""
    out: dict[int, list[Bar]] = {i: [] for i in instrument_ids}
    rows = session.execute(
        select(PriceEOD.instrument_id, PriceEOD.date, PriceEOD.high, PriceEOD.low, PriceEOD.close)
        .where(
            PriceEOD.instrument_id.in_(instrument_ids),
            PriceEOD.date >= since,
            PriceEOD.date <= until,
        )
        .order_by(PriceEOD.instrument_id, PriceEOD.date)
    ).all()
    for iid, day, high, low, close in rows:
        c = float(close)
        if c <= 0 or math.isnan(c):
            continue
        out[iid].append(
            Bar(
                day, float(high) if high is not None else c, float(low) if low is not None else c, c
            )
        )
    return out


def _dec(value: float, places: int = 6) -> Decimal:
    return Decimal(str(round(value, places)))


def scan(session: Session, today: date) -> JobResult:
    """Replay both signals over every share and fund the app prices, store tonight's gates, and
    store the signals that fire on the newest bar."""
    result = JobResult()
    instruments = list(
        session.scalars(
            select(Instrument)
            .where(Instrument.active.is_(True), Instrument.asset_class.in_(("stock", "etf")))
            .order_by(Instrument.code)
        )
    )
    result.attempted = len(instruments)
    bars_of = load_bars(
        session, [i.id for i in instruments], today - timedelta(days=HISTORY_DAYS), today
    )
    usable = [i for i in instruments if len(bars_of[i.id]) >= MIN_BARS]
    for kind in SIGNALS:
        trades: list[Trade] = []
        for inst in usable:
            trades += replay(kind, bars_of[inst.id])
        stats = gate_stats(trades)
        row = session.scalar(
            select(SwingGate).where(SwingGate.signal == kind, SwingGate.computed_on == today)
        ) or SwingGate(signal=kind, computed_on=today)
        row.n_trades = stats.n_trades
        row.win_rate = _dec(stats.win_rate, 2)
        row.expectancy = _dec(stats.expectancy, 4)
        row.profit_factor = None if stats.profit_factor is None else _dec(stats.profit_factor, 3)
        row.first_half = None if stats.first_half is None else _dec(stats.first_half, 4)
        row.second_half = None if stats.second_half is None else _dec(stats.second_half, 4)
        row.universe = len(usable)
        row.passed = stats.passed
        session.add(row)
        result.rows_written += 1
    session.execute(delete(SwingSignal).where(SwingSignal.scanned_on == today))
    for inst in usable:
        bars = bars_of[inst.id]
        last = len(bars) - 1
        if (today - bars[last].day).days > MAX_SIGNAL_AGE_DAYS:
            continue
        closes = [b.close for b in bars]
        a = atr(bars, last)
        if a is None or a <= 0:
            continue
        for kind in SIGNALS:
            if not signal_at(kind, closes, last):
                continue
            stop, target, hold = levels(kind, closes[last], a)
            session.add(
                SwingSignal(
                    scanned_on=today,
                    day=bars[last].day,
                    code=inst.code,
                    isin=inst.isin,
                    name=inst.name,
                    currency=inst.currency,
                    signal=kind,
                    entry=_dec(closes[last]),
                    stop=_dec(stop),
                    target=_dec(target),
                    atr=_dec(a),
                    max_hold=hold,
                )
            )
            result.rows_written += 1
    session.commit()
    return result


def latest_gates(session: Session) -> dict[str, SwingGate]:
    """The newest gate row of each signal type."""
    out: dict[str, SwingGate] = {}
    for row in session.scalars(select(SwingGate).order_by(SwingGate.computed_on)):
        out[row.signal] = row  # the newest wins
    return out


SIGNAL_TEXT = {
    "trend": (
        "Trend: the close crosses above the 50-day average while the 50-day average is above the "
        "200-day one. Stop 2 ATR below, target 3 ATR above, at most 30 days."
    ),
    "reversion": (
        "Dip in an uptrend: RSI(2) under 10 while the close is above the 200-day average. Stop "
        "2 ATR below, target 1.5 ATR above, at most 10 days."
    ),
}

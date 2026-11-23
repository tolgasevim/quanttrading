"""The backtesting lab (Phase 3, PRD §6.9): test a rule against stored prices and compare it with
buying and holding a benchmark.

A first version, kept small and honest:

- **Rules**: `buy_hold` (the benchmark itself), `trend` (hold one instrument while its close is
  above its 200-day average, else cash), `momentum` (each month hold the top N of a universe by
  the 12-1 month return, equal weight) and `score` (each month hold the top N by the composite
  factor score as it was stored that day, FR-35).
- **No look-ahead**: a decision uses closes up to the last trading day of the month, and is
  carried out at the close of the next trading day. Scores are read as stored on or before the
  decision day, never recomputed.
- **Costs and tax**: a fee per trade (Trade Republic: 1 EUR) and the German flat tax on realised
  gains at each sale, with losses carried forward against later gains. No Freibetrag, no
  Vorabpauschale and no partial exemption: it is a rough figure, and the buy-and-hold benchmark
  pays no tax because it never sells.
- **Money is Decimal**; only the statistics (ratios, annualised figures) use floats.

What it cannot do, and says so in every result (`WARNINGS`): the universe is what the app prices
*today*, so shares that were delisted are missing (survivorship bias flatters momentum and score
rules); the score history is short until the app has run for a while; prices are close prices in
euros at the ECB rate of the day.
"""

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import FactorScore, Instrument
from quant.portfolio import metrics
from quant.portfolio.performance import Prices, load_prices

ZERO = Decimal(0)
HUNDRED = Decimal(100)
STRATEGIES = ("buy_hold", "trend", "momentum", "score")
TREND_DAYS = 200
MOMENTUM_SKIP_DAYS = 30  # the 12-1 month return skips the latest month
MOMENTUM_LOOKBACK_DAYS = 365
MOMENTUM_MAX_AGE = 10  # a price this old still counts on a signal day
MIN_CALENDAR_DAYS = 60
MIN_TRADE_SHARE = Decimal("0.01")  # of the portfolio: smaller drifts are not traded
PRELOAD_DAYS = 420  # prices read before the start, for the averages and the 12-1 return
MIN_SCORE_HISTORY_DAYS = 60
SCORE_MAX_AGE_DAYS = 7
CURVE_POINTS = 240

WARNINGS = (
    "Hypothetical: a backtest is not a forecast, and past results do not predict future returns.",
    "Survivorship bias: the universe is what the app prices today. Shares that were delisted are "
    "missing, which flatters rules that pick winners.",
    "Prices are daily closes in euros at the ECB rate of the day. Orders are filled at the close "
    "of the day after the signal, with no spread.",
    "Tax is a rough figure: the flat tax on gains at each sale, with losses carried forward. No "
    "Freibetrag, no Vorabpauschale, no partial exemption. The benchmark never sells, so it pays "
    "none.",
)


class BacktestError(ValueError):
    """The test cannot be run as asked. The text says why, in words for the user."""


@dataclass(frozen=True)
class Params:
    strategy: str
    start: date
    end: date
    capital: Decimal
    fee: Decimal  # per trade, EUR
    tax_pct: Decimal  # on realised gains
    top_n: int
    benchmark: str  # an instrument code
    universe: tuple[str, ...]  # instrument codes (momentum, score); the one asset for trend


@dataclass
class Position:
    shares: Decimal = ZERO
    cost: Decimal = ZERO  # total cost basis


@dataclass
class Run:
    curve: list[tuple[date, Decimal]] = field(default_factory=list)
    trades: int = 0
    fees: Decimal = ZERO
    tax: Decimal = ZERO
    traded: Decimal = ZERO  # the sum of the value of every trade, both ways
    rebalances: int = 0


@dataclass(frozen=True)
class Stats:
    final_value: Decimal
    total_return_pct: float
    cagr_pct: float | None
    volatility_pct: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float | None
    trades: int
    fees: Decimal
    tax: Decimal
    turnover_pct_year: float | None  # one-way trading a year, in percent of the average value


@dataclass
class Result:
    params: Params
    strategy: Stats
    benchmark: Stats
    curve: list[tuple[date, float, float]]  # day, strategy, benchmark (both start at the capital)
    first_day: date
    last_day: date
    universe_size: int
    notes: list[str] = field(default_factory=list)
    warnings: tuple[str, ...] = WARNINGS


# --- the calendar and the signals -------------------------------------------------------------


def month_ends(calendar: list[date]) -> list[date]:
    """The last trading day of each month in the calendar."""
    last: dict[tuple[int, int], date] = {}
    for d in calendar:
        last[(d.year, d.month)] = d
    return sorted(last.values())


def _closes_upto(prices: Prices, key: str, day: date, count: int) -> list[Decimal]:
    days = prices.days.get(key)
    if not days:
        return []
    i = bisect_right(days, day)
    return prices.closes[key][max(0, i - count) : i]


def trend_weight(prices: Prices, code: str, day: date) -> Decimal:
    """1 while the close is above the 200-day average of closes, else 0."""
    closes = _closes_upto(prices, code, day, TREND_DAYS)
    if len(closes) < TREND_DAYS:
        return ZERO
    average = sum(closes, ZERO) / TREND_DAYS
    return Decimal(1) if closes[-1] > average else ZERO


def momentum_pick(
    prices: Prices, universe: tuple[str, ...], day: date, n: int
) -> dict[str, Decimal]:
    """The top `n` by the return from 12 months ago to one month ago, equal weight. A name needs
    both prices and a history that reaches back a year."""
    ranked: list[tuple[Decimal, str]] = []
    for code in universe:
        first = prices.first_day(code)
        if first is None or first > day - timedelta(days=MOMENTUM_LOOKBACK_DAYS - 10):
            continue
        old = prices.on(code, day - timedelta(days=MOMENTUM_LOOKBACK_DAYS), MOMENTUM_MAX_AGE)
        recent = prices.on(code, day - timedelta(days=MOMENTUM_SKIP_DAYS), MOMENTUM_MAX_AGE)
        if old is None or recent is None or old <= 0:
            continue
        ranked.append((recent / old - 1, code))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    chosen = [code for _, code in ranked[:n]]
    return {code: Decimal(1) / len(chosen) for code in chosen} if chosen else {}


@dataclass
class Scores:
    """Composite scores as stored, by day: `by_day[day][code]`."""

    by_day: dict[date, dict[str, Decimal]] = field(default_factory=dict)
    first_day: date | None = None

    def on(self, day: date) -> dict[str, Decimal]:
        days = sorted(self.by_day)
        i = bisect_right(days, day) - 1
        if i < 0 or (day - days[i]).days > SCORE_MAX_AGE_DAYS:
            return {}
        return self.by_day[days[i]]


def score_pick(scores: Scores, day: date, n: int) -> dict[str, Decimal]:
    """The top `n` by the composite score stored on or just before `day`. With fewer than `n`
    scored names the rule stays in cash: a thin list is not a ranking."""
    today = scores.on(day)
    if len(today) < n:
        return {}
    ranked = sorted(today.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return {code: Decimal(1) / n for code, _ in ranked}


def make_plan(
    p: Params, prices: Prices, calendar: list[date], scores: Scores | None
) -> dict[date, dict[str, Decimal]]:
    """The target weights to trade into, by the day the trade is done."""
    plan: dict[date, dict[str, Decimal]] = {}
    if p.strategy == "buy_hold":
        plan[calendar[0]] = {p.benchmark: Decimal(1)}
        return plan
    for signal_day in month_ends(calendar):
        later = [d for d in calendar if d > signal_day]
        if not later:
            continue
        exec_day = later[0]
        if p.strategy == "trend":
            w = trend_weight(prices, p.universe[0], signal_day)
            plan[exec_day] = {p.universe[0]: w} if w > 0 else {}
        elif p.strategy == "momentum":
            plan[exec_day] = momentum_pick(prices, p.universe, signal_day, p.top_n)
        elif p.strategy == "score":
            plan[exec_day] = score_pick(scores or Scores(), signal_day, p.top_n)
    return plan


# --- the simulation ---------------------------------------------------------------------------


class _Book:
    def __init__(self, capital: Decimal, fee: Decimal, tax_pct: Decimal) -> None:
        self.cash = capital
        self.fee = fee
        self.tax_pct = tax_pct
        self.pos: dict[str, Position] = defaultdict(Position)
        self.loss_pot = ZERO
        self.run = Run()
        self.last_px: dict[str, Decimal] = {}

    def value(self, prices: Prices, day: date) -> Decimal:
        total = self.cash
        for code, p in self.pos.items():
            if p.shares == 0:
                continue
            px = prices.on(code, day)
            if px is not None:
                self.last_px[code] = px
            use = self.last_px.get(code)
            total += p.shares * (use if use is not None else p.cost / p.shares)
        return total

    def _tax_on(self, gain: Decimal) -> Decimal:
        if gain <= 0:
            self.loss_pot += -gain
            return ZERO
        used = min(self.loss_pot, gain)
        self.loss_pot -= used
        return (gain - used) * self.tax_pct / HUNDRED

    def rebalance(self, prices: Prices, day: date, weights: dict[str, Decimal]) -> None:
        total = self.value(prices, day)
        if total <= 0:
            return
        px: dict[str, Decimal] = {}
        for code in set(self.pos) | set(weights):
            price = prices.on(code, day)
            if price is not None and price > 0:
                px[code] = price
        self.run.rebalances += 1
        # Sells first, so the cash is there for the buys.
        for code, position in list(self.pos.items()):
            if position.shares == 0 or code not in px:
                continue
            target = weights.get(code, ZERO) * total
            current = position.shares * px[code]
            excess = current - target
            if target == 0 or excess > MIN_TRADE_SHARE * total:
                sold = position.shares if target == 0 else excess / px[code]
                sold = min(sold, position.shares)
                proceeds = sold * px[code]
                cost_out = position.cost * sold / position.shares
                tax = self._tax_on(proceeds - cost_out)
                position.shares -= sold
                position.cost -= cost_out
                self.cash += proceeds - self.fee - tax
                self._count(proceeds, tax)
        for code, weight in weights.items():
            if code not in px or weight <= 0:
                continue
            position = self.pos[code]
            target = weight * total
            shortfall = target - position.shares * px[code]
            if shortfall > MIN_TRADE_SHARE * total or (position.shares == 0 and shortfall > 0):
                spend = min(shortfall, self.cash - self.fee)
                if spend <= 0:
                    continue
                position.shares += spend / px[code]
                position.cost += spend
                self.cash -= spend + self.fee
                self._count(spend, ZERO)

    def _count(self, value: Decimal, tax: Decimal) -> None:
        self.run.trades += 1
        self.run.fees += self.fee
        self.run.tax += tax
        self.run.traded += value


def simulate(
    prices: Prices, calendar: list[date], plan: dict[date, dict[str, Decimal]], p: Params
) -> Run:
    book = _Book(p.capital, p.fee, p.tax_pct)
    for day in calendar:
        if day in plan:
            book.rebalance(prices, day, plan[day])
        book.run.curve.append((day, book.value(prices, day)))
    return book.run


# --- the statistics ---------------------------------------------------------------------------


def stats_of(run: Run, capital: Decimal) -> Stats:
    curve = run.curve
    final = curve[-1][1]
    days = [d for d, _ in curve]
    levels = [float(v) for _, v in curve]
    returns = [b / a - 1 for a, b in zip(levels, levels[1:], strict=False) if a > 0]
    years = (days[-1] - days[0]).days / 365.25
    total = float(final / capital - 1)
    cagr = (float(final / capital) ** (1 / years) - 1) * 100 if years >= 0.5 and final > 0 else None
    vol = metrics.annualised_volatility(returns)
    mean = metrics.mean(returns) if returns else 0.0
    sd = metrics.stdev(returns)
    sharpe = mean / sd * math.sqrt(metrics.TRADING_DAYS) if sd else None
    downside = [r for r in returns if r < 0]
    dd_sd = math.sqrt(sum(r * r for r in downside) / len(returns)) if returns and downside else 0.0
    sortino = mean / dd_sd * math.sqrt(metrics.TRADING_DAYS) if dd_sd else None
    drawdown = metrics.max_drawdown(days, levels)
    average = sum(levels) / len(levels)
    turnover = (
        float(run.traded) / 2 / average / years * 100 if years >= 0.5 and average > 0 else None
    )
    return Stats(
        final_value=final,
        total_return_pct=total * 100,
        cagr_pct=cagr,
        volatility_pct=None if vol is None else vol * 100,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=None if drawdown is None else drawdown[0] * 100,
        trades=run.trades,
        fees=run.fees,
        tax=run.tax,
        turnover_pct_year=turnover,
    )


def _downsample(rows: list[tuple[date, float, float]]) -> list[tuple[date, float, float]]:
    if len(rows) <= CURVE_POINTS:
        return rows
    step = len(rows) / CURVE_POINTS
    picked = [rows[int(i * step)] for i in range(CURVE_POINTS)]
    return [*picked, rows[-1]]


# --- loading and running ----------------------------------------------------------------------


def load_scores(session: Session, codes: tuple[str, ...], since: date, until: date) -> Scores:
    isin_to_code = {
        i.isin: i.code
        for i in session.scalars(select(Instrument).where(Instrument.code.in_(codes)))
        if i.isin
    }
    scores = Scores()
    rows = session.execute(
        select(FactorScore.isin, FactorScore.day, FactorScore.composite, FactorScore.data_quality)
        .where(
            FactorScore.isin.in_(list(isin_to_code)),
            FactorScore.day >= since,
            FactorScore.day <= until,
        )
        .order_by(FactorScore.day)
    ).all()
    for isin, day, composite, quality in rows:
        if composite is None or quality == "poor":
            continue  # a score with gaps does not rank anything
        scores.by_day.setdefault(day, {})[isin_to_code[isin]] = Decimal(composite)
        if scores.first_day is None:
            scores.first_day = day
    return scores


def check(p: Params) -> None:
    if p.strategy not in STRATEGIES:
        raise BacktestError(f"the rule must be one of: {', '.join(STRATEGIES)}")
    if p.end <= p.start:
        raise BacktestError("the end must be after the start")
    if p.capital <= 0:
        raise BacktestError("the capital must be positive")
    if p.fee < 0 or not (ZERO <= p.tax_pct <= HUNDRED):
        raise BacktestError(
            "the fee and the tax rate must not be negative, and the tax at most 100%"
        )
    if p.top_n < 1:
        raise BacktestError("the number of names must be at least 1")
    if p.strategy in ("momentum", "score") and len(p.universe) < p.top_n:
        raise BacktestError("the universe has fewer names than the number to hold")
    if p.strategy == "trend" and len(p.universe) != 1:
        raise BacktestError("the trend rule needs exactly one instrument")


def run_backtest(session: Session, p: Params) -> Result:
    check(p)
    codes = {p.benchmark, *p.universe}
    prices = load_prices(session, set(), codes, p.start - timedelta(days=PRELOAD_DAYS), p.end)
    bench_days = prices.days.get(p.benchmark, [])
    scores: Scores | None = None
    start = p.start
    notes: list[str] = []
    if p.strategy == "score":
        scores = load_scores(session, p.universe, p.start, p.end)
        if scores.first_day is None or not scores.by_day:
            raise BacktestError("there are no stored scores for these names yet")
        span = (max(scores.by_day) - scores.first_day).days
        if span < MIN_SCORE_HISTORY_DAYS:
            raise BacktestError(
                f"the stored score history is too short ({span} days, from {scores.first_day}): "
                f"it needs at least {MIN_SCORE_HISTORY_DAYS}"
            )
        if scores.first_day > start:
            start = scores.first_day
            notes.append(f"The test starts on {start}, the first day with stored scores.")
    calendar = [d for d in bench_days if start <= d <= p.end]
    if len(calendar) < MIN_CALENDAR_DAYS:
        raise BacktestError(
            f"the benchmark {p.benchmark} has only {len(calendar)} prices in this period: "
            f"it needs at least {MIN_CALENDAR_DAYS}"
        )
    if p.strategy == "trend" and prices.first_day(p.universe[0]) is None:
        raise BacktestError(f"no prices for {p.universe[0]}")
    plan = make_plan(p, prices, calendar, scores)
    run = simulate(prices, calendar, plan, p)
    bench_params = Params(
        "buy_hold", p.start, p.end, p.capital, p.fee, p.tax_pct, 1, p.benchmark, (p.benchmark,)
    )
    bench_run = simulate(
        prices, calendar, make_plan(bench_params, prices, calendar, None), bench_params
    )
    if run.trades == 0:
        notes.append("The rule never traded in this period: it stayed in cash.")
    curve = _downsample(
        [(d, float(a), float(b)) for (d, a), (_, b) in zip(run.curve, bench_run.curve, strict=True)]
    )
    return Result(
        params=p,
        strategy=stats_of(run, p.capital),
        benchmark=stats_of(bench_run, p.capital),
        curve=curve,
        first_day=calendar[0],
        last_day=calendar[-1],
        universe_size=len(p.universe),
        notes=notes,
    )

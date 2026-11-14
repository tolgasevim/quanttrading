"""Performance and risk of one user's portfolio (FR-21, FR-22).

The portfolio here is the securities sleeve: the shares, funds and coins the history says the
user holds, valued every weekday at the latest stored close in euros. Cash is not in it, so a
deposit or a withdrawal never moves the result. What moves it:

- a **flow** is money going into the securities (a buy, with its fee) or coming out of them (a
  sell, net of fee and withheld tax, or the cash of a corporate action). Flows are not return.
- **income** (dividends, distributions) is cash the securities paid out. It counts as return.
- free receipts (savings plan bonus, Saveback) and swaps carry no flow: what they are worth is
  return, as it was when the user got them.

Daily return = (value today - flow today + income today) / value yesterday - 1, with trades at
the close (so a buy changes the value and the flow by the same amount and gives no return). The
time-weighted return (TWR) chains these. The money-weighted return (XIRR) is the annualised rate
that makes the dated flows and the final value balance.

A close is "stale" after two weeks. A holding with no usable close is valued at the price of the
user's last trade in it, and the day is counted as estimated: the answer says how many were.
The benchmark is an accumulating UCITS ETF, so its price return is its total return. Its XIRR is
what the same flows would have returned in the benchmark.
"""

import uuid
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from quant.models import FxRate, Instrument, PriceEOD, Transaction
from quant.portfolio import metrics
from quant.portfolio.positions import QUANTITY_CATEGORIES, compute_positions, moves_quantity

# The benchmarks the user can choose (D35). The seed list holds a UCITS ETF for each.
BENCHMARKS: dict[str, str] = {"SXRV": "Nasdaq-100", "EUNL": "MSCI World", "SXR8": "S&P 500"}
DEFAULT_BENCHMARK = "SXRV"
PERIODS = ("all", "ytd", "1y", "3y")

MAX_STALE_DAYS = 14
MAX_FX_GAP_DAYS = 7
CORRELATION_POSITIONS = 10
CORRELATION_WINDOW = metrics.TRADING_DAYS
MIN_CORRELATION_DAYS = 60
MAX_SERIES_POINTS = 400

ZERO = Decimal(0)


@dataclass(frozen=True)
class DayPoint:
    day: date
    value: Decimal
    flow: Decimal
    income: Decimal
    estimated: bool


@dataclass
class Prices:
    """Euro closes by key (an ISIN or an instrument code), each ascending by day."""

    days: dict[str, list[date]] = field(default_factory=dict)
    closes: dict[str, list[Decimal]] = field(default_factory=dict)

    def add(self, key: str, day: date, close: Decimal) -> None:
        self.days.setdefault(key, []).append(day)
        self.closes.setdefault(key, []).append(close)

    def on(self, key: str, day: date, max_age: int = MAX_STALE_DAYS) -> Decimal | None:
        """The latest close on or before `day`, if it is not stale."""
        days = self.days.get(key)
        if not days:
            return None
        i = bisect_right(days, day) - 1
        if i < 0 or (day - days[i]).days > max_age:
            return None
        return self.closes[key][i]

    def first_day(self, key: str) -> date | None:
        days = self.days.get(key)
        return days[0] if days else None


class FxTable:
    """ECB rates (1 EUR = rate units of a currency) by day."""

    def __init__(self, rows: list[tuple[str, date, Decimal]]) -> None:
        self._days: dict[str, list[date]] = {}
        self._rates: dict[str, list[Decimal]] = {}
        for quote, day, rate in sorted(rows, key=lambda r: (r[0], r[1])):
            self._days.setdefault(quote, []).append(day)
            self._rates.setdefault(quote, []).append(rate)

    def to_eur(self, price: Decimal, currency: str, day: date) -> Decimal | None:
        if currency == "EUR":
            return price
        days = self._days.get(currency)
        if not days:
            return None
        i = bisect_right(days, day) - 1
        if i < 0 or (day - days[i]).days > MAX_FX_GAP_DAYS or self._rates[currency][i] <= 0:
            return None
        return price / self._rates[currency][i]


def load_prices(
    session: Session, isins: set[str], codes: set[str], since: date, until: date
) -> Prices:
    """Euro closes for the held ISINs and the benchmark codes. A price in another currency is
    converted at the ECB rate of its own day; one with no rate is left out."""
    prices = Prices()
    if not isins and not codes:
        return prices
    rows = session.execute(
        select(Instrument.isin, Instrument.code, PriceEOD.date, PriceEOD.close, PriceEOD.currency)
        .join(PriceEOD, PriceEOD.instrument_id == Instrument.id)
        .where(
            or_(Instrument.isin.in_(isins), Instrument.code.in_(codes)),
            PriceEOD.date >= since - timedelta(days=MAX_STALE_DAYS),
            PriceEOD.date <= until,
        )
        .order_by(PriceEOD.date)
    ).all()
    quotes = {currency for *_, currency in rows if currency and currency != "EUR"}
    fx = FxTable(
        [
            (quote, day, rate)
            for quote, day, rate in session.execute(
                select(FxRate.quote, FxRate.date, FxRate.rate).where(
                    FxRate.quote.in_(quotes),
                    FxRate.date >= since - timedelta(days=MAX_STALE_DAYS + MAX_FX_GAP_DAYS),
                    FxRate.date <= until,
                )
            )
        ]
        if quotes
        else []
    )
    for isin, code, day, close, currency in rows:
        if close is None:
            continue
        eur = fx.to_eur(close, currency or "EUR", day)
        if eur is None or eur <= 0:
            continue
        if isin and isin in isins:
            prices.add(isin, day, eur)
        if code in codes:
            prices.add(code, day, eur)
    return prices


def load_history(session: Session, user_id: uuid.UUID) -> list[Transaction]:
    """Every row that moves quantity, money into or out of the securities, or income."""
    return list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.isin.is_not(None),
                or_(
                    Transaction.category.in_(QUANTITY_CATEGORIES),
                    and_(
                        Transaction.category == "CASH",
                        Transaction.kind.in_(("income", "corporate_action")),
                    ),
                ),
            )
            .order_by(Transaction.executed_at, Transaction.id)
        )
    )


def weekdays(start: date, end: date) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def next_weekday(day: date) -> date:
    """A weekend row is booked on the Monday after."""
    return day + timedelta(days=7 - day.weekday()) if day.weekday() >= 5 else day


def flow_and_income(tx: Transaction) -> tuple[Decimal, Decimal]:
    """(flow into the securities, income) of one row, in euros. A buy is a positive flow."""
    amount = tx.amount or ZERO
    fee = tx.fee or ZERO
    tax = tx.tax or ZERO
    if tx.category == "TRADING":
        if tx.amount is None:
            if tx.shares is None or tx.price is None:
                return ZERO, ZERO
            amount = -(tx.shares * tx.price)
        return -(amount + fee + tax), ZERO
    if tx.category == "CASH" and tx.kind == "corporate_action":
        return -amount, ZERO
    if tx.category == "CASH" and tx.kind == "income":
        return ZERO, amount + fee + tax
    return ZERO, ZERO


@dataclass
class Timeline:
    points: list[DayPoint]
    quantities: dict[str, Decimal]  # held on the last day
    unpriced: set[str]  # held at some time, never had any price to value it with
    estimated_isins: set[str]  # valued at the last trade price on at least one day
    net_invested: Decimal
    income: Decimal


def build_timeline(history: list[Transaction], prices: Prices, end: date) -> Timeline | None:
    """The securities' value, flows and income for every weekday from the first row to `end`."""
    if not history:
        return None
    start = next_weekday(history[0].date)
    if start > end:
        return None
    days = weekdays(start, end)
    booked = [(next_weekday(tx.date), tx) for tx in history]
    quantities: dict[str, Decimal] = {}
    last_trade: dict[str, Decimal] = {}
    unpriced: set[str] = set()
    estimated_isins: set[str] = set()
    points: list[DayPoint] = []
    net_invested = income_total = ZERO
    i = 0
    for day in days:
        flow = income = ZERO
        while i < len(booked) and booked[i][0] <= day:
            tx = booked[i][1]
            i += 1
            f, inc = flow_and_income(tx)
            flow += f
            income += inc
            if moves_quantity(tx):
                assert tx.isin is not None and tx.shares is not None
                quantities[tx.isin] = quantities.get(tx.isin, ZERO) + tx.shares
                if tx.shares != 0:
                    if tx.category == "TRADING" and tx.amount is not None:
                        last_trade[tx.isin] = abs(tx.amount / tx.shares)
                    elif tx.price is not None:
                        last_trade[tx.isin] = tx.price
        net_invested += flow
        income_total += income
        value = ZERO
        estimated = False
        for isin, quantity in quantities.items():
            if quantity <= 0:
                continue
            price = prices.on(isin, day)
            if price is None:
                price = last_trade.get(isin)
                if price is None:
                    unpriced.add(isin)
                    continue
                estimated = True
                estimated_isins.add(isin)
            value += quantity * price
        points.append(DayPoint(day, value, flow, income, estimated))
    return Timeline(points, quantities, unpriced, estimated_isins, net_invested, income_total)


def period_start(period: str, end: date) -> date | None:
    if period == "ytd":
        return date(end.year, 1, 1)
    if period == "1y":
        return end - timedelta(days=365)
    if period == "3y":
        return end - timedelta(days=3 * 365)
    return None


@dataclass
class SeriesPoint:
    day: date
    portfolio: float
    benchmark: float | None


@dataclass
class CorrelationMatrix:
    isins: list[str]
    names: list[str]
    values: list[list[float | None]]


@dataclass
class Analysis:
    benchmark: str
    benchmark_name: str
    period: str
    has_history: bool = False
    start: date | None = None
    end: date | None = None
    current_value: Decimal = ZERO
    net_invested: Decimal = ZERO
    income: Decimal = ZERO
    twr: float | None = None
    xirr: float | None = None
    benchmark_twr: float | None = None
    benchmark_xirr: float | None = None
    volatility: float | None = None
    benchmark_volatility: float | None = None
    max_drawdown: float | None = None
    drawdown_peak: date | None = None
    drawdown_low: date | None = None
    beta: float | None = None
    var95: float | None = None
    cvar95: float | None = None
    observations: int = 0
    days: int = 0
    estimated_days: int = 0
    unpriced: list[str] = field(default_factory=list)
    estimated: list[str] = field(default_factory=list)
    series: list[SeriesPoint] = field(default_factory=list)
    correlation: CorrelationMatrix | None = None
    warnings: list[str] = field(default_factory=list)


def _thin(points: list[SeriesPoint]) -> list[SeriesPoint]:
    if len(points) <= MAX_SERIES_POINTS:
        return points
    step = -(-len(points) // MAX_SERIES_POINTS)
    thinned = points[::step]
    if thinned[-1] is not points[-1]:
        thinned.append(points[-1])
    return thinned


def _returns(days: list[date], closes: list[Decimal | None]) -> dict[date, float]:
    out: dict[date, float] = {}
    for i in range(1, len(days)):
        prev, now = closes[i - 1], closes[i]
        if prev is not None and now is not None and prev > 0:
            out[days[i]] = float(now / prev - 1)
    return out


def _correlation(
    prices: Prices,
    quantities: dict[str, Decimal],
    names: dict[str, str],
    days: list[date],
    end: date,
) -> CorrelationMatrix | None:
    """Correlation of the daily price returns of the biggest holdings over the last year."""
    window = days[-CORRELATION_WINDOW:]
    ranked: list[tuple[Decimal, str]] = []
    for isin, quantity in quantities.items():
        price = prices.on(isin, end)
        if quantity > 0 and price is not None:
            ranked.append((quantity * price, isin))
    ranked.sort(reverse=True)
    series: dict[str, dict[date, float]] = {}
    for _, isin in ranked:
        if len(series) == CORRELATION_POSITIONS:
            break
        returns = _returns(window, [prices.on(isin, d) for d in window])
        if len(returns) >= MIN_CORRELATION_DAYS:
            series[isin] = returns
    if len(series) < 2:
        return None
    isins = list(series)
    values: list[list[float | None]] = []
    for a in isins:
        row: list[float | None] = []
        for b in isins:
            if a == b:
                row.append(1.0)
                continue
            shared = sorted(series[a].keys() & series[b].keys())
            if len(shared) < MIN_CORRELATION_DAYS:
                row.append(None)
                continue
            value = metrics.correlation(
                [series[a][d] for d in shared], [series[b][d] for d in shared]
            )
            row.append(None if value is None else round(value, 3))
        values.append(row)
    return CorrelationMatrix(isins, [names.get(i, i) for i in isins], values)


def analyse(
    session: Session,
    user_id: uuid.UUID,
    benchmark: str = DEFAULT_BENCHMARK,
    period: str = "all",
    today: date | None = None,
) -> Analysis:
    result = Analysis(
        benchmark=benchmark, benchmark_name=BENCHMARKS.get(benchmark, benchmark), period=period
    )
    history = load_history(session, user_id)
    if not history:
        return result
    end = today or date.today()
    isins = {tx.isin for tx in history if tx.isin and moves_quantity(tx)}
    first = next_weekday(history[0].date)
    prices = load_prices(session, isins, {benchmark}, first, end)
    timeline = build_timeline(history, prices, end)
    if timeline is None:
        return result
    points = timeline.points
    result.has_history = True
    result.current_value = timeline.points[-1].value
    result.net_invested = timeline.net_invested
    result.income = timeline.income
    result.unpriced = sorted(timeline.unpriced)
    result.estimated = sorted(timeline.estimated_isins)
    names = {isin: p.name or isin for isin, p in compute_positions(history).items()}
    if result.unpriced:
        result.warnings.append(
            f"{len(result.unpriced)} holdings have no price and no trade price. "
            "They are left out of the value."
        )
    if result.estimated:
        result.warnings.append(
            f"{len(result.estimated)} holdings have no recent price on some days. "
            "Those days use the price of your last trade."
        )

    # Where the comparison starts: the period, and no earlier than the benchmark's first price.
    wanted = period_start(period, points[-1].day)
    base = 0
    if wanted is not None and wanted > points[0].day:
        base = max(i for i, p in enumerate(points) if p.day < wanted)
    bench_first = prices.first_day(benchmark)
    bench_ok = bench_first is not None
    if bench_first is not None and bench_first > points[base].day:
        later = [i for i, p in enumerate(points) if p.day >= bench_first]
        if later:
            base = later[0]
            result.warnings.append(
                f"The benchmark has prices only from {bench_first.isoformat()}, "
                "so everything is compared from the first day after that."
            )
    if not bench_ok:
        result.warnings.append(f"There are no stored prices for the benchmark ({benchmark}).")
    window = points[base:]
    result.start, result.end, result.days = window[0].day, window[-1].day, len(window)
    result.estimated_days = sum(1 for p in window if p.estimated)

    # Time-weighted return and the daily returns.
    returns: dict[date, float] = {}
    levels = [100.0]
    for prev, cur in zip(points[base:-1], points[base + 1 :], strict=True):
        if prev.value > 0:
            r = float((cur.value - cur.flow + cur.income) / prev.value - 1)
            returns[cur.day] = r
        else:
            r = 0.0
        levels.append(levels[-1] * (1 + r))
    result.observations = len(returns)
    result.twr = metrics.total_return(list(returns.values())) if returns else None

    # Money-weighted return: the flows in the window, and the value at the end.
    bench_close = [prices.on(benchmark, p.day) for p in window]
    flows: list[tuple[date, float]] = []
    if base > 0 and window[0].value > 0:
        flows.append((window[0].day, -float(window[0].value)))
        rest = window[1:]
    elif base > 0:
        rest = window[1:]
    else:
        rest = window
    for p in rest:
        flows.append((p.day, float(-p.flow + p.income)))
    flows.append((window[-1].day, float(window[-1].value)))
    result.xirr = metrics.xirr(flows)

    # The benchmark over the same days, and the same flows put into it.
    bench_returns: dict[date, float] = {}
    if bench_ok and all(c is not None for c in bench_close):
        closes = [c for c in bench_close if c is not None]
        bench_returns = _returns([p.day for p in window], list(bench_close))
        if len(closes) >= 2 and closes[0] > 0:
            result.benchmark_twr = float(closes[-1] / closes[0] - 1)
            units = window[0].value / closes[0] if base > 0 else ZERO
            bench_flows: list[tuple[date, float]] = []
            if base > 0 and window[0].value > 0:
                bench_flows.append((window[0].day, -float(window[0].value)))
            for p, close in zip(rest, closes[len(window) - len(rest) :], strict=True):
                cash = p.flow - p.income
                units += cash / close
                bench_flows.append((p.day, float(-cash)))
            bench_flows.append((window[-1].day, float(units * closes[-1])))
            result.benchmark_xirr = metrics.xirr(bench_flows)
        series_bench: list[float | None] = [float(c / closes[0] * 100) for c in closes]
    else:
        series_bench = [None] * len(window)
        if bench_ok:
            result.warnings.append("The benchmark has gaps in its prices in this period.")
    result.series = _thin(
        [
            SeriesPoint(p.day, round(level, 3), None if b is None else round(b, 3))
            for p, level, b in zip(window, levels, series_bench, strict=True)
        ]
    )

    # Risk (FR-22).
    daily = list(returns.values())
    result.volatility = metrics.annualised_volatility(daily)
    if bench_returns:
        result.benchmark_volatility = metrics.annualised_volatility(list(bench_returns.values()))
        result.beta = metrics.beta(returns, bench_returns)
    drawdown = metrics.max_drawdown([p.day for p in window], levels)
    if drawdown is not None and len(returns) >= metrics.MIN_RISK_OBSERVATIONS:
        result.max_drawdown, result.drawdown_peak, result.drawdown_low = drawdown
    shortfall = metrics.var_cvar(daily)
    if shortfall is not None:
        result.var95, result.cvar95 = shortfall
    result.correlation = _correlation(
        prices, timeline.quantities, names, [p.day for p in points], points[-1].day
    )
    return result

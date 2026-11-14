"""Return and risk statistics (FR-21, FR-22): pure functions over plain numbers.

These are ratios and statistics, not money, so they use floats. Money (values, flows) stays
`Decimal` everywhere else and is converted at the edge.
"""

import math
from collections.abc import Mapping, Sequence
from datetime import date

TRADING_DAYS = 252
MIN_XIRR_DAYS = 30  # a shorter span cannot be annualised in a way that means anything
MIN_RISK_OBSERVATIONS = 30
MIN_BETA_OBSERVATIONS = 60
MIN_VAR_OBSERVATIONS = 60
VAR_WINDOW = TRADING_DAYS  # "1-year historical" (FR-22)
VAR_LEVEL = 0.95


def xirr(flows: Sequence[tuple[date, float]]) -> float | None:
    """The annualised money-weighted return of dated cash flows (FR-21): the rate at which their
    present value is zero. Money paid in is negative, money paid out (and the final value) is
    positive. None when there is no answer: no sign change, a span under 30 days, or no root."""
    items = [(d, a) for d, a in flows if a != 0]
    if len(items) < 2:
        return None
    if not (any(a < 0 for _, a in items) and any(a > 0 for _, a in items)):
        return None
    first = min(d for d, _ in items)
    last = max(d for d, _ in items)
    if (last - first).days < MIN_XIRR_DAYS:
        return None

    def npv(rate: float) -> float:
        return sum(a / math.pow(1.0 + rate, (d - first).days / 365.0) for d, a in items)

    low, high = -0.9999, 1000.0
    f_low, f_high = npv(low), npv(high)
    if math.isnan(f_low) or math.isnan(f_high) or f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if f_low * f_mid <= 0:
            high = mid
        else:
            low, f_low = mid, f_mid
        if high - low < 1e-12:
            break
    return (low + high) / 2


def chain(returns: Sequence[float], start: float = 100.0) -> list[float]:
    """Index levels from daily returns: `start`, then one level per return."""
    levels = [start]
    for r in returns:
        levels.append(levels[-1] * (1.0 + r))
    return levels


def total_return(returns: Sequence[float]) -> float | None:
    """The chain-linked return of daily returns (the time-weighted return, FR-21)."""
    if not returns:
        return None
    return chain(returns, 1.0)[-1] - 1.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def stdev(values: Sequence[float]) -> float | None:
    """The sample standard deviation; None for fewer than two values."""
    if len(values) < 2:
        return None
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def annualised_volatility(returns: Sequence[float]) -> float | None:
    if len(returns) < MIN_RISK_OBSERVATIONS:
        return None
    s = stdev(returns)
    return None if s is None else s * math.sqrt(TRADING_DAYS)


def max_drawdown(days: Sequence[date], levels: Sequence[float]) -> tuple[float, date, date] | None:
    """The deepest fall from a peak to a later low, as a positive fraction (0.25 = a 25% fall),
    with the day of the peak and of the low."""
    if len(days) != len(levels) or len(levels) < 2:
        return None
    peak_level, peak_day = levels[0], days[0]
    worst, worst_peak, worst_low = 0.0, days[0], days[0]
    for day, level in zip(days, levels, strict=True):
        if level > peak_level:
            peak_level, peak_day = level, day
        if peak_level > 0:
            fall = (peak_level - level) / peak_level
            if fall > worst:
                worst, worst_peak, worst_low = fall, peak_day, day
    return worst, worst_peak, worst_low


def beta(portfolio: Mapping[date, float], benchmark: Mapping[date, float]) -> float | None:
    """Slope of the portfolio's daily returns on the benchmark's, over the days both have."""
    days = sorted(portfolio.keys() & benchmark.keys())
    if len(days) < MIN_BETA_OBSERVATIONS:
        return None
    p = [portfolio[d] for d in days]
    b = [benchmark[d] for d in days]
    mp, mb = mean(p), mean(b)
    variance = sum((x - mb) ** 2 for x in b)
    if variance == 0:
        return None
    return sum((x - mb) * (y - mp) for x, y in zip(b, p, strict=True)) / variance


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    if len(a) != len(b) or len(a) < 2:
        return None
    ma, mb = mean(a), mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / math.sqrt(va * vb)


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """The q-quantile (0 to 1) of ascending values, interpolating between the two nearest."""
    pos = (len(sorted_values) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def var_cvar(returns: Sequence[float], level: float = VAR_LEVEL) -> tuple[float, float] | None:
    """One-day historical value at risk and expected shortfall over the last year of returns
    (FR-22), as positive fractions of the portfolio. VaR is the loss that was exceeded on
    (1 - level) of the days; CVaR is the average loss on those days."""
    window = list(returns[-VAR_WINDOW:])
    if len(window) < MIN_VAR_OBSERVATIONS:
        return None
    ordered = sorted(window)
    cut = percentile(ordered, 1.0 - level)
    tail = [r for r in ordered if r <= cut]
    return max(0.0, -cut), max(0.0, -mean(tail))

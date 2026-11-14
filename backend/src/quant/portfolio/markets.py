"""Commodities, key drivers, the regime and the hedge check (FR-60, FR-61, FR-63, FR-64).

Everything here reads stored prices only: the commodity futures and macro series the price job
fetches (`seed/instruments.yaml`), the coins, and the user's own portfolio returns. It is
information, not advice: nothing here changes a target or places a trade (D2, D5).

- `commodity_panel`: last price in dollars and euros, the change over a week, a month, a year and
  five years, and the 52-week range. A change is blank when the history is too short for it.
- `drivers`: the free-data key drivers of FR-61. The EIA oil inventories need a key the app does
  not have, so that line says so.
- `regime`: a rule of thumb of five signals (real yield, dollar, credit spread, inflation, oil),
  each a vote for risk-on or risk-off. It is *not* a forecast, and says so.
- `hedge_correlation`: how the user's portfolio moved with gold over three windows.
"""

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import FxRate, Instrument, PriceEOD
from quant.portfolio import metrics
from quant.portfolio.performance import FxTable

ZERO = Decimal(0)
HUNDRED = Decimal(100)

# code, label, unit
COMMODITIES: tuple[tuple[str, str, str], ...] = (
    ("GOLD", "Gold", "USD per troy ounce"),
    ("SILVER", "Silver", "USD per troy ounce"),
    ("WTI", "WTI crude oil", "USD per barrel"),
    ("BRENT", "Brent crude oil", "USD per barrel"),
    ("COPPER", "Copper", "USD per pound"),
    ("NATGAS", "Natural gas", "USD per MMBtu"),
)
CHANGE_DAYS = {"1w": 7, "1m": 30, "1y": 365, "5y": 5 * 365}
HISTORY_DAYS = 5 * 366 + 14
MAX_STALE_DAYS = 10  # the newest price may be this old and still be "now"
GAP_DAYS = 7  # a price this far before the day asked still stands in for it
HEDGE_WINDOWS = (60, 120, 250)
MIN_HEDGE_DAYS = 30
CORR_WINDOW_DAYS = 90
CORR_MIN_DAYS = 30
VOL_DAYS = 30
CRYPTO_DAYS_A_YEAR = 365

Series = list[tuple[date, Decimal]]


def load_series(session: Session, code: str, since: date, until: date) -> Series:
    """Stored closes of an instrument by its code, oldest first."""
    rows = session.execute(
        select(PriceEOD.date, PriceEOD.close)
        .join(Instrument, Instrument.id == PriceEOD.instrument_id)
        .where(Instrument.code == code, PriceEOD.date >= since, PriceEOD.date <= until)
        .order_by(PriceEOD.date)
    ).all()
    return [(d, Decimal(c)) for d, c in rows]


def value_on(series: Series, day: date, gap: int = GAP_DAYS) -> Decimal | None:
    """The close on `day`, or the latest one up to `gap` days before it."""
    days = [d for d, _ in series]
    i = bisect_right(days, day) - 1
    if i < 0 or (day - days[i]).days > gap:
        return None
    return series[i][1]


def change(series: Series, days: int) -> Decimal | None:
    """The move in percent from `days` before the newest close to the newest close. None when
    the stored history does not reach back that far."""
    if len(series) < 2:
        return None
    last_day, last = series[-1]
    then = value_on(series, last_day - timedelta(days=days))
    first_day = series[0][0]
    if then is None or then <= 0 or first_day > last_day - timedelta(days=days - GAP_DAYS):
        return None
    return (last - then) / then * HUNDRED


def point_change(series: Series, days: int) -> Decimal | None:
    """The difference (not the percent) between the newest value and the one `days` earlier:
    for series that are themselves percentages, such as a yield."""
    if len(series) < 2:
        return None
    last_day, last = series[-1]
    then = value_on(series, last_day - timedelta(days=days))
    if then is None or series[0][0] > last_day - timedelta(days=days - GAP_DAYS):
        return None
    return last - then


def fresh(series: Series, today: date) -> bool:
    return bool(series) and (today - series[-1][0]).days <= MAX_STALE_DAYS


def returns_of(series: Series) -> dict[date, float]:
    out: dict[date, float] = {}
    for (_, prev), (day, now) in zip(series, series[1:], strict=False):
        if prev > 0:
            out[day] = float(now / prev - 1)
    return out


def usd_to_eur(session: Session, since: date, until: date) -> FxTable:
    rows = session.execute(
        select(FxRate.quote, FxRate.date, FxRate.rate).where(
            FxRate.quote == "USD",
            FxRate.date >= since - timedelta(days=GAP_DAYS * 2),
            FxRate.date <= until,
        )
    ).all()
    return FxTable([(q, d, Decimal(r)) for q, d, r in rows])


# --- FR-60: the commodity panel --------------------------------------------------------------


@dataclass(frozen=True)
class CommodityRow:
    code: str
    name: str
    unit: str
    usd: Decimal | None
    eur: Decimal | None
    as_of: date | None
    changes: dict[str, Decimal | None]
    low_52w: Decimal | None
    high_52w: Decimal | None
    range_pos: Decimal | None  # where the price sits in its 52-week range, 0 to 100
    stale: bool


def commodity_panel(session: Session, today: date) -> list[CommodityRow]:
    since = today - timedelta(days=HISTORY_DAYS)
    fx = usd_to_eur(session, since, today)
    out: list[CommodityRow] = []
    for code, name, unit in COMMODITIES:
        series = load_series(session, code, since, today)
        if not series:
            blank: dict[str, Decimal | None] = {label: None for label in CHANGE_DAYS}
            out.append(
                CommodityRow(code, name, unit, None, None, None, blank, None, None, None, True)
            )
            continue
        day, last = series[-1]
        year = [c for d, c in series if d > day - timedelta(days=365)]
        low, high = min(year), max(year)
        pos = (last - low) / (high - low) * HUNDRED if high > low else None
        out.append(
            CommodityRow(
                code, name, unit, last, fx.to_eur(last, "USD", day), day,
                {label: change(series, n) for label, n in CHANGE_DAYS.items()},
                low, high, pos, not fresh(series, today),
            )
        )  # fmt: skip
    return out


# --- FR-61: key drivers ------------------------------------------------------------------------


@dataclass(frozen=True)
class DriverItem:
    label: str
    value: Decimal | None
    unit: str
    detail: str  # plain words on what moved
    available: bool = True


@dataclass(frozen=True)
class DriverGroup:
    title: str
    items: list[DriverItem]


def _sd(returns: list[float], per_year: int) -> float | None:
    s = metrics.stdev(returns)
    return None if s is None else s * math.sqrt(per_year)


def _drawdown(series: Series) -> Decimal | None:
    if not series:
        return None
    peak = max(c for _, c in series)
    return (peak - series[-1][1]) / peak * HUNDRED if peak > 0 else None


def drivers(session: Session, today: date) -> list[DriverGroup]:
    since = today - timedelta(days=HISTORY_DAYS)

    def s(code: str) -> Series:
        return load_series(session, code, since, today)

    groups: list[DriverGroup] = []

    # Gold and silver
    real, usd, gold, silver = s("DFII10"), s("DTWEXBGS"), s("GOLD"), s("SILVER")
    items: list[DriverItem] = []
    pp = point_change(real, 90)
    items.append(
        DriverItem(
            "10-year real yield",
            real[-1][1] if real else None,
            "percent",
            "no data" if not real else (
                "no 3-month comparison yet" if pp is None
                else f"{pp:+.2f} points over 3 months. Rising real yields have weighed on gold."
            ),
            bool(real),
        )
    )  # fmt: skip
    ch = change(usd, 90)
    items.append(
        DriverItem(
            "US dollar index (broad)",
            usd[-1][1] if usd else None,
            "index",
            "no data" if not usd else (
                "no 3-month comparison yet" if ch is None
                else f"{ch:+.1f}% over 3 months. A stronger dollar has weighed on gold."
            ),
            bool(usd),
        )
    )  # fmt: skip
    ratios = _ratio_series(gold, silver)
    if ratios:
        year = [r for d, r in ratios if d > ratios[-1][0] - timedelta(days=365)]
        avg = sum(year, ZERO) / len(year)
        items.append(
            DriverItem(
                "Gold to silver ratio", ratios[-1][1], "ounces of silver per ounce of gold",
                f"The average of the last year is {avg:.1f}. A high ratio means silver is cheap "
                "against gold.",
            )
        )  # fmt: skip
    else:
        items.append(DriverItem("Gold to silver ratio", None, "", "no data", False))
    groups.append(DriverGroup("Gold and silver", items))

    # Crypto
    nasdaq = returns_of(s("SXRV"))
    crypto: list[DriverItem] = []
    for code, label in (("BTC", "Bitcoin"), ("ETH", "Ethereum")):
        series = s(code)
        if not series:
            crypto.append(DriverItem(label, None, "EUR", "no data", False))
            continue
        rets = returns_of(series)
        recent = [rets[d] for d, _ in series[-VOL_DAYS:] if d in rets]
        vol = _sd(recent, CRYPTO_DAYS_A_YEAR) if len(recent) >= 10 else None
        dd = _drawdown(series)
        window = [(d, r) for d, r in rets.items() if d > today - timedelta(days=CORR_WINDOW_DAYS)]
        shared = [(r, nasdaq[d]) for d, r in window if d in nasdaq]
        corr = (
            metrics.correlation([a for a, _ in shared], [b for _, b in shared])
            if len(shared) >= CORR_MIN_DAYS
            else None
        )
        parts = [
            "volatility " + (f"{vol * 100:.0f}% a year (30 days)" if vol is not None else "n/a"),
            "down " + (f"{dd:.0f}% from its highest stored close" if dd is not None else "n/a"),
            "correlation with the Nasdaq-100 "
            + (f"{corr:.2f} (90 days)" if corr is not None else "n/a"),
        ]
        crypto.append(DriverItem(label, series[-1][1], "EUR", "; ".join(parts)))
    groups.append(DriverGroup("Crypto", crypto))

    # Oil
    brent, wti = s("BRENT"), s("WTI")
    spreads = _spread_series(brent, wti)
    oil: list[DriverItem] = []
    if spreads:
        year = [v for d, v in spreads if d > spreads[-1][0] - timedelta(days=365)]
        avg_spread = sum(year, ZERO) / len(year)
        oil.append(
            DriverItem(
                "Brent minus WTI", spreads[-1][1], "USD per barrel",
                f"The average of the last year is {avg_spread:.2f}.",
            )
        )  # fmt: skip
    else:
        oil.append(DriverItem("Brent minus WTI", None, "", "no data", False))
    oil.append(
        DriverItem(
            "Weekly US oil inventories (EIA)", None, "",
            "Not connected: the EIA data needs an API key this app does not hold.", False,
        )
    )  # fmt: skip
    groups.append(DriverGroup("Oil", oil))
    return groups


def _ratio_series(a: Series, b: Series) -> Series:
    bd = dict(b)
    return [(d, v / bd[d]) for d, v in a if d in bd and bd[d] > 0]


def _spread_series(a: Series, b: Series) -> Series:
    bd = dict(b)
    return [(d, v - bd[d]) for d, v in a if d in bd]


# --- FR-63: the regime -------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    name: str
    vote: int  # +1 risk-on, -1 risk-off, 0 neutral, only counted when available
    available: bool
    detail: str


@dataclass(frozen=True)
class Regime:
    label: str  # risk-on | risk-off | mixed | unknown
    score: int
    signals: list[Signal]
    implication: str
    disclaimer: str = (
        "A rule of thumb on five public series. It is not a forecast, it does not change your "
        "targets, and it is not investment advice."
    )


IMPLICATIONS = {
    "risk-on": (
        "Conditions have tended to favour growth and tech shares. Nothing here asks you to "
        "change your target weights."
    ),
    "risk-off": (
        "Rising real yields, a strong dollar or wider credit spreads have tended to hurt "
        "long-duration growth shares. It is a reason to check your limits and your cash, not to "
        "sell by rule."
    ),
    "mixed": "The signals disagree. There is no tilt to read from them.",
    "unknown": "Too few of the series have data yet.",
}


def regime(session: Session, today: date) -> Regime:
    since = today - timedelta(days=HISTORY_DAYS)

    def s(code: str) -> Series:
        return load_series(session, code, since, today)

    signals: list[Signal] = []

    pp = point_change(s("DFII10"), 90)
    signals.append(_signal("Real yield (3 months)", pp, Decimal("0.25"), "points", invert=True))
    usd = change(s("DTWEXBGS"), 90)
    signals.append(_signal("US dollar (3 months)", usd, Decimal(2), "%", invert=True))
    hy = point_change(s("BAMLH0A0HYM2"), 90)
    signals.append(_signal("Credit spread (3 months)", hy, Decimal("0.5"), "points", invert=True))
    cpi = s("CPIAUCSL")
    signals.append(_inflation_signal(cpi))
    brent = change(s("BRENT"), 90)
    signals.append(_signal("Brent oil (3 months)", brent, Decimal(15), "%", invert=True))

    counted = [x for x in signals if x.available]
    score = sum(x.vote for x in counted)
    if len(counted) < 3:
        label = "unknown"
    elif score >= 2:
        label = "risk-on"
    elif score <= -2:
        label = "risk-off"
    else:
        label = "mixed"
    return Regime(label, score, signals, IMPLICATIONS[label])


def _signal(name: str, value: Decimal | None, limit: Decimal, unit: str, invert: bool) -> Signal:
    """A rise above `limit` votes risk-off (a fall below -limit risk-on); `invert` flips that."""
    if value is None:
        return Signal(name, 0, False, "no data")
    vote = 0
    if value > limit:
        vote = -1 if invert else 1
    elif value < -limit:
        vote = 1 if invert else -1
    word = {1: "risk-on", -1: "risk-off", 0: "neutral"}[vote]
    return Signal(name, vote, True, f"{value:+.2f} {unit}: {word}")


def _inflation_signal(cpi: Series) -> Signal:
    """Year-on-year inflation now against three months ago."""
    if len(cpi) < 2:
        return Signal("Inflation (3 months)", 0, False, "no data")
    last_day, last = cpi[-1]
    year_ago = value_on(cpi, last_day - timedelta(days=365), gap=45)
    prior_day = last_day - timedelta(days=90)
    prior = value_on(cpi, prior_day, gap=45)
    prior_year_ago = value_on(cpi, prior_day - timedelta(days=365), gap=45)
    if not year_ago or not prior or not prior_year_ago:
        return Signal("Inflation (3 months)", 0, False, "no data")
    now_yoy = (last / year_ago - 1) * HUNDRED
    then_yoy = (prior / prior_year_ago - 1) * HUNDRED
    diff = now_yoy - then_yoy
    sig = _signal("Inflation (3 months)", diff, Decimal("0.3"), "points", invert=True)
    return Signal(sig.name, sig.vote, True, f"{now_yoy:.1f}% a year, {sig.detail}")


# --- FR-64: is gold hedging? ---------------------------------------------------------------------


@dataclass(frozen=True)
class HedgeWindow:
    days: int
    observations: int
    correlation: float | None


@dataclass
class Hedge:
    windows: list[HedgeWindow] = field(default_factory=list)
    verdict: str = ""
    available: bool = False


def hedge_correlation(session: Session, today: date, daily: dict[date, float]) -> Hedge:
    """Correlation of the portfolio's daily returns with gold's (in euros), over the last 60,
    120 and 250 days both have a return."""
    out = Hedge()
    since = today - timedelta(days=HISTORY_DAYS)
    gold = load_series(session, "GOLD", since, today)
    if len(gold) < MIN_HEDGE_DAYS or not daily:
        out.verdict = "Not enough history of gold prices or of your portfolio yet."
        return out
    fx = usd_to_eur(session, since, today)
    eur: Series = []
    for d, v in gold:
        converted = fx.to_eur(v, "USD", d)
        if converted is not None:
            eur.append((d, converted))
    gold_returns = returns_of(eur)
    days = sorted(gold_returns.keys() & daily.keys())
    for window in HEDGE_WINDOWS:
        used = days[-window:]
        corr = (
            metrics.correlation([daily[d] for d in used], [gold_returns[d] for d in used])
            if len(used) >= MIN_HEDGE_DAYS
            else None
        )
        out.windows.append(HedgeWindow(window, len(used), corr))
    out.available = any(w.correlation is not None for w in out.windows)
    first = next((w.correlation for w in out.windows if w.correlation is not None), None)
    out.verdict = hedge_verdict(first)
    return out


def hedge_verdict(correlation: float | None) -> str:
    """What a correlation says about gold as a hedge, in plain words."""
    if correlation is None:
        return "Not enough shared days yet."
    if correlation <= -0.1:
        return "Gold has tended to move against your portfolio: it has been hedging."
    if correlation < 0.3:
        return (
            "Gold has moved almost independently of your portfolio: it diversifies, but it is "
            "not a hedge that pays when your shares fall."
        )
    return "Gold has moved with your portfolio lately: it is not hedging you."

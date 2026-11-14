"""Stock factor scores (FR-30, FR-31, FR-34, FR-35).

Every share in the universe gets five factor scores from 0 to 100 and a composite:

- *momentum* and *low volatility* come from the stored prices, so every share with a year of
  prices has them;
- *value*, *quality* and *growth* need fundamentals. The free price sources give none, so an
  admin loads them (`Fundamental`). A share without them has no value, quality or growth score.

Each factor is the mean of percentile ranks of its inputs, ranked against the share's sector
peers (the whole universe when the sector has fewer than `MIN_PEERS` names, which is flagged).
An input that is missing is left out and never made up. The composite uses the weights of the
Growth profile (D26), re-spread over the factors that exist.

Data quality (FR-34): the score says how far to trust it. It is `ok` only when all five factors
exist and nothing is stale; `poor` when fewer than three exist, the prices are stale or the
history is short. A score is never shown without its flags.

One row a share and day is kept for ever (FR-35), so a trend can be shown and backtested.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult
from quant.models import FactorScore, Fundamental, Instrument, PriceEOD
from quant.portfolio import metrics

# Growth profile (D26). Re-spread over the factors a share has.
WEIGHTS: dict[str, int] = {"momentum": 30, "quality": 25, "growth": 25, "value": 10, "low_vol": 10}
FACTORS = tuple(WEIGHTS)
MIN_PEERS = 5
MIN_FACTORS_FOR_COMPOSITE = 2
MIN_FACTORS_NOT_POOR = 3
HISTORY_DAYS = 400  # prices read for one score
MIN_HISTORY_DAYS = 300  # calendar days the history must span for a 12-1 month momentum
STALE_PRICE_DAYS = 7
STALE_FUNDAMENTALS_DAYS = 200
BENCHMARK = "SXRV"  # the Nasdaq-100 ETF: beta is measured against it
TRADING_DAYS = 252
MA_DAYS = 200

FLAG_TEXT = {
    "no_fundamentals": "No fundamentals loaded: value, quality and growth are missing.",
    "partial_inputs": "Some inputs of a factor are missing. It uses the rest.",
    "stale_price": "The newest price is old.",
    "short_history": "Under a year of prices: momentum and risk may be missing or weak.",
    "stale_fundamentals": "The fundamentals are over six months old.",
    "small_peer_group": "Fewer than five peers in the sector: ranked against the whole universe.",
}

# For each factor: (input name, higher is better).
INPUTS: dict[str, tuple[tuple[str, bool], ...]] = {
    "momentum": (("ret_12_1", True), ("ret_6m", True), ("dist_ma200", True)),
    "low_vol": (("vol_1y", False), ("drawdown_1y", False), ("beta", False)),
    "value": (("ev_ebit", False), ("fcf_yield", True), ("ev_sales", False)),
    "quality": (("roic", True), ("gross_margin", True), ("net_debt_ebitda", False)),
    "growth": (("revenue_growth", True), ("revenue_cagr_3y", True), ("eps_revision", True)),
}
FUNDAMENTAL_FACTORS = ("value", "quality", "growth")
FUNDAMENTAL_FIELDS = tuple(name for factor in FUNDAMENTAL_FACTORS for name, _ in INPUTS[factor])
# A multiple of a company with a loss is not "cheap": it is missing.
POSITIVE_ONLY = ("ev_ebit", "ev_sales")

Series = list[tuple[date, float]]


@dataclass
class Raw:
    """The inputs of one share, before ranking. None: not available."""

    isin: str
    name: str
    sector: str | None
    values: dict[str, float | None] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)


@dataclass
class Scored:
    isin: str
    factors: dict[str, float | None]
    composite: float | None
    data_quality: str
    flags: list[str]
    peers: int


def percentile_ranks(values: dict[str, float], higher_is_better: bool) -> dict[str, float]:
    """Rank each key 0 to 100 against the others (ties share the middle of their range). A lone
    key is 50: there is nothing to compare it with."""
    n = len(values)
    if n == 0:
        return {}
    if n == 1:
        return {k: 50.0 for k in values}
    out: dict[str, float] = {}
    for key, v in values.items():
        below = sum(1 for o in values.values() if o < v)
        equal = sum(1 for o in values.values() if o == v) - 1
        rank = (below + equal / 2) / (n - 1) * 100
        out[key] = rank if higher_is_better else 100 - rank
    return out


def _value_at(series: Series, day: date, gap: int = 7) -> float | None:
    """The close on `day`, or the latest one up to `gap` days earlier."""
    best: float | None = None
    for d, v in series:
        if d > day:
            break
        if (day - d).days <= gap:
            best = v
    return best


def price_inputs(
    series: Series, benchmark_returns: dict[date, float] | None
) -> tuple[dict[str, float | None], set[str]]:
    """Momentum and risk inputs from closes (oldest first), and the flags they raise."""
    flags: set[str] = set()
    out: dict[str, float | None] = {
        name: None for f in ("momentum", "low_vol") for name, _ in INPUTS[f]
    }
    if len(series) < 2:
        flags.add("short_history")
        return out, flags
    last_day, last = series[-1]
    if (series[-1][0] - series[0][0]).days < MIN_HISTORY_DAYS:
        flags.add("short_history")
    old = _value_at(series, last_day - timedelta(days=365))
    month = _value_at(series, last_day - timedelta(days=30))
    half = _value_at(series, last_day - timedelta(days=182))
    if old and month and old > 0:
        out["ret_12_1"] = month / old - 1
    if half and half > 0:
        out["ret_6m"] = last / half - 1
    if len(series) >= MA_DAYS:
        ma = sum(v for _, v in series[-MA_DAYS:]) / MA_DAYS
        if ma > 0:
            out["dist_ma200"] = last / ma - 1
    year = [(d, v) for d, v in series if d >= last_day - timedelta(days=365)]
    returns: dict[date, float] = {}
    for (_, a), (d, b) in zip(year, year[1:], strict=False):
        if a > 0 and b > 0:
            returns[d] = math.log(b / a)
    vol = metrics.annualised_volatility(list(returns.values()))
    if vol is not None:
        out["vol_1y"] = vol
    dd = metrics.max_drawdown([d for d, _ in year], [v for _, v in year])
    if dd is not None and len(year) >= 60:
        out["drawdown_1y"] = dd[0]
    if benchmark_returns:
        out["beta"] = metrics.beta(returns, benchmark_returns)
    return out, flags


def fundamental_inputs(
    row: Fundamental | None, today: date
) -> tuple[dict[str, float | None], set[str]]:
    flags: set[str] = set()
    out: dict[str, float | None] = {name: None for name in FUNDAMENTAL_FIELDS}
    if row is None:
        flags.add("no_fundamentals")
        return out, flags
    for name in FUNDAMENTAL_FIELDS:
        raw = getattr(row, name)
        if raw is None:
            continue
        v = float(raw)
        if name in POSITIVE_ONLY and v <= 0:
            continue
        out[name] = v
    if (today - row.as_of).days > STALE_FUNDAMENTALS_DAYS:
        flags.add("stale_fundamentals")
    if all(v is None for v in out.values()):
        flags.add("no_fundamentals")
    return out, flags


def score_all(raws: list[Raw]) -> dict[str, Scored]:
    """Rank every share against its sector peers and build the factors and the composite."""
    by_sector: dict[str | None, list[Raw]] = defaultdict(list)
    for r in raws:
        by_sector[r.sector].append(r)

    # ranks[isin][input] = 0..100 against the peer group
    ranks: dict[str, dict[str, float]] = {r.isin: {} for r in raws}
    small: set[str] = set()
    for sector, members in by_sector.items():
        group = members
        if sector is None or len(members) < MIN_PEERS:
            group = raws
            small.update(m.isin for m in members)
        own = {m.isin for m in members}
        for factor in FACTORS:
            for name, higher in INPUTS[factor]:
                known = {m.isin: v for m in group if (v := m.values.get(name)) is not None}
                for isin, rank in percentile_ranks(known, higher).items():
                    if isin in own:
                        ranks[isin][name] = rank

    out: dict[str, Scored] = {}
    for r in raws:
        factors: dict[str, float | None] = {}
        flags = set(r.flags)
        for factor in FACTORS:
            parts = [ranks[r.isin][n] for n, _ in INPUTS[factor] if n in ranks[r.isin]]
            factors[factor] = sum(parts) / len(parts) if parts else None
            if parts and len(parts) < len(INPUTS[factor]):
                flags.add("partial_inputs")
        if r.isin in small:
            flags.add("small_peer_group")
        have = {f: v for f, v in factors.items() if v is not None}
        composite: float | None = None
        if len(have) >= MIN_FACTORS_FOR_COMPOSITE:
            total = sum(WEIGHTS[f] for f in have)
            composite = sum(v * WEIGHTS[f] for f, v in have.items()) / total
        peers = len(by_sector[r.sector]) if r.sector is not None else len(raws)
        out[r.isin] = Scored(
            r.isin, factors, composite, _data_quality(len(have), flags), sorted(flags), peers
        )
    return out


def _data_quality(present: int, flags: set[str]) -> str:
    if present < MIN_FACTORS_NOT_POOR or "stale_price" in flags or "short_history" in flags:
        return "poor"
    if present < len(FACTORS) or flags:
        return "partial"
    return "ok"


def universe(session: Session) -> list[Instrument]:
    """Every active share the app prices: held, watched, picked or on a list (FR-30). The mapping
    job turns each of those ISINs into an instrument, so this is the union."""
    return list(
        session.scalars(
            select(Instrument)
            .where(
                Instrument.asset_class == "stock",
                Instrument.active.is_(True),
                Instrument.isin.is_not(None),
            )
            .order_by(Instrument.code)
        )
    )


def _series(
    session: Session, instrument_ids: list[int], since: date, until: date
) -> dict[int, Series]:
    out: dict[int, Series] = defaultdict(list)
    rows = session.execute(
        select(PriceEOD.instrument_id, PriceEOD.date, PriceEOD.close)
        .where(
            PriceEOD.instrument_id.in_(instrument_ids),
            PriceEOD.date >= since,
            PriceEOD.date <= until,
        )
        .order_by(PriceEOD.instrument_id, PriceEOD.date)
    ).all()
    for iid, d, c in rows:
        out[iid].append((d, float(c)))
    return out


def _daily_returns(series: Series) -> dict[date, float]:
    return {
        d: math.log(b / a)
        for (_, a), (d, b) in zip(series, series[1:], strict=False)
        if a > 0 and b > 0
    }


def latest_fundamentals(session: Session, isins: list[str]) -> dict[str, Fundamental]:
    out: dict[str, Fundamental] = {}
    for row in session.scalars(
        select(Fundamental).where(Fundamental.isin.in_(isins)).order_by(Fundamental.as_of)
    ):
        out[row.isin] = row  # the newest wins
    return out


def _dec(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(round(v, 2)))


def compute_scores(session: Session, today: date) -> JobResult:
    """Score the whole universe for `today` and store one row a share (a rerun the same day
    replaces that day's rows)."""
    result = JobResult()
    shares = universe(session)
    result.attempted = len(shares)
    if not shares:
        return result
    series = _series(session, [s.id for s in shares], today - timedelta(days=HISTORY_DAYS), today)
    bench_id = session.scalar(select(Instrument.id).where(Instrument.code == BENCHMARK))
    bench: dict[date, float] | None = None
    if bench_id is not None:
        bench = _daily_returns(
            _series(session, [bench_id], today - timedelta(days=HISTORY_DAYS), today)[bench_id]
        )
    isins = [s.isin for s in shares if s.isin]
    fundamentals = latest_fundamentals(session, isins)

    raws: list[Raw] = []
    for s in shares:
        if not s.isin:
            continue
        values: dict[str, float | None] = {}
        flags: set[str] = set()
        closes = series.get(s.id, [])
        p_values, p_flags = price_inputs(closes, bench)
        f_values, f_flags = fundamental_inputs(fundamentals.get(s.isin), today)
        values.update(p_values)
        values.update(f_values)
        flags |= p_flags | f_flags
        if not closes or (today - closes[-1][0]).days > STALE_PRICE_DAYS:
            flags.add("stale_price")
        raws.append(Raw(s.isin, s.name, s.sector, values, flags))

    scored = score_all(raws)
    ranked = sorted(v.composite for v in scored.values() if v.composite is not None)
    existing = {
        row.isin: row
        for row in session.scalars(select(FactorScore).where(FactorScore.day == today))
    }
    for isin, sc in scored.items():
        pct: float | None = None
        if sc.composite is not None and len(ranked) > 1:
            below = sum(1 for c in ranked if c < sc.composite)
            equal = sum(1 for c in ranked if c == sc.composite) - 1
            pct = (below + equal / 2) / (len(ranked) - 1) * 100
        elif sc.composite is not None:
            pct = 50.0
        row = existing.get(isin) or FactorScore(isin=isin, day=today)
        row.value = _dec(sc.factors["value"])
        row.quality = _dec(sc.factors["quality"])
        row.growth = _dec(sc.factors["growth"])
        row.momentum = _dec(sc.factors["momentum"])
        row.low_vol = _dec(sc.factors["low_vol"])
        row.composite = _dec(sc.composite)
        row.composite_pct = _dec(pct)
        row.data_quality = sc.data_quality
        row.flags = sc.flags
        row.peers = sc.peers
        session.add(row)
        result.rows_written += 1
    session.commit()
    return result


# --- reading -------------------------------------------------------------------------------

TREND_POINTS = (90, 60, 30, 0)  # days back, oldest first
TREND_MIN_MOVE = 5.0  # points a factor must move over the three months to count as a trend


def latest(session: Session, isin: str) -> FactorScore | None:
    return session.scalar(
        select(FactorScore)
        .where(FactorScore.isin == isin)
        .order_by(FactorScore.day.desc())
        .limit(1)
    )


def fresh(session: Session, isin: str, today: date, max_age: int = 7) -> FactorScore | None:
    """The newest score of a share, or None when there is none or it is over `max_age` days old."""
    row = latest(session, isin)
    if row is None or (today - row.day).days > max_age:
        return None
    return row


def history(session: Session, isin: str, days: int, today: date) -> list[FactorScore]:
    return list(
        session.scalars(
            select(FactorScore)
            .where(FactorScore.isin == isin, FactorScore.day >= today - timedelta(days=days))
            .order_by(FactorScore.day)
        )
    )


def trend_notes(rows: list[FactorScore]) -> list[str]:
    """Plain sentences about the factors that moved steadily over about three months: "Quality has
    fallen for 3 months". Needs scores from four points three months apart (+- 10 days), so a young
    history says nothing."""
    if len(rows) < 2:
        return []
    newest = rows[-1]
    notes: list[str] = []
    points: list[FactorScore] = []
    for back in TREND_POINTS:
        target = newest.day - timedelta(days=back)
        near = min(rows, key=lambda r: abs((r.day - target).days))
        if abs((near.day - target).days) > 10:
            return []
        points.append(near)
    for factor in (*FACTORS, "composite"):
        series = [getattr(p, factor) for p in points]
        if any(v is None for v in series):
            continue
        values = [float(v) for v in series]
        steps = [b - a for a, b in zip(values, values[1:], strict=False)]
        label = (
            "The composite score"
            if factor == "composite"
            else f"{factor.replace('_', ' ').capitalize()}"
        )
        if all(s < 0 for s in steps) and values[0] - values[-1] >= TREND_MIN_MOVE:
            notes.append(f"{label} has fallen for 3 months ({values[0]:.0f} to {values[-1]:.0f}).")
        elif all(s > 0 for s in steps) and values[-1] - values[0] >= TREND_MIN_MOVE:
            notes.append(f"{label} has risen for 3 months ({values[0]:.0f} to {values[-1]:.0f}).")
    return notes

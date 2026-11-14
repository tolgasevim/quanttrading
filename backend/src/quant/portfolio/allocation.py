"""Target allocation, drift, guardrails, rebalancing and the cash deployment plan
(FR-40, FR-41, FR-42, FR-43, FR-49).

The portfolio here is the valued securities plus the *investable* cash (the balance minus the
reserves, FR-28a). The trading book (leveraged products and knock-outs, FR-48) is left out of the
drift maths and has its own cap against the whole.

**Buckets** (D26): tech single stocks, broad and tech ETFs, crypto (coins), commodity hedge, cash,
and "other" (non-tech shares, bond funds, private-market funds). A share is "tech" when it is in
one of the nine tech sub-groups (`subgroups.py`, FR-47), which the user can override. Each bucket
has a target and a band. A bucket outside its band is drift (FR-41).

**Guardrails** (FR-43) work on the look-through view (`lookthrough.py`): a single share, a sector
and a country as a share of the portfolio, the cash minimum, the combined crypto cap, the trading
book cap and a drawdown alert.

**Suggestions** (FR-42) put cash flow first: new money (free cash above the target) goes to the
buckets that are short; a savings plan in a bucket that is too big is turned down and the money is
sent to a bucket that is short; selling comes last, with an estimate of the gain it would realise.
Nothing is placed: these are instructions for the user to carry out at the broker.

**Deployment plan** (FR-49): when the free cash is above the cash band, the excess is split into
monthly tranches (6 by default), each steered to the buckets that are short. The plan is stored
when the user starts it; purchases made since then tick the tranches off.
"""

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import AllocationSettings, Instrument, PriceEOD, Transaction
from quant.portfolio import book, cash, lookthrough, performance, service, subgroups
from quant.portfolio import instruments as kinds

ZERO = Decimal(0)
HUNDRED = Decimal(100)
CENT = Decimal("0.01")

TECH, ETFS, CRYPTO, COMMODITIES, CASH, OTHER = (
    "tech_stocks",
    "etfs",
    "crypto",
    "commodities",
    "cash",
    "other",
)
BUCKETS = (TECH, ETFS, CRYPTO, COMMODITIES, CASH, OTHER)
TRADING_BOOK = "trading_book"
LABELS = {
    TECH: "Tech single stocks",
    ETFS: "Broad and tech ETFs",
    CRYPTO: "Crypto",
    COMMODITIES: "Commodity hedge",
    CASH: "Cash",
    OTHER: "Other (non-tech shares, bond and private funds)",
    TRADING_BOOK: "Trading book",
}
# Buckets that new money may go to.
INVESTABLE = (TECH, ETFS, CRYPTO, COMMODITIES)


@dataclass(frozen=True)
class Band:
    target: Decimal
    low: Decimal
    high: Decimal


# D26 / FR-40. "Other" takes the 5% the five named buckets leave.
DEFAULT_TARGETS: dict[str, Band] = {
    TECH: Band(Decimal(35), Decimal(30), Decimal(40)),
    ETFS: Band(Decimal(45), Decimal(40), Decimal(50)),
    CRYPTO: Band(Decimal(7), Decimal(0), Decimal(10)),
    COMMODITIES: Band(Decimal(3), Decimal(0), Decimal(5)),
    CASH: Band(cash.CASH_TARGET_PCT, cash.CASH_BAND_LOW_PCT, cash.CASH_BAND_HIGH_PCT),
    OTHER: Band(Decimal(5), Decimal(0), Decimal(10)),
}


@dataclass(frozen=True)
class Guardrails:
    max_single_stock: Decimal = Decimal(15)  # D17, on look-through exposure
    max_sector: Decimal = Decimal(60)
    max_country: Decimal = Decimal(80)
    min_cash: Decimal = cash.CASH_BAND_LOW_PCT
    max_crypto: Decimal = Decimal(10)  # D32: coins plus crypto-linked shares
    max_trading_book: Decimal = Decimal(3)  # D26
    drawdown_alert: Decimal = Decimal(20)  # percent fall from the portfolio's peak


GUARDRAIL_FIELDS = tuple(Guardrails.__dataclass_fields__)
DEFAULT_TRANCHES = 6
MAX_TRANCHES = 12
PULL_FORWARD_FALL_PCT = Decimal(10)
PLAN_LOOKBACK_DAYS = 90
SHIFT_STEP = Decimal(5)  # savings-plan changes are suggested in steps of 5 euros
MIN_DRAWDOWN_DAYS = 30
SHIFT_MONTHS = 6  # a surplus is worked off over about this many months of plan changes


class AllocationError(ValueError):
    """The request is not valid. The text says why, in plain words."""


# --- settings ------------------------------------------------------------------------------


def _decimal(value: Any, what: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except InvalidOperation as error:
        raise AllocationError(f"{what} is not a number") from error
    if not out.is_finite():
        raise AllocationError(f"{what} is not a number")
    return out


def validate_targets(raw: dict[str, tuple[Decimal, Decimal, Decimal]]) -> dict[str, Band]:
    """Targets as (target, low, high) in percent for every bucket. The targets may not add up
    to more than 100, and each band must hold its target."""
    missing = set(BUCKETS) - raw.keys()
    unknown = raw.keys() - set(BUCKETS)
    if missing or unknown:
        raise AllocationError(f"the buckets must be exactly: {', '.join(BUCKETS)}")
    out: dict[str, Band] = {}
    for bucket, (target, low, high) in raw.items():
        if not ZERO <= low <= target <= high <= HUNDRED:
            raise AllocationError(
                f"{LABELS[bucket]}: the band must satisfy 0 <= low <= target <= high <= 100"
            )
        out[bucket] = Band(target, low, high)
    total = sum((b.target for b in out.values()), ZERO)
    if total > HUNDRED:
        raise AllocationError(f"the targets add up to {total}%, which is more than 100%")
    return out


def validate_guardrails(values: dict[str, Decimal]) -> Guardrails:
    for name, value in values.items():
        if name not in GUARDRAIL_FIELDS:
            raise AllocationError(f"unknown guardrail {name}")
        if not ZERO <= value <= HUNDRED:
            raise AllocationError(f"{name} must be between 0 and 100")
    merged = {**{n: getattr(Guardrails(), n) for n in GUARDRAIL_FIELDS}, **values}
    return Guardrails(**merged)


@dataclass(frozen=True)
class Deployment:
    started: date
    tranches: int
    excess: Decimal  # the free cash above the target when the plan started
    index_close: Decimal | None  # the Nasdaq-100 fund's close then (for the pull-forward offer)


@dataclass(frozen=True)
class Settings:
    targets: dict[str, Band]
    guardrails: Guardrails
    deployment: Deployment | None
    custom_targets: bool
    custom_guardrails: bool


def load_settings(session: Session, user_id: uuid.UUID) -> Settings:
    row = session.get(AllocationSettings, user_id)
    targets = dict(DEFAULT_TARGETS)
    guardrails = Guardrails()
    deployment: Deployment | None = None
    if row is not None:
        if row.targets:
            targets = {
                b: Band(Decimal(v["target"]), Decimal(v["low"]), Decimal(v["high"]))
                for b, v in row.targets.items()
                if b in BUCKETS
            }
            targets = {b: targets.get(b, DEFAULT_TARGETS[b]) for b in BUCKETS}
        if row.guardrails:
            guardrails = Guardrails(
                **{
                    **{n: getattr(guardrails, n) for n in GUARDRAIL_FIELDS},
                    **{n: Decimal(v) for n, v in row.guardrails.items() if n in GUARDRAIL_FIELDS},
                }
            )
        if row.deployment:
            d = row.deployment
            close = d.get("index_close")
            deployment = Deployment(
                started=date.fromisoformat(str(d["started"])),
                tranches=int(str(d["tranches"])),
                excess=Decimal(str(d["excess"])),
                index_close=None if close is None else Decimal(str(close)),
            )
    return Settings(
        targets,
        guardrails,
        deployment,
        custom_targets=bool(row and row.targets),
        custom_guardrails=bool(row and row.guardrails),
    )


def _row(session: Session, user_id: uuid.UUID) -> AllocationSettings:
    row = session.get(AllocationSettings, user_id)
    if row is None:
        row = AllocationSettings(user_id=user_id)
        session.add(row)
    return row


def save_targets(session: Session, user_id: uuid.UUID, bands: dict[str, Band]) -> None:
    row = _row(session, user_id)
    row.targets = {
        b: {"target": str(v.target), "low": str(v.low), "high": str(v.high)}
        for b, v in bands.items()
    }
    session.commit()


def reset_targets(session: Session, user_id: uuid.UUID) -> None:
    row = _row(session, user_id)
    row.targets = None
    session.commit()


def save_guardrails(session: Session, user_id: uuid.UUID, guardrails: Guardrails) -> None:
    row = _row(session, user_id)
    row.guardrails = {n: str(getattr(guardrails, n)) for n in GUARDRAIL_FIELDS}
    session.commit()


# --- the picture ---------------------------------------------------------------------------


def bucket_of(kind: str, subgroup: str, crypto_linked: bool) -> str:
    """The bucket a position belongs to. `subgroup` is its tech sub-group, or `non_tech`."""
    if kind == kinds.CRYPTO:
        return CRYPTO
    if kind == kinds.ETC:
        return COMMODITIES
    if kind in kinds.TRADING_BOOK_TYPES:
        return TRADING_BOOK
    if kind == kinds.ETF:
        return ETFS
    if kind == kinds.STOCK and (subgroups.is_tech(subgroup) or crypto_linked):
        return TECH
    return OTHER


@dataclass
class BucketView:
    bucket: str
    value: Decimal
    pct: Decimal
    target: Decimal
    low: Decimal
    high: Decimal
    status: str  # below | ok | above
    gap_pp: Decimal  # current minus target
    gap_eur: Decimal  # what it would take to reach the target (positive: needs money)


@dataclass(frozen=True)
class Rule:
    rule: str  # single_stock | sector | country | min_cash | crypto | trading_book | drawdown
    subject: str
    current_pct: Decimal
    limit_pct: Decimal
    breached: bool
    note: str = ""


@dataclass(frozen=True)
class Suggestion:
    kind: str  # new_money | redirect_plan | reduce_plan | sell
    text: str
    amount: Decimal
    bucket: str
    isin: str | None = None
    name: str | None = None
    to_bucket: str | None = None
    to_isin: str | None = None
    to_name: str | None = None
    why: str = ""
    gain_estimate: Decimal | None = None


@dataclass(frozen=True)
class Tranche:
    number: int
    due: date
    amount: Decimal
    done: bool
    split: list[tuple[str, Decimal, str | None]]  # bucket, amount, instrument name


@dataclass
class DeploymentView:
    active: bool
    started: date | None
    tranches: int
    excess: Decimal  # now (if no plan) or at the start (if there is one)
    invested_since: Decimal
    plan: list[Tranche] = field(default_factory=list)
    pull_forward: bool = False  # the market fell more than 10% since the start: offer only
    pull_forward_note: str = ""


@dataclass(frozen=True)
class SubgroupRow:
    key: str
    label: str
    value: Decimal
    pct: Decimal  # of the portfolio base
    pct_of_tech: Decimal
    target: Decimal | None
    cap: Decimal | None
    breached: bool
    members: list[tuple[str, str, Decimal, bool]]  # isin, name, value, overridden by the user


@dataclass
class Report:
    base: Decimal = ZERO  # investable portfolio: securities (no trading book) plus free cash
    total: Decimal = ZERO  # with the trading book
    free_cash: Decimal = ZERO
    securities: Decimal = ZERO
    trading_book: Decimal = ZERO
    buckets: list[BucketView] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    plans: dict[str, Decimal] = field(default_factory=dict)  # savings plans: isin -> euro a month
    deployment: DeploymentView | None = None
    subgroups: list[SubgroupRow] = field(default_factory=list)
    non_tech: Decimal = ZERO  # direct non-tech shares (reported apart, no cap: D31)
    sub_of: dict[str, str] = field(default_factory=dict)  # isin -> sub-group of each share
    stocks: list[tuple[str, str, str, str, bool, Decimal]] = field(default_factory=list)
    # (isin, name, sub-group, automatic sub-group, overridden by the user, value) of each share
    settings: Settings | None = None
    has_portfolio: bool = False
    warnings: list[str] = field(default_factory=list)


def _pct(value: Decimal, base: Decimal) -> Decimal:
    return value / base * HUNDRED if base > 0 else ZERO


def savings_plans(session: Session, user_id: uuid.UUID, today: date) -> dict[str, Decimal]:
    """What the user puts into each savings plan a month: the plan executions of the last 90
    days, averaged over three months."""
    rows = session.execute(
        select(Transaction.isin, Transaction.amount, Transaction.fee).where(
            Transaction.user_id == user_id,
            Transaction.savings_plan.is_(True),
            Transaction.category == "TRADING",
            Transaction.date >= today - timedelta(days=PLAN_LOOKBACK_DAYS),
            Transaction.isin.is_not(None),
        )
    ).all()
    out: dict[str, Decimal] = {}
    for isin, amount, fee in rows:
        if isin and amount is not None and amount < 0:
            out[isin] = out.get(isin, ZERO) + (-amount - (fee or ZERO))
    return {isin: (v / 3).quantize(CENT) for isin, v in out.items() if v > 0}


def current_drawdown(session: Session, user_id: uuid.UUID, today: date) -> Decimal | None:
    """How far the portfolio is below its peak, in percent (time-weighted, so a deposit is no
    peak). None without enough history."""
    result = performance.analyse(session, user_id, today=today)
    levels = [p.portfolio for p in result.series]
    if len(levels) < MIN_DRAWDOWN_DAYS:
        return None
    peak = max(levels)
    return Decimal(str((peak - levels[-1]) / peak * 100)) if peak > 0 else None


def _candidate(
    bucket: str,
    members: dict[str, list[tuple[str, str, Decimal]]],
    plans: dict[str, Decimal],
) -> tuple[str, str] | None:
    """The instrument to put money into for a bucket: a position in it that has a savings plan,
    else the biggest one. None when the bucket holds nothing yet."""
    held = members.get(bucket) or []
    if not held:
        return None
    with_plan = [h for h in held if h[0] in plans]
    pool = with_plan or held
    best = max(pool, key=lambda h: plans.get(h[0], ZERO) if with_plan else h[2])
    return best[0], best[1]


def analyse(session: Session, user_id: uuid.UUID, today: date | None = None) -> Report:
    """The whole picture for one user."""
    today = today or service.today()
    settings = load_settings(session, user_id)
    report = Report(settings=settings)
    held = service.build_holdings(session, user_id)
    conc = lookthrough.concentration(session, held)
    state = cash.cash_status(session, user_id)
    report.free_cash = state.investable
    members: dict[str, list[tuple[str, str, Decimal]]] = {}
    values: dict[str, Decimal] = {b: ZERO for b in (*BUCKETS, TRADING_BOOK)}
    tags = book.load_tags(session, user_id)
    for p in conc.positions:
        group = tech_subgroup(held, p, tags)
        if p.type == kinds.STOCK:
            report.sub_of[p.isin] = group
            sector_raw, industry = held.sectors.get(p.isin, (None, None))
            auto = subgroups.auto_subgroup(
                kinds.canonical_sector(sector_raw), industry, p.name, p.isin
            )
            report.stocks.append(
                (
                    p.isin,
                    p.name,
                    group,
                    auto,
                    bool(tags.get(p.isin) and tags[p.isin].subgroup),
                    p.value,
                )
            )
        bucket = bucket_of(p.type, group, p.crypto_linked)
        if tags.get(p.isin) is not None and tags[p.isin].short_term and bucket != TRADING_BOOK:
            bucket = TRADING_BOOK  # the user tagged it a short-term trade (FR-48)
        values[bucket] += p.value
        members.setdefault(bucket, []).append((p.isin, p.name, p.value))
    values[CASH] = state.investable
    report.securities = conc.total
    report.trading_book = values[TRADING_BOOK]
    report.base = sum((values[b] for b in BUCKETS), ZERO)
    report.total = report.base + report.trading_book
    report.has_portfolio = report.total > 0
    if not report.has_portfolio:
        return report

    for bucket in BUCKETS:
        band = settings.targets[bucket]
        pct = _pct(values[bucket], report.base)
        status = "below" if pct < band.low else "above" if pct > band.high else "ok"
        report.buckets.append(
            BucketView(
                bucket=bucket,
                value=values[bucket],
                pct=pct,
                target=band.target,
                low=band.low,
                high=band.high,
                status=status,
                gap_pp=pct - band.target,
                gap_eur=(band.target - pct) / HUNDRED * report.base,
            )
        )
    report.plans = savings_plans(session, user_id, today)
    _subgroups(session, user_id, report, conc, tags, held, values)
    report.rules = _rules(session, user_id, today, report, conc, settings.guardrails)
    report.suggestions = _suggestions(report, members, held)
    report.deployment = _deployment(session, user_id, today, report, settings, members)
    if conc.unlooked:
        report.warnings.append(
            "Some funds are not looked through, so the share, sector and country limits "
            "can miss exposure inside them."
        )
    if conc.unvalued:
        report.warnings.append(f"{conc.unvalued} position(s) have no price and are left out.")
    return report


def tech_subgroup(
    held: service.Holdings, p: lookthrough.PositionView, tags: dict[str, book.Tag]
) -> str:
    """The sub-group of a position: the user's override, else the automatic tag."""
    tag = tags.get(p.isin)
    if tag is not None and tag.subgroup:
        return tag.subgroup
    sector_raw, industry = held.sectors.get(p.isin, (None, None))
    return subgroups.auto_subgroup(kinds.canonical_sector(sector_raw), industry, p.name, p.isin)


def _subgroups(
    session: Session,
    user_id: uuid.UUID,
    report: Report,
    conc: lookthrough.Concentration,
    tags: dict[str, book.Tag],
    held: service.Holdings,
    values: dict[str, Decimal],
) -> None:
    """The weight of each tech sub-group (direct shares only), with the user's optional soft
    target and cap (FR-47)."""
    limits = book.load_subgroup_limits(session, user_id)
    by_group: dict[str, list[tuple[str, str, Decimal, bool]]] = {
        g: [] for g in subgroups.TECH_GROUPS
    }
    for p in conc.positions:
        if p.type != kinds.STOCK:
            continue
        tag = tags.get(p.isin)
        group = report.sub_of[p.isin]
        if tag is not None and tag.short_term:
            continue  # a short-term trade is in the trading book, not in a long-term group
        if subgroups.is_tech(group):
            by_group[group].append((p.isin, p.name, p.value, bool(tag and tag.subgroup)))
        else:
            report.non_tech += p.value
    tech_total = sum((m[2] for rows in by_group.values() for m in rows), ZERO)
    for group in subgroups.TECH_GROUPS:
        rows = sorted(by_group[group], key=lambda m: m[2], reverse=True)
        value = sum((m[2] for m in rows), ZERO)
        limit = limits.get(group, book.SubgroupLimit())
        pct = _pct(value, report.base)
        if value <= 0 and limit.target is None and limit.cap is None:
            continue
        report.subgroups.append(
            SubgroupRow(
                group, subgroups.LABELS[group], value, pct, _pct(value, tech_total),
                limit.target, limit.cap,
                breached=limit.cap is not None and pct > limit.cap,
                members=rows,
            )
        )  # fmt: skip
    _ = held, values


def _rules(
    session: Session,
    user_id: uuid.UUID,
    today: date,
    report: Report,
    conc: lookthrough.Concentration,
    g: Guardrails,
) -> list[Rule]:
    """Every guardrail with its current value. A breach is `breached`; the rest are context."""
    out: list[Rule] = []
    whole = report.total  # guardrails are shares of everything the user holds in this app
    # The cash in these shares is the free cash, which is part of the portfolio.
    denominator = whole if whole > 0 else Decimal(1)

    def add(rule: str, subject: str, value: Decimal, limit: Decimal, note: str = "") -> None:
        pct = _pct(value, denominator)
        out.append(Rule(rule, subject, pct, limit, pct > limit, note))

    for u in conc.underlying:
        if u.type == kinds.STOCK:
            add("single_stock", u.name, u.total, g.max_single_stock,
                "direct holding plus the share inside funds")  # fmt: skip
    skip = {
        lookthrough.NOT_LOOKED_THROUGH,
        lookthrough.OTHER_IN_FUNDS,
        kinds.UNKNOWN,
        kinds.CRYPTO_SECTOR,
        "Commodities",
        "??",
        "n/a",
    }
    for sector, value in conc.by_sector.items():
        if sector not in skip:
            add("sector", sector, value, g.max_sector)
    for country, value in conc.by_country.items():
        if country not in skip:
            add("country", country, value, g.max_country)
    for row in report.subgroups:
        if row.cap is not None:
            add("subgroup", row.label, row.value, row.cap, "direct shares only")
    add("crypto", "Coins and crypto-linked shares", conc.crypto_value, g.max_crypto)
    add(
        "trading_book", "Leveraged products and knock-outs", report.trading_book, g.max_trading_book
    )
    cash_pct = _pct(report.free_cash, denominator)
    out.append(
        Rule(
            "min_cash",
            "Free cash",
            cash_pct,
            g.min_cash,
            cash_pct < g.min_cash,
            "lower is a breach",
        )  # fmt: skip
    )
    fall = current_drawdown(session, user_id, today)
    if fall is not None:
        out.append(
            Rule("drawdown", "Fall from the peak", fall, g.drawdown_alert, fall >= g.drawdown_alert)
        )
    # Breaches first, then by size.
    out.sort(key=lambda r: (not r.breached, -r.current_pct))
    return out


def _round_step(value: Decimal) -> Decimal:
    return (value / SHIFT_STEP).to_integral_value(rounding="ROUND_DOWN") * SHIFT_STEP


def _suggestions(
    report: Report,
    members: dict[str, list[tuple[str, str, Decimal]]],
    held: service.Holdings,
) -> list[Suggestion]:
    """Cash flow first: new money, then savings plans, and selling only when those cannot do it."""
    out: list[Suggestion] = []
    views = {b.bucket: b for b in report.buckets}
    short = [v for b in INVESTABLE if (v := views[b]).gap_eur > CENT and v.pct < v.target]
    short.sort(key=lambda v: v.gap_eur, reverse=True)
    total_gap = sum((v.gap_eur for v in short), ZERO)
    cash_view = views[CASH]
    target_cash = report.base * cash_view.target / HUNDRED
    spare = max(report.free_cash - target_cash, ZERO)

    # 1. New money: the free cash above its target goes to the buckets that are short.
    if spare > CENT and short:
        for v in short:
            part = min(v.gap_eur, spare * v.gap_eur / total_gap).quantize(CENT)
            if part < Decimal(1):
                continue
            pick = _candidate(v.bucket, members, report.plans)
            where = f" (for example {pick[1]})" if pick else ""
            out.append(
                Suggestion(
                    "new_money",
                    f"Put {part:,.2f} EUR of your free cash into {LABELS[v.bucket]}{where}.",
                    part,
                    v.bucket,
                    to_bucket=v.bucket,
                    to_isin=pick[0] if pick else None,
                    to_name=pick[1] if pick else None,
                    why=f"{LABELS[v.bucket]} is {v.pct:.1f}% against a target of {v.target:.0f}%, "
                    f"and you hold more cash than your {cash_view.target:.0f}% target.",
                )
            )

    # 2. Savings plans: turn down a plan in a bucket that is too big, send it where it is short.
    for v in report.buckets:
        if v.bucket not in INVESTABLE or v.status != "above":
            continue
        surplus = (v.pct - v.target) / HUNDRED * report.base
        plans_here = sorted(
            ((isin, name, report.plans[isin]) for isin, name, _ in members.get(v.bucket, [])
             if isin in report.plans),
            key=lambda p: p[2],
            reverse=True,
        )  # fmt: skip
        left = surplus
        for isin, name, monthly in plans_here:
            shift = min(monthly, _round_step(surplus / SHIFT_MONTHS))
            if shift < SHIFT_STEP:
                continue
            target_bucket = short[0].bucket if short else None
            pick = _candidate(target_bucket, members, report.plans) if target_bucket else None
            now, new = monthly, monthly - shift
            text = f"Reduce the savings plan {name} from {now:,.0f} to {new:,.0f} EUR a month"
            if pick and target_bucket:
                text += f" and add {shift:,.0f} EUR a month to {pick[1]}."
            elif target_bucket:
                text += f" and put {shift:,.0f} EUR a month into {LABELS[target_bucket]}."
            else:
                text += "."
            out.append(
                Suggestion(
                    "redirect_plan" if target_bucket else "reduce_plan",
                    text,
                    shift,
                    v.bucket,
                    isin=isin,
                    name=name,
                    to_bucket=target_bucket,
                    to_isin=pick[0] if pick else None,
                    to_name=pick[1] if pick else None,
                    why=f"{LABELS[v.bucket]} is {v.pct:.1f}% against a band of "
                    f"{v.low:.0f}-{v.high:.0f}%. Changing a plan needs no sale and no tax.",
                )
            )
            left -= shift * SHIFT_MONTHS
            break  # one plan change a bucket at a time

        # 3. Selling, last: only what a year of plan changes cannot work off.
        if left > report.base * Decimal("0.01") and v.pct > v.high:
            biggest = max(members.get(v.bucket, []), key=lambda m: m[2], default=None)
            if biggest is None:
                continue
            isin, name, value = biggest
            amount = min(left, value).quantize(CENT)
            gain = _gain(held, isin, amount, value)
            tax_note = (
                f" That would realise about {gain:,.2f} EUR of gain, which can be taxed."
                if gain is not None and gain > 0
                else " Check the Tax page first: a sale can create a taxable gain."
            )
            out.append(
                Suggestion(
                    "sell",
                    f"If you must act now, sell about {amount:,.2f} EUR of {name}.{tax_note}",
                    amount,
                    v.bucket,
                    isin=isin,
                    name=name,
                    why=f"{LABELS[v.bucket]} is above its band and new money cannot fix it soon.",
                    gain_estimate=gain,
                )
            )
    return out


def _gain(held: service.Holdings, isin: str, amount: Decimal, value: Decimal) -> Decimal | None:
    """The unrealised gain in the part of a position that `amount` is (a rough estimate: the
    same share of the cost)."""
    cost = held.costs.get(isin)
    if cost is None or value <= 0 or not cost.total_cost:
        return None
    share = min(amount / value, Decimal(1))
    return ((value - cost.total_cost) * share).quantize(CENT)


# --- the deployment plan (FR-49) -------------------------------------------------------------


def add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def invested_since(session: Session, user_id: uuid.UUID, start: date) -> Decimal:
    """The money put into purchases since `start` (buy amounts and their fees)."""
    rows = session.execute(
        select(Transaction.amount, Transaction.fee).where(
            Transaction.user_id == user_id,
            Transaction.category == "TRADING",
            Transaction.date >= start,
            Transaction.amount < 0,
        )
    ).all()
    return sum((-(a or ZERO) - (f or ZERO) for a, f in rows), ZERO)


def _index_close(session: Session, on: date | None = None) -> Decimal | None:
    inst = session.scalar(select(Instrument).where(Instrument.code == "SXRV"))
    if inst is None:
        return None
    query = select(PriceEOD.close).where(PriceEOD.instrument_id == inst.id)
    if on is not None:
        query = query.where(PriceEOD.date <= on)
    return session.scalar(query.order_by(PriceEOD.date.desc()).limit(1))


def _excess(report: Report) -> Decimal:
    """The free cash above the target, when the cash is above its band (else nothing)."""
    view = next(b for b in report.buckets if b.bucket == CASH)
    if view.status != "above":
        return ZERO
    return max(report.free_cash - report.base * view.target / HUNDRED, ZERO).quantize(CENT)


def _split(
    amount: Decimal,
    report: Report,
    members: dict[str, list[tuple[str, str, Decimal]]],
) -> list[tuple[str, Decimal, str | None]]:
    """One tranche across the buckets that are short, by how short they are."""
    views = {b.bucket: b for b in report.buckets}
    short = [(b, views[b].gap_eur) for b in INVESTABLE if views[b].gap_eur > CENT]
    total = sum((g for _, g in short), ZERO)
    if not short or total <= 0:
        pick = _candidate(ETFS, members, report.plans)
        return [(ETFS, amount, pick[1] if pick else None)]
    out: list[tuple[str, Decimal, str | None]] = []
    for bucket, gap in short:
        part = (amount * gap / total).quantize(CENT)
        pick = _candidate(bucket, members, report.plans)
        out.append((bucket, part, pick[1] if pick else None))
    return out


def _deployment(
    session: Session,
    user_id: uuid.UUID,
    today: date,
    report: Report,
    settings: Settings,
    members: dict[str, list[tuple[str, str, Decimal]]],
) -> DeploymentView:
    plan = settings.deployment
    if plan is None:
        excess = _excess(report)
        n = DEFAULT_TRANCHES
        view = DeploymentView(
            active=False, started=None, tranches=n, excess=excess, invested_since=ZERO
        )
        if excess > 0:
            view.plan = _tranches(excess, n, today, ZERO, report, members)
        return view
    done = invested_since(session, user_id, plan.started)
    view = DeploymentView(
        active=True,
        started=plan.started,
        tranches=plan.tranches,
        excess=plan.excess,
        invested_since=done,
        plan=_tranches(plan.excess, plan.tranches, plan.started, done, report, members),
    )
    now = _index_close(session)
    if plan.index_close and now and plan.index_close > 0:
        fall = (plan.index_close - now) / plan.index_close * HUNDRED
        if fall > PULL_FORWARD_FALL_PCT and any(not t.done for t in view.plan):
            view.pull_forward = True
            view.pull_forward_note = (
                f"The Nasdaq-100 fund is {fall:.1f}% below its close when the plan started. "
                "You may bring the remaining tranches forward. This is an offer: nothing "
                "happens unless you do it."
            )
    return view


def _tranches(
    excess: Decimal,
    n: int,
    start: date,
    done: Decimal,
    report: Report,
    members: dict[str, list[tuple[str, str, Decimal]]],
) -> list[Tranche]:
    each = (excess / n).quantize(CENT)
    out: list[Tranche] = []
    cumulative = ZERO
    for i in range(n):
        amount = each if i < n - 1 else (excess - each * (n - 1)).quantize(CENT)
        cumulative += amount
        out.append(
            Tranche(
                number=i + 1,
                due=add_months(start, i),
                amount=amount,
                # A tranche counts as done once the purchases cover it (within 10%).
                done=done >= cumulative * Decimal("0.9"),
                split=_split(amount, report, members),
            )
        )
    return out


def start_deployment(
    session: Session, user_id: uuid.UUID, tranches: int, today: date | None = None
) -> Deployment:
    today = today or service.today()
    if not 1 <= tranches <= MAX_TRANCHES:
        raise AllocationError(f"the number of tranches must be from 1 to {MAX_TRANCHES}")
    report = analyse(session, user_id, today)
    excess = _excess(report)
    if excess <= 0:
        raise AllocationError("there is no cash above the cash band to deploy")
    plan = Deployment(today, tranches, excess, _index_close(session))
    row = _row(session, user_id)
    row.deployment = {
        "started": plan.started.isoformat(),
        "tranches": plan.tranches,
        "excess": str(plan.excess),
        "index_close": None if plan.index_close is None else str(plan.index_close),
    }
    session.commit()
    return plan


def cancel_deployment(session: Session, user_id: uuid.UUID) -> None:
    row = _row(session, user_id)
    row.deployment = None
    session.commit()

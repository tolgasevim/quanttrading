"""Consolidation and redundancy (FR-45, FR-46).

The app steers towards a leaner portfolio, but **being small is never on its own a reason to
sell** (D25). A position under 0.5% of the securities gets one of these statuses:

- `protected`: the user pinned it. (A top-30% score in its sector will protect it too, once the
  factor scores exist, FR-31.) It is never suggested for exit.
- `consolidate`: it is redundant with a bigger holding: the same company under another name, or a
  fund that overlaps another fund by 70% or more (FR-46) and is not marked intentional.
- `exit_candidate`: its thesis looks broken: no usable price at all. A weak score will count here
  once scores exist.
- `tracker`: the user keeps it on purpose; no prompts for the period they chose (6 months by
  default in the page), then it comes up again as `review`.
- `review`: small, nothing wrong with it as far as the app can tell. It is left alone.

Prompts are limited to N a week (3 by default), most urgent first. Fund overlap uses the holdings
files (`lookthrough.py`): the shared weight, summed over the names both funds hold. Without files
the app falls back to the index in the fund's name ("Nasdaq 100", "MSCI World"). An intentional
overlap (D28) is left out of the prompts but still counts in look-through and in the caps; it is
asked again once a year.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import combinations

from sqlalchemy.orm import Session

from quant.models import EtfHolding
from quant.portfolio import allocation, book, lookthrough, service
from quant.portfolio import instruments as kinds

ZERO = Decimal(0)
HUNDRED = Decimal(100)
SMALL_PCT = Decimal("0.5")
OVERLAP_PCT = Decimal(70)
RECONFIRM_DAYS = 365
TRACKER_MONTHS = 6

COMPANY_NOISE = frozenset(
    [
        "inc", "corp", "corporation", "co", "company", "ltd", "plc", "ag", "se", "sa", "nv",
        "class", "a", "b", "c", "ordinary", "shares", "share", "registered", "namen", "akt",
        "adr", "ads", "the", "holdings", "holding", "group",
    ]
)  # fmt: skip

# The index a fund tracks, from its name, and a word that says it is a variant of it.
INDEX_BASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nasdaq100", re.compile(r"nasdaq[\s-]*100|\bndx\b", re.I)),
    ("sp500", re.compile(r"s&p[\s-]*500|\bsp[\s-]*500\b", re.I)),
    ("msci_world", re.compile(r"msci[\s-]*world", re.I)),
    ("msci_acwi", re.compile(r"msci[\s-]*acwi", re.I)),
    ("msci_em", re.compile(r"msci[\s-]*(emerging|em\b)", re.I)),
    ("ftse_all_world", re.compile(r"ftse[\s-]*all[\s-]*world", re.I)),
    ("stoxx600", re.compile(r"stoxx[\s-]*(europe[\s-]*)?600", re.I)),
    ("msci_usa", re.compile(r"msci[\s-]*usa", re.I)),
)
FACTOR_WORDS = (
    "momentum", "quality", "value", "small cap", "equal weight", "minimum volatility",
    "min vol", "dividend", "semiconductor", "technology", "esg", "sri", "hedged",
)  # fmt: skip


def company_key(name: str) -> str:
    """The name of a company, stripped of legal forms and share classes."""
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in COMPANY_NOISE)


def index_key(name: str) -> str | None:
    """The index a fund tracks and its factor words, as one comparable text; None if the name
    names no index we know."""
    for base, pattern in INDEX_BASES:
        if pattern.search(name):
            low = name.lower()
            factors = sorted(w for w in FACTOR_WORDS if w in low)
            return base + "|" + ",".join(factors)
    return None


def shared_weight(a: list[EtfHolding], b: list[EtfHolding]) -> Decimal:
    """How much of two funds is the same: the smaller weight of each name both hold, summed."""

    def keyed(rows: list[EtfHolding]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for r in rows:
            key = r.holding_isin or (f"t:{r.ticker}" if r.ticker else f"n:{r.name.upper()}")
            out[key] = out.get(key, ZERO) + r.weight
        return out

    ka, kb = keyed(a), keyed(b)
    return sum((min(w, kb[k]) for k, w in ka.items() if k in kb), ZERO)


@dataclass(frozen=True)
class FundOverlap:
    a_isin: str
    a_name: str
    b_isin: str
    b_name: str
    overlap_pct: Decimal | None  # None when only the index name says they match
    basis: str  # "holdings" | "index"
    intentional: bool
    reconfirm: bool  # intentional, but last confirmed over a year ago
    suggestion: str


@dataclass(frozen=True)
class PositionStatus:
    isin: str
    name: str
    type: str
    value: Decimal | None
    pct: Decimal | None  # of the securities; None without a price
    status: str  # protected | consolidate | exit_candidate | tracker | review
    why: str
    suggestion: str = ""
    gain_estimate: Decimal | None = None


@dataclass(frozen=True)
class Prompt:
    isin: str
    name: str
    kind: str
    text: str


@dataclass
class Consolidation:
    direct: int = 0
    look_through: int = 0
    min_positions: int = 40
    max_positions: int = 60
    state: str = "within"  # below | within | above
    small_pct: Decimal = SMALL_PCT
    small: list[PositionStatus] = field(default_factory=list)
    overlaps: list[FundOverlap] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)
    held_back: int = 0  # prompts left for a later week by the weekly limit
    notes: list[str] = field(default_factory=list)


def _fund_overlaps(
    session: Session,
    conc: lookthrough.Concentration,
    settings: book.BookSettings,
    plans: dict[str, Decimal],
    today: date,
) -> list[FundOverlap]:
    funds = [p for p in conc.positions if p.type == kinds.ETF]
    files = lookthrough.holdings_by_fund(session, [f.isin for f in funds])
    intentional = {book.pair_key(o.a, o.b): o for o in settings.intentional}
    out: list[FundOverlap] = []
    for x, y in combinations(funds, 2):
        pct: Decimal | None = None
        basis = ""
        if x.isin in files and y.isin in files:
            # Two holdings files settle it: the names say nothing the weights do not.
            pct = shared_weight(files[x.isin], files[y.isin])
            if pct >= OVERLAP_PCT:
                basis = "holdings"
        else:
            kx, ky = index_key(x.name), index_key(y.name)
            if kx is not None and kx == ky:
                basis = "index"
        if not basis:
            continue
        mark = intentional.get(book.pair_key(x.isin, y.isin))
        smaller, bigger = sorted((x, y), key=lambda p: p.value)
        text = f"{smaller.name} and {bigger.name} " + (
            f"share {pct:.0f}% of their holdings."
            if basis == "holdings" and pct is not None
            else "follow the same index."
        )
        if smaller.isin in plans:
            text += (
                f" Redirect the savings plan of {smaller.name} ({plans[smaller.isin]:,.0f} EUR a "
                f"month) into {bigger.name}. That needs no sale and no tax."
            )
        else:
            text += f" Put new money into {bigger.name} and let {smaller.name} run down."
        out.append(
            FundOverlap(
                x.isin, x.name, y.isin, y.name, pct, basis,
                intentional=mark is not None,
                reconfirm=mark is not None and (today - mark.confirmed).days > RECONFIRM_DAYS,
                suggestion=text,
            )
        )  # fmt: skip
    return out


def analyse(
    session: Session,
    held: service.Holdings,
    conc: lookthrough.Concentration,
    tags: dict[str, book.Tag],
    settings: book.BookSettings,
    plans: dict[str, Decimal],
    today: date,
) -> Consolidation:
    out = Consolidation(min_positions=settings.min_positions, max_positions=settings.max_positions)
    out.direct = len(held.positions)
    out.look_through = sum(1 for u in conc.underlying if u.total > 0)
    out.state = (
        "below"
        if out.direct < settings.min_positions
        else "above"
        if out.direct > settings.max_positions
        else "within"
    )
    out.overlaps = _fund_overlaps(session, conc, settings, plans, today)
    redundant: dict[str, FundOverlap] = {}
    for o in out.overlaps:
        if o.intentional:
            continue
        smaller = o.a_isin if _value(conc, o.a_isin) <= _value(conc, o.b_isin) else o.b_isin
        redundant[smaller] = o

    # The same company held twice (two listings, a share and a receipt).
    by_company: dict[str, list[lookthrough.PositionView]] = {}
    for p in conc.positions:
        if p.type == kinds.STOCK and company_key(p.name):
            by_company.setdefault(company_key(p.name), []).append(p)
    twin_of: dict[str, lookthrough.PositionView] = {}
    for group in by_company.values():
        if len(group) > 1:
            group.sort(key=lambda p: p.value, reverse=True)
            for smaller in group[1:]:
                twin_of[smaller.isin] = group[0]

    total = conc.total
    for p in conc.positions:
        pct = p.value / total * HUNDRED if total > 0 else ZERO
        if pct >= SMALL_PCT:
            continue
        tag = tags.get(p.isin)
        status, why, suggestion = (
            "review",
            "Small, and nothing is wrong with it. It is left alone.",
            "",
        )
        if tag and tag.tracker_until and tag.tracker_until >= today:
            status, why = "tracker", f"You keep it as a tracker until {tag.tracker_until}."
        elif tag and tag.pinned:
            status, why = "protected", "You pinned it as a protected small bet."
        elif p.isin in twin_of:
            big = twin_of[p.isin]
            status = "consolidate"
            why = f"The same company as {big.name}, which you hold in a bigger amount."
            suggestion = f"Merge it into {big.name}: direct new money there instead."
        elif p.isin in redundant:
            status = "consolidate"
            why = "A fund that overlaps another fund you hold."
            suggestion = redundant[p.isin].suggestion
        elif tag and tag.tracker_until and tag.tracker_until < today:
            status, why = "review", "The tracker period is over. Decide: keep it, or let it go."
        out.small.append(
            PositionStatus(p.isin, p.name, p.type, p.value, pct, status, why, suggestion)
        )

    # No usable price at all: the thesis may be broken (delisted, worthless, no data).
    priced = {p.isin for p in conc.positions}
    for pos in held.positions:
        if pos.isin in priced:
            continue
        cost = held.costs.get(pos.isin)
        loss = -cost.total_cost if cost is not None and cost.total_cost else None
        out.small.append(
            PositionStatus(
                pos.isin,
                pos.name or pos.isin,
                kinds.classify(pos.asset_class, pos.name, pos.isin).type,
                None,
                None,
                "exit_candidate",
                "No usable price: it may be delisted, worthless or missing from the data.",
                "Check it at your broker. If it is worthless, selling it books the loss, which can "
                "offset gains (see the Tax page).",
                loss,
            )
        )  # fmt: skip

    urgent = [
        Prompt(s.isin, s.name, s.status, f"{s.name}: {s.why} {s.suggestion}".strip())
        for s in out.small
        if s.status in ("exit_candidate", "consolidate")
        or (s.status == "review" and "tracker period" in s.why)
    ]
    urgent.sort(key=lambda q: (q.kind != "exit_candidate", q.kind != "consolidate"))
    out.prompts = urgent[: settings.weekly_prompts]
    out.held_back = max(len(urgent) - len(out.prompts), 0)
    if out.state == "above":
        out.notes.append(
            f"You hold {out.direct} positions against a soft target of {settings.min_positions} "
            f"to {settings.max_positions}. Small is not a reason to sell: look at the "
            "consolidation prompts first."
        )
    out.notes.append(
        "The factor scores that would protect the best small bets, and mark weak ones as exit "
        "candidates, are not built yet. Until then only a pin protects a small position."
    )
    return out


def _value(conc: lookthrough.Concentration, isin: str) -> Decimal:
    return next((p.value for p in conc.positions if p.isin == isin), ZERO)


def tracker_until(today: date) -> date:
    """The default end of a "keep as tracker" period: six months out."""
    return allocation.add_months(today, TRACKER_MONTHS)

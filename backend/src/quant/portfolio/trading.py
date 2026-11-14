"""The trading book (FR-48, D24).

Leveraged and inverse ETPs, knock-outs, and anything the user tags as a short-term trade are the
trading book. It is left out of the long-term targets and the drift maths (`allocation.py`), and
included in net worth and risk. This module adds what it needs of its own:

- a capital cap (default 3% of everything, the guardrail of FR-43);
- alerts: a daily-reset product held longer than N days (10), a knock-out closer than X% (5%) to
  its barrier, and a price at or below the stop the user set;
- its own statistics from the closed trades: profit and loss, hit rate, average holding time and
  how many ended worthless at the barrier. The lot engine already books each sale, liquidation
  and expiry as a disposal, so the numbers come from the same history as the tax page;
- profit and loss by year, kept apart for tax. The loss-offset limit for such products was
  abolished in 2024. It is shown as a note, to be verified with a tax adviser.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import Instrument, JournalEntry, PriceEOD
from quant.portfolio import book, lookthrough, service
from quant.portfolio import instruments as kinds

ZERO = Decimal(0)
HUNDRED = Decimal(100)
MAX_PRICE_AGE_DAYS = 10
TAX_NOTE = (
    "Gains and losses on these products are kept apart for tax. The old 20,000 EUR limit on "
    "offsetting such losses was abolished in 2024. Verify this with a tax adviser."
)


@dataclass(frozen=True)
class Alert:
    code: str  # hold | barrier | stop
    text: str


@dataclass(frozen=True)
class OpenTrade:
    isin: str
    name: str
    type: str
    value: Decimal
    days_held: int | None
    stop_price: Decimal | None
    price: Decimal | None
    barrier_distance_pct: Decimal | None
    alerts: list[Alert]
    flags: list[str]


@dataclass
class TradeStats:
    closed: int = 0
    wins: int = 0
    expired: int = 0  # ended worthless at a knock-out barrier or were written off
    pnl: Decimal = ZERO
    avg_days: Decimal | None = None
    by_year: dict[int, Decimal] = field(default_factory=dict)

    @property
    def hit_rate(self) -> Decimal | None:
        return None if self.closed == 0 else Decimal(self.wins) / self.closed * HUNDRED


@dataclass
class TradingBook:
    open: list[OpenTrade] = field(default_factory=list)
    value: Decimal = ZERO
    stats: TradeStats = field(default_factory=TradeStats)
    needs_thesis: list[tuple[str, str]] = field(default_factory=list)
    tax_note: str = TAX_NOTE


def latest_close(session: Session, isin: str, today: date) -> Decimal | None:
    """The newest stored close for an instrument, if it is recent."""
    row = session.execute(
        select(PriceEOD.close, PriceEOD.date)
        .join(Instrument, Instrument.id == PriceEOD.instrument_id)
        .where(Instrument.isin == isin, PriceEOD.date <= today)
        .order_by(PriceEOD.date.desc())
        .limit(1)
    ).first()
    if row is None or (today - row[1]).days > MAX_PRICE_AGE_DAYS:
        return None
    return Decimal(row[0])


def is_trade(type_: str, tag: book.Tag | None) -> bool:
    return type_ in kinds.TRADING_BOOK_TYPES or bool(tag and tag.short_term)


def analyse(
    session: Session,
    user_id: uuid.UUID,
    held: service.Holdings,
    conc: lookthrough.Concentration,
    tags: dict[str, book.Tag],
    settings: book.BookSettings,
    today: date,
) -> TradingBook:
    out = TradingBook()
    for p in conc.positions:
        tag = tags.get(p.isin)
        if not is_trade(p.type, tag):
            continue
        out.value += p.value
        lots = [lot.acquired for lot in held.book.lots.get(p.isin, []) if lot.quantity > 0]
        days = (today - min(lots)).days if lots else None
        mark = held.marks.get(p.isin)
        price = mark.price if mark else None
        alerts: list[Alert] = []
        if (
            days is not None
            and (p.type == kinds.LEVERAGED or (tag and tag.short_term))
            and days > settings.hold_days
        ):
            alerts.append(
                Alert(
                    "hold",
                    f"Held for {days} days. A daily-reset product decays over time "
                    f"(your limit is {settings.hold_days} days).",
                )
            )
        distance: Decimal | None = None
        if tag and tag.barrier and (tag.barrier_isin or p.isin):
            under = latest_close(session, tag.barrier_isin or p.isin, today)
            if under is not None and under > 0:
                long = True if tag.barrier_long is None else tag.barrier_long
                gap = (under - tag.barrier) if long else (tag.barrier - under)
                distance = gap / under * HUNDRED
                if distance < settings.barrier_pct:
                    alerts.append(
                        Alert(
                            "barrier",
                            f"Only {distance:.1f}% from its barrier (your limit is "
                            f"{settings.barrier_pct}%). It is worth nothing if the barrier is hit.",
                        )
                    )
        if tag and tag.stop_price and price is not None and price <= tag.stop_price:
            alerts.append(
                Alert("stop", f"The price {price:.2f} is at or below your stop {tag.stop_price}.")
            )
        out.open.append(
            OpenTrade(
                p.isin, p.name, p.type, p.value, days, tag.stop_price if tag else None,
                price, distance, alerts, p.flags,
            )
        )  # fmt: skip
    out.open.sort(key=lambda t: (not t.alerts, -t.value))

    trade_isins = {
        isin
        for isin, pos in held.everything.items()
        if is_trade(kinds.classify(pos.asset_class, pos.name, isin).type, tags.get(isin))
    }
    weighted = ZERO
    quantity = ZERO
    for d in held.book.disposals:
        if d.isin not in trade_isins:
            continue
        pnl = d.realised_pnl
        out.stats.closed += 1
        out.stats.wins += 1 if pnl > 0 else 0
        out.stats.expired += 1 if d.kind in {"expiry", "write_off"} else 0
        out.stats.pnl += pnl
        out.stats.by_year[d.date.year] = out.stats.by_year.get(d.date.year, ZERO) + pnl
        for s in d.slices:
            weighted += s.quantity * (d.date - s.acquired).days
            quantity += s.quantity
    if quantity > 0:
        out.stats.avg_days = weighted / quantity

    open_isins = [t.isin for t in out.open]
    if open_isins:
        have = set(
            session.scalars(
                select(JournalEntry.isin).where(
                    JournalEntry.user_id == user_id, JournalEntry.isin.in_(open_isins)
                )
            )
        )
        out.needs_thesis = [(t.isin, t.name) for t in out.open if t.isin not in have]
    return out

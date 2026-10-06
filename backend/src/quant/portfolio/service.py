"""Holdings read model: positions from the user's history, reconciled with their statements,
with cost basis and profit and loss from the FIFO lot engine."""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from quant.importers import tr_crypto_pdf, tr_depot_pdf
from quant.models import FxRate, Instrument, PriceEOD, Snapshot, Transaction, UnitCost
from quant.portfolio.lots import (
    FLAG_PRICE_DERIVED,
    LotBook,
    PositionCost,
    build_lots,
    missing_costs,
    position_costs,
)
from quant.portfolio.positions import (
    QUANTITY_CATEGORIES,
    Position,
    compute_positions,
    open_positions,
)
from quant.portfolio.reconcile import (
    Finding,
    Scope,
    StatementLine,
    Status,
    all_but,
    classes,
    find_by_name,
    merge_lines,
    reconcile,
    summarise,
)
from quant.portfolio.tax import Income, Refund, YearEstimate, estimate

# Which asset classes each statement source speaks for. The Depotauszug lists everything the
# broker keeps in custody; coins are on their own statement.
SOURCE_SCOPE: dict[str, Scope] = {
    tr_crypto_pdf.SOURCE: classes(*tr_crypto_pdf.ASSET_CLASSES),
    tr_depot_pdf.SOURCE: all_but(*tr_crypto_pdf.ASSET_CLASSES),
}

# A statement prints each Kurswert/Kaufwert to the cent; derived lots (price x units) add up to
# half a cent of rounding each.
COST_TOLERANCE = Decimal("0.01")
COST_TOLERANCE_PER_DERIVED_LOT = Decimal("0.005")


@dataclass(frozen=True)
class CostCheck:
    """A statement's purchase value (Kaufwert) against the one rebuilt from the history."""

    name: str
    isin: str
    statement_cost: Decimal
    computed_cost: Decimal

    @property
    def difference(self) -> Decimal:
        return self.computed_cost - self.statement_cost


@dataclass
class SourceReconciliation:
    source: str
    as_of: str
    findings: list[Finding]
    cost_checks: list[tuple[CostCheck, bool]] = field(default_factory=list)  # (check, ok)

    @property
    def counts(self) -> dict[str, int]:
        return summarise(self.findings)


@dataclass(frozen=True)
class Mark:
    """A market price taken from a broker statement (the broker mark, FR-19)."""

    price: Decimal
    as_of: date
    source: str
    # What the price applies to: the quantity and cost on the statement date, so that value and
    # profit describe one day even when later trades have changed the position.
    quantity: Decimal
    cost: PositionCost | None


@dataclass
class Holdings:
    positions: list[Position]
    verified: set[str] = field(default_factory=set)  # ISINs confirmed by a statement
    differs: set[str] = field(default_factory=set)  # ISINs a statement disagrees with
    reconciliations: list[SourceReconciliation] = field(default_factory=list)
    everything: dict[str, Position] = field(default_factory=dict)  # closed positions too
    book: LotBook = field(default_factory=LotBook)
    costs: dict[str, PositionCost] = field(default_factory=dict)
    marks: dict[str, Mark] = field(default_factory=dict)
    # (sector, industry) by ISIN, for the shares a data source has classified.
    sectors: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)


def load_sectors(session: Session, isins: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """The sector and industry of each ISIN that has one. The instrument list is shared market
    data, so this reads no personal data."""
    if not isins:
        return {}
    rows = session.execute(
        select(Instrument.isin, Instrument.sector, Instrument.industry).where(
            Instrument.isin.in_(isins), Instrument.sector.is_not(None)
        )
    )
    return {isin: (sector, industry) for isin, sector, industry in rows if isin}


def load_movements(session: Session, user_id: uuid.UUID) -> list[Transaction]:
    """Rows that can change a quantity or a cost: trades, deliveries, corporate actions, and the
    cash rows of corporate actions (proceeds of a liquidation, the price of a rights issue)."""
    return list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.isin.is_not(None),
                or_(
                    and_(
                        Transaction.shares.is_not(None),
                        Transaction.category.in_(QUANTITY_CATEGORIES),
                    ),
                    and_(
                        Transaction.category == "CASH",
                        Transaction.kind == "corporate_action",
                        Transaction.amount.is_not(None),
                    ),
                ),
            )
        )
    )


def load_unit_costs(session: Session, user_id: uuid.UUID) -> dict[str, Decimal]:
    """The cost per unit the owner entered, by ISIN (FR-20)."""
    rows = session.scalars(select(UnitCost).where(UnitCost.user_id == user_id))
    return {row.isin: row.unit_cost for row in rows}


def statement_lines(snapshot: Snapshot) -> list[StatementLine]:
    return merge_lines(
        [
            StatementLine(
                name=line["name"],
                quantity=Decimal(line["quantity"]),
                isin=line.get("isin"),
                value_eur=Decimal(line["value_eur"]),
            )
            for line in snapshot.lines
        ]
    )


def latest_snapshots(session: Session, user_id: uuid.UUID) -> list[Snapshot]:
    """The newest snapshot per source."""
    rows = session.scalars(
        select(Snapshot)
        .where(Snapshot.user_id == user_id)
        .order_by(Snapshot.as_of.desc(), Snapshot.created_at.desc())
    )
    newest: dict[str, Snapshot] = {}
    for snapshot in rows:
        newest.setdefault(snapshot.source, snapshot)
    return list(newest.values())


def reconcile_snapshot(positions: list[Position], snapshot: Snapshot) -> SourceReconciliation:
    scope = SOURCE_SCOPE[snapshot.source]
    return SourceReconciliation(
        source=snapshot.source,
        as_of=snapshot.as_of.isoformat(),
        findings=reconcile(positions, statement_lines(snapshot), scope),
    )


def cost_checks(
    snapshot: Snapshot, as_of_positions: list[Position], book: LotBook
) -> list[tuple[CostCheck, bool]]:
    """Compare each statement line's purchase value with the lots as of the statement date."""
    costs = position_costs(book)
    scoped = [p for p in as_of_positions if SOURCE_SCOPE[snapshot.source](p.asset_class)]
    results: list[tuple[CostCheck, bool]] = []
    for line in snapshot.lines:
        stated = line.get("cost_eur")
        position = find_by_name(scoped, line["name"])
        if stated is None or position is None or position.isin not in costs:
            continue
        cost = costs[position.isin]
        derived = sum(
            1 for lot in book.lots.get(position.isin, []) if FLAG_PRICE_DERIVED in lot.flags
        )
        tolerance = COST_TOLERANCE + COST_TOLERANCE_PER_DERIVED_LOT * derived
        check = CostCheck(line["name"], position.isin, Decimal(stated), cost.cost)
        results.append((check, abs(check.difference) <= tolerance))
    return results


def marks_from_snapshot(
    snapshot: Snapshot, as_of_positions: list[Position], book: LotBook
) -> dict[str, Mark]:
    """Prices from a statement, with the quantity and cost the position had on that day."""
    scoped = [p for p in as_of_positions if SOURCE_SCOPE[snapshot.source](p.asset_class)]
    costs = position_costs(book)
    marks: dict[str, Mark] = {}
    by_isin = {p.isin: p for p in scoped}
    for line in snapshot.lines:
        position = by_isin.get(line.get("isin", "")) or find_by_name(scoped, line["name"])
        if position is not None and "price_eur" in line:
            marks[position.isin] = Mark(
                Decimal(line["price_eur"]),
                snapshot.as_of,
                snapshot.source,
                position.quantity,
                costs.get(position.isin),
            )
    return marks


# A stored price counts only while it is no more than this many days old. A ticker that stopped
# updating (delisted, renamed) or a price job that stopped then shows as unpriced instead of
# passing an old price off as the current value.
MAX_PRICE_AGE_DAYS = 10
# An ECB rate counts for a price only when it is at most this many days older than the price day.
# It covers weekends and holidays. A currency the ECB stopped publishing gives no value.
MAX_FX_AGE_DAYS = 7


def today() -> date:
    """Today's date. Tests replace it, so fixed price dates do not age out of the tests."""
    return date.today()


def price_marks(
    session: Session,
    positions: list[Position],
    costs: dict[str, PositionCost],
    on: date | None = None,
) -> dict[str, Mark]:
    """The latest stored price of each open position, in euros (FR-20).

    A price in another currency is converted at the ECB rate of its own day (or the latest rate
    before it). No rate means no mark: better a blank than a wrong value."""
    isins = [p.isin for p in positions]
    if not isins:
        return {}
    newest = (
        select(PriceEOD.instrument_id, func.max(PriceEOD.date).label("d"))
        .group_by(PriceEOD.instrument_id)
        .subquery()
    )
    rows = session.execute(
        select(Instrument.isin, PriceEOD.close, PriceEOD.currency, PriceEOD.date, PriceEOD.source)
        .join(PriceEOD, PriceEOD.instrument_id == Instrument.id)
        .join(
            newest,
            (newest.c.instrument_id == PriceEOD.instrument_id) & (newest.c.d == PriceEOD.date),
        )
        .where(Instrument.isin.in_(isins), Instrument.active.is_(True))
    ).all()
    quantities = {p.isin: p.quantity for p in positions}
    oldest_usable = (on or today()) - timedelta(days=MAX_PRICE_AGE_DAYS)
    marks: dict[str, Mark] = {}
    for isin, close, currency, day, source in rows:
        if close is None or not isin:
            continue
        if day < oldest_usable:
            continue
        price = close
        if currency and currency != "EUR":
            rate = session.scalar(
                select(FxRate.rate)
                .where(
                    FxRate.quote == currency,
                    FxRate.date <= day,
                    FxRate.date >= day - timedelta(days=MAX_FX_AGE_DAYS),
                )
                .order_by(FxRate.date.desc())
                .limit(1)
            )
            if not rate:
                continue
            price = close / rate
        marks[isin] = Mark(price, day, source, quantities[isin], costs.get(isin))
    return marks


def build_holdings(session: Session, user_id: uuid.UUID) -> Holdings:
    movements = load_movements(session, user_id)
    everything = compute_positions(movements)
    current = open_positions(everything)
    unit_costs = load_unit_costs(session, user_id)
    book = build_lots(movements, unit_costs)
    holdings = Holdings(
        positions=current,
        everything=everything,
        book=book,
        costs=position_costs(book),
        sectors=load_sectors(session, [p.isin for p in current]),
    )
    current_by_isin = {p.isin: p for p in current}
    for snapshot in latest_snapshots(session, user_id):
        if snapshot.source not in SOURCE_SCOPE:
            continue
        # A statement describes the day it was issued, so compare it with the history up to
        # that day. Later trades are not discrepancies.
        as_of_rows = [m for m in movements if m.date <= snapshot.as_of]
        as_of_positions = open_positions(compute_positions(as_of_rows))
        result = reconcile_snapshot(as_of_positions, snapshot)
        as_of_book = build_lots(as_of_rows, unit_costs)
        # The check validates the engine against the broker's own figure, so it must not see the
        # costs the owner entered; profit and loss and the marks below do.
        broker_book = build_lots(as_of_rows) if unit_costs else as_of_book
        result.cost_checks = cost_checks(snapshot, as_of_positions, broker_book)
        holdings.reconciliations.append(result)
        holdings.marks.update(marks_from_snapshot(snapshot, as_of_positions, as_of_book))
        for f in result.findings:
            if f.isin is None:
                continue
            if f.status == Status.MATCH:
                # Confirmed only while nothing has moved since the statement.
                now = current_by_isin.get(f.isin)
                if now is not None and now.last_date <= result.as_of:
                    holdings.verified.add(f.isin)
            elif f.status in (Status.QUANTITY_MISMATCH, Status.NOT_ON_STATEMENT):
                holdings.differs.add(f.isin)
    # Stored market prices fill in what no statement priced; the newer of two prices wins.
    for isin, mark in price_marks(session, current, holdings.costs).items():
        known = holdings.marks.get(isin)
        if known is None or mark.as_of > known.as_of:
            holdings.marks[isin] = mark
    return holdings


def build_tax(session: Session, user_id: uuid.UUID) -> list[YearEstimate]:
    """The German tax estimate per year (FR-26) from the user's whole history."""
    movements = load_movements(session, user_id)
    book = build_lots(movements, load_unit_costs(session, user_id))
    classes = {p.isin: p.asset_class for p in compute_positions(movements).values()}
    rows = session.scalars(
        select(Transaction).where(
            Transaction.user_id == user_id, Transaction.kind.in_(("income", "interest", "tax"))
        )
    )
    income: list[Income] = []
    refunds: list[Refund] = []
    for tx in rows:
        if tx.kind == "tax":
            # A loss-pot or allowance adjustment. What reaches the cash account is the refund,
            # whichever column the broker put it in; a negative result is extra tax charged.
            net = (tx.amount or Decimal(0)) + (tx.tax or Decimal(0))
            if net != 0:
                refunds.append(Refund(tx.date, net))
        elif tx.amount is not None:
            asset_class = classes.get(tx.isin) if tx.isin else None
            income.append(
                Income(tx.date, asset_class or tx.asset_class, tx.amount, -(tx.tax or Decimal(0)))
            )
    return estimate(book.disposals, classes, income, refunds)


@dataclass(frozen=True)
class CostItem:
    isin: str
    name: str | None
    asset_class: str | None
    open_units: Decimal
    sold_units: Decimal
    sales: int
    unit_cost: Decimal | None  # what the owner entered, if anything
    note: str | None


def build_cost_items(session: Session, user_id: uuid.UUID) -> list[CostItem]:
    """Instruments whose cost the broker did not give, with what the owner has entered. Entries
    that no longer match any unit stay listed so they can be changed or removed."""
    movements = load_movements(session, user_id)
    entries = {
        row.isin: row
        for row in session.scalars(select(UnitCost).where(UnitCost.user_id == user_id))
    }
    book = build_lots(movements, {i: e.unit_cost for i, e in entries.items()})
    everything = compute_positions(movements)
    missing = missing_costs(book)
    items = []
    for isin in sorted(set(missing) | set(entries)):
        gap = missing.get(isin)
        known = everything.get(isin)
        entry = entries.get(isin)
        items.append(
            CostItem(
                isin=isin,
                name=known.name if known else None,
                asset_class=known.asset_class if known else None,
                open_units=gap.open_units if gap else Decimal(0),
                sold_units=gap.sold_units if gap else Decimal(0),
                sales=gap.sales if gap else 0,
                unit_cost=entry.unit_cost if entry else None,
                note=entry.note if entry else None,
            )
        )
    # Instruments that still need a cost come first.
    return sorted(items, key=lambda i: (i.unit_cost is not None, i.name or i.isin))


def known_isins(session: Session, user_id: uuid.UUID) -> set[str]:
    """Every ISIN in the user's unit-moving history."""
    return set(compute_positions(load_movements(session, user_id)))

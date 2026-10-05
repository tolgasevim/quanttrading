"""FIFO cost-basis lots and realised profit and loss (FR-20).

Built from the same rows as the position engine (`positions.py`), and the two must always agree
on quantities. What each row does to the lots:

- **Buy** (`TRADING`, positive shares): a new lot. Purchase value is the amount paid; fees and
  transaction taxes (e.g. a stamp duty) are kept separately as acquisition costs, because the
  broker's own statements quote the purchase value without them while German tax counts both.
- **Sell**: consumes the oldest lots first (FIFO, as German tax rules require within one
  custody account). Realised gain = proceeds - sell fees - cost of the lots consumed. Tax the
  broker withheld is reported but is not part of the gain.
- **Free receipt** (`DELIVERY`, e.g. crypto Saveback or a transfer in): a lot valued at the price
  on the day of receipt (FR-10e). Flagged `price_derived`.
- **Corporate action**: rows of one action (same type, within minutes of each other) form a group.
  A negative leg on one ISIN with a positive leg on another is a swap (merger, exchange, reverse
  split, ISIN change): the lots, acquisition dates and cost move across. Added units on an ISIN
  already held are a split: the cost is spread over more units. A positive leg on a new ISIN
  (spin-off, rights, ...) is a lot whose cost the broker does not give: flagged `cost_unknown`,
  for the owner to fill in. A lone negative leg (liquidation, knock-out expiry, worthless) is a
  disposal; cash rows of the same ISIN within a day are its proceeds, or the price paid for the
  new units of a rights issue. Cash that fits no action is listed, never silently dropped.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from quant.portfolio.positions import DUST

ZERO = Decimal(0)

FLAG_COST_UNKNOWN = "cost_unknown"  # the broker gives no cost: needs the owner's input
FLAG_PRICE_DERIVED = "price_derived"  # valued at price x units, not at an amount paid
FLAG_CARRIED = "carried"  # moved over from another ISIN by a corporate action
FLAG_INCOMPLETE_HISTORY = "incomplete_history"  # sold more than the history shows was bought

FLAG_COST_ENTERED = "cost_entered"  # the owner supplied the cost per unit

# Flags that mean part of the cost is missing (counted as zero), so a gain or profit on it is
# overstated: the broker gave no cost, or the history has fewer purchases than sales.
COST_MISSING = frozenset({FLAG_COST_UNKNOWN, FLAG_INCOMPLETE_HISTORY})

# One action's rows share a type and are written within minutes of each other.
GROUP_GAP = timedelta(minutes=5)
# Cash rows of a corporate action are booked up to a day away from the unit rows.
CASH_WINDOW = timedelta(days=1)

SCALING_TYPES = frozenset({"SPLIT", "STOCK_DIVIDEND"})  # units added to a holding: spread cost
RESCALING_TYPES = frozenset({"SPLIT", "REVERSE_SPLIT"})
DISPOSAL_KINDS = {
    "LIQUIDATION": "liquidation",
    "LIQUIDATION_DIVIDEND": "liquidation",
    "WARRANT_EXERCISE": "expiry",
    "WORTHLESS": "write_off",
    "OPTIONAL_DIVIDEND": "write_off",
}


class LotTx(Protocol):
    """Read-only view of a transaction: parsed rows and the ORM model both fit."""

    @property
    def isin(self) -> str | None: ...
    @property
    def category(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def type(self) -> str: ...
    @property
    def shares(self) -> Decimal | None: ...
    @property
    def price(self) -> Decimal | None: ...
    @property
    def amount(self) -> Decimal | None: ...
    @property
    def fee(self) -> Decimal | None: ...
    @property
    def tax(self) -> Decimal | None: ...
    @property
    def executed_at(self) -> datetime: ...
    @property
    def date(self) -> date | str: ...


@dataclass
class Lot:
    isin: str
    acquired: date
    quantity: Decimal
    cost: Decimal  # purchase value of the remaining units, EUR
    costs: Decimal  # fees and transaction taxes paid to acquire them
    origin: str  # "buy" | "delivery" | "corporate_action"
    flags: frozenset[str] = frozenset()

    def take(self, quantity: Decimal) -> "Lot":
        """Split off the first `quantity` units (cost pro rata) and return them."""
        if quantity >= self.quantity:
            taken = Lot(
                self.isin,
                self.acquired,
                self.quantity,
                self.cost,
                self.costs,
                self.origin,
                self.flags,
            )
            self.quantity = self.cost = self.costs = ZERO
            return taken
        ratio = quantity / self.quantity
        taken = Lot(
            self.isin,
            self.acquired,
            quantity,
            self.cost * ratio,
            self.costs * ratio,
            self.origin,
            self.flags,
        )
        self.quantity -= quantity
        self.cost -= taken.cost
        self.costs -= taken.costs
        return taken


@dataclass(frozen=True)
class Disposal:
    isin: str
    date: date
    kind: str  # "sale" | "liquidation" | "expiry" | "write_off"
    quantity: Decimal
    proceeds: Decimal  # gross, before fees
    fees: Decimal  # sell-side fees, positive
    tax_withheld: Decimal  # withheld by the broker, positive; not part of the gain
    slices: tuple[Lot, ...]  # the lots consumed, oldest first
    flags: frozenset[str]

    @property
    def cost(self) -> Decimal:
        return sum((s.cost for s in self.slices), ZERO)

    @property
    def acquisition_costs(self) -> Decimal:
        return sum((s.costs for s in self.slices), ZERO)

    @property
    def cost_unknown(self) -> bool:
        """Some units sold had no cost from the broker, so the gain is overstated."""
        return bool(self.flags & COST_MISSING) or any(s.flags & COST_MISSING for s in self.slices)

    @property
    def realised_pnl(self) -> Decimal:
        """Gain before tax: proceeds less sell fees and the full acquisition cost."""
        return self.proceeds - self.fees - self.cost - self.acquisition_costs


@dataclass(frozen=True)
class UnattributedCash:
    isin: str
    date: date
    type: str
    amount: Decimal


@dataclass
class LotBook:
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    disposals: list[Disposal] = field(default_factory=list)
    unattributed: list[UnattributedCash] = field(default_factory=list)

    def open_quantity(self, isin: str) -> Decimal:
        return sum((lot.quantity for lot in self.lots.get(isin, [])), ZERO)

    def open_quantities(self) -> dict[str, Decimal]:
        return {i: q for i in self.lots if abs(q := self.open_quantity(i)) > DUST}


def _day(tx: LotTx) -> date:
    return tx.date if isinstance(tx.date, date) else date.fromisoformat(tx.date)


def _abs(value: Decimal | None) -> Decimal:
    return abs(value) if value is not None else ZERO


def _withheld(value: Decimal | None) -> Decimal:
    """Tax the broker took (a negative amount). A positive one is a refund, not withholding."""
    return -value if value is not None and value < 0 else ZERO


@dataclass
class _Group:
    type: str
    rows: list[LotTx]
    cash: list[LotTx] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.rows[0].executed_at

    @property
    def end(self) -> datetime:
        return self.rows[-1].executed_at


def _is_unit_row(tx: LotTx) -> bool:
    return tx.isin is not None and tx.shares is not None


def _is_ca_cash(tx: LotTx) -> bool:
    return (
        tx.category == "CASH"
        and tx.kind == "corporate_action"
        and tx.isin is not None
        and tx.amount is not None
        and tx.amount != 0
    )


def _groups(rows: list[LotTx]) -> list[_Group]:
    groups: list[_Group] = []
    open_by_type: dict[str, _Group] = {}
    for tx in rows:
        if not (tx.category == "CORPORATE_ACTION" and _is_unit_row(tx)):
            continue
        group = open_by_type.get(tx.type)
        if group is None or tx.executed_at - group.end > GROUP_GAP:
            group = _Group(tx.type, [])
            open_by_type[tx.type] = group
            groups.append(group)
        group.rows.append(tx)

    cash = [tx for tx in rows if _is_ca_cash(tx)]
    claimed: set[int] = set()
    for group in groups:
        isins = {tx.isin for tx in group.rows}
        for tx in cash:
            if id(tx) in claimed or tx.isin not in isins:
                continue
            if group.start - CASH_WINDOW <= tx.executed_at <= group.end + CASH_WINDOW:
                group.cash.append(tx)
                claimed.add(id(tx))
    return groups


def _pair_legs(
    sources: dict[str, Decimal], targets: dict[str, Decimal]
) -> list[tuple[dict[str, Decimal], dict[str, Decimal]]]:
    """Unrelated swaps of one type booked together would share a group. When there are as many
    outgoing as incoming instruments, pair them in booking order (one swap each) so cost and
    acquisition dates are not mixed; any other shape (a split into many, a merger of many) is one
    swap."""
    if len(sources) > 1 and len(sources) == len(targets):
        return [({s: sources[s]}, {t: targets[t]}) for s, t in zip(sources, targets, strict=True)]
    return [(sources, targets)]


class _Engine:
    def __init__(self, unit_costs: Mapping[str, Decimal] | None = None) -> None:
        self.book = LotBook()
        self.unit_costs = unit_costs or {}

    # -- lots ----------------------------------------------------------------------------

    def _entered(self, lot: Lot) -> Lot:
        """Give a lot received without a cost the per-unit cost the owner entered for its ISIN.

        Only lots whose cost is exactly unknown (`cost_unknown`, nothing paid) are filled. A lot
        that already has a cost keeps it, and units the history never bought
        (`incomplete_history`) are left alone: a cost does not fix their acquisition date, and the
        gap usually means an import is missing."""
        unit = self.unit_costs.get(lot.isin)
        if unit is None or FLAG_COST_UNKNOWN not in lot.flags or lot.cost != 0:
            return lot
        flags = (lot.flags - {FLAG_COST_UNKNOWN}) | {FLAG_COST_ENTERED}
        # Fees and transaction taxes paid on the purchase are real costs the owner is not asked for.
        return Lot(
            lot.isin, lot.acquired, lot.quantity, lot.quantity * unit, lot.costs, lot.origin, flags
        )

    def add(self, lot: Lot) -> None:
        lot = self._entered(lot)
        lots = self.book.lots.setdefault(lot.isin, [])
        lots.append(lot)
        lots.sort(key=lambda existing: existing.acquired)  # stable: FIFO by acquisition date

    def consume(self, isin: str, quantity: Decimal, when: date) -> tuple[list[Lot], frozenset[str]]:
        """Take `quantity` units FIFO. Units the history never bought come back as a flagged
        zero-cost slice rather than disappearing."""
        taken: list[Lot] = []
        remaining = quantity
        lots = self.book.lots.setdefault(isin, [])
        while remaining > DUST and lots:
            lot = lots[0]
            piece = lot.take(remaining)
            taken.append(piece)
            remaining -= piece.quantity
            if lot.quantity <= DUST:
                lots.pop(0)
        flags: frozenset[str] = frozenset()
        if remaining > DUST:
            taken.append(
                Lot(isin, when, remaining, ZERO, ZERO, "buy", frozenset({FLAG_INCOMPLETE_HISTORY}))
            )
            flags = frozenset({FLAG_INCOMPLETE_HISTORY})
        return taken, flags

    # -- rows ----------------------------------------------------------------------------

    def buy(self, tx: LotTx) -> None:
        assert tx.isin is not None and tx.shares is not None
        flags: set[str] = set()
        if tx.amount is not None:
            cost = _abs(tx.amount)
        elif tx.price is not None:  # e.g. private funds: units x unit price, no amount booked
            cost = abs(tx.price * tx.shares)
            flags.add(FLAG_PRICE_DERIVED)
        else:
            cost = ZERO
            flags.add(FLAG_COST_UNKNOWN)
        self.add(
            Lot(
                tx.isin,
                _day(tx),
                tx.shares,
                cost,
                _abs(tx.fee) + _abs(tx.tax),
                "buy",
                frozenset(flags),
            )
        )

    def sell(self, tx: LotTx) -> None:
        assert tx.isin is not None and tx.shares is not None
        quantity = abs(tx.shares)
        proceeds = _abs(tx.amount) if tx.amount is not None else abs((tx.price or ZERO) * tx.shares)
        slices, flags = self.consume(tx.isin, quantity, _day(tx))
        self.book.disposals.append(
            Disposal(
                tx.isin,
                _day(tx),
                "sale",
                quantity,
                proceeds,
                _abs(tx.fee),
                _withheld(tx.tax),
                tuple(slices),
                flags,
            )
        )

    def delivery(self, tx: LotTx) -> None:
        assert tx.isin is not None and tx.shares is not None
        if tx.shares < 0:
            # Units moved out (to a wallet or another depot) are not a sale: no proceeds, no gain
            # or loss. The lots leave this book with their cost.
            self.consume(tx.isin, abs(tx.shares), _day(tx))
            return
        if tx.price is not None:
            cost, flags = abs(tx.price * tx.shares), frozenset({FLAG_PRICE_DERIVED})
        else:
            cost, flags = ZERO, frozenset({FLAG_COST_UNKNOWN})
        self.add(Lot(tx.isin, _day(tx), tx.shares, cost, ZERO, "delivery", flags))

    # -- corporate actions -------------------------------------------------------------

    def corporate_action(self, group: _Group) -> None:
        sources: dict[str, Decimal] = defaultdict(lambda: ZERO)
        targets: dict[str, Decimal] = defaultdict(lambda: ZERO)
        net: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for tx in group.rows:
            assert tx.isin is not None and tx.shares is not None
            net[tx.isin] += tx.shares
        for isin, qty in net.items():
            if qty < 0:
                sources[isin] = -qty
            elif qty > 0:
                targets[isin] = qty
        when = _day(group.rows[0])
        cash_by_isin: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for tx in group.cash:
            assert tx.isin is not None and tx.amount is not None
            cash_by_isin[tx.isin] += tx.amount

        if sources and targets:
            for part_sources, part_targets in _pair_legs(sources, targets):
                self._swap(part_sources, part_targets, cash_by_isin, when)
        elif targets:
            self._additions(group.type, targets, cash_by_isin, when)
        elif sources:
            self._removals(group.type, sources, cash_by_isin, when)
        # Cash on ISINs the action did not move (e.g. a rights issue paid for in shares already
        # held) is handled by the branches above; whatever is left over is reported.
        for isin, amount in cash_by_isin.items():
            if amount != 0:
                self.book.unattributed.append(UnattributedCash(isin, when, group.type, amount))

    def _swap(
        self,
        sources: dict[str, Decimal],
        targets: dict[str, Decimal],
        cash: dict[str, Decimal],
        when: date,
    ) -> None:
        taken: list[Lot] = []
        for isin, qty in sources.items():
            slices, _ = self.consume(isin, qty, when)
            taken.extend(slices)
        total_in = sum(targets.values(), ZERO)
        total_out = sum((s.quantity for s in taken), ZERO)
        for isin, qty in targets.items():
            share = qty / total_in
            paid = ZERO
            if cash.get(isin, ZERO) < 0:  # paid for the new units (rights issue)
                paid = -cash.pop(isin)
            for piece in taken:
                if piece.quantity <= DUST:
                    continue
                part = piece.quantity / total_out * qty  # this piece's units, spread over targets
                flags = (set(piece.flags) | {FLAG_CARRIED}) - {FLAG_COST_ENTERED}
                lot_cost = piece.cost * share
                lot_costs = piece.costs * share
                extra = paid * (piece.quantity / total_out) if paid else ZERO
                if extra:
                    flags.discard(FLAG_COST_UNKNOWN)  # the price paid is the cost
                self.add(
                    Lot(
                        isin,
                        piece.acquired,
                        part,
                        lot_cost + extra,
                        lot_costs,
                        "corporate_action",
                        frozenset(flags),
                    )
                )

    def _additions(
        self, type_: str, targets: dict[str, Decimal], cash: dict[str, Decimal], when: date
    ) -> None:
        for isin, delta in targets.items():
            held = self.book.open_quantity(isin)
            if type_ in SCALING_TYPES and held > DUST:
                self._rescale(isin, (held + delta) / held)
                continue
            paid = ZERO
            if cash.get(isin, ZERO) < 0:
                paid = -cash.pop(isin)
            flags = frozenset() if paid else frozenset({FLAG_COST_UNKNOWN})
            self.add(Lot(isin, when, delta, paid, ZERO, "corporate_action", flags))

    def _removals(
        self, type_: str, sources: dict[str, Decimal], cash: dict[str, Decimal], when: date
    ) -> None:
        for isin, qty in sources.items():
            held = self.book.open_quantity(isin)
            if type_ in RESCALING_TYPES and held - qty > DUST:  # same-ISIN reverse split
                self._rescale(isin, (held - qty) / held)
                continue
            slices, flags = self.consume(isin, qty, when)
            proceeds = ZERO
            if cash.get(isin, ZERO) > 0:
                proceeds = cash.pop(isin)
            self.book.disposals.append(
                Disposal(
                    isin,
                    when,
                    DISPOSAL_KINDS.get(type_, "write_off"),
                    qty,
                    proceeds,
                    ZERO,
                    ZERO,
                    tuple(slices),
                    flags,
                )
            )

    def _rescale(self, isin: str, factor: Decimal) -> None:
        for lot in self.book.lots.get(isin, []):
            lot.quantity *= factor  # the cost stays: it is now spread over more (or fewer) units


def build_lots(
    transactions: Iterable[LotTx], unit_costs: Mapping[str, Decimal] | None = None
) -> LotBook:
    """Replay the history into FIFO lots. `unit_costs` is what the owner entered per unit for
    ISINs whose cost the broker does not give (FR-20); it applies to every lot flagged as
    missing a cost, whether still held or already sold."""
    rows = sorted(transactions, key=lambda t: t.executed_at)
    engine = _Engine(unit_costs)
    groups = _groups(rows)
    events: list[tuple[datetime, int, _Group | LotTx]] = [
        (tx.executed_at, 0, tx)
        for tx in rows
        if tx.category in ("TRADING", "DELIVERY") and _is_unit_row(tx)
    ]
    events.extend((group.start, 1, group) for group in groups)
    events.sort(key=lambda e: (e[0], e[1]))  # stable: rows at the same instant keep their order

    for _, _, item in events:
        if isinstance(item, _Group):
            engine.corporate_action(item)
        elif item.category == "TRADING":
            shares = item.shares or ZERO
            if shares > 0:
                engine.buy(item)
            elif shares < 0:
                engine.sell(item)
        else:
            engine.delivery(item)

    claimed = {id(c) for g in groups for c in g.cash}
    for tx in rows:
        if _is_ca_cash(tx) and id(tx) not in claimed:
            assert tx.isin is not None and tx.amount is not None
            engine.book.unattributed.append(UnattributedCash(tx.isin, _day(tx), tx.type, tx.amount))
    return engine.book


# -- summaries ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionCost:
    isin: str
    quantity: Decimal
    cost: Decimal  # purchase value, EUR (what the broker's statements quote)
    costs: Decimal  # fees and transaction taxes
    flags: frozenset[str]
    earliest_acquired: date | None

    @property
    def total_cost(self) -> Decimal:
        return self.cost + self.costs

    @property
    def average_cost(self) -> Decimal | None:
        """Per unit, fees included: the figure German tax works from."""
        return self.total_cost / self.quantity if self.quantity > DUST else None


def position_costs(book: LotBook) -> dict[str, PositionCost]:
    result: dict[str, PositionCost] = {}
    for isin, lots in book.lots.items():
        live = [lot for lot in lots if lot.quantity > DUST]
        if not live:
            continue
        result[isin] = PositionCost(
            isin=isin,
            quantity=sum((lot.quantity for lot in live), ZERO),
            cost=sum((lot.cost for lot in live), ZERO),
            costs=sum((lot.costs for lot in live), ZERO),
            flags=frozenset().union(*(lot.flags for lot in live)),
            earliest_acquired=min(lot.acquired for lot in live),
        )
    return result


@dataclass(frozen=True)
class YearPnl:
    year: int
    gains: Decimal  # sum of the profitable disposals
    losses: Decimal  # sum of the losing ones, negative
    fees: Decimal
    tax_withheld: Decimal
    disposals: int
    cost_unknown_disposals: int  # of which sold units whose cost the broker did not give

    @property
    def net(self) -> Decimal:
        return self.gains + self.losses


def realised_by_year(disposals: Iterable[Disposal]) -> list[YearPnl]:
    years: dict[int, list[Disposal]] = defaultdict(list)
    for d in disposals:
        years[d.date.year].append(d)
    out = []
    for year in sorted(years):
        items = years[year]
        pnl = [d.realised_pnl for d in items]
        out.append(
            YearPnl(
                year=year,
                gains=sum((p for p in pnl if p > 0), ZERO),
                losses=sum((p for p in pnl if p < 0), ZERO),
                fees=sum((d.fees for d in items), ZERO),
                tax_withheld=sum((d.tax_withheld for d in items), ZERO),
                disposals=len(items),
                cost_unknown_disposals=sum(1 for d in items if d.cost_unknown),
            )
        )
    return out


def realised_by_isin(disposals: Iterable[Disposal]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for d in disposals:
        totals[d.isin] += d.realised_pnl
    return dict(totals)


def cost_unknown_isins(disposals: Iterable[Disposal]) -> set[str]:
    return {d.isin for d in disposals if d.cost_unknown}


@dataclass(frozen=True)
class MissingCost:
    """Units of one instrument whose cost the broker did not give (or the owner has entered)."""

    isin: str
    open_units: Decimal  # still held
    sold_units: Decimal  # already sold
    sales: int  # disposals that include such units
    entered: bool  # the owner has supplied a cost per unit


def missing_costs(book: LotBook) -> dict[str, MissingCost]:
    """Per ISIN, the units received without a cost that need one from the owner, or have one
    entered. Units sold that the history never bought are not listed: a cost does not fix them."""
    marks = frozenset({FLAG_COST_UNKNOWN, FLAG_COST_ENTERED})
    open_units: dict[str, Decimal] = defaultdict(lambda: ZERO)
    sold_units: dict[str, Decimal] = defaultdict(lambda: ZERO)
    sales: dict[str, int] = defaultdict(int)
    entered: set[str] = set()
    for isin, lots in book.lots.items():
        for lot in lots:
            if lot.quantity > DUST and lot.flags & marks:
                open_units[isin] += lot.quantity
                if FLAG_COST_ENTERED in lot.flags:
                    entered.add(isin)
    for d in book.disposals:
        hit = [s for s in d.slices if s.flags & marks]
        if not hit:
            continue
        sales[d.isin] += 1
        sold_units[d.isin] += sum((s.quantity for s in hit), ZERO)
        if any(FLAG_COST_ENTERED in s.flags for s in hit):
            entered.add(d.isin)
    isins = set(open_units) | set(sold_units)
    return {i: MissingCost(i, open_units[i], sold_units[i], sales[i], i in entered) for i in isins}

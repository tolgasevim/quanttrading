"""Rebuild positions from the transaction history (FR-10c).

Only three kinds of rows change how many units are held:

- `TRADING` rows (buys and sells; sells carry negative shares),
- `DELIVERY` rows (free receipts, e.g. crypto Saveback), and
- `CORPORATE_ACTION` rows (splits, mergers, exchanges, ...), whose shares are signed: an action
  that replaces one instrument with another has a negative leg on the old ISIN and a positive leg
  on the new one, and a split is just the extra units.

Dividend and distribution rows also carry a `shares` value, but it is the number of units held on
the record date, not a movement. Counting it was the cause of the phantom holdings seen on the
first replay of the owner's history (149 positions against ~116 real ones).

Quantities only. Cost basis lots (FR-20) build on the same stream in the next slice.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

QUANTITY_CATEGORIES = frozenset({"TRADING", "DELIVERY", "CORPORATE_ACTION"})

# Anything smaller than this is rounding dust from fractional savings plans, not a holding.
DUST = Decimal("1E-8")

# Corporate-action types that need a closer look when lots and cost basis are built.
CA_TYPES_NEEDING_LOT_RULES = frozenset(
    {
        "SPLIT",
        "REVERSE_SPLIT",
        "MERGER",
        "EXCHANGE",
        "SPIN_OFF",
        "TAX_EXCHANGE",
        "RIGHTS",
        "STOCK_DIVIDEND",
        "OPTIONAL_DIVIDEND",
        "CAPITAL_INCR_CASH",
        "INTERMEDIATE_SECURITIES_DISTRIBUTION",
    }
)


class TxLike(Protocol):
    """Read-only view of a transaction: satisfied by parsed rows and by the ORM model."""

    @property
    def isin(self) -> str | None: ...
    @property
    def name(self) -> str | None: ...
    @property
    def asset_class(self) -> str | None: ...
    @property
    def category(self) -> str: ...
    @property
    def type(self) -> str: ...
    @property
    def shares(self) -> Decimal | None: ...
    @property
    def executed_at(self) -> datetime: ...
    @property
    def date(self) -> date | str: ...


@dataclass
class Position:
    isin: str
    name: str | None
    asset_class: str | None
    quantity: Decimal
    first_date: str
    last_date: str
    event_types: set[str] = field(default_factory=set)

    @property
    def is_open(self) -> bool:
        return abs(self.quantity) > DUST

    @property
    def corporate_action_touched(self) -> bool:
        return bool(self.event_types & CA_TYPES_NEEDING_LOT_RULES)


def moves_quantity(tx: TxLike) -> bool:
    return bool(tx.isin) and tx.shares is not None and tx.category in QUANTITY_CATEGORIES


def compute_positions(transactions: Iterable[TxLike]) -> dict[str, Position]:
    """All positions ever held, keyed by ISIN (closed ones included, with quantity 0)."""
    positions: dict[str, Position] = {}
    for tx in sorted(transactions, key=lambda t: t.executed_at):
        if not moves_quantity(tx):
            continue
        assert tx.isin is not None and tx.shares is not None
        day = tx.date if isinstance(tx.date, str) else tx.date.isoformat()
        pos = positions.get(tx.isin)
        if pos is None:
            pos = positions[tx.isin] = Position(
                isin=tx.isin,
                name=tx.name,
                asset_class=tx.asset_class,
                quantity=Decimal(0),
                first_date=day,
                last_date=day,
            )
        pos.quantity += tx.shares
        pos.last_date = day
        pos.event_types.add(tx.type)
        if tx.name:  # the latest name wins: mergers and renames change it
            pos.name = tx.name
        if tx.asset_class:
            pos.asset_class = tx.asset_class
    return positions


def holding_since(transactions: Iterable[TxLike]) -> dict[str, str]:
    """For each ISIN still held, the day the current holding began: the last time its quantity
    came up from nothing. A position sold in full and bought again begins at the second purchase,
    which `Position.first_date` (the first quantity row ever) does not say."""
    quantity: dict[str, Decimal] = {}
    since: dict[str, str] = {}
    for tx in sorted(transactions, key=lambda t: t.executed_at):
        if not moves_quantity(tx):
            continue
        assert tx.isin is not None and tx.shares is not None
        day = tx.date if isinstance(tx.date, str) else tx.date.isoformat()
        before = abs(quantity.get(tx.isin, Decimal(0)))
        quantity[tx.isin] = quantity.get(tx.isin, Decimal(0)) + tx.shares
        after = abs(quantity[tx.isin])
        if after > DUST and before <= DUST:
            since[tx.isin] = day  # a holding begins
        elif after <= DUST:
            since.pop(tx.isin, None)  # it ended
    return since


def open_positions(positions: dict[str, Position]) -> list[Position]:
    return sorted(
        (p for p in positions.values() if p.is_open),
        key=lambda p: ((p.name or p.isin).casefold(), p.isin),
    )

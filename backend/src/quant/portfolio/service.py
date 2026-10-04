"""Holdings read model: positions from the user's history, reconciled with their statements."""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.importers import tr_crypto_pdf
from quant.models import Snapshot, Transaction
from quant.portfolio.positions import (
    QUANTITY_CATEGORIES,
    Position,
    compute_positions,
    open_positions,
)
from quant.portfolio.reconcile import Finding, StatementLine, Status, reconcile, summarise

# Which asset classes each statement source speaks for.
SOURCE_SCOPE = {tr_crypto_pdf.SOURCE: tr_crypto_pdf.ASSET_CLASSES}


@dataclass
class SourceReconciliation:
    source: str
    as_of: str
    findings: list[Finding]

    @property
    def counts(self) -> dict[str, int]:
        return summarise(self.findings)


@dataclass
class Holdings:
    positions: list[Position]
    verified: set[str] = field(default_factory=set)  # ISINs confirmed by a statement
    differs: set[str] = field(default_factory=set)  # ISINs a statement disagrees with
    reconciliations: list[SourceReconciliation] = field(default_factory=list)


def load_movements(session: Session, user_id: uuid.UUID) -> list[Transaction]:
    """Every row that can change a quantity (see quant.portfolio.positions)."""
    return list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.shares.is_not(None),
                Transaction.isin.is_not(None),
                Transaction.category.in_(QUANTITY_CATEGORIES),
            )
        )
    )


def statement_lines(snapshot: Snapshot) -> list[StatementLine]:
    return [
        StatementLine(
            name=line["name"],
            quantity=Decimal(line["quantity"]),
            value_eur=Decimal(line["value_eur"]),
        )
        for line in snapshot.lines
    ]


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


def build_holdings(session: Session, user_id: uuid.UUID) -> Holdings:
    movements = load_movements(session, user_id)
    current = open_positions(compute_positions(movements))
    current_by_isin = {p.isin: p for p in current}
    holdings = Holdings(positions=current)
    for snapshot in latest_snapshots(session, user_id):
        if snapshot.source not in SOURCE_SCOPE:
            continue
        # A statement describes the day it was issued, so compare it with the history up to
        # that day. Later trades are not discrepancies.
        as_of = [m for m in movements if m.date <= snapshot.as_of]
        result = reconcile_snapshot(open_positions(compute_positions(as_of)), snapshot)
        holdings.reconciliations.append(result)
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
    return holdings

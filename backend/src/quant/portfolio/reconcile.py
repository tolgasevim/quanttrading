"""Compare history-derived positions with a broker statement (FR-19, FR-10c).

A position is never silently shown as a holding when the statement disagrees: every difference
becomes an item in the review queue.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from quant.portfolio.positions import Position

# Statements print quantities to 6 decimals; allow for that rounding and nothing more.
QUANTITY_TOLERANCE = Decimal("0.000001")


class Status(StrEnum):
    MATCH = "match"
    QUANTITY_MISMATCH = "quantity_mismatch"
    MISSING_IN_HISTORY = "missing_in_history"  # on the statement, not in the history
    NOT_ON_STATEMENT = "not_on_statement"  # in the history, not on the statement


@dataclass(frozen=True)
class StatementLine:
    name: str
    quantity: Decimal
    isin: str | None = None
    value_eur: Decimal | None = None


@dataclass(frozen=True)
class Finding:
    status: Status
    isin: str | None
    name: str
    history_quantity: Decimal | None
    statement_quantity: Decimal | None

    @property
    def difference(self) -> Decimal | None:
        if self.history_quantity is None or self.statement_quantity is None:
            return None
        return self.history_quantity - self.statement_quantity


def normalise_name(name: str) -> str:
    """'Ethereum (Ethereum)' → 'ethereum'; case, spacing and a trailing '(...)' are ignored."""
    name = re.sub(r"\s*\(.*?\)\s*$", "", name.strip())
    return re.sub(r"\s+", " ", name).casefold()


def find_by_name(positions: list[Position], name: str) -> Position | None:
    """The one position with this name, or None when there is none or it is ambiguous."""
    wanted = normalise_name(name)
    matches = [p for p in positions if normalise_name(p.name or "") == wanted]
    return matches[0] if len(matches) == 1 else None


def reconcile(
    positions: list[Position], lines: list[StatementLine], asset_classes: frozenset[str]
) -> list[Finding]:
    """Findings for the asset classes the statement covers. `positions` are the open ones."""
    scoped = [p for p in positions if p.asset_class in asset_classes]
    by_isin = {p.isin: p for p in scoped}
    by_name: dict[str, list[Position]] = {}
    for p in scoped:
        by_name.setdefault(normalise_name(p.name or ""), []).append(p)

    findings: list[Finding] = []
    matched: set[str] = set()
    for line in lines:
        pos = by_isin.get(line.isin) if line.isin else None
        if pos is None:
            candidates = [
                c for c in by_name.get(normalise_name(line.name), []) if c.isin not in matched
            ]
            pos = candidates[0] if len(candidates) == 1 else None
        if pos is None:
            findings.append(
                Finding(Status.MISSING_IN_HISTORY, line.isin, line.name, None, line.quantity)
            )
            continue
        matched.add(pos.isin)
        close = abs(pos.quantity - line.quantity) <= QUANTITY_TOLERANCE
        findings.append(
            Finding(
                Status.MATCH if close else Status.QUANTITY_MISMATCH,
                pos.isin,
                pos.name or line.name,
                pos.quantity,
                line.quantity,
            )
        )
    findings.extend(
        Finding(Status.NOT_ON_STATEMENT, p.isin, p.name or p.isin, p.quantity, None)
        for p in scoped
        if p.isin not in matched
    )
    return findings


def summarise(findings: list[Finding]) -> dict[str, int]:
    counts = {s.value: 0 for s in Status}
    for f in findings:
        counts[f.status.value] += 1
    return counts

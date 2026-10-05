"""Trade Republic Crypto-Übersicht (crypto holdings statement, PDF), FR-11b.

Unlike the securities statement this one carries the purchase value (Kaufwert) per coin, so it
cross-checks both the quantities and the cost basis rebuilt from the history.

Privacy (FR-19a): the page header holds the account holder's name, address and account number.
Only the table between the column header and the footer is ever read, so none of it reaches the
parsed result, the database or the logs. The uploaded file is not stored.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from quant.importers.statement_pdf import (
    MAX_PAGES,
    NUMBER,
    StatementFormatError,
    extract_text,
    german_decimal,
)

__all__ = ["MAX_PAGES", "StatementFormatError", "extract_text", "german_decimal"]

SOURCE = "tr_crypto_statement"
ASSET_CLASSES = frozenset({"CRYPTO"})

HEADER_MARKER = "NOMINALE"
FOOTER_COUNT = re.compile(r"ANZAHL DER POSITIONEN:\s*(\d+)")
FOOTER_TOTAL = re.compile(r"SUMME KURSWERTE:\s*(-?[\d.]+(?:,\d+)?)")
AS_OF = re.compile(r"CRYPTO-ÜBERSICHT\s+zum\s+(\d{2})\.(\d{2})\.(\d{4})")

ROW = re.compile(
    rf"^(?P<qty>[\d.]+(?:,\d+)?)\s+(?:Stk\.\s+)?(?P<name>.+?)\s+(?P<price>{NUMBER})\s+(?P<cost>{NUMBER})"
    rf"\s+(?P<pl>{NUMBER})\s+(?P<value>{NUMBER})$"
)


def is_crypto_text(text: str) -> bool:
    return "CRYPTO-ÜBERSICHT" in text


@dataclass(frozen=True)
class CryptoLine:
    name: str
    quantity: Decimal
    price_eur: Decimal
    cost_eur: Decimal  # Kaufwert
    pnl_eur: Decimal
    value_eur: Decimal  # Kurswert

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "quantity": str(self.quantity),
            "price_eur": str(self.price_eur),
            "cost_eur": str(self.cost_eur),
            "pnl_eur": str(self.pnl_eur),
            "value_eur": str(self.value_eur),
        }


@dataclass(frozen=True)
class CryptoStatement:
    as_of: date
    lines: list[CryptoLine]
    footer_count: int
    footer_total: Decimal

    @property
    def total_value(self) -> Decimal:
        return sum((line.value_eur for line in self.lines), Decimal(0))


def parse_text(text: str) -> CryptoStatement:
    lines = [re.sub(r"[ \t]+", " ", raw.replace("\xa0", " ")).strip() for raw in text.splitlines()]
    # The layout puts columns far apart; collapse runs of spaces only after finding the table.
    start = next((i for i, line in enumerate(lines) if line.startswith(HEADER_MARKER)), None)
    end = next((i for i, line in enumerate(lines) if "ANZAHL DER POSITIONEN" in line), None)
    if start is None or end is None or end < start:
        raise StatementFormatError("this doesn't look like a Trade Republic crypto statement")

    count = FOOTER_COUNT.search(lines[end])
    total = FOOTER_TOTAL.search(lines[end])
    when = AS_OF.search(" ".join(lines[:start]))
    if not (count and total and when):
        raise StatementFormatError("the statement's date or footer could not be read")
    try:
        as_of = date(int(when.group(3)), int(when.group(2)), int(when.group(1)))
    except ValueError as exc:
        raise StatementFormatError("the statement date is invalid") from exc

    parsed: list[CryptoLine] = []
    for line in lines[start + 1 : end]:
        match = ROW.match(line)
        if match:
            parsed.append(
                CryptoLine(
                    name=re.sub(r"\s*\(.*?\)\s*$", "", match["name"]).strip() or match["name"],
                    quantity=german_decimal(match["qty"]),
                    price_eur=german_decimal(match["price"]),
                    cost_eur=german_decimal(match["cost"]),
                    pnl_eur=german_decimal(match["pl"]),
                    value_eur=german_decimal(match["value"]),
                )
            )
    statement = CryptoStatement(
        as_of=as_of,
        lines=parsed,
        footer_count=int(count.group(1)),
        footer_total=german_decimal(total.group(1)),
    )
    verify_checksum(statement)
    return statement


def verify_checksum(statement: CryptoStatement) -> None:
    """FR-19: position count and total must equal the footer, otherwise reject with a diff.

    Each line's Kurswert is rounded to the cent by the broker, so the sum may differ from the
    footer by up to a cent per line.
    """
    problems = []
    if len(statement.lines) != statement.footer_count:
        problems.append(
            f"statement says {statement.footer_count} positions, "
            f"{len(statement.lines)} rows were read"
        )
    tolerance = Decimal("0.01") * max(len(statement.lines), 1)
    difference = statement.total_value - statement.footer_total
    if abs(difference) > tolerance:
        problems.append(
            f"rows add up to {statement.total_value:.2f} EUR, the footer says "
            f"{statement.footer_total:.2f} EUR (difference {difference:.2f} EUR)"
        )
    if problems:
        raise StatementFormatError("; ".join(problems) + ". Nothing was imported.")

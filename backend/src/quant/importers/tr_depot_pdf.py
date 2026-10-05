"""Trade Republic Depotauszug (securities account statement, PDF), FR-11.

A holdings snapshot with no cost basis (FR-17): quantity, name, ISIN, custody country, price and
EUR value per line, and a footer with the position count and the total. It reconciles the
positions rebuilt from the history (FR-19) and gives the broker's own price as a mark.

The layout is read line by line, so a page break or an extra line in the file does not shift
anything. A row is a line that starts with a quantity and ends with a price and a value; the
ISIN and the custody country come from the lines under it, until the next row. Check a real
statement on your own machine with `check-holdings --depot-pdf` (PRD §9a): it prints only counts.

Privacy (FR-19a): the page header holds the account holder's name, address and account number.
Only lines under the column header are read, only the row line itself is kept as the name, and
only the ISIN and the custody country are taken from the lines below it, so none of the header
reaches the parsed result, the database or the logs. The uploaded file is not stored.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from quant.importers.statement_pdf import NUMBER, StatementFormatError, german_decimal

SOURCE = "tr_depot_statement"

TITLE = re.compile(r"DEPOTAUSZUG", re.IGNORECASE)
# The column header, repeated on every page: "NOMINALE ... KURSWERT IN EUR".
HEADER = re.compile(r"\bNOMINALE\b.*\bKURSWERT", re.IGNORECASE)
FOOTER_COUNT = re.compile(r"ANZAHL DER POSITIONEN:\s*(\d+)", re.IGNORECASE)
FOOTER_TOTAL = re.compile(rf"\bSUMME[^:\d]*:\s*({NUMBER})", re.IGNORECASE)
AS_OF = re.compile(r"\b(?:zum|per|stand)\s+(\d{2})\.(\d{2})\.(\d{4})", re.IGNORECASE)

ROW = re.compile(
    rf"^(?P<qty>[\d.]+(?:,\d+)?)\s+(?:Stk\.\s+)?(?P<name>.+?)\s+(?P<price>{NUMBER})\s+(?P<value>{NUMBER})$"
)
ISIN = r"[A-Z]{2}[A-Z0-9]{9}\d"
# An ISIN counts under a row when it is labelled, or when it is the only thing on its line (a
# "Stk." that wrapped may come before it). A stray 12-character token in a longer line does not.
ISIN_TOKEN = re.compile(rf"\bISIN:?\s*({ISIN})\b|^(?:Stk\.\s+)?({ISIN})$")
CUSTODY = re.compile(r"\b(?:Lagerland|Verwahrland|Lagerstelle):?\s*(?P<country>[^\d:]+?)\s*$")

# The price is rounded by the broker, so quantity x price can differ from the value by a little.
PRICE_TOLERANCE = Decimal("0.005")  # of the value
PRICE_TOLERANCE_MIN = Decimal("0.01")


@dataclass(frozen=True)
class DepotLine:
    name: str
    isin: str
    custody: str | None
    quantity: Decimal
    price_eur: Decimal | None  # None when price x quantity is not the value (a bond in % of par)
    value_eur: Decimal

    def to_json(self) -> dict[str, str]:
        out = {
            "name": self.name,
            "isin": self.isin,
            "quantity": str(self.quantity),
            "value_eur": str(self.value_eur),
        }
        if self.custody:
            out["custody"] = self.custody
        if self.price_eur is not None:
            out["price_eur"] = str(self.price_eur)
        return out


@dataclass(frozen=True)
class DepotStatement:
    as_of: date
    lines: list[DepotLine]
    footer_count: int
    footer_total: Decimal

    @property
    def total_value(self) -> Decimal:
        return sum((line.value_eur for line in self.lines), Decimal(0))


def _clean(raw: str) -> str:
    return re.sub(r"[ \t]+", " ", raw.replace("\xa0", " ")).strip()


def is_depot_text(text: str) -> bool:
    return bool(TITLE.search(text)) and any(HEADER.search(_clean(raw)) for raw in text.splitlines())


def _consistent(quantity: Decimal, price: Decimal, value: Decimal) -> bool:
    tolerance = max(PRICE_TOLERANCE_MIN, abs(value) * PRICE_TOLERANCE)
    return abs(quantity * price - value) <= tolerance


class _Row:
    def __init__(self, match: re.Match[str]) -> None:
        self.isin: str | None = None
        self.custody: str | None = None
        name = match["name"]
        # The ISIN may sit on the row line itself.
        inline = re.search(rf"\s*\bISIN:?\s*({ISIN})\b", name)
        if inline:
            self.isin = inline.group(1)
            name = name.replace(inline.group(0), " ")
        self.name = name.strip() or match["name"]
        self.quantity = german_decimal(match["qty"])
        self.price = german_decimal(match["price"])
        self.value = german_decimal(match["value"])

    def line(self) -> DepotLine:
        if self.isin is None:
            raise StatementFormatError(
                "a position on the statement has no ISIN, so the layout is not the one this app "
                "reads. Nothing was imported."
            )
        price = self.price if _consistent(self.quantity, self.price, self.value) else None
        return DepotLine(self.name, self.isin, self.custody, self.quantity, price, self.value)


def parse_text(text: str) -> DepotStatement:
    lines = [_clean(raw) for raw in text.splitlines()]
    first_header = next((i for i, line in enumerate(lines) if HEADER.search(line)), None)
    end = next((i for i, line in enumerate(lines) if FOOTER_COUNT.search(line)), None)
    if not TITLE.search(text) or first_header is None or end is None or end < first_header:
        raise StatementFormatError("this doesn't look like a Trade Republic securities statement")

    count = FOOTER_COUNT.search(lines[end])
    # The total is on the count line or next to it, before or after.
    footer = " ".join(lines[max(end - 2, first_header) : end + 3])
    total = FOOTER_TOTAL.search(footer)
    # The date after the title is the statement's; another date above it (a letter date) is not.
    title = next((i for i, line in enumerate(lines[:first_header]) if TITLE.search(line)), 0)
    when = AS_OF.search(" ".join(lines[title:first_header])) or AS_OF.search(
        " ".join(lines[:first_header])
    )
    if not (count and total and when):
        raise StatementFormatError("the statement's date or footer could not be read")
    try:
        as_of = date(int(when.group(3)), int(when.group(2)), int(when.group(1)))
    except ValueError as exc:
        raise StatementFormatError("the statement date is invalid") from exc

    rows: list[_Row] = []
    current: _Row | None = None
    for line in lines[first_header + 1 : end]:
        if not line or HEADER.search(line):
            continue
        row = ROW.match(line)
        if row:
            current = _Row(row)
            rows.append(current)
            continue
        if current is None:
            continue
        isin = ISIN_TOKEN.search(line)
        if isin and current.isin is None:
            current.isin = isin.group(1) or isin.group(2)
        custody = CUSTODY.search(line)
        if custody and current.custody is None:
            current.custody = custody["country"].strip()

    statement = DepotStatement(
        as_of=as_of,
        lines=[row.line() for row in rows],
        footer_count=int(count.group(1)),
        footer_total=german_decimal(total.group(1)),
    )
    verify_checksum(statement)
    return statement


def verify_checksum(statement: DepotStatement) -> None:
    """FR-19: position count and total must equal the footer, otherwise reject with a diff.

    The broker rounds each line to the cent, so the sum may differ from the footer by up to a
    cent per line.
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

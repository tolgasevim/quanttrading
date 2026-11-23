"""Trade Republic trade confirmation (Wertpapierabrechnung, PDF), FR-11a.

One file is one order: the share or fund, the quantity, the price, the amount, the fees and the
taxes withheld, and the total that left or came into the account. That gives the cost basis of the
trade (amount plus fees) and the tax withheld on it, which the transaction export carries as
separate rows.

**Not yet checked on a real confirmation.** It is written from the layout as it is documented
(labelled lines: ISIN, a quantity in "Stk.", the amounts, the tax lines, a total) and fails
closed: a file with a missing piece, or one whose numbers do not add up, is refused whole, never
half read. Check a real one on your own machine with `check-confirmation` (PRD §9a): it prints only
counts and pass or fail.

Privacy (FR-19a): the page header has the account holder's name, address and account number. Only
labelled lines are read, and only their numbers, the ISIN and the instrument name are kept, so none
of the header reaches the result. The uploaded file is not stored.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from quant.importers.statement_pdf import NUMBER, StatementFormatError, german_decimal

TITLE = re.compile(r"WERTPAPIERABRECHNUNG|ABRECHNUNG\s+(?:KAUF|VERKAUF|SPARPLAN)", re.IGNORECASE)
SIDES = (
    ("sell", re.compile(r"\bVerkauf\b|\bSell\b", re.IGNORECASE)),
    ("buy", re.compile(r"\bKauf\b|\bSparplan(?:ausführung)?\b|\bBuy\b", re.IGNORECASE)),
)
ISIN = r"[A-Z]{2}[A-Z0-9]{9}\d"
ISIN_LINE = re.compile(rf"\bISIN:?\s*({ISIN})\b")
DATE_LINE = re.compile(
    r"\b(?:Ausführung|Ausfuehrung|Handelstag|Datum)\b[^\d]*(\d{2})\.(\d{2})\.(\d{4})", re.IGNORECASE
)
# "Apple Inc. ... 0,5 Stk. 172,38 EUR 86,19 EUR": the name, a quantity, a price, an amount.
POSITION = re.compile(
    rf"^(?P<name>.+?)\s+(?P<qty>[\d.]+(?:,\d+)?)\s*Stk\.?\s+(?P<price>{NUMBER})\s*EUR\s+(?P<amount>{NUMBER})\s*EUR\s*$"
)
MONEY = re.compile(rf"({NUMBER})\s*EUR\s*$")
TOTAL = re.compile(r"^(?:GESAMT|Total|Summe)\b", re.IGNORECASE)
FEE_LABELS = ("Gebühr", "Gebuehr", "Fremdkosten", "Provision", "Entgelt")
TAX_LABELS = {
    "Kapitalertragsteuer": "kapitalertragsteuer",
    "Solidaritätszuschlag": "solidaritaetszuschlag",
    "Solidaritaetszuschlag": "solidaritaetszuschlag",
    "Kirchensteuer": "kirchensteuer",
    "Finanztransaktionssteuer": "finanztransaktionssteuer",
    "Quellensteuer": "quellensteuer",
}
TOLERANCE = Decimal("0.02")  # the broker rounds each line to the cent


@dataclass(frozen=True)
class Confirmation:
    side: str  # "buy" | "sell"
    isin: str
    name: str
    day: date
    quantity: Decimal
    price: Decimal  # EUR a unit, as printed
    amount: Decimal  # EUR, positive: the gross value of the trade
    fees: Decimal  # EUR, positive
    taxes: dict[str, Decimal] = field(default_factory=dict)  # EUR, positive, by kind
    total: Decimal = Decimal(0)  # EUR, positive: what was paid (buy) or credited (sell)

    @property
    def tax_total(self) -> Decimal:
        return sum(self.taxes.values(), Decimal(0))

    @property
    def cost_basis(self) -> Decimal | None:
        """What a purchase cost in all: the amount plus the fees (taxes are not part of it)."""
        return self.amount + self.fees if self.side == "buy" else None


def _money(line: str) -> Decimal | None:
    m = MONEY.search(line)
    return abs(german_decimal(m.group(1))) if m else None


def parse_text(text: str) -> Confirmation:
    if not TITLE.search(text):
        raise StatementFormatError("this is not a trade confirmation")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    side = next((name for name, rx in SIDES if any(rx.search(ln) for ln in lines[:12])), None)
    if side is None:
        raise StatementFormatError("the confirmation does not say whether it is a buy or a sell")
    isins = {m.group(1) for ln in lines if (m := ISIN_LINE.search(ln))}
    if len(isins) != 1:
        raise StatementFormatError("expected exactly one ISIN in the confirmation")
    d = next((m for ln in lines if (m := DATE_LINE.search(ln))), None)
    if d is None:
        raise StatementFormatError("no execution date found")
    row = next((m for ln in lines if (m := POSITION.match(ln))), None)
    if row is None:
        raise StatementFormatError("no position line (name, quantity, price, amount) found")

    quantity = german_decimal(row["qty"])
    price = abs(german_decimal(row["price"]))
    amount = abs(german_decimal(row["amount"]))
    fees = Decimal(0)
    taxes: dict[str, Decimal] = {}
    total: Decimal | None = None
    for ln in lines:
        if TOTAL.match(ln):
            total = _money(ln)
            continue
        value = _money(ln)
        if value is None:
            continue
        if ln.startswith(FEE_LABELS):
            fees += value
        else:
            for label, kind in TAX_LABELS.items():
                if ln.startswith(label):
                    taxes[kind] = taxes.get(kind, Decimal(0)) + value
    if total is None:
        raise StatementFormatError("no total (GESAMT) found")
    if quantity <= 0 or amount <= 0:
        raise StatementFormatError("the quantity and the amount must be positive")
    result = Confirmation(
        side=side,
        isin=next(iter(isins)),
        name=re.sub(r"\s+", " ", row["name"]).strip(),
        day=date(int(d.group(3)), int(d.group(2)), int(d.group(1))),
        quantity=quantity,
        price=price,
        amount=amount,
        fees=fees,
        taxes=taxes,
        total=total,
    )
    verify(result)
    return result


def verify(c: Confirmation) -> None:
    """The numbers must add up, or the whole file is refused: quantity times price is the amount,
    and the total is the amount plus the costs (a buy) or less them (a sell)."""
    if abs(c.quantity * c.price - c.amount) > max(TOLERANCE, c.amount * Decimal("0.005")):
        raise StatementFormatError("quantity times price does not equal the amount")
    costs = c.fees + c.tax_total
    expected = c.amount + costs if c.side == "buy" else c.amount - costs
    if abs(expected - c.total) > TOLERANCE:
        raise StatementFormatError("the amount, the costs and the total do not add up")

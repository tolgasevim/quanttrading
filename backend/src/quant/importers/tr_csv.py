"""Trade Republic unified transaction export (CSV), FR-10 and PRD §6.2a.

Privacy (FR-10a, FR-19a): everything that identifies people or spending is dropped here, before
anything is stored or logged:
- counterparty names, IBANs, payment references and merchant category codes, always;
- the free-text description, always (only a "savings plan" flag is derived from it);
- the `name` column unless the row refers to an instrument (on transfers it holds account
  holders' names);
- for card and bank-transfer rows, everything but id, time, type and the EUR amount/fee/tax.
"""

import csv
import enum
import io
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

SOURCE = "tr_transactions_csv"
BROKER = "tr"

REQUIRED_COLUMNS = {
    "datetime",
    "date",
    "category",
    "type",
    "asset_class",
    "name",
    "symbol",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "currency",
    "transaction_id",
}


class Kind(enum.StrEnum):
    TRADE = "trade"
    INCOME = "income"  # dividends, distributions, fund earnings
    INTEREST = "interest"
    BENEFIT = "benefit"  # saveback, bonus, stock perks
    DELIVERY = "delivery"  # free receipts (e.g. crypto saveback), FR-10e
    CORPORATE_ACTION = "corporate_action"  # FR-10c engine consumes these
    TAX = "tax"  # tax optimisation / refunds
    TRANSFER = "transfer"  # deposits and withdrawals
    CARD = "card"  # card spending, cash balance only (FR-10a)
    FEE = "fee"
    OTHER = "other"


# (category, type) -> kind. Types not listed fall back by category, then to OTHER with a warning.
TYPE_KINDS: dict[tuple[str, str], Kind] = {
    ("TRADING", "BUY"): Kind.TRADE,
    ("TRADING", "SELL"): Kind.TRADE,
    ("CASH", "PRIVATE_MARKET_BUY"): Kind.TRADE,
    ("CASH", "IPO_SUBSCRIPTION"): Kind.TRADE,
    ("CASH", "DIVIDEND"): Kind.INCOME,
    ("CASH", "DISTRIBUTION"): Kind.INCOME,
    ("CASH", "EARNINGS"): Kind.INCOME,
    ("CASH", "INTEREST_PAYMENT"): Kind.INTEREST,
    ("CASH", "BENEFITS_SAVEBACK"): Kind.BENEFIT,
    ("CASH", "BONUS"): Kind.BENEFIT,
    ("CASH", "STOCKPERK"): Kind.BENEFIT,
    ("CASH", "TAX_OPTIMIZATION"): Kind.TAX,
    ("CASH", "CARD_TRANSACTION"): Kind.CARD,
    ("CASH", "CARD_TRANSACTION_INTERNATIONAL"): Kind.CARD,
    ("CASH", "CARD_ORDERING_FEE"): Kind.FEE,
    ("CASH", "CUSTOMER_INBOUND"): Kind.TRANSFER,
    ("CASH", "CUSTOMER_OUTBOUND_REQUEST"): Kind.TRANSFER,
    ("CASH", "TRANSFER_INBOUND"): Kind.TRANSFER,
    ("CASH", "TRANSFER_OUTBOUND"): Kind.TRANSFER,
    ("CASH", "TRANSFER_INSTANT_INBOUND"): Kind.TRANSFER,
    ("CASH", "TRANSFER_INSTANT_OUTBOUND"): Kind.TRANSFER,
    ("CASH", "TRANSFER_DIRECT_DEBIT_INBOUND"): Kind.TRANSFER,
    # Cash legs of corporate actions and redemptions.
    ("CASH", "EXCHANGE"): Kind.CORPORATE_ACTION,
    ("CASH", "LIQUIDATION_PROCEEDS"): Kind.CORPORATE_ACTION,
    ("CASH", "TILG"): Kind.CORPORATE_ACTION,  # knock-out/redemption payout
    ("CASH", "SEC_ACCOUNT"): Kind.CORPORATE_ACTION,
}
CATEGORY_KINDS: dict[str, Kind] = {
    "CORPORATE_ACTION": Kind.CORPORATE_ACTION,
    "DELIVERY": Kind.DELIVERY,
    "TRADING": Kind.TRADE,
}

# Kinds reduced to id, time, type and EUR amounts.
MINIMAL_KINDS = {Kind.CARD, Kind.TRANSFER, Kind.FEE}

SAVINGS_PLAN_PREFIX = "savings plan execution"


class ImportFormatError(ValueError):
    """The file is not a TR transaction export we understand. Nothing is imported."""


@dataclass
class ParsedTransaction:
    external_id: str
    executed_at: datetime
    date: str  # ISO date; kept as text so staged rows are JSON-serialisable
    kind: Kind
    category: str
    type: str
    asset_class: str | None = None
    isin: str | None = None
    name: str | None = None
    shares: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    fee: Decimal | None = None
    tax: Decimal | None = None
    currency: str | None = None
    original_amount: Decimal | None = None
    original_currency: str | None = None
    fx_rate: Decimal | None = None
    savings_plan: bool = False

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
            elif isinstance(value, datetime):
                data[key] = value.isoformat()
        data["kind"] = str(self.kind)
        return data


@dataclass
class ParseResult:
    transactions: list[ParsedTransaction]
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0

    def summary(self) -> dict[str, Any]:
        """Aggregate view for the preview screen. Contains no names and no amounts per row."""
        kinds = Counter(str(t.kind) for t in self.transactions)
        dates = [t.date for t in self.transactions]
        cash = sum(
            ((t.amount or Decimal(0)) + (t.fee or Decimal(0)) + (t.tax or Decimal(0)))
            for t in self.transactions
        )
        return {
            "rows": len(self.transactions),
            "skipped": self.skipped,
            "by_kind": dict(sorted(kinds.items())),
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
            "instruments": len({t.isin for t in self.transactions if t.isin}),
            "savings_plan_executions": sum(t.savings_plan for t in self.transactions),
            # Net cash flow of this file: the history-derived cash balance (FR-28) when the file
            # covers the whole account life.
            "net_cash_flow": str(cash),
            "warnings": self.warnings[:50],
            "warning_count": len(self.warnings),
        }


def _decimal(value: str | None, column: str, line: int) -> Decimal | None:
    if value is None or value.strip() == "":
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"line {line}: {column} is not a number") from exc


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def classify(category: str, type_: str) -> Kind | None:
    return TYPE_KINDS.get((category, type_)) or CATEGORY_KINDS.get(category)


def parse(text: str) -> ParseResult:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise ImportFormatError(
            "this doesn't look like a Trade Republic transaction export "
            f"(missing columns: {', '.join(sorted(missing))})"
        )

    result = ParseResult(transactions=[])
    seen: set[str] = set()
    unknown_types: Counter[str] = Counter()

    for line, row in enumerate(reader, start=2):
        try:
            parsed = _parse_row(row, line)
        except ValueError as exc:
            result.skipped += 1
            result.warnings.append(str(exc))
            continue
        if parsed.external_id in seen:
            result.skipped += 1
            result.warnings.append(f"line {line}: duplicate transaction id, skipped")
            continue
        seen.add(parsed.external_id)
        if parsed.kind == Kind.OTHER:
            unknown_types[f"{parsed.category}/{parsed.type}"] += 1
        result.transactions.append(parsed)

    for type_name, count in sorted(unknown_types.items()):
        result.warnings.append(f"unknown type {type_name} ({count} rows), imported as 'other'")
    return result


def _parse_row(row: dict[str, str], line: int) -> ParsedTransaction:
    external_id = _text(row.get("transaction_id"))
    if not external_id:
        raise ValueError(f"line {line}: missing transaction_id")
    try:
        executed_at = datetime.fromisoformat((row.get("datetime") or "").strip())
        day = datetime.fromisoformat((row.get("date") or "").strip()).date()
    except ValueError as exc:
        raise ValueError(f"line {line}: invalid date") from exc
    if executed_at.tzinfo is None:
        raise ValueError(f"line {line}: datetime has no timezone")

    category = (row.get("category") or "").strip()
    type_ = (row.get("type") or "").strip()
    kind = classify(category, type_) or Kind.OTHER
    isin = _text(row.get("symbol"))

    tx = ParsedTransaction(
        external_id=external_id,
        executed_at=executed_at,
        date=day.isoformat(),
        kind=kind,
        category=category,
        type=type_,
        amount=_decimal(row.get("amount"), "amount", line),
        fee=_decimal(row.get("fee"), "fee", line),
        tax=_decimal(row.get("tax"), "tax", line),
        currency=_text(row.get("currency")),
    )
    if kind in MINIMAL_KINDS:
        return tx

    tx.asset_class = _text(row.get("asset_class"))
    tx.isin = isin
    # Without an instrument, `name` is a person (e.g. the account holder on a deposit).
    tx.name = _text(row.get("name")) if isin else None
    tx.shares = _decimal(row.get("shares"), "shares", line)
    tx.price = _decimal(row.get("price"), "price", line)
    tx.original_amount = _decimal(row.get("original_amount"), "original_amount", line)
    tx.original_currency = _text(row.get("original_currency"))
    tx.fx_rate = _decimal(row.get("fx_rate"), "fx_rate", line)
    tx.savings_plan = (row.get("description") or "").strip().lower().startswith(SAVINGS_PLAN_PREFIX)
    return tx


# Raw columns whose values must never appear in parsed output (used by the privacy self-check).
PRIVATE_COLUMNS = ("counterparty_name", "counterparty_iban", "payment_reference", "mcc_code")


def privacy_leaks(text: str, result: ParseResult) -> int:
    """Count private values from the raw file that survived into the parsed rows.

    Checks counterparty fields always, and the name/description of card and transfer rows.
    Text values (4+ characters) must not appear inside any parsed text field; purely numeric
    values (e.g. merchant category codes) must not appear as a whole field, since as substrings
    they would match every date and amount.
    """
    private: set[str] = set()
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        values = [row.get(c) for c in PRIVATE_COLUMNS]
        kind = classify((row.get("category") or "").strip(), (row.get("type") or "").strip())
        if kind in MINIMAL_KINDS or not (row.get("symbol") or "").strip():
            values += [row.get("name"), row.get("description")]
        private.update(v.strip() for v in values if v and len(v.strip()) >= 4)
    numeric = {v for v in private if v.replace(".", "").replace("-", "").isdigit()}
    textual = private - numeric

    leaks: set[str] = set()
    for tx in result.transactions:
        fields = [v for v in tx.to_json().values() if isinstance(v, str)]
        leaks.update(v for v in fields if v in numeric)
        leaks.update(p for p in textual for v in fields if p in v)
    return len(leaks)

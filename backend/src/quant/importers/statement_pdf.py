"""What the Trade Republic statement parsers share: reading a PDF into text, and German numbers."""

import io
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

# A holdings statement is a few pages; a "PDF" with hundreds is not one.
MAX_PAGES = 20

# The broker drops trailing zeros: 2012.90 prints as "2.012,9" and 2500 as "2.500", so the
# decimal part is optional everywhere.
NUMBER = r"-?[\d.]+(?:,\d+)?"


class StatementFormatError(ValueError):
    """Not a statement we understand, or its totals don't add up. Nothing is imported."""


def german_decimal(text: str) -> Decimal:
    """'12.423,854702' → Decimal('12423.854702'); '2.500' (no decimals) → Decimal('2500')."""
    try:
        return Decimal(text.replace(".", "").replace(",", "."))
    except InvalidOperation as exc:
        raise StatementFormatError(f"not a number: {text!r}") from exc


def extract_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PAGES:
            raise StatementFormatError(f"this PDF has more than {MAX_PAGES} pages")
        return "\n".join(page.extract_text(extraction_mode="layout") for page in reader.pages)
    except StatementFormatError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types on malformed input
        raise StatementFormatError("this file is not a readable PDF") from exc

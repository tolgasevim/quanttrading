from datetime import date
from decimal import Decimal

import pytest

from quant.importers import tr_crypto_pdf as c

from .pdfutil import crypto_statement_pdf

ROWS = [
    ("4,944466", "Ethereum", "2.375,16", "12.558,27", "-814,37", "11.743,9"),
    ("12.423,854702", "Cardano", "0,23", "2.007,13", "803,28", "2.810,41"),
    ("2.030,784567", "XRP", "1,35", "2.012,9", "726,54", "2.739,43"),
    ("0,093152", "Bitcoin", "74.360,55", "4.850,63", "2.076,21", "6.926,83"),
]
# The broker rounds each line, so the footer is a cent off the sum of the rows.
TOTAL = "24.220,58"

# What pypdf's layout mode produces for the real statement: non-breaking spaces, and "Stk."
# wrapped onto the next line for long quantities.
LAYOUT_TEXT = """\
TRADE REPUBLIC BANK GMBH           BRUNNENSTRASSE 19-21           10119 BERLIN
MAX MUSTERMANN
Musterweg 12                                   DATUM                             27.09.2026
12345 Musterstadt
                                                       CRYPTO-ÜBERSICHT
                                                                      zum 27.09.2026
Aufstellung über die Cryptos in deinem Depot 0123456789 zum 27.09.2026.

NOMINALE           INSTRUMENT NAME                 PREIS JE ANTEIL                  KAUFWERT IN EUR         GEWINN / VERLUST                    KURSWERT IN EUR
4,944466 Stk.      Ethereum\xa0(Ethereum)             2.375,16                         12.558,27               -814,37                                    11.743,9
                                                   27.09.2026                                               -6,48%
12.423,854702 Cardano\xa0(Cardano)                    0,23                             2.007,13                803,28                                     2.810,41
Stk.                                               27.09.2026                                               40,02%
2.030,784567       XRP\xa0(XRP)                       1,35                             2.012,9                 726,54                                     2.739,43
Stk.                                               27.09.2026                                               36,09%
0,093152 Stk.      Bitcoin\xa0(Bitcoin)               74.360,55                        4.850,63                2.076,21                                   6.926,83
                                                   27.09.2026                                               42,8%
                   ANZAHL DER POSITIONEN:\xa04                                                                              SUMME KURSWERTE: 24.220,58\xa0€
"""
PII = ["MUSTERMANN", "Musterweg", "Musterstadt", "0123456789", "BRUNNENSTRASSE"]


def test_parses_layout_text_with_wrapped_units_and_nbsp() -> None:
    statement = c.parse_text(LAYOUT_TEXT)
    assert statement.as_of == date(2026, 9, 27)
    assert [line.name for line in statement.lines] == ["Ethereum", "Cardano", "XRP", "Bitcoin"]
    cardano = statement.lines[1]
    assert cardano.quantity == Decimal("12423.854702") and cardano.cost_eur == Decimal("2007.13")
    assert statement.lines[0].pnl_eur == Decimal("-814.37")
    assert statement.footer_count == 4 and statement.footer_total == Decimal("24220.58")
    assert statement.total_value == Decimal("24220.57")  # a cent of rounding is fine


def test_personal_data_in_the_header_never_reaches_the_result() -> None:
    statement = c.parse_text(LAYOUT_TEXT)
    dumped = str([line.to_json() for line in statement.lines]) + str(statement)
    for secret in PII:
        assert secret not in dumped, secret


def test_german_decimals() -> None:
    assert c.german_decimal("12.423,854702") == Decimal("12423.854702")
    assert c.german_decimal("-6,48") == Decimal("-6.48")
    with pytest.raises(c.StatementFormatError):
        c.german_decimal("abc")


def test_checksum_rejects_a_missing_row_with_a_diff() -> None:
    broken = "\n".join(line for line in LAYOUT_TEXT.splitlines() if "XRP" not in line)
    with pytest.raises(c.StatementFormatError) as error:
        c.parse_text(broken)
    message = str(error.value)
    assert "says 4 positions, 3 rows were read" in message
    assert "difference" in message and "Nothing was imported" in message


def test_checksum_rejects_a_wrong_total() -> None:
    broken = LAYOUT_TEXT.replace("24.220,58", "25.000,00")
    with pytest.raises(c.StatementFormatError, match="footer says 25000.00"):
        c.parse_text(broken)


def test_other_documents_are_rejected() -> None:
    with pytest.raises(c.StatementFormatError, match="doesn't look like"):
        c.parse_text("Depotauszug\nNOMINALE INSTRUMENT\nirgendwas")
    with pytest.raises(c.StatementFormatError, match="not a readable PDF"):
        c.extract_text(b"%PDF-1.4 this is not really a pdf")


def test_extraction_from_a_real_pdf_file() -> None:
    pdf = crypto_statement_pdf(ROWS, total=TOTAL)
    statement = c.parse_text(c.extract_text(pdf))
    assert [str(line.quantity) for line in statement.lines] == [
        "4.944466",
        "12423.854702",
        "2030.784567",
        "0.093152",
    ]
    assert statement.as_of == date(2026, 9, 27)
    for secret in PII:
        assert secret not in str(statement)


def test_a_pdf_with_too_many_pages_is_rejected() -> None:
    import io

    from pypdf import PdfReader, PdfWriter

    page = PdfReader(io.BytesIO(crypto_statement_pdf(ROWS, total=TOTAL)))
    writer = PdfWriter()
    for _ in range(c.MAX_PAGES + 1):
        writer.add_page(page.pages[0])
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(c.StatementFormatError, match="more than 20 pages"):
        c.extract_text(out.getvalue())


def test_corrupted_pdfs_only_ever_raise_a_statement_error() -> None:
    """Fuzz: a seeded run of damaged copies of a valid statement. pypdf raises AttributeError,
    TypeError, IndexError and LookupError on some of them, which used to escape as a 500."""
    import random

    base = crypto_statement_pdf(ROWS, total=TOTAL)
    rng = random.Random(7)  # noqa: S311 - a reproducible fuzz seed, not cryptography
    failures = 0
    for _ in range(300):
        data = bytearray(base)
        for _ in range(rng.randint(1, 8)):
            pos = rng.randrange(len(data))
            roll = rng.random()
            if roll < 0.5:
                data[pos] = rng.randrange(256)
            elif roll < 0.8:
                del data[pos : pos + rng.randint(1, 40)]
            else:
                data[pos:pos] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 20)))
        try:
            c.extract_text(bytes(data))
        except c.StatementFormatError:
            failures += 1
    assert failures > 0  # the run really did produce damaged files

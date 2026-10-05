from datetime import date
from decimal import Decimal

import pytest

from quant.importers import tr_depot_pdf as d
from quant.importers.statement_pdf import StatementFormatError, extract_text

from .pdfutil import depot_statement_pdf

ROWS = [
    ("10", "NVIDIA Corp. Registered Shares DL-,001", "US67066G1040", "Vereinigte Staaten", "120,5", "1.205"),
    ("0,5", "iShares NASDAQ 100 UCITS ETF (Acc)", "IE00B53SZB19", "Irland", "200", "100"),
    ("2.500", "Beispiel Anleihe 2030", "DE0000000001", "Deutschland", "98,5", "2.462,5"),
]  # fmt: skip
TOTAL = "3.767,5"

# What pypdf's layout mode produces: non-breaking spaces, a page break between a row and its
# ISIN, the holder's name and address again at the top of the second page, and a repeated header.
LAYOUT_TEXT = """\
TRADE REPUBLIC BANK GMBH           BRUNNENSTRASSE 19-21           10119 BERLIN
MAX MUSTERMANN
Musterweg 12                                   DATUM                             27.09.2026
12345 Musterstadt
                                                       DEPOTAUSZUG
                                                                      zum 27.09.2026
Depot 0123456789

STK./NOMINALE       BEZEICHNUNG                                      KURS PRO STÜCK         KURSWERT IN EUR
10 Stk.             NVIDIA Corp. Registered Shares DL-,001           120,5                  1.205
                    ISIN: US67066G1040
                    Lagerland: Vereinigte Staaten
                                                                     27.09.2026
0,5 Stk.            iShares\xa0NASDAQ 100 UCITS ETF (Acc)              200                    100
                                                    Seite 1 von 2
MAX MUSTERMANN
Musterweg 12
12345 Musterstadt
STK./NOMINALE       BEZEICHNUNG                                      KURS PRO STÜCK         KURSWERT IN EUR
                    ISIN: IE00B53SZB19
                    Lagerland: Irland
2.500               Beispiel Anleihe 2030                            98,5                   2.462,5
Stk.                DE0000000001
                    Lagerland: Deutschland
                    ANZAHL DER POSITIONEN:\xa03                                             SUMME KURSWERTE: 3.767,5\xa0€
"""
PII = ["MUSTERMANN", "Musterweg", "Musterstadt", "0123456789", "BRUNNENSTRASSE"]


def test_parses_layout_text_across_a_page_break() -> None:
    statement = d.parse_text(LAYOUT_TEXT)
    assert statement.as_of == date(2026, 9, 27)
    assert [line.isin for line in statement.lines] == [
        "US67066G1040",
        "IE00B53SZB19",
        "DE0000000001",
    ]
    nvidia, etf, bond = statement.lines
    assert nvidia.quantity == Decimal(10) and nvidia.price_eur == Decimal("120.5")
    assert nvidia.custody == "Vereinigte Staaten" and nvidia.value_eur == Decimal(1205)
    assert etf.name == "iShares NASDAQ 100 UCITS ETF (Acc)" and etf.custody == "Irland"
    assert bond.quantity == Decimal(2500)  # "2.500" with no comma is whole
    assert statement.footer_count == 3 and statement.footer_total == Decimal("3767.5")


def test_the_holders_details_never_reach_the_result() -> None:
    statement = d.parse_text(LAYOUT_TEXT)
    dumped = str([line.to_json() for line in statement.lines]) + str(statement)
    for secret in PII:
        assert secret not in dumped, secret


def test_a_price_that_is_not_price_times_quantity_is_not_kept() -> None:
    # A bond quoted in percent of par: 2,500 units at 98.5 would be 246,250, the value is 2,462.5.
    statement = d.parse_text(LAYOUT_TEXT)
    assert [line.price_eur is not None for line in statement.lines] == [True, True, False]
    assert "price_eur" not in statement.lines[2].to_json()


def test_an_isin_on_the_row_line_is_found_and_left_out_of_the_name() -> None:
    text = LAYOUT_TEXT.replace(
        "NVIDIA Corp. Registered Shares DL-,001           120,5",
        "NVIDIA Corp. ISIN: US67066G1040 120,5",
    ).replace("                    ISIN: US67066G1040\n", "")
    nvidia = d.parse_text(text).lines[0]
    assert nvidia.isin == "US67066G1040" and nvidia.name == "NVIDIA Corp."


def test_a_row_without_an_isin_is_refused() -> None:
    broken = LAYOUT_TEXT.replace("                    ISIN: US67066G1040\n", "")
    with pytest.raises(StatementFormatError, match="no ISIN"):
        d.parse_text(broken)


def test_checksum_rejects_a_missing_row_with_a_diff() -> None:
    broken = LAYOUT_TEXT.replace("0,5 Stk.", "xx Stk.")
    with pytest.raises(StatementFormatError) as error:
        d.parse_text(broken)
    message = str(error.value)
    assert "says 3 positions, 2 rows were read" in message
    assert "difference" in message and "Nothing was imported" in message


def test_checksum_rejects_a_wrong_total() -> None:
    with pytest.raises(StatementFormatError, match="footer says 9999.00"):
        d.parse_text(LAYOUT_TEXT.replace("3.767,5", "9.999"))


def test_a_cent_of_rounding_per_line_is_fine() -> None:
    assert d.parse_text(LAYOUT_TEXT.replace("3.767,5", "3.767,51")).footer_count == 3


@pytest.mark.parametrize("text", ["", "hello", "DEPOTAUSZUG only", LAYOUT_TEXT.split("ANZAHL")[0]])
def test_other_text_is_not_a_statement(text: str) -> None:
    with pytest.raises(StatementFormatError):
        d.parse_text(text)


def test_a_missing_date_is_refused() -> None:
    with pytest.raises(StatementFormatError, match="date or footer"):
        d.parse_text(LAYOUT_TEXT.replace("zum 27.09.2026", "").replace("DATUM  ", "DATUM"))


def test_depot_text_is_told_from_crypto_text() -> None:
    from .test_tr_crypto_pdf import LAYOUT_TEXT as CRYPTO_TEXT

    assert d.is_depot_text(LAYOUT_TEXT)
    assert not d.is_depot_text(CRYPTO_TEXT)


def test_a_real_pdf_goes_end_to_end() -> None:
    pdf = depot_statement_pdf(ROWS, total=TOTAL)
    statement = d.parse_text(extract_text(pdf))
    assert [line.isin for line in statement.lines] == [r[2] for r in ROWS]
    assert statement.lines[0].custody == "Vereinigte Staaten"
    dumped = str(statement)
    for secret in PII:
        assert secret not in dumped, secret


def test_the_date_after_the_title_is_the_statements_not_an_earlier_one() -> None:
    text = LAYOUT_TEXT.replace(
        "TRADE REPUBLIC BANK GMBH           BRUNNENSTRASSE 19-21           10119 BERLIN",
        "Erstellt zum 05.10.2026",
        1,
    )
    assert d.parse_text(text).as_of == date(2026, 9, 27)


def test_a_stray_isin_shaped_word_in_a_longer_line_is_not_the_rows_isin() -> None:
    text = LAYOUT_TEXT.replace(
        "                    ISIN: US67066G1040\n",
        "                    Hinweis AB1234567890 siehe unten\n",
    )
    with pytest.raises(StatementFormatError, match="no ISIN"):
        d.parse_text(text)

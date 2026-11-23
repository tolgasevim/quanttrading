"""TR trade confirmations (FR-11a). The text is synthetic: no real broker file is ever committed."""

from decimal import Decimal as D
from pathlib import Path

import pytest

from quant import cli
from quant.importers import statement_pdf
from quant.importers import tr_confirmation_pdf as tc
from quant.importers.statement_pdf import StatementFormatError

HEADER = """
TRADE REPUBLIC BANK GMBH
Max Mustermann
Beispielstrasse 1
10115 Berlin
Konto DE00 0000 0000 0000 00
"""


def confirmation(
    side: str = "Kauf",
    qty: str = "0,5",
    price: str = "172,38",
    amount: str = "86,19",
    costs: tuple[str, ...] = ("Fremdkostenzuschlag -1,00 EUR",),
    total: str = "-87,19",
    isin: str = "US0378331005",
) -> str:
    return (
        HEADER
        + f"""
WERTPAPIERABRECHNUNG
{side}
Ausführung 03.03.2025 um 10:15 Uhr
POSITION            ANZAHL        DURCHSCHNITTSKURS        BETRAG
Apple Inc. Registered Shares o.N.   {qty} Stk.   {price} EUR   {amount} EUR
ISIN: {isin}
ABRECHNUNG
"""
        + "\n".join(costs)
        + f"\nGESAMT   {total} EUR\n"
    )


def test_a_buy_gives_the_cost_basis_and_no_tax() -> None:
    c = tc.parse_text(confirmation())
    assert (c.side, c.isin, c.name) == ("buy", "US0378331005", "Apple Inc. Registered Shares o.N.")
    assert c.day.isoformat() == "2025-03-03"
    assert (c.quantity, c.price, c.amount, c.fees, c.total) == (
        D("0.5"), D("172.38"), D("86.19"), D("1.00"), D("87.19"),
    )  # fmt: skip
    assert c.cost_basis == D("87.19") and c.tax_total == 0 and c.taxes == {}


def test_a_sell_gives_the_tax_withheld_by_kind() -> None:
    c = tc.parse_text(
        confirmation(
            side="Verkauf",
            qty="10",
            price="20,00",
            amount="200,00",
            costs=(
                "Fremdkostenzuschlag -1,00 EUR",
                "Kapitalertragsteuer -5,00 EUR",
                "Solidaritätszuschlag -0,27 EUR",
                "Kirchensteuer -0,40 EUR",
            ),
            total="193,33",
        )
    )
    assert c.side == "sell" and c.cost_basis is None
    assert c.taxes == {
        "kapitalertragsteuer": D("5.00"),
        "solidaritaetszuschlag": D("0.27"),
        "kirchensteuer": D("0.40"),
    }
    assert c.tax_total == D("5.67") and c.fees == D("1.00") and c.total == D("193.33")


def test_a_savings_plan_is_a_buy_and_thousands_are_read() -> None:
    c = tc.parse_text(
        confirmation(
            side="Sparplanausführung", qty="1.250", price="2,00", amount="2.500,00",
            costs=(), total="-2.500,00",
        )
    )  # fmt: skip
    assert c.side == "buy" and c.quantity == D("1250") and c.amount == D("2500") and c.fees == 0


def test_no_personal_data_reaches_the_result() -> None:
    shown = repr(tc.parse_text(confirmation()))
    for secret in ("Mustermann", "Beispielstrasse", "Berlin", "DE00"):
        assert secret not in shown


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("Kontoauszug\nnothing here", "not a trade confirmation"),
        (confirmation(price="999,99"), "quantity times price"),
        (confirmation(total="-90,00"), "do not add up"),
        (confirmation(total="-85,19"), "do not add up"),  # a buy cannot cost less than the amount
        (confirmation().replace("ISIN: US0378331005", ""), "exactly one ISIN"),
        (
            confirmation().replace("ABRECHNUNG\n", "ISIN: DE0007164600\nABRECHNUNG\n"),
            "exactly one ISIN",
        ),
        (confirmation().replace("Ausführung 03.03.2025", "Irgendwann"), "no execution date"),
        (confirmation().replace("GESAMT", "ERGEBNIS"), "no total"),
        (confirmation().replace(" Stk.", ""), "no position line"),
        (confirmation(side="Orderart"), "buy or a sell"),
        (confirmation(qty="0", price="1,00", amount="0,00", costs=(), total="0,00"), "positive"),
    ],
)
def test_a_confirmation_that_is_wrong_is_refused_whole(text: str, words: str) -> None:
    with pytest.raises(StatementFormatError, match=words):
        tc.parse_text(text)


def test_the_check_command_prints_pass_or_fail_and_never_the_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pdf = tmp_path / "abrechnung.pdf"
    pdf.write_bytes(b"%PDF fake")
    monkeypatch.setattr(statement_pdf, "extract_text", lambda _data: confirmation())
    assert cli.check_confirmation(pdf) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "a buy" in out
    for secret in ("Apple", "US0378331005", "86,19", "86.19", "Mustermann"):
        assert secret not in out
    monkeypatch.setattr(statement_pdf, "extract_text", lambda _data: confirmation(total="-1,00"))
    assert cli.check_confirmation(pdf) == 1
    assert "FAIL" in capsys.readouterr().out

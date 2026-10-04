from collections.abc import Sequence
from pathlib import Path

import pytest

from quant.cli import check_holdings

from .pdfutil import crypto_statement_pdf
from .test_holdings_pnl_api import BTC, ETH, STATEMENT_ROWS, history, row
from .test_tr_crypto_pdf import PII

TOTAL = "10.000"


def write(
    tmp_path: Path,
    extra: list[dict[str, str]] | None = None,
    rows: Sequence[tuple[str, ...]] = STATEMENT_ROWS,
) -> tuple[Path, Path]:
    csv = tmp_path / "tx.csv"
    pdf = tmp_path / "crypto.pdf"
    csv.write_text(history(extra))
    pdf.write_bytes(crypto_statement_pdf(list(rows), total=TOTAL))  # type: ignore[arg-type]
    return csv, pdf


def test_passes_when_history_and_statement_agree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv, pdf = write(tmp_path)
    assert check_holdings(csv, pdf) == 0
    out = capsys.readouterr().out
    assert "PASS crypto quantities match the statement" in out and "match                 2" in out
    assert "PASS cost-basis lots agree with the positions" in out
    assert "PASS every sale is covered by earlier purchases" in out
    assert "PASS crypto cost basis matches the statement" in out
    assert "positions needing a cost from you: 1" in out  # the spin-off
    assert "corporate-action cash that fits no action: 1" in out
    for secret in [*PII, "Bitcoin", "Alpha", "XF000", BTC, ETH, "US00000"]:
        assert secret not in out  # counts only: no names, no ISINs


def test_fails_when_a_quantity_differs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv, pdf = write(
        tmp_path,
        [
            row(
                9,
                "2024-04-01",
                "TRADING",
                "BUY",
                "CRYPTO",
                "Bitcoin",
                BTC,
                shares="0.2",
                price="40000",
                amount="-8000",
                fee="-1",
            )
        ],
    )
    assert check_holdings(csv, pdf) == 1
    assert "quantity_mismatch     1" in capsys.readouterr().out


def test_fails_when_the_cost_basis_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [("0,1", "Bitcoin", "50.000", "3.500", "1.500", "5.000"), STATEMENT_ROWS[1]]
    csv, pdf = write(tmp_path, rows=rows)
    assert check_holdings(csv, pdf) == 1
    out = capsys.readouterr().out
    assert "PASS crypto quantities match the statement" in out
    assert "FAIL crypto cost basis matches the statement" in out


def test_trades_after_the_statement_date_do_not_fail_the_check(tmp_path: Path) -> None:
    later = row(
        9,
        "2026-10-02",
        "TRADING",
        "BUY",
        "CRYPTO",
        "Bitcoin",
        BTC,
        shares="0.5",
        price="40000",
        amount="-20000",
        fee="-1",
    )
    csv, pdf = write(tmp_path, [later])
    assert check_holdings(csv, pdf) == 0


def test_works_without_a_statement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv, _ = write(tmp_path)
    assert check_holdings(csv, None) == 0
    out = capsys.readouterr().out
    assert (
        "open positions rebuilt from 8 transactions: 4" in out
    )  # A, Spin Co, BTC, ETH + the orphan row is not a position
    assert "PASS cost-basis lots agree with the positions" in out

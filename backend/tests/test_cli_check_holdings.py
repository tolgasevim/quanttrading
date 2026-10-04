from pathlib import Path

import pytest

from quant.cli import check_holdings

from .pdfutil import crypto_statement_pdf
from .test_holdings_api import HISTORY, csv_text
from .test_tr_crypto_pdf import PII, ROWS, TOTAL


def write(tmp_path: Path, history: list = HISTORY, rows: list = ROWS) -> tuple[Path, Path]:  # type: ignore[type-arg]
    csv = tmp_path / "tx.csv"
    pdf = tmp_path / "crypto.pdf"
    csv.write_text(csv_text(history))
    pdf.write_bytes(crypto_statement_pdf(rows, total=TOTAL))
    return csv, pdf


def test_passes_when_history_and_statement_agree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv, pdf = write(tmp_path)
    assert check_holdings(csv, pdf) == 0
    out = capsys.readouterr().out
    assert "PASS crypto quantities match the statement" in out and "match                 4" in out
    for secret in [*PII, "Bitcoin", "NVIDIA", "XF000"]:
        assert secret not in out  # counts only: no names, no ISINs


def test_fails_when_a_quantity_differs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    history = list(HISTORY)
    history[4] = (5, "TRADING", "BUY", "CRYPTO", "Bitcoin", "XF000BTC0017", "0.2")
    csv, pdf = write(tmp_path, history)
    assert check_holdings(csv, pdf) == 1
    assert "quantity_mismatch     1" in capsys.readouterr().out


def test_works_without_a_statement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv, _ = write(tmp_path)
    assert check_holdings(csv, None) == 0
    assert "open positions rebuilt from 10 transactions: 5" in capsys.readouterr().out


def test_trades_after_the_statement_date_do_not_fail_the_check(tmp_path: Path) -> None:
    history = [*HISTORY, (11, "TRADING", "BUY", "CRYPTO", "Bitcoin", "XF000BTC0017", "0.5")]
    csv = tmp_path / "tx.csv"
    pdf = tmp_path / "crypto.pdf"
    csv.write_text(csv_text(history, dates={11: "2026-10-02"}))
    pdf.write_bytes(crypto_statement_pdf(ROWS, total=TOTAL))
    assert check_holdings(csv, pdf) == 0

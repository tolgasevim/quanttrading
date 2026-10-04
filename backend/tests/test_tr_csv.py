import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quant.importers import tr_csv
from quant.importers.tr_csv import Kind

from .conftest import FIXTURES

SYNTHETIC = (FIXTURES / "tr_transactions_synthetic.csv").read_text()
# Personal data planted in the synthetic file. None of it may survive parsing.
PII = [
    "MUSTERMANN",
    "DE00123456780000000001",
    "DE00987654320000000002",
    "BAECKEREI",
    "CAFE ISTANBUL",
    "KARLSRUHE",
    "Rent share",
    "Birthday",
    "5462",
    "5812",
]


@pytest.fixture(scope="module")
def result() -> tr_csv.ParseResult:
    return tr_csv.parse(SYNTHETIC)


def by_id(result: tr_csv.ParseResult, n: int) -> tr_csv.ParsedTransaction:
    ext = f"00000000-0000-4000-8000-{n:012d}"
    return next(t for t in result.transactions if t.external_id == ext)


def test_classifies_every_row(result: tr_csv.ParseResult) -> None:
    assert result.summary()["by_kind"] == {
        "card": 2,
        "corporate_action": 1,
        "delivery": 1,
        "income": 1,
        "interest": 1,
        "other": 1,
        "trade": 3,
        "transfer": 2,
    }


def test_personal_data_never_survives_parsing(result: tr_csv.ParseResult) -> None:
    dumped = json.dumps([t.to_json() for t in result.transactions]) + json.dumps(result.summary())
    for secret in PII:
        assert secret not in dumped, secret


def test_card_rows_keep_only_id_time_type_and_eur_amount(result: tr_csv.ParseResult) -> None:
    card = by_id(result, 5)
    assert card.kind == Kind.CARD and card.amount == Decimal("-9.870000")
    assert card.name is None and card.original_amount is None and card.fx_rate is None
    assert card.original_currency is None


def test_deposit_drops_account_holder_name(result: tr_csv.ParseResult) -> None:
    deposit = by_id(result, 1)
    assert deposit.kind == Kind.TRANSFER and deposit.amount == Decimal("5000.000000")
    assert deposit.name is None


def test_trade_fields(result: tr_csv.ParseResult) -> None:
    buy = by_id(result, 2)
    assert buy.kind == Kind.TRADE and buy.isin == "US67066G1040" and buy.name == "NVIDIA Corp."
    assert buy.shares == Decimal("10") and buy.price == Decimal("50") and buy.fee == Decimal("-1")
    assert buy.executed_at == datetime(2024, 1, 3, 10, 15, tzinfo=UTC)
    assert not buy.savings_plan
    assert by_id(result, 3).savings_plan

    dividend = by_id(result, 6)
    assert dividend.original_currency == "USD" and dividend.fx_rate == Decimal("1.0811")
    assert dividend.tax == Decimal("-0.1")


def test_bad_rows_duplicates_and_unknown_types_are_reported(result: tr_csv.ParseResult) -> None:
    assert result.skipped == 2
    joined = " | ".join(result.warnings)
    assert "line 14: shares is not a number" in joined
    assert "line 15: duplicate transaction id" in joined
    assert "unknown type CASH/SOMETHING_NEW (1 rows)" in joined
    assert by_id(result, 11).kind == Kind.OTHER


def test_summary(result: tr_csv.ParseResult) -> None:
    summary = result.summary()
    assert summary["rows"] == 12 and summary["instruments"] == 3
    assert summary["first_date"] == "2024-01-02" and summary["last_date"] == "2024-07-03"
    assert Decimal(str(summary["net_cash_flow"])) == Decimal("6293.99")
    assert summary["savings_plan_executions"] == 1


def test_byte_order_mark_is_ignored() -> None:
    assert len(tr_csv.parse("﻿" + SYNTHETIC).transactions) == 12


def test_other_files_are_rejected() -> None:
    with pytest.raises(tr_csv.ImportFormatError, match="missing columns"):
        tr_csv.parse("Date,Open,High,Low,Close\n2024-01-01,1,2,0.5,1.5\n")


def test_privacy_self_check_catches_leaks(result: tr_csv.ParseResult) -> None:
    assert tr_csv.privacy_leaks(SYNTHETIC, result) == 0
    leaky = tr_csv.parse(SYNTHETIC)
    leaky.transactions[0].name = "MAX MUSTERMANN"  # simulate a parser regression
    leaky.transactions[1].name = "5462"  # merchant code as a whole value
    assert tr_csv.privacy_leaks(SYNTHETIC, leaky) == 2


def test_cli_check_passes_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    from quant.cli import check_tr_csv

    assert check_tr_csv(FIXTURES / "tr_transactions_synthetic.csv") == 1  # bad row + unknown type
    out = capsys.readouterr().out
    assert "PASS no personal data in parsed rows" in out and "FAIL no rows skipped" in out
    for secret in PII:
        assert secret not in out

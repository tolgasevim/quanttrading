from decimal import Decimal

from quant.portfolio.positions import Position
from quant.portfolio.reconcile import (
    StatementLine,
    Status,
    all_but,
    classes,
    normalise_name,
    reconcile,
    summarise,
)

CRYPTO = classes("CRYPTO")


def pos(isin: str, name: str, qty: str, cls: str = "CRYPTO") -> Position:
    return Position(isin, name, cls, Decimal(qty), "2024-01-01", "2025-01-01")


def line(name: str, qty: str, isin: str | None = None) -> StatementLine:
    return StatementLine(name=name, quantity=Decimal(qty), isin=isin)


def statuses(findings: list) -> dict[str, Status]:  # type: ignore[type-arg]
    return {f.name: f.status for f in findings}


def test_normalise_name() -> None:
    assert normalise_name("Ethereum (Ethereum)") == "ethereum"
    assert normalise_name("  XRP   (XRP) ") == "xrp"
    assert normalise_name("Bitcoin") == "bitcoin"


def test_match_by_name_within_rounding() -> None:
    findings = reconcile(
        [pos("X1", "Bitcoin", "0.0931520000"), pos("X2", "XRP", "2030.784567")],
        [line("Bitcoin (Bitcoin)", "0.093152"), line("XRP (XRP)", "2030.784567")],
        CRYPTO,
    )
    assert all(f.status == Status.MATCH for f in findings)
    assert summarise(findings)["match"] == 2


def test_quantity_mismatch_reports_the_difference() -> None:
    [finding] = reconcile(
        [pos("X1", "Cardano", "12423.9")], [line("Cardano", "12423.854702")], CRYPTO
    )
    assert finding.status == Status.QUANTITY_MISMATCH
    assert finding.difference == Decimal("12423.9") - Decimal("12423.854702")


def test_on_statement_but_not_in_history() -> None:
    [finding] = reconcile([], [line("Solana", "3")], CRYPTO)
    assert finding.status == Status.MISSING_IN_HISTORY and finding.history_quantity is None


def test_in_history_but_not_on_statement_is_a_ghost() -> None:
    [finding] = reconcile([pos("X9", "Dogecoin", "100")], [], CRYPTO)
    assert finding.status == Status.NOT_ON_STATEMENT and finding.statement_quantity is None


def test_only_the_statements_asset_classes_are_compared() -> None:
    findings = reconcile(
        [pos("X1", "Bitcoin", "1"), pos("US1", "Apple", "10", cls="STOCK")],
        [line("Bitcoin", "1")],
        CRYPTO,
    )
    assert statuses(findings) == {"Bitcoin": Status.MATCH}  # the stock is not a ghost


def test_isin_takes_priority_over_name() -> None:
    [finding] = reconcile(
        [pos("DE1", "Old Name", "5", cls="STOCK")],
        [line("New Name", "5", isin="DE1")],
        classes("STOCK"),
    )
    assert finding.status == Status.MATCH


def test_ambiguous_names_are_not_guessed() -> None:
    findings = reconcile(
        [pos("A1", "Same", "1"), pos("A2", "Same", "1")], [line("Same", "1")], CRYPTO
    )
    assert summarise(findings) == {
        "match": 0,
        "quantity_mismatch": 0,
        "missing_in_history": 1,
        "not_on_statement": 2,
    }


def test_a_depot_statement_covers_everything_but_crypto() -> None:
    depot = all_but("CRYPTO")
    findings = reconcile(
        [
            pos("US1", "Apple", "10", cls="STOCK"),
            pos("DE9", "Mystery", "1", cls=None),  # type: ignore[arg-type]
            pos("X1", "Bitcoin", "1"),
        ],
        [line("Apple Inc.", "10", isin="US1")],
        depot,
    )
    assert {f.name: f.status for f in findings} == {
        "Apple": Status.MATCH,
        "Mystery": Status.NOT_ON_STATEMENT,  # a position of unknown class is checked too
    }  # the coin is for the crypto statement


def test_two_lines_with_the_same_isin_are_one_position() -> None:
    from quant.portfolio.reconcile import merge_lines

    merged = merge_lines(
        [
            StatementLine("Apple", Decimal("4"), "US1", Decimal("400")),
            StatementLine("Apple", Decimal("6"), "US1", Decimal("600")),
            StatementLine("Other", Decimal("1"), None, Decimal("1")),
        ]
    )
    assert [(m.name, m.quantity, m.value_eur) for m in merged] == [
        ("Apple", Decimal("10"), Decimal("1000")),
        ("Other", Decimal("1"), Decimal("1")),
    ]

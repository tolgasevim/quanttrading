from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from quant.portfolio.positions import compute_positions, open_positions

T0 = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


@dataclass
class Tx:
    isin: str | None
    category: str
    type: str
    shares: Decimal | None
    executed_at: datetime
    name: str | None = None
    asset_class: str | None = "STOCK"

    @property
    def date(self) -> date:
        return self.executed_at.date()


class Ledger:
    """Builds transactions with increasing timestamps."""

    def __init__(self) -> None:
        self.rows: list[Tx] = []

    def add(
        self, isin: str | None, category: str, type_: str, shares: str | None, **kw: object
    ) -> "Ledger":
        when = T0 + timedelta(days=len(self.rows))
        qty = Decimal(shares) if shares is not None else None
        self.rows.append(Tx(isin, category, type_, qty, when, **kw))  # type: ignore[arg-type]
        return self

    def buy(self, isin: str, n: str, **kw: object) -> "Ledger":
        return self.add(isin, "TRADING", "BUY", n, **kw)

    def sell(self, isin: str, n: str, **kw: object) -> "Ledger":
        return self.add(isin, "TRADING", "SELL", f"-{n}", **kw)

    def positions(self) -> dict[str, Decimal]:
        return {p.isin: p.quantity for p in open_positions(compute_positions(self.rows))}


def test_buys_and_sells() -> None:
    assert Ledger().buy("A", "10").buy("A", "2.5").sell("A", "4").positions() == {
        "A": Decimal("8.5")
    }


def test_fully_sold_position_is_closed() -> None:
    assert Ledger().buy("A", "10").sell("A", "10").positions() == {}


def test_dividend_rows_carry_record_date_shares_and_must_not_count() -> None:
    """Regression: summing every row with shares turned 33 closed or small positions into
    phantom holdings on the owner's real history."""
    ledger = (
        Ledger()
        .buy("A", "10")
        .add("A", "CASH", "DIVIDEND", "10")
        .add("A", "CASH", "DIVIDEND", "10")
        .add("F", "CASH", "DISTRIBUTION", "250", asset_class="FUND")
    )
    assert ledger.positions() == {"A": Decimal("10")}


def test_split_adds_the_extra_units() -> None:
    assert Ledger().buy("A", "10").add("A", "CORPORATE_ACTION", "SPLIT", "90").positions() == {
        "A": Decimal("100")
    }


def test_reverse_split_removes_and_adds_in_the_same_isin() -> None:
    ledger = Ledger().buy("A", "100").add("A", "CORPORATE_ACTION", "REVERSE_SPLIT", "-90")
    assert ledger.positions() == {"A": Decimal("10")}


def test_merger_and_exchange_move_the_position_to_the_new_isin() -> None:
    ledger = (
        Ledger()
        .buy("OLD", "40")
        .add("OLD", "CORPORATE_ACTION", "MERGER", "-40")
        .add("NEW", "CORPORATE_ACTION", "MERGER", "12.5", name="Merged Corp")
    )
    assert ledger.positions() == {"NEW": Decimal("12.5")}
    positions = compute_positions(ledger.rows)
    assert positions["NEW"].corporate_action_touched and positions["NEW"].name == "Merged Corp"
    assert not positions["OLD"].is_open


def test_spin_off_creates_a_new_position_and_keeps_the_parent() -> None:
    ledger = Ledger().buy("PARENT", "30").add("CHILD", "CORPORATE_ACTION", "SPIN_OFF", "6")
    assert ledger.positions() == {"PARENT": Decimal("30"), "CHILD": Decimal("6")}


def test_liquidation_and_worthless_close_positions() -> None:
    ledger = (
        Ledger()
        .buy("F", "2920", asset_class="FUND")
        .add("F", "CORPORATE_ACTION", "LIQUIDATION", "-2920", asset_class="FUND")
        .buy("S", "20")
        .add("S", "CORPORATE_ACTION", "WORTHLESS", "-20")
    )
    assert ledger.positions() == {}


def test_knock_out_certificate_exercised_is_closed() -> None:
    ledger = (
        Ledger()
        .buy("KO", "700", asset_class="DERIVATIVE")
        .add("KO", "CORPORATE_ACTION", "WARRANT_EXERCISE", "-700", asset_class="DERIVATIVE")
    )
    assert ledger.positions() == {}


def test_free_receipts_add_to_the_position() -> None:
    ledger = (
        Ledger()
        .buy("BTC", "0.01", asset_class="CRYPTO")
        .add("BTC", "DELIVERY", "FREE_RECEIPT", "0.000123", asset_class="CRYPTO")
    )
    assert ledger.positions() == {"BTC": Decimal("0.010123")}


def test_rows_are_applied_in_time_order_not_input_order() -> None:
    ledger = Ledger().buy("A", "10").sell("A", "10").buy("A", "3")
    ledger.rows.reverse()
    assert Ledger.positions(ledger) == {"A": Decimal("3")}


def test_rounding_dust_counts_as_closed() -> None:
    ledger = Ledger().buy("A", "0.1").buy("A", "0.2").sell("A", "0.3000000000000001")
    assert ledger.positions() == {}


def test_rows_without_isin_or_shares_are_ignored() -> None:
    ledger = (
        Ledger()
        .buy("A", "5")
        .add(None, "TRADING", "BUY", "9")
        .add("A", "CASH", "EXCHANGE", None)
        .add("A", "CASH", "INTEREST_PAYMENT", None)
    )
    assert ledger.positions() == {"A": Decimal("5")}


def test_latest_name_wins() -> None:
    ledger = Ledger().buy("A", "1", name="Old Name Inc").buy("A", "1", name="New Name plc")
    assert compute_positions(ledger.rows)["A"].name == "New Name plc"


def test_plain_positions_are_not_flagged_as_corporate_action() -> None:
    positions = compute_positions(Ledger().buy("A", "1").add("A", "CASH", "DIVIDEND", "1").rows)
    assert not positions["A"].corporate_action_touched

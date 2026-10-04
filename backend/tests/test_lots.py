from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from quant.portfolio.lots import (
    FLAG_CARRIED,
    FLAG_COST_UNKNOWN,
    FLAG_INCOMPLETE_HISTORY,
    FLAG_PRICE_DERIVED,
    LotBook,
    build_lots,
    position_costs,
    realised_by_isin,
    realised_by_year,
)
from quant.portfolio.positions import compute_positions

T0 = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


@dataclass
class Tx:
    isin: str | None
    category: str
    kind: str
    type: str
    shares: D | None
    executed_at: datetime
    price: D | None = None
    amount: D | None = None
    fee: D | None = None
    tax: D | None = None
    name: str | None = None
    asset_class: str | None = "STOCK"

    @property
    def date(self) -> date:
        return self.executed_at.date()


class Ledger:
    """Transactions with increasing timestamps (one day apart unless `at` is given)."""

    def __init__(self) -> None:
        self.rows: list[Tx] = []
        self.clock = T0

    def _when(self, at: datetime | None) -> datetime:
        if at is not None:
            return at
        self.clock += timedelta(days=1)
        return self.clock

    def buy(
        self,
        isin: str,
        n: str,
        paid: str,
        fee: str = "1",
        tax: str | None = None,
        at: datetime | None = None,
    ) -> "Ledger":
        self.rows.append(
            Tx(
                isin,
                "TRADING",
                "trade",
                "BUY",
                D(n),
                self._when(at),
                amount=-D(paid),
                fee=-D(fee),
                tax=-D(tax) if tax else None,
            )
        )
        return self

    def sell(
        self,
        isin: str,
        n: str,
        got: str,
        fee: str = "1",
        tax: str | None = None,
        at: datetime | None = None,
    ) -> "Ledger":
        self.rows.append(
            Tx(
                isin,
                "TRADING",
                "trade",
                "SELL",
                -D(n),
                self._when(at),
                amount=D(got),
                fee=-D(fee),
                tax=-D(tax) if tax else None,
            )
        )
        return self

    def receive(self, isin: str, n: str, price: str | None) -> "Ledger":
        self.rows.append(
            Tx(
                isin,
                "DELIVERY",
                "delivery",
                "FREE_RECEIPT",
                D(n),
                self._when(None),
                price=D(price) if price else None,
            )
        )
        return self

    def action(self, type_: str, isin: str, shares: str, at: datetime | None = None) -> "Ledger":
        self.rows.append(
            Tx(isin, "CORPORATE_ACTION", "corporate_action", type_, D(shares), self._when(at))
        )
        return self

    def cash(self, type_: str, isin: str, amount: str, at: datetime | None = None) -> "Ledger":
        self.rows.append(
            Tx(isin, "CASH", "corporate_action", type_, None, self._when(at), amount=D(amount))
        )
        return self

    def dividend(self, isin: str, shares: str) -> "Ledger":
        self.rows.append(
            Tx(isin, "CASH", "income", "DIVIDEND", D(shares), self._when(None), amount=D("1"))
        )
        return self

    def book(self) -> LotBook:
        return build_lots(self.rows)


def at(day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(2024, 3, day, hour, minute, tzinfo=UTC)


def test_buy_keeps_purchase_value_and_costs_apart() -> None:
    book = Ledger().buy("A", "10", "500", fee="1", tax="1.5").book()
    cost = position_costs(book)["A"]
    assert (cost.quantity, cost.cost, cost.costs) == (D(10), D(500), D("2.5"))
    assert cost.total_cost == D("502.5") and cost.average_cost == D("50.25")


def test_sell_consumes_oldest_lots_first_and_splits_a_lot_pro_rata() -> None:
    book = (
        Ledger()
        .buy("A", "10", "100", fee="0")
        .buy("A", "10", "200", fee="0")
        .sell("A", "15", "450", fee="1")
        .book()
    )
    [sale] = book.disposals
    assert [(s.quantity, s.cost) for s in sale.slices] == [(D(10), D(100)), (D(5), D(100))]
    assert sale.proceeds == D(450) and sale.fees == D(1)
    assert sale.realised_pnl == D(450) - D(1) - D(200)
    left = position_costs(book)["A"]
    assert (left.quantity, left.cost) == (D(5), D(100))


def test_withheld_tax_is_reported_but_not_part_of_the_gain() -> None:
    book = (
        Ledger().buy("A", "10", "100", fee="0").sell("A", "10", "200", fee="1", tax="26.38").book()
    )
    [sale] = book.disposals
    assert sale.tax_withheld == D("26.38") and sale.realised_pnl == D(99)


def test_a_loss_is_negative() -> None:
    [sale] = (
        Ledger().buy("A", "10", "200", fee="0").sell("A", "10", "150", fee="0").book().disposals
    )
    assert sale.realised_pnl == D(-50)


def test_selling_more_than_the_history_shows_is_flagged_not_hidden() -> None:
    [sale] = Ledger().buy("A", "5", "50", fee="0").sell("A", "8", "100", fee="0").book().disposals
    assert FLAG_INCOMPLETE_HISTORY in sale.flags
    assert sale.slices[-1].quantity == D(3) and sale.slices[-1].cost == 0


def test_dividend_rows_with_shares_do_not_create_lots() -> None:
    book = Ledger().buy("A", "10", "100").dividend("A", "10").dividend("A", "10").book()
    assert book.open_quantity("A") == D(10)


def test_free_receipt_is_valued_at_the_price_on_receipt() -> None:
    book = Ledger().receive("BTC", "0.5", "40000").receive("PERK", "3", None).book()
    btc = position_costs(book)["BTC"]
    assert btc.cost == D(20000) and FLAG_PRICE_DERIVED in btc.flags
    assert FLAG_COST_UNKNOWN in position_costs(book)["PERK"].flags


def test_a_buy_without_an_amount_uses_units_times_price() -> None:
    row = Tx("ELTIF", "TRADING", "trade", "BUY", D(5), T0, price=D(100), asset_class="PRIVATE_FUND")
    cost = position_costs(build_lots([row]))["ELTIF"]
    assert cost.cost == D(500) and FLAG_PRICE_DERIVED in cost.flags


def test_split_spreads_the_cost_over_more_units() -> None:
    ledger = Ledger().buy("A", "10", "1000", fee="0").action("SPLIT", "A", "90")
    cost = position_costs(ledger.book())["A"]
    assert (cost.quantity, cost.cost, cost.average_cost) == (D(100), D(1000), D(10))
    [sale] = ledger.sell("A", "50", "700", fee="0").book().disposals
    assert sale.cost == D(500) and sale.realised_pnl == D(200)


def test_split_scales_every_lot_and_keeps_acquisition_dates() -> None:
    book = (
        Ledger()
        .buy("A", "2", "100", fee="0")
        .buy("A", "3", "300", fee="0")
        .action("SPLIT", "A", "15")
        .book()
    )
    assert [(lot.quantity, lot.cost) for lot in book.lots["A"]] == [(D(8), D(100)), (D(12), D(300))]
    assert book.lots["A"][0].acquired < book.lots["A"][1].acquired


def test_reverse_split_on_the_same_isin_rescales() -> None:
    cost = position_costs(
        Ledger().buy("A", "100", "500", fee="0").action("REVERSE_SPLIT", "A", "-90").book()
    )["A"]
    assert (cost.quantity, cost.cost) == (D(10), D(500))


def test_merger_carries_cost_dates_and_flags_to_the_new_isin() -> None:
    ledger = (
        Ledger()
        .buy("OLD", "10", "100", fee="1")
        .buy("OLD", "30", "600", fee="1")
        .action("MERGER", "OLD", "-40", at=at(10, 22, 4))
        .action("MERGER", "NEW", "8", at=at(10, 22, 4))
    )
    book = ledger.book()
    assert "OLD" not in position_costs(book)
    new = book.lots["NEW"]
    assert [(lot.quantity, lot.cost, lot.costs) for lot in new] == [
        (D(2), D(100), D(1)),
        (D(6), D(600), D(1)),
    ]
    assert all(FLAG_CARRIED in lot.flags for lot in new)
    assert new[0].acquired < new[1].acquired  # holding period survives the merger
    assert not book.disposals


def test_unrelated_swaps_booked_together_are_not_mixed() -> None:
    ledger = (
        Ledger()
        .buy("OLD1", "10", "100", fee="0", at=at(1))
        .buy("OLD2", "10", "900", fee="0", at=at(2))
        .action("MERGER", "OLD1", "-10", at=at(10, 22, 4))
        .action("MERGER", "OLD2", "-10", at=at(10, 22, 4))
        .action("MERGER", "NEW1", "10", at=at(10, 22, 4))
        .action("MERGER", "NEW2", "10", at=at(10, 22, 4))
    )
    book = ledger.book()
    new1, new2 = (book.lots[i] for i in ("NEW1", "NEW2"))
    assert [(lot.quantity, lot.cost, lot.acquired.day) for lot in new1] == [(D(10), D(100), 1)]
    assert [(lot.quantity, lot.cost, lot.acquired.day) for lot in new2] == [(D(10), D(900), 2)]


def test_a_swap_into_an_isin_already_held_keeps_fifo_order_by_date() -> None:
    ledger = (
        Ledger()
        .buy("NEW", "11", "110", fee="0", at=at(20))
        .buy("OLD", "20", "400", fee="0", at=at(1))
        .action("EXCHANGE", "OLD", "-20", at=at(25, 9, 53))
        .action("EXCHANGE", "NEW", "20", at=at(25, 9, 53))
    )
    lots = ledger.book().lots["NEW"]
    assert [lot.acquired.day for lot in lots] == [1, 20]  # the carried (older) lot sells first
    assert sum(lot.quantity for lot in lots) == D(31)


def test_a_swap_with_two_targets_splits_the_cost_by_units() -> None:
    ledger = (
        Ledger()
        .buy("OLD", "10", "1000", fee="0")
        .action("MERGER", "OLD", "-10", at=at(5, 12, 0))
        .action("MERGER", "A", "3", at=at(5, 12, 0))
        .action("MERGER", "B", "1", at=at(5, 12, 0))
    )
    book = ledger.book()
    assert position_costs(book)["A"].cost == D(750) and position_costs(book)["B"].cost == D(250)


def test_rights_issue_cost_is_the_cash_paid_for_the_new_units() -> None:
    ledger = (
        Ledger()
        .action("CAPITAL_INCR_CASH", "RIGHTS", "100", at=at(1, 8))
        .action("RIGHTS", "RIGHTS", "-100", at=at(10, 11, 2))
        .action("RIGHTS", "SHARE", "20", at=at(10, 11, 2))
        .cash("EXCHANGE", "SHARE", "-153.4", at=at(10, 11, 31))
    )
    book = ledger.book()
    share = position_costs(book)["SHARE"]
    assert (share.quantity, share.cost) == (D(20), D("153.4"))
    assert FLAG_COST_UNKNOWN not in share.flags  # the price paid is the cost
    assert not book.unattributed


def test_spin_off_creates_a_lot_whose_cost_the_broker_does_not_give() -> None:
    ledger = Ledger().buy("PARENT", "30", "900").action("SPIN_OFF", "CHILD", "6")
    book = ledger.book()
    assert FLAG_COST_UNKNOWN in position_costs(book)["CHILD"].flags
    assert position_costs(book)["PARENT"].cost == D(900)  # the parent is left as it was


def test_liquidation_is_a_disposal_with_the_cash_row_as_proceeds() -> None:
    ledger = (
        Ledger()
        .buy("F", "2310", "7000", fee="0")
        .action("LIQUIDATION", "F", "-2310", at=at(30, 22, 25))
        .cash("EXCHANGE", "F", "7337.75", at=at(31, 0, 8))
    )
    [d] = ledger.book().disposals
    assert d.kind == "liquidation" and d.proceeds == D("7337.75") and d.realised_pnl == D("337.75")
    assert not ledger.book().unattributed


@pytest.mark.parametrize(
    ("type_", "kind"),
    [
        ("WARRANT_EXERCISE", "expiry"),
        ("WORTHLESS", "write_off"),
        ("LIQUIDATION_DIVIDEND", "liquidation"),
    ],
)
def test_removals_by_type(type_: str, kind: str) -> None:
    ledger = (
        Ledger()
        .buy("X", "700", "108", fee="1")
        .action(type_, "X", "-700", at=at(12, 8, 37))
        .cash("TILG", "X", "0.7", at=at(12, 8, 37))
    )
    [d] = ledger.book().disposals
    assert d.kind == kind and d.proceeds == D("0.7") and d.realised_pnl == D("0.7") - D(109)


def test_a_removal_without_cash_is_a_total_loss() -> None:
    [d] = Ledger().buy("X", "40", "50", fee="0").action("WORTHLESS", "X", "-40").book().disposals
    assert d.proceeds == 0 and d.realised_pnl == D(-50)


def test_cash_that_fits_no_action_is_listed_not_dropped() -> None:
    ledger = Ledger().buy("G", "17", "100", fee="0").cash("EXCHANGE", "G", "5.8", at=at(15))
    [item] = ledger.book().unattributed
    assert (item.isin, item.amount, item.type) == ("G", D("5.8"), "EXCHANGE")


def test_separate_actions_of_the_same_type_are_not_merged() -> None:
    ledger = (
        Ledger()
        .buy("A", "10", "100", fee="0")
        .buy("B", "10", "100", fee="0")
        .action("SPLIT", "A", "10", at=at(3, 10, 0))
        .action("SPLIT", "B", "10", at=at(20, 10, 0))
    )
    book = ledger.book()
    assert book.open_quantity("A") == D(20) and book.open_quantity("B") == D(20)


def test_quantities_always_agree_with_the_position_engine() -> None:
    ledger = (
        Ledger()
        .buy("A", "10", "100")
        .dividend("A", "10")
        .action("SPLIT", "A", "90")
        .buy("B", "5", "50")
        .sell("B", "2", "30")
        .receive("C", "0.25", "100")
        .buy("OLD", "40", "400")
        .action("MERGER", "OLD", "-40", at=at(10, 22, 4))
        .action("MERGER", "NEW", "8", at=at(10, 22, 4))
        .buy("F", "100", "900")
        .action("LIQUIDATION", "F", "-100", at=at(20, 9, 0))
        .cash("EXCHANGE", "F", "950", at=at(20, 9, 5))
        .action("SPIN_OFF", "CHILD", "3")
    )
    mine = ledger.book().open_quantities()
    theirs = {i: p.quantity for i, p in compute_positions(ledger.rows).items() if p.is_open}
    assert mine.keys() == theirs.keys()
    for isin, qty in theirs.items():
        assert abs(mine[isin] - qty) < D("1e-9"), isin


def test_all_cost_is_either_still_open_or_has_been_consumed() -> None:
    ledger = (
        Ledger()
        .buy("A", "10", "100", fee="1")
        .buy("A", "10", "300", fee="1")
        .sell("A", "13", "500")
    )
    book = ledger.book()
    open_total = position_costs(book)["A"].total_cost
    consumed = sum(d.cost + d.acquisition_costs for d in book.disposals)
    assert open_total + consumed == D(100) + D(300) + D(2)


def test_realised_totals_by_year_and_instrument() -> None:
    ledger = Ledger().buy("A", "10", "100", fee="0").sell("A", "10", "150", fee="2", tax="13")
    ledger.clock = datetime(2025, 5, 1, tzinfo=UTC)
    ledger.buy("B", "1", "100", fee="0").sell("B", "1", "70", fee="1")
    book = ledger.book()
    years = {y.year: y for y in realised_by_year(book.disposals)}
    assert (years[2024].gains, years[2024].losses, years[2024].fees, years[2024].tax_withheld) == (
        D(48),
        D(0),
        D(2),
        D(13),
    )
    assert (years[2025].gains, years[2025].losses, years[2025].net) == (D(0), D(-31), D(-31))
    assert realised_by_isin(book.disposals) == {"A": D(48), "B": D(-31)}


def test_units_transferred_out_are_not_a_loss() -> None:
    ledger = Ledger().receive("COIN", "10", "100")
    ledger.rows.append(
        Tx("COIN", "DELIVERY", "delivery", "TRANSFER_OUT", D("-4"), ledger._when(None))
    )
    book = ledger.book()
    assert not book.disposals  # no sale, so no realised loss
    assert position_costs(book)["COIN"].quantity == D(6)
    assert position_costs(book)["COIN"].cost == D(600)


def test_a_trade_row_without_shares_is_ignored() -> None:
    ledger = Ledger().buy("A", "10", "100")
    ledger.rows.append(Tx("A", "TRADING", "trade", "SELL", None, ledger._when(None)))
    ledger.rows.append(Tx("A", "TRADING", "trade", "SELL", D("0"), ledger._when(None)))
    book = ledger.book()
    assert not book.disposals and position_costs(book)["A"].quantity == D(10)


def test_a_positive_tax_amount_is_not_counted_as_withheld() -> None:
    ledger = Ledger().buy("A", "10", "100").sell("A", "5", "80")
    ledger.rows[-1].tax = D("3")  # a refund
    assert ledger.book().disposals[0].tax_withheld == D(0)


def test_selling_units_with_unknown_cost_is_flagged() -> None:
    ledger = Ledger().buy("PARENT", "30", "900").action("SPIN_OFF", "CHILD", "6")
    ledger.sell("CHILD", "6", "60")
    book = ledger.book()
    [d] = book.disposals
    assert d.cost_unknown and d.realised_pnl == D(59)  # the proceeds less the fee: overstated
    [year] = realised_by_year(book.disposals)
    assert year.cost_unknown_disposals == 1


def test_selling_units_with_a_known_cost_is_not_flagged() -> None:
    book = Ledger().buy("A", "10", "100").sell("A", "5", "80").book()
    assert not book.disposals[0].cost_unknown
    assert realised_by_year(book.disposals)[0].cost_unknown_disposals == 0

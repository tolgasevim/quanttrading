from datetime import UTC, date, datetime
from decimal import Decimal as D

from quant.portfolio.tax import Income, Refund, YearEstimate, estimate

from .test_lots import Ledger


def when(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 9, 0, tzinfo=UTC)


def gain(ledger: Ledger, isin: str, year: int, pnl: str) -> Ledger:
    """A buy and a sell in `year` whose realised gain is exactly `pnl` (no fees)."""
    cost = D(1000)
    proceeds = cost + D(pnl)
    ledger.buy(isin, "1", str(cost), fee="0", at=when(year, 2, 1))
    ledger.sell(isin, "1", str(proceeds), fee="0", at=when(year, 3, 1))
    return ledger


def run(
    ledger: Ledger,
    classes: dict[str, str],
    income: list[Income] | None = None,
    refunds: list[Refund] | None = None,
) -> dict[int, YearEstimate]:
    result = estimate(ledger.book().disposals, classes, income or [], refunds or [])
    return {y.year: y for y in result}


def test_a_share_gain_pays_26_375_percent_above_the_allowance() -> None:
    y = run(gain(Ledger(), "S", 2025, "2000"), {"S": "STOCK"})[2025]
    assert (y.taxable_before_allowance, y.allowance_used, y.taxable) == (D(2000), D(1000), D(1000))
    assert y.tax == D("263.75")  # 1000 x 25% x 1.055


def test_a_gain_below_the_allowance_pays_nothing() -> None:
    y = run(gain(Ledger(), "S", 2025, "400"), {"S": "STOCK"})[2025]
    assert (y.allowance_used, y.taxable, y.tax) == (D(400), D(0), D(0))


def test_share_losses_carry_forward_and_offset_later_share_gains() -> None:
    ledger = gain(gain(Ledger(), "A", 2024, "-500"), "B", 2025, "300")
    years = run(ledger, {"A": "STOCK", "B": "STOCK"})
    assert years[2024].stock_loss_carried == D(500)
    assert (years[2025].stock_loss_brought, years[2025].stock_loss_carried) == (D(500), D(200))
    assert years[2025].tax == D(0)


def test_share_losses_do_not_offset_fund_gains() -> None:
    ledger = gain(gain(Ledger(), "A", 2025, "-1000"), "F", 2025, "3000")
    y = run(ledger, {"A": "STOCK", "F": "FUND"})[2025]
    assert y.stock_loss_carried == D(1000)  # stays in the share pot
    assert y.fund_pnl == D(2100)  # 3000 less the 30% Teilfreistellung
    assert (y.taxable, y.tax) == (D(1100), D("290.13"))  # 290.125 rounds up


def test_general_losses_offset_share_gains() -> None:
    ledger = gain(gain(Ledger(), "D", 2025, "-400"), "S", 2025, "1400")
    y = run(ledger, {"D": "DERIVATIVE", "S": "STOCK"})[2025]
    assert (y.taxable_before_allowance, y.tax, y.general_loss_carried) == (D(1000), D(0), D(0))


def test_a_general_loss_beyond_the_gains_carries_forward() -> None:
    ledger = gain(gain(Ledger(), "D", 2024, "-900"), "S", 2024, "300")
    years = run(ledger, {"D": "DERIVATIVE", "S": "STOCK"})
    assert years[2024].general_loss_carried == D(600)


def test_dividends_count_and_fund_distributions_get_the_teilfreistellung() -> None:
    income = [
        Income(date(2025, 5, 1), "STOCK", D("500"), D("0")),
        Income(date(2025, 6, 1), "FUND", D("1000"), D("0")),
        Income(date(2025, 7, 1), None, D("100"), D("0")),  # interest
    ]
    ledger = gain(Ledger(), "S", 2025, "0")
    y = run(ledger, {"S": "STOCK"}, income)[2025]
    assert y.income == D(1300)  # 500 + 700 + 100
    assert (y.taxable, y.tax) == (D(300), D("79.13"))  # 79.125 rounds up


def test_what_the_broker_withheld_is_netted_against_its_refunds() -> None:
    ledger = gain(Ledger(), "S", 2025, "2000")
    ledger.rows[-1].tax = D("-50")  # the broker withheld 50 on the sale
    refunds = [Refund(date(2025, 12, 20), D("20"))]
    y = run(ledger, {"S": "STOCK"}, refunds=refunds)[2025]
    assert y.withheld == D("30.00") and y.to_settle == D("233.75")  # 263.75 - 30


def test_crypto_held_over_a_year_is_tax_free_and_the_rest_counts() -> None:
    ledger = Ledger()
    ledger.buy("C", "1", "1000", fee="0", at=when(2024, 1, 10))
    ledger.buy("C", "1", "1000", fee="0", at=when(2024, 3, 1))
    # Sell both units on 2025-02-01: the first is 1 year and 3 weeks old, the second 11 months.
    ledger.sell("C", "2", "6000", fee="0", at=when(2025, 2, 1))
    crypto = run(ledger, {"C": "CRYPTO"})[2025].crypto
    assert (crypto.tax_free_gain, crypto.taxable_gain) == (D(2000), D(2000))
    assert crypto.under_freigrenze is False


def test_a_crypto_gain_under_the_freigrenze_is_not_taxed_at_all() -> None:
    ledger = Ledger()
    ledger.buy("C", "1", "1000", fee="0", at=when(2025, 1, 10))
    ledger.sell("C", "1", "1500", fee="0", at=when(2025, 6, 1))
    crypto = run(ledger, {"C": "CRYPTO"})[2025].crypto
    assert (crypto.taxable_gain, crypto.freigrenze, crypto.under_freigrenze) == (
        D(500),
        D(1000),
        True,
    )
    assert crypto.taxable_after_limit == D(0)


def test_the_freigrenze_was_600_until_2023() -> None:
    ledger = Ledger()
    ledger.buy("C", "1", "1000", fee="0", at=when(2023, 1, 10))
    ledger.sell("C", "1", "1700", fee="0", at=when(2023, 6, 1))
    crypto = run(ledger, {"C": "CRYPTO"})[2023].crypto
    assert (crypto.freigrenze, crypto.under_freigrenze) == (D(600), False)


def test_crypto_does_not_use_the_capital_gains_tax_or_allowance() -> None:
    ledger = Ledger()
    ledger.buy("C", "1", "1000", fee="0", at=when(2025, 1, 10))
    ledger.sell("C", "1", "9000", fee="0", at=when(2025, 6, 1))
    y = run(ledger, {"C": "CRYPTO"})[2025]
    assert (y.taxable_before_allowance, y.tax) == (D(0), D(0))
    assert y.crypto.taxable_gain == D(8000)


def test_years_come_out_oldest_first() -> None:
    ledger = gain(gain(Ledger(), "A", 2025, "10"), "B", 2023, "10")
    assert [y.year for y in estimate(ledger.book().disposals, {"A": "STOCK", "B": "STOCK"})] == [
        2023,
        2025,
    ]

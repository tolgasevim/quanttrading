"""German tax estimate (FR-26, FR-29), a pure function of the lot engine's disposals and the
income rows. An estimate for planning, never tax advice.

Rules modelled (single filer, no church tax: D34, D40):
- Capital gains tax 25% plus 5.5% solidarity surcharge on it: 26.375% in total.
- Two loss pots. Losses on **shares** offset only gains on shares. Losses from everything else
  (the general pot) offset any gain. Unused losses carry forward to the next year.
- Teilfreistellung: 30% of the gains, losses and distributions of an equity fund are exempt.
  The broker data does not say which funds are equity funds, so every fund is assumed to be one.
- The Sparerpauschbetrag (EUR 1,000) is applied to what is left.
- Crypto is not capital gains: a sale is a private disposal (section 23 EStG). Units held for
  more than a year are tax-free. The rest is taxed at the personal rate (not known here) and
  only if the year's total reaches the Freigrenze (EUR 600 until 2023, EUR 1,000 from 2024).

Not modelled: Vorabpauschale (needs fund prices), foreign withholding tax credit, the pre-2025
rules for losses on derivatives (a EUR 20,000 cap and own pot), and funds that are not equity funds.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from quant.portfolio.lots import Disposal

ZERO = Decimal(0)
CENT = Decimal("0.01")

TAX_RATE = Decimal("0.25")
SOLI_RATE = Decimal("0.055")
SPARERPAUSCHBETRAG = Decimal(1000)  # single filer, all of it at the broker (D34)
TEILFREISTELLUNG = Decimal("0.30")  # equity funds
CRYPTO_FREIGRENZE_UNTIL_2023 = Decimal(600)
CRYPTO_FREIGRENZE_FROM_2024 = Decimal(1000)

STOCK = "STOCK"
FUND = "FUND"
CRYPTO = "CRYPTO"

ASSUMPTIONS = [
    "Single filer, no church tax, EUR 1,000 Sparerpauschbetrag, all of it at the broker.",
    "Every fund is treated as an equity fund (30% Teilfreistellung). Gold ETCs, bond funds and "
    "mixed funds would differ.",
    "Vorabpauschale on accumulating funds is not included.",
    "Derivative losses before 2025 are not limited to derivative gains (the old EUR 20,000 rules).",
    "Crypto gains over the Freigrenze are taxed at your personal income-tax rate, which is not "
    "known here, so no crypto tax amount is estimated.",
    "An estimate for planning. Check with a tax advisor.",
]


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Income:
    """A dividend, distribution or interest payment. `amount` is gross; `withheld` positive."""

    date: date
    asset_class: str | None
    amount: Decimal
    withheld: Decimal


@dataclass(frozen=True)
class Refund:
    """Tax the broker paid back during the year (a loss-pot or allowance adjustment)."""

    date: date
    amount: Decimal  # positive: paid back; negative: extra tax charged


def crypto_freigrenze(year: int) -> Decimal:
    return CRYPTO_FREIGRENZE_UNTIL_2023 if year <= 2023 else CRYPTO_FREIGRENZE_FROM_2024


def _held_over_a_year(acquired: date, disposed: date) -> bool:
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # 29 February
        anniversary = acquired.replace(year=acquired.year + 1, day=28)
    return disposed > anniversary


@dataclass(frozen=True)
class CryptoYear:
    taxable_gain: Decimal  # sales of units held a year or less, after fees; losses included
    tax_free_gain: Decimal  # units held over a year
    freigrenze: Decimal
    under_freigrenze: bool  # the taxable gain is below the limit, so nothing is due

    @property
    def taxable_after_limit(self) -> Decimal:
        if self.under_freigrenze:
            return ZERO
        return max(self.taxable_gain, ZERO)


@dataclass(frozen=True)
class YearEstimate:
    year: int
    stock_pnl: Decimal  # shares, before loss carry-forward
    fund_pnl: Decimal  # funds, after Teilfreistellung
    other_pnl: Decimal  # bonds, derivatives, private funds
    income: Decimal  # dividends, distributions and interest (funds after Teilfreistellung)
    stock_loss_brought: Decimal  # losses carried in from earlier years
    general_loss_brought: Decimal
    stock_loss_carried: Decimal  # carried out to next year
    general_loss_carried: Decimal
    taxable_before_allowance: Decimal
    allowance_used: Decimal
    taxable: Decimal
    tax: Decimal  # 25% plus soli
    withheld: Decimal  # net of refunds, as the broker took it
    to_settle: Decimal  # tax less withheld: positive is still owed, negative is refundable
    crypto: CryptoYear
    fund_disposals: int  # sales that used the equity-fund assumption
    cost_unknown_sales: int  # sales of units received with no cost: the gain is overstated
    history_gap_sales: int  # sales of units the history never bought: an import is missing


@dataclass
class _Year:
    stock: Decimal = ZERO
    fund: Decimal = ZERO
    other: Decimal = ZERO
    income: Decimal = ZERO
    withheld: Decimal = ZERO
    refunds: Decimal = ZERO
    funds: int = 0
    crypto_taxable: Decimal = ZERO
    crypto_free: Decimal = ZERO
    unknown: int = 0
    gap: int = 0


def _crypto_split(d: Disposal) -> tuple[Decimal, Decimal]:
    """The disposal's gain, split into units held a year or less and units held longer. Proceeds
    and fees are shared out by quantity."""
    taxable = free = ZERO
    if d.quantity == 0:
        return taxable, free
    for piece in d.slices:
        share = piece.quantity / d.quantity
        gain = (d.proceeds - d.fees) * share - piece.cost - piece.costs
        if _held_over_a_year(piece.acquired, d.date):
            free += gain
        else:
            taxable += gain
    return taxable, free


def estimate(
    disposals: Iterable[Disposal],
    classes: Mapping[str, str | None],
    income: Iterable[Income] = (),
    refunds: Iterable[Refund] = (),
) -> list[YearEstimate]:
    """One estimate per year with activity, oldest first. Losses carry forward between years."""
    years: dict[int, _Year] = defaultdict(_Year)
    for d in disposals:
        y = years[d.date.year]
        cls = classes.get(d.isin)
        y.withheld += d.tax_withheld
        y.unknown += d.no_cost_given
        y.gap += d.history_gap
        if cls == CRYPTO:
            taxable, free = _crypto_split(d)
            y.crypto_taxable += taxable
            y.crypto_free += free
        elif cls == STOCK:
            y.stock += d.realised_pnl
        elif cls == FUND:
            y.fund += d.realised_pnl * (1 - TEILFREISTELLUNG)
            y.funds += 1
        else:
            y.other += d.realised_pnl
    for row in income:
        if row.asset_class == CRYPTO:
            continue  # staking and similar are other income (section 22 EStG), not capital income
        y = years[row.date.year]
        factor = 1 - TEILFREISTELLUNG if row.asset_class == FUND else Decimal(1)
        y.income += row.amount * factor
        y.withheld += row.withheld
    for refund in refunds:
        years[refund.date.year].refunds += refund.amount

    out: list[YearEstimate] = []
    stock_carry = ZERO  # losses carried in, positive numbers
    general_carry = ZERO
    for year in sorted(years):
        y = years[year]
        stock_net = y.stock - stock_carry
        general_net = y.fund + y.other + y.income - general_carry

        stock_gain = max(stock_net, ZERO)
        stock_loss = max(-stock_net, ZERO)
        # General losses offset gains on shares too; share losses offset only share gains.
        total = general_net + stock_gain
        general_loss = max(-total, ZERO)
        before_allowance = max(total, ZERO)
        allowance = min(SPARERPAUSCHBETRAG, before_allowance)
        taxable = before_allowance - allowance
        tax = _round(taxable * TAX_RATE * (1 + SOLI_RATE))
        withheld = _round(y.withheld - y.refunds)
        limit = crypto_freigrenze(year)
        out.append(
            YearEstimate(
                year=year,
                stock_pnl=_round(y.stock),
                fund_pnl=_round(y.fund),
                other_pnl=_round(y.other),
                income=_round(y.income),
                stock_loss_brought=_round(stock_carry),
                general_loss_brought=_round(general_carry),
                stock_loss_carried=_round(stock_loss),
                general_loss_carried=_round(general_loss),
                taxable_before_allowance=_round(before_allowance),
                allowance_used=_round(allowance),
                taxable=_round(taxable),
                tax=tax,
                withheld=withheld,
                to_settle=_round(tax - withheld),
                crypto=CryptoYear(
                    taxable_gain=_round(y.crypto_taxable),
                    tax_free_gain=_round(y.crypto_free),
                    freigrenze=limit,
                    under_freigrenze=y.crypto_taxable < limit,
                ),
                fund_disposals=y.funds,
                cost_unknown_sales=y.unknown,
                history_gap_sales=y.gap,
            )
        )
        stock_carry = stock_loss
        general_carry = general_loss
    return out

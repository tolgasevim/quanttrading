"""German tax estimate (FR-26, FR-29). An estimate for planning, not tax advice."""

from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel

from quant.api.deps import CurrentUser, UserDb
from quant.portfolio import service
from quant.portfolio.tax import ASSUMPTIONS, YearEstimate

router = APIRouter(prefix="/api/tax", tags=["tax"])


class CryptoOut(BaseModel):
    taxable_gain: Decimal  # units held a year or less, after fees
    tax_free_gain: Decimal  # units held over a year
    freigrenze: Decimal
    under_freigrenze: bool


class YearTaxOut(BaseModel):
    year: int
    stock_pnl: Decimal
    fund_pnl: Decimal  # after the 30% Teilfreistellung
    other_pnl: Decimal
    income: Decimal  # dividends, distributions and interest
    stock_loss_brought: Decimal
    general_loss_brought: Decimal
    stock_loss_carried: Decimal
    general_loss_carried: Decimal
    taxable_before_allowance: Decimal
    allowance_used: Decimal
    taxable: Decimal
    tax: Decimal
    withheld: Decimal  # what the broker took, net of its refunds
    to_settle: Decimal  # tax less withheld: positive is owed, negative is refundable
    fund_disposals: int
    cost_unknown_sales: int  # sales of units received with no cost: enter it on the Costs page
    history_gap_sales: int  # sales of units the history never bought: an import is missing
    crypto: CryptoOut


class TaxOut(BaseModel):
    years: list[YearTaxOut]
    assumptions: list[str]


def _year(y: YearEstimate) -> YearTaxOut:
    return YearTaxOut(
        year=y.year,
        stock_pnl=y.stock_pnl,
        fund_pnl=y.fund_pnl,
        other_pnl=y.other_pnl,
        income=y.income,
        stock_loss_brought=y.stock_loss_brought,
        general_loss_brought=y.general_loss_brought,
        stock_loss_carried=y.stock_loss_carried,
        general_loss_carried=y.general_loss_carried,
        taxable_before_allowance=y.taxable_before_allowance,
        allowance_used=y.allowance_used,
        taxable=y.taxable,
        tax=y.tax,
        withheld=y.withheld,
        to_settle=y.to_settle,
        fund_disposals=y.fund_disposals,
        cost_unknown_sales=y.cost_unknown_sales,
        history_gap_sales=y.history_gap_sales,
        crypto=CryptoOut(
            taxable_gain=y.crypto.taxable_gain,
            tax_free_gain=y.crypto.tax_free_gain,
            freigrenze=y.crypto.freigrenze,
            under_freigrenze=y.crypto.under_freigrenze,
        ),
    )


@router.get("", response_model=TaxOut)
def get_tax(user: CurrentUser, db: UserDb) -> TaxOut:
    years = service.build_tax(db, user.id)
    return TaxOut(years=[_year(y) for y in years], assumptions=ASSUMPTIONS)

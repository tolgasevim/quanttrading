"""Crypto at the broker: per-lot tax-free dates and the Freigrenze (FR-29, D23).

German rules for coins: a sale is a private disposal (§23 EStG). Units held **more than one
year** are tax-free; units held a year or less are taxed at the personal rate, and only if the
year's taxable gains reach the Freigrenze (1,000 EUR from 2024). It is a *Freigrenze*, not a
*Freibetrag*: one euro over and the whole gain is taxable. Units leave in FIFO order, oldest
first, so the units that become tax-free first are the ones sold first.

The app shows, per coin, each lot with the day it turns tax-free and the share of the coin that
already is. Every figure is an estimate: verify with a tax adviser. The tax itself is not
estimated, because the personal rate is not known here. The realised side (what was sold this
year) comes from the tax module (`tax.py`), the same numbers as the Tax page.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from quant.portfolio import cash, lookthrough, service, tax
from quant.portfolio import instruments as kinds

ZERO = Decimal(0)
HUNDRED = Decimal(100)
NOTE = "An estimate. Verify with a tax adviser."


def tax_free_from(acquired: date) -> date:
    """The first day a lot bought on `acquired` can be sold tax-free: the day after its first
    anniversary (it must be held *more than* a year)."""
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # 29 February
        anniversary = acquired.replace(year=acquired.year + 1, day=28)
    return anniversary + timedelta(days=1)


@dataclass(frozen=True)
class LotView:
    acquired: date
    quantity: Decimal
    cost: Decimal  # purchase value plus acquisition costs
    value: Decimal | None
    gain: Decimal | None
    tax_free_from: date
    free: bool
    days_left: int  # 0 once it is tax-free
    origin: str


@dataclass(frozen=True)
class CoinView:
    isin: str
    name: str
    quantity: Decimal
    price: Decimal | None
    value: Decimal | None
    free_quantity: Decimal
    free_share_pct: Decimal  # of the units
    next_free_date: date | None
    next_free_quantity: Decimal
    taxable_gain_if_sold: Decimal | None  # on the units not yet tax-free
    lots: list[LotView]


@dataclass
class CryptoView:
    year: int
    coins: list[CoinView] = field(default_factory=list)
    value: Decimal = ZERO
    free_value: Decimal = ZERO
    taxable_gain_if_sold: Decimal = ZERO
    realised_taxable: Decimal = ZERO  # sales this year of units held a year or less
    realised_free: Decimal = ZERO
    freigrenze: Decimal = ZERO
    headroom: Decimal = ZERO  # how much more taxable gain fits under the Freigrenze
    over_limit: bool = False
    exposure_pct: Decimal = ZERO  # coins plus crypto-linked shares, of the whole portfolio
    note: str = NOTE


def analyse(session: Session, user_id: uuid.UUID, today: date | None = None) -> CryptoView:
    today = today or service.today()
    held = service.build_holdings(session, user_id)
    conc = lookthrough.concentration(session, held)
    out = CryptoView(year=today.year, freigrenze=tax.crypto_freigrenze(today.year))

    for pos in sorted(held.positions, key=lambda p: p.name or p.isin):
        if kinds.classify(pos.asset_class, pos.name, pos.isin).type != kinds.CRYPTO:
            continue
        mark = held.marks.get(pos.isin)
        price = mark.price if mark else None
        lots: list[LotView] = []
        for lot in sorted(held.book.lots.get(pos.isin, []), key=lambda lt: lt.acquired):
            if lot.quantity <= 0:
                continue
            free_day = tax_free_from(lot.acquired)
            value = None if price is None else lot.quantity * price
            cost = lot.cost + lot.costs
            lots.append(
                LotView(
                    lot.acquired, lot.quantity, cost, value,
                    None if value is None else value - cost,
                    free_day, today >= free_day, max((free_day - today).days, 0), lot.origin,
                )
            )  # fmt: skip
        quantity = sum((lt.quantity for lt in lots), ZERO)
        free_q = sum((lt.quantity for lt in lots if lt.free), ZERO)
        waiting = [lt for lt in lots if not lt.free]
        nxt = waiting[0] if waiting else None
        gains = [lt.gain for lt in waiting]
        unknown = any(g is None for g in gains)
        coin_value = None if price is None else quantity * price
        out.coins.append(
            CoinView(
                pos.isin, pos.name or pos.isin, quantity, price, coin_value, free_q,
                free_q / quantity * HUNDRED if quantity > 0 else ZERO,
                nxt.tax_free_from if nxt else None,
                nxt.quantity if nxt else ZERO,
                None if unknown else sum((g for g in gains if g is not None), ZERO),
                lots,
            )
        )  # fmt: skip
        if coin_value is not None:
            out.value += coin_value
            out.free_value += sum((lt.value or ZERO for lt in lots if lt.free), ZERO)
        if not unknown:
            out.taxable_gain_if_sold += sum((g for g in gains if g is not None), ZERO)

    # What was sold this year (the same figures as the Tax page).
    for year in tax_years(session, user_id):
        if year.year == today.year:
            out.realised_taxable = year.crypto.taxable_gain
            out.realised_free = year.crypto.tax_free_gain
    out.over_limit = out.realised_taxable >= out.freigrenze
    out.headroom = max(out.freigrenze - max(out.realised_taxable, ZERO), ZERO)

    free_cash = cash.cash_status(session, user_id).investable
    whole = conc.total + free_cash
    out.exposure_pct = conc.crypto_value / whole * HUNDRED if whole > 0 else ZERO
    return out


def tax_years(session: Session, user_id: uuid.UUID) -> list[tax.YearEstimate]:
    return service.build_tax(session, user_id)

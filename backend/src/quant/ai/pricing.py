"""What a model call cost (FR-56). Prices are USD per million tokens, from the settings; the
ledger also stores EUR, converted at the latest ECB rate (D36 caps the spend in euros)."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.ai.client import Usage
from quant.config import Settings
from quant.models import FxRate

MILLION = Decimal(1_000_000)
MAX_RATE_AGE_DAYS = 7


def cost_usd(usage: Usage, settings: Settings) -> Decimal:
    total = (
        usage.input_tokens * settings.llm_price_input
        + usage.output_tokens * settings.llm_price_output
        + usage.cache_read_tokens * settings.llm_price_cache_read
        + usage.cache_write_tokens * settings.llm_price_cache_write
    ) / MILLION
    return total.quantize(Decimal("0.000001"))


def usd_per_eur(session: Session, today: date | None = None) -> Decimal | None:
    """The latest ECB rate no older than a week, or None. FxRate is shared market data."""
    today = today or date.today()
    row = session.scalar(
        select(FxRate)
        .where(FxRate.quote == "USD", FxRate.date >= today - timedelta(days=MAX_RATE_AGE_DAYS))
        .order_by(FxRate.date.desc())
        .limit(1)
    )
    return row.rate if row is not None and row.rate > 0 else None


def to_eur(usd: Decimal, rate: Decimal | None) -> Decimal:
    """Without a fresh rate one dollar counts as one euro. That over-counts (the euro is worth
    more), so the cap errs on the safe side."""
    value = usd / rate if rate else usd
    return value.quantize(Decimal("0.000001"))

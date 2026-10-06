"""The pick log (FR-52): every buy, sell or hold the AI recommends is stored with the question,
the reason, the horizon and the price on that day, so the advice can be scored later.

The model records a pick by calling the `record_pick` tool. A pick that fails validation goes
back to the model as a tool error, so it can fix it.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import AiPick, Instrument, PriceEOD

DIRECTIONS = ("buy", "sell", "hold")
MAX_HORIZON_MONTHS = 120
TOOL_NAME = "record_pick"

TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Record one recommendation (buy, sell or hold) in the user's pick log. Call it once for "
        "every security you recommend or advise against in your answer, before you finish. "
        "Do not call it for general explanations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the security"},
            "isin": {"type": "string", "description": "ISIN when known"},
            "ticker": {"type": "string", "description": "Ticker when known"},
            "direction": {"type": "string", "enum": list(DIRECTIONS)},
            "horizon_months": {"type": "integer", "description": "Time horizon in months"},
            "rationale": {"type": "string", "description": "The reason, in one to three sentences"},
        },
        "required": ["name", "direction", "horizon_months", "rationale"],
    },
}


class PickInvalid(ValueError):
    pass


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def candidates(session: Session, isin: str | None, ticker: str | None) -> list[Instrument]:
    """The instruments the pick may mean: the one with that ISIN, then the one with that ticker.
    The model may give a wrong ISIN or only a ticker, so both are tried."""
    found: list[Instrument] = []
    if isin:
        found += session.scalars(select(Instrument).where(Instrument.isin == isin)).all()
    if ticker:
        found += session.scalars(select(Instrument).where(Instrument.code == ticker.upper())).all()
    return found


def latest_price(
    session: Session, isin: str | None, ticker: str | None
) -> tuple[Decimal, str, date] | None:
    """The newest stored close of the first candidate that has one, from the shared market data."""
    for instrument in candidates(session, isin, ticker):
        row = session.scalar(
            select(PriceEOD)
            .where(PriceEOD.instrument_id == instrument.id)
            .order_by(PriceEOD.date.desc())
            .limit(1)
        )
        if row is not None:
            return row.close, row.currency, row.date
    return None


def record_pick(
    session: Session,
    user_id: uuid.UUID,
    *,
    question: str,
    data: dict[str, Any],
    held_isins: set[str],
    usage_id: uuid.UUID | None,
) -> AiPick:
    name = _text(data.get("name"), 200)
    direction = _text(data.get("direction"), 10)
    rationale = _text(data.get("rationale"), 2000)
    horizon = data.get("horizon_months")
    if not name:
        raise PickInvalid("name is required")
    if direction is None or direction.lower() not in DIRECTIONS:
        raise PickInvalid("direction must be buy, sell or hold")
    if not rationale:
        raise PickInvalid("rationale is required")
    if isinstance(horizon, bool) or not isinstance(horizon, int):
        raise PickInvalid("horizon_months must be a whole number")
    if not 1 <= horizon <= MAX_HORIZON_MONTHS:
        raise PickInvalid(f"horizon_months must be between 1 and {MAX_HORIZON_MONTHS}")
    isin = _text(data.get("isin"), 20)
    isin = isin.upper() if isin else None
    ticker = _text(data.get("ticker"), 30)
    quote = latest_price(session, isin, ticker)
    known = {i.isin for i in candidates(session, isin, ticker) if i.isin}
    held = bool(({isin} | known) & held_isins)
    pick = AiPick(
        user_id=user_id,
        usage_id=usage_id,
        question=question[:1000],
        name=name,
        isin=isin,
        ticker=ticker,
        direction=direction.lower(),
        horizon_months=horizon,
        rationale=rationale,
        held=held,
        price=quote[0] if quote else None,
        price_currency=quote[1] if quote else None,
        price_date=quote[2] if quote else None,
    )
    session.add(pick)
    session.flush()
    return pick

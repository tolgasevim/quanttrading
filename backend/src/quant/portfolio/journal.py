"""The decision journal (FR-44).

The user writes down what they did with a piece of guidance: bought, sold, or ignored it, and
why. The price at the time is kept. Later the journal shows what the price did since, so the user
can see whether following or ignoring the guidance paid. An entry can name the AI pick it answers
(FR-52); then it also says whether the pick's direction would have been right.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import AiPick, JournalEntry

ACTIONS = ("bought", "sold", "ignored")
ZERO = Decimal(0)


class JournalError(ValueError):
    """The entry is not valid. The text says why, in plain words."""


def add_entry(
    session: Session,
    user_id: uuid.UUID,
    *,
    action: str,
    name: str,
    reason: str,
    isin: str | None,
    guidance: str | None,
    pick_id: uuid.UUID | None,
) -> JournalEntry:
    # The AI layer reads the allocation, so it is imported here and not at the top.
    from quant.ai import picks

    if action not in ACTIONS:
        raise JournalError("the action must be bought, sold or ignored")
    name = " ".join(name.split())
    reason = " ".join(reason.split())
    if not name:
        raise JournalError("say what it was about")
    if not reason:
        raise JournalError("write the reason: a journal without reasons teaches nothing")
    isin = isin.strip().upper() if isin and isin.strip() else None
    if pick_id is not None:
        owned = session.scalar(
            select(AiPick.id).where(AiPick.id == pick_id, AiPick.user_id == user_id)
        )
        if owned is None:
            raise JournalError("no such pick")
    quote = picks.latest_price(session, picks.candidates(session, isin, None)) if isin else None
    entry = JournalEntry(
        user_id=user_id,
        action=action,
        name=name[:200],
        isin=isin,
        reason=reason[:1000],
        guidance=(guidance or "").strip()[:200] or None,
        pick_id=pick_id,
        price=quote[0] if quote else None,
        price_currency=quote[1] if quote else None,
        price_date=quote[2] if quote else None,
    )
    session.add(entry)
    session.commit()
    return entry


@dataclass(frozen=True)
class Outcome:
    since_pct: Decimal | None  # price move since the entry
    guidance_right: bool | None  # for an entry tied to a pick: was its direction right?
    verdict: str


def outcome(session: Session, entry: JournalEntry) -> Outcome:
    """What the price did since the entry, and what that says about the decision."""
    from quant.ai import picks

    if not entry.isin or entry.price is None or entry.price <= 0:
        return Outcome(None, None, "No price was stored, so there is nothing to compare.")
    quote = picks.latest_price(session, picks.candidates(session, entry.isin, None))
    if quote is None or quote[1] != entry.price_currency:
        return Outcome(None, None, "There is no recent price to compare with.")
    move = (quote[0] - entry.price) / entry.price * 100
    right: bool | None = None
    if entry.pick_id is not None:
        pick = session.get(AiPick, entry.pick_id)
        if pick is not None:
            right = move < 0 if pick.direction == "sell" else move > 0
    if entry.action == "bought":
        verdict = f"Since you bought, the price is {move:+.1f}%."
    elif entry.action == "sold":
        verdict = f"Since you sold, the price is {move:+.1f}%." + (
            " Selling looks right so far." if move < 0 else " It kept rising after the sale."
        )
    else:
        verdict = f"Since you ignored it, the price is {move:+.1f}%."
    if right is not None:
        verdict += (
            " The pick's direction was right." if right else " The pick's direction was wrong."
        )
    return Outcome(move.quantize(Decimal("0.1")), right, verdict)


def entries(session: Session, user_id: uuid.UUID, limit: int = 200) -> list[JournalEntry]:
    return list(
        session.scalars(
            select(JournalEntry)
            .where(JournalEntry.user_id == user_id)
            .order_by(JournalEntry.at.desc(), JournalEntry.id)
            .limit(limit)
        )
    )

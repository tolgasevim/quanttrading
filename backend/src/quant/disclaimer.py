"""The app-wide disclaimer (FR-4): every user accepts it on first login, and again after every
change of its text. Change the text and raise `VERSION` together.

This is separate from the AI consent (`ai/consent.py`, FR-57), which says what is sent to the AI
provider. This one covers the whole app: the numbers, the tax estimate and the AI.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.models import DisclaimerAcceptance

VERSION = 1
TEXT = (
    "QuantTrading is a private tool for the owner and family. It gives guidance only. It is "
    "not investment advice and not tax advice, and it never places trades. Prices can be "
    "late or wrong, the tax figure is an estimate for planning, and the AI may be wrong. "
    "You decide what to do, and you carry the risk. Check important numbers with your broker "
    "and a tax advisor."
)


def accepted(session: Session, user_id: uuid.UUID) -> bool:
    row = session.scalar(
        select(DisclaimerAcceptance).where(DisclaimerAcceptance.user_id == user_id)
    )
    return row is not None and row.version == VERSION


def accept(session: Session, user_id: uuid.UUID) -> None:
    """Record acceptance of the current version. Accepting the same version again changes
    nothing, so the time of the first acceptance stays. One statement, so two requests at once
    cannot collide."""
    now = datetime.now(UTC)
    insert_stmt = insert(DisclaimerAcceptance).values(
        user_id=user_id, version=VERSION, accepted_at=now
    )
    session.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=[DisclaimerAcceptance.user_id],
            set_={"version": VERSION, "accepted_at": now},
            where=DisclaimerAcceptance.version < VERSION,  # never lower a newer acceptance
        )
    )
    session.commit()

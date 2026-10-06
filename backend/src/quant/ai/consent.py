"""The AI consent and the disclaimer (FR-57, FR-4).

A user must accept the current text once before any question leaves the app. The version number
goes up when the text changes, which asks everybody again. The option "hide amounts" sends the
weight of each position and never a euro value, a quantity or a profit (FR-57).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quant.models import AiConsent

DISCLAIMER_VERSION = 1
LABEL = "AI-generated. It may be wrong. It is not investment advice."
DISCLAIMER = (
    "This tool gives guidance. It never places trades and never holds your broker login. "
    "The answers come from a language model. The model may be wrong, may miss news, and does "
    "not know your full situation. This is not investment advice. You decide, and you carry "
    "the risk. To answer, the app sends your question and a summary of your portfolio (names, "
    "ISINs, sectors and weights, and unless you hide amounts, values and profit) to the AI "
    "provider. It never sends your name, address, IBAN or account number."
)


class ConsentRequired(Exception):
    pass


def get(session: Session, user_id: uuid.UUID) -> AiConsent | None:
    row = session.scalar(select(AiConsent).where(AiConsent.user_id == user_id))
    return row if row is not None and row.version == DISCLAIMER_VERSION else None


def accept(session: Session, user_id: uuid.UUID, anonymise_amounts: bool) -> AiConsent:
    """Store the consent. The time of acceptance changes only when the disclaimer version does,
    not when the user only flips "hide amounts"."""
    for attempt in (1, 2):
        row = session.scalar(select(AiConsent).where(AiConsent.user_id == user_id))
        if row is None:
            row = AiConsent(
                user_id=user_id,
                version=DISCLAIMER_VERSION,
                accepted_at=datetime.now(UTC),
                anonymise_amounts=anonymise_amounts,
            )
            session.add(row)
        else:
            if row.version != DISCLAIMER_VERSION:
                row.version = DISCLAIMER_VERSION
                row.accepted_at = datetime.now(UTC)
            row.anonymise_amounts = anonymise_amounts
        try:
            session.commit()
            return row
        except IntegrityError:  # a second request inserted the row first: update that one
            session.rollback()
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def require(session: Session, user_id: uuid.UUID) -> AiConsent:
    row = get(session, user_id)
    if row is None:
        raise ConsentRequired("accept the AI disclaimer first")
    return row


def withdraw(session: Session, user_id: uuid.UUID) -> None:
    """Take the consent back (FR-57): nothing is sent again until the user accepts anew."""
    row = session.scalar(select(AiConsent).where(AiConsent.user_id == user_id))
    if row is not None:
        session.delete(row)
        session.commit()

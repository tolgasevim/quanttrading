"""The spend ledger and the monthly caps (FR-56, D36).

Every model call is a `llm_usage` row, written right after the call so a later failure never
loses it. A month is a Europe/Berlin calendar month. Two caps apply before a call: one for the
user and one for everybody. The total is read in a separate session with row-level security
bypassed, because it has to count every user's rows. At 50, 80 and 100 percent of the total cap
each admin gets one notification a month (dedupe key `llm_cap:{month}:{percent}`).

A cap is checked before every call, not during it, so the last call may pass the cap by its
own cost. Two calls at once can both pass a check; with a handful of users that is accepted.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant import rls
from quant.ai.client import LlmReply
from quant.ai.pricing import cost_usd, to_eur, usd_per_eur
from quant.config import Settings, get_settings
from quant.db import get_sessionmaker
from quant.models import LlmUsage, Notification, Role, User

log = logging.getLogger(__name__)

THRESHOLDS = (50, 80, 100)
BERLIN = ZoneInfo("Europe/Berlin")


class BudgetExceeded(Exception):
    """A monthly cap is reached. `scope` is "user" or "total"."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        who = "your" if scope == "user" else "the household's"
        super().__init__(f"{who} monthly AI budget is used up. It resets next month.")


@dataclass(frozen=True)
class Spend:
    month: str
    user_eur: Decimal
    user_cap_eur: Decimal
    total_eur: Decimal | None  # None when the caller may not see it
    total_cap_eur: Decimal


def month_bounds(now: datetime | None = None) -> tuple[datetime, str]:
    local = (now or datetime.now(UTC)).astimezone(BERLIN)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC), f"{local.year:04d}-{local.month:02d}"


def user_spend(session: Session, user_id: uuid.UUID, now: datetime | None = None) -> Decimal:
    start, _ = month_bounds(now)
    total = session.scalar(
        select(func.coalesce(func.sum(LlmUsage.cost_eur), 0)).where(
            LlmUsage.user_id == user_id, LlmUsage.created_at >= start
        )
    )
    return Decimal(total or 0)


def total_spend(now: datetime | None = None) -> Decimal:
    start, _ = month_bounds(now)
    with get_sessionmaker()() as session:
        rls.bypass(session)
        total = session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost_eur), 0)).where(
                LlmUsage.created_at >= start
            )
        )
        return Decimal(total or 0)


def check(
    session: Session,
    user_id: uuid.UUID,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> None:
    """Raise `BudgetExceeded` when the user's or the household's cap is reached."""
    settings = settings or get_settings()
    over_user = user_spend(session, user_id, now) >= settings.llm_user_monthly_cap_eur
    session.commit()  # give the connection back before total_spend takes another one
    if over_user:
        raise BudgetExceeded("user")
    if total_spend(now) >= settings.llm_monthly_cap_eur:
        raise BudgetExceeded("total")


def record(
    session: Session,
    user_id: uuid.UUID,
    purpose: str,
    reply: LlmReply,
    settings: Settings | None = None,
) -> LlmUsage:
    """Store what one call cost, commit it, and raise the cap alerts when a threshold is crossed."""
    settings = settings or get_settings()
    usd = cost_usd(reply.usage, settings)
    row = LlmUsage(
        user_id=user_id,
        purpose=purpose,
        model=reply.model[:60],
        input_tokens=reply.usage.input_tokens,
        output_tokens=reply.usage.output_tokens,
        cache_read_tokens=reply.usage.cache_read_tokens,
        cache_write_tokens=reply.usage.cache_write_tokens,
        cost_usd=usd,
        cost_eur=to_eur(usd, usd_per_eur(session)),
        stop_reason=reply.stop_reason,
        request_id=reply.request_id,
    )
    session.add(row)
    session.commit()
    try:
        alert_thresholds(settings, added=row.cost_eur)
    except Exception:  # an alert problem must not fail the user's request
        log.exception("could not store the AI budget alerts")
    return row


def alert_thresholds(
    settings: Settings | None = None,
    now: datetime | None = None,
    added: Decimal | None = None,
) -> int:
    """Tell every admin once a month at each threshold of the total cap. Returns rows stored.

    With `added` (what the latest call cost), only a threshold that call crossed is considered, so
    a month already past the cap does no more work per call. Without it, every threshold the spend
    has reached is (the dedupe key still stores each alert once)."""
    settings = settings or get_settings()
    cap = settings.llm_monthly_cap_eur
    if cap <= 0:
        return 0
    spent = total_spend(now)
    _, month = month_bounds(now)
    before = spent - added if added is not None else None
    crossed = [
        pct
        for pct in THRESHOLDS
        if spent >= cap * pct / 100 and (before is None or before < cap * pct / 100)
    ]
    if not crossed:
        return 0
    stored = 0
    with get_sessionmaker()() as session:
        rls.bypass(session)
        admins = session.scalars(select(User.id).where(User.role == Role.ADMIN)).all()
        for pct in crossed:
            for admin_id in admins:
                inserted = session.scalars(
                    insert(Notification)
                    .values(
                        user_id=admin_id,
                        kind="ai_budget",
                        severity="info" if pct < 100 else "warning",
                        title=f"AI budget: {pct}% of the monthly cap used",
                        body=(
                            f"The household has used {spent:.2f} of {cap:.2f} EUR this month. "
                            + ("The AI chat is paused until next month." if pct >= 100 else "")
                        ).strip(),
                        dedupe_key=f"llm_cap:{month}:{pct}",
                    )
                    .on_conflict_do_nothing(constraint="uq_notifications_user_key")
                    .returning(Notification.id)
                ).all()
                stored += len(inserted)
        session.commit()
    return stored


def summary(
    session: Session, user_id: uuid.UUID, include_total: bool, settings: Settings | None = None
) -> Spend:
    settings = settings or get_settings()
    _, month = month_bounds()
    mine = user_spend(session, user_id)
    session.commit()  # give the connection back before total_spend takes another one
    return Spend(
        month=month,
        user_eur=mine,
        user_cap_eur=settings.llm_user_monthly_cap_eur,
        total_eur=total_spend() if include_total else None,
        total_cap_eur=settings.llm_monthly_cap_eur,
    )

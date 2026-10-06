"""The weekly commentary (FR-54): a short text for each user, once a week.

With AI on, the text comes from the same assistant as the chat (`ai.service.ask`, purpose
"weekly"), so its recommendations go into the pick log (FR-52), it is counted in the spend ledger
(FR-56) and it carries the AI label (FR-53). Without consent, without a key, on a provider error
or when a budget cap is reached, the commentary is a template built from the data, with no model
call: the weekly text still arrives, as FR-56 asks. A template says why it is one.

One commentary per user and week. A second run for the same week changes nothing.
"""

import logging
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant import rls
from quant.ai import budget, consent
from quant.ai import service as ai_service
from quant.ai.client import LlmClient, LlmError
from quant.config import get_settings
from quant.db import get_sessionmaker
from quant.ingest.jobs import JobResult
from quant.models import (
    AiCommentary,
    AiPick,
    AiPickScore,
    Instrument,
    Notification,
    PriceEOD,
    User,
)
from quant.portfolio import service

log = logging.getLogger(__name__)

AI_NOTHING = "The AI gave no answer."
QUESTION = (
    "Write my weekly portfolio commentary for the week starting {monday} (Monday to Sunday). "
    "In 150 to 250 words: what changed this week, the main risks to watch next week, and at most "
    "two ideas worth a closer look. Start with the most important point. Use the facts and the "
    "tools; say what you cannot see."
)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def weekly_moves(session: Session, isins: list[str], today: date) -> dict[str, Decimal]:
    """The price change in percent over the last week, per ISIN, in the instrument's currency,
    from the stored closes (the newest close against the one a week before it)."""
    moves: dict[str, Decimal] = {}
    if not isins:
        return moves
    for instrument in session.scalars(select(Instrument).where(Instrument.isin.in_(isins))):
        bars = session.execute(
            select(PriceEOD.date, PriceEOD.close)
            .where(
                PriceEOD.instrument_id == instrument.id,
                PriceEOD.date >= today - timedelta(days=21),
            )
            .order_by(PriceEOD.date)
        ).all()
        if not bars:
            continue
        last_day, last = bars[-1]
        if (today - last_day).days > 7:
            continue  # no recent close: no move to report
        base = next((c for d, c in reversed(bars) if d <= last_day - timedelta(days=7)), None)
        if base and instrument.isin:
            moves[instrument.isin] = ((last / base - 1) * 100).quantize(Decimal("0.1"))
    return moves


def _name(holdings: service.Holdings, isin: str) -> str:
    known = holdings.everything.get(isin)
    return (known.name if known and known.name else isin) or isin


def _week_counts(session: Session, user_id: uuid.UUID, since: datetime) -> dict[str, int]:
    """Alerts, picks and new pick scores of the user since `since`."""
    alerts = session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.created_at >= since,
            Notification.kind != "weekly_commentary",
        )
    )
    picks = session.scalar(
        select(func.count())
        .select_from(AiPick)
        .where(AiPick.user_id == user_id, AiPick.created_at >= since)
    )
    scores = session.scalar(
        select(func.count())
        .select_from(AiPickScore)
        .where(AiPickScore.user_id == user_id, AiPickScore.scored_at >= since)
    )
    return {"alerts": alerts or 0, "picks": picks or 0, "scores": scores or 0}


def facts(
    session: Session, user_id: uuid.UUID, holdings: service.Holdings, today: date
) -> tuple[str, list[str]]:
    """What the app itself knows about the week: the facts for the model and for the template.
    Returns the lines (also the body of the template) and the sections used."""
    weights, total, valued = service.weights(holdings)
    held = [p.isin for p in holdings.positions]
    moves = weekly_moves(session, held, today)
    since = datetime.combine(week_start(today), time.min, tzinfo=ZoneInfo(get_settings().timezone))
    counts = _week_counts(session, user_id, since)
    lines = [
        f"Portfolio: {len(held)} positions, {valued} with a price, market value {total:,.2f} EUR."
    ]
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:5]
    if top:
        lines.append(
            "Largest weights: " + ", ".join(f"{_name(holdings, i)} {w:.1f} %" for i, w in top) + "."
        )
    ranked = sorted(moves.items(), key=lambda kv: kv[1], reverse=True)
    ups = [(i, m) for i, m in ranked if m > 0][:3]
    downs = [(i, m) for i, m in reversed(ranked) if m < 0][:3]
    if ups:
        lines.append(
            "Biggest gains this week (price): "
            + ", ".join(f"{_name(holdings, i)} +{m} %" for i, m in ups)
            + "."
        )
    if downs:
        lines.append(
            "Biggest falls this week (price): "
            + ", ".join(f"{_name(holdings, i)} {m} %" for i, m in downs)
            + "."
        )
    if not moves:
        lines.append("No recent closes are stored, so there are no weekly price moves.")
    lines.append(
        f"Alerts this week: {counts['alerts']}. AI picks made this week: {counts['picks']}; "
        f"new pick scores: {counts['scores']}."
    )
    return "\n".join(lines), ["portfolio", "weights", "weekly moves", "alerts", "picks"]


def template_text(monday: date, body: str, why: str) -> str:
    return (
        f"Weekly summary for the week of {monday.strftime('%d %b %Y')}.\n\n{body}\n\n"
        f"(A plain summary of your data, not written by the AI: {why}.)"
    )


def _store(
    session: Session,
    user_id: uuid.UUID,
    monday: date,
    kind: str,
    text: str,
    model: str | None,
    why: str | None,
) -> bool:
    """Insert the commentary and its notification. False when the week already has one."""
    inserted = session.scalars(
        insert(AiCommentary)
        .values(
            user_id=user_id,
            week_start=monday,
            kind=kind,
            text=text[:10000],
            model=(model or "")[:60] or None,
            why_template=why[:200] if why else None,
        )
        .on_conflict_do_nothing(constraint="uq_ai_commentaries_week")
        .returning(AiCommentary.id)
    ).all()
    if inserted:
        session.execute(
            insert(Notification)
            .values(
                user_id=user_id,
                kind="weekly_commentary",
                severity="info",
                title="Your weekly commentary is ready",
                body="Open the AI guide to read it."
                if kind == "ai"
                else "A plain summary of your week.",
                dedupe_key=f"weekly:{monday.isoformat()}",
            )
            .on_conflict_do_nothing(constraint="uq_notifications_user_key")
        )
    session.commit()
    return bool(inserted)


def create_commentary(
    session: Session, user_id: uuid.UUID, today: date, client: LlmClient | None = None
) -> AiCommentary | None:
    """Make this week's commentary for one user, on a session scoped to that user. Returns the
    commentary (the new one, or the one the week already had), None when the user has no
    holdings to talk about."""
    monday = week_start(today)
    existing = session.scalars(
        select(AiCommentary).where(
            AiCommentary.user_id == user_id, AiCommentary.week_start == monday
        )
    ).first()
    if existing is not None:
        return existing
    holdings = service.build_holdings(session, user_id)
    if not holdings.positions:
        return None
    body, _ = facts(session, user_id, holdings, today)

    why: str | None = None
    text = ""
    model: str | None = None
    try:
        result = ai_service.ask(
            session,
            user_id,
            QUESTION.format(monday=monday.isoformat()),
            client,
            purpose="weekly",
            extra="Facts the app computed for the week:\n" + body,
        )
        if result.refused:
            why = "the AI declined to write it"
        elif not result.answer.strip() or result.answer == AI_NOTHING:
            why = "the AI gave no text"
        else:
            text = f"{result.answer}\n\n{result.label}"
            if result.notes:
                text += "\n" + " ".join(result.notes)
            model = result.model
    except consent.ConsentRequired:
        why = "the AI is not switched on for you (accept the disclaimer on the AI page)"
    except budget.BudgetExceeded:
        why = "the monthly AI budget is used up"
    except LlmError:
        why = "the AI was not available"
    except Exception:  # noqa: BLE001 - the weekly text must arrive in some form
        log.exception("weekly commentary: unexpected error for a user")
        session.rollback()
        why = "the AI step failed"
    if why is not None:
        _store(session, user_id, monday, "template", template_text(monday, body, why), None, why)
    else:
        _store(session, user_id, monday, "ai", text, model, None)
    return session.scalars(
        select(AiCommentary).where(
            AiCommentary.user_id == user_id, AiCommentary.week_start == monday
        )
    ).first()


def run_weekly(today: date, client: LlmClient | None = None) -> JobResult:
    """The weekly job: a commentary for every user with holdings. Each user is its own session,
    scoped to that user (row-level security), so one failure never touches another's text."""
    result = JobResult()
    maker = get_sessionmaker()
    with maker() as lister:
        rls.bypass(lister)
        user_ids = list(lister.scalars(select(User.id).order_by(User.created_at)))
    for user_id in user_ids:
        result.attempted += 1
        try:
            with maker() as session:
                rls.scope_to_user(session, user_id)
                before = session.scalar(
                    select(func.count()).where(
                        AiCommentary.user_id == user_id,
                        AiCommentary.week_start == week_start(today),
                    )
                )
                made = create_commentary(session, user_id, today, client)
                if made is not None and not before:
                    result.rows_written += 1
        except Exception as exc:  # noqa: BLE001 - the next user still gets theirs
            log.exception("weekly commentary failed for a user")
            result.errors[str(user_id)] = type(exc).__name__
    return result

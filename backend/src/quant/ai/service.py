"""Ask the AI about the portfolio (FR-52, FR-53, FR-56, FR-57).

Order of a question: consent, budget, portfolio summary, model call (a short tool loop in which
the model records its picks), usage ledger after every call. The answer always carries the label
and the list of data the model was given. If the answer reads like a recommendation but the model
recorded no pick, one follow-up asks it to record the picks or to say there are none.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from quant.ai import budget, consent, picks
from quant.ai.client import LlmClient, LlmError, LlmReply, default_client
from quant.config import get_settings
from quant.models import AiPick
from quant.portfolio import service
from quant.portfolio.lots import COST_MISSING

MAX_TURNS = 3
MAX_POSITIONS = 60
MAX_QUESTION_CHARS = 1000
RECOMMENDS = re.compile(
    r"\b(buy|sell|hold|accumulate|add to|reduce|trim|avoid|overweight|underweight"
    r"|kaufen|verkaufen|halten)\b",
    re.IGNORECASE,
)

SYSTEM = """You are a careful investing guide for a private investor in Germany. You give \
guidance only. You never place trades. You are not a financial adviser and you say so when it \
matters.

The user message holds a summary of the user's portfolio and a question. You have no live market \
data and no news: say when an answer depends on facts you cannot see, and do not invent prices \
or figures. Use the portfolio data you are given, and name which data you used.

Be short and concrete. Give the main risk next to every recommendation. For every security you \
recommend to buy, sell or hold, call the tool record_pick once, with a time horizon in months. \
Do not record a pick for a general explanation. Never ask for passwords or broker logins."""


@dataclass
class AskResult:
    answer: str
    label: str
    picks: list[AiPick]
    data_points: list[str]
    model: str
    fallback_used: bool
    refused: bool = False
    notes: list[str] = field(default_factory=list)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def build_context(
    session: Session, user_id: uuid.UUID, anonymise: bool
) -> tuple[str, list[str], set[str]]:
    """The portfolio as compact JSON for the prompt, what it contains, and the held ISINs."""
    holdings = service.build_holdings(session, user_id)
    weights, total, valued = service.weights(holdings)
    rows = []
    for p in holdings.positions:
        mark = holdings.marks.get(p.isin)
        value = service.mark_value(mark) if mark else None
        sector, _ = holdings.sectors.get(p.isin, (None, None))
        row: dict[str, Any] = {
            "name": p.name,
            "isin": p.isin,
            "class": p.asset_class,
            "sector": sector,
            "weight_pct": _money(weights.get(p.isin)),
        }
        if not anonymise:
            cost = mark.cost if mark else None
            row["value_eur"] = _money(value)
            # Like the holdings table: no profit when the cost is unknown or the history is short.
            if (
                value is not None
                and cost is not None
                and not cost.flags & COST_MISSING
                and cost.total_cost
            ):
                row["unrealised_pct"] = _money((value - cost.total_cost) / cost.total_cost * 100)
        rows.append((value or Decimal(0), row))
    rows.sort(key=lambda r: r[0], reverse=True)
    shown = [row for _, row in rows[:MAX_POSITIONS]]
    payload: dict[str, Any] = {
        "today": date.today().isoformat(),
        "positions_total": len(rows),
        "positions_shown": len(shown),
        "positions": shown,
    }
    if not anonymise:
        payload["valued_total_eur"] = _money(total)
    points = [f"{len(shown)} of {len(rows)} positions (largest first), {valued} with a price"]
    points.append(
        "weights only; euro values, quantities and profit were hidden"
        if anonymise
        else "weights, euro values and unrealised profit"
    )
    return json.dumps(payload, separators=(",", ":")), points, {p.isin for p in holdings.positions}


def _handle_tools(
    session: Session,
    user_id: uuid.UUID,
    reply: LlmReply,
    *,
    question: str,
    held: set[str],
    usage_id: uuid.UUID,
    saved: list[AiPick],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for call in reply.tool_calls:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id}
        if call.name != picks.TOOL_NAME:
            block.update(content="unknown tool", is_error=True)
        else:
            try:
                with session.begin_nested():
                    pick = picks.record_pick(
                        session,
                        user_id,
                        question=question,
                        data=call.input,
                        held_isins=held,
                        usage_id=usage_id,
                    )
                saved.append(pick)
                block["content"] = "recorded"
            except picks.PickInvalid as exc:
                block.update(content=str(exc), is_error=True)
        results.append(block)
    session.commit()
    return results


def ask(
    session: Session,
    user_id: uuid.UUID,
    question: str,
    client: LlmClient | None = None,
) -> AskResult:
    question = " ".join(question.split())[:MAX_QUESTION_CHARS]
    agreed = consent.require(session, user_id)
    settings = get_settings()
    budget.check(session, user_id, settings)
    client = client or default_client()

    context, points, held = build_context(session, user_id, agreed.anonymise_amounts)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Portfolio summary:\n{context}\n\nQuestion: {question}"}
    ]
    saved: list[AiPick] = []
    models: set[str] = set()
    answer = ""
    notes: list[str] = []
    followed_up = False
    failed = 0  # picks the model got wrong on the last turn, with no turn left to fix them

    for _turn in range(MAX_TURNS + 1):
        # No transaction stays open while the model thinks: the connection goes back to the pool.
        session.commit()
        if _turn > 0:  # every paid call is checked, not only the first
            try:
                budget.check(session, user_id, settings)
            except budget.BudgetExceeded:
                notes.append("The AI budget ran out, so the answer stops here.")
                break
        try:
            reply = client.send(system=SYSTEM, messages=messages, tools=[picks.TOOL])
        except LlmError:
            if _turn == 0:
                raise
            # The earlier calls were paid for and their picks are stored: keep what we have.
            notes.append("The AI service failed part way, so the answer may be incomplete.")
            break
        models.add(reply.model)
        usage = budget.record(session, user_id, "ask", reply, settings)
        if reply.refused:
            return _result(
                "The AI declined to answer this question.", saved, points, models, settings, True
            )
        if reply.stop_reason == "max_tokens":
            notes.append("The answer was cut off at the length limit.")
        # A reply to the follow-up, or a short "Recorded." after the tool results, is not the
        # answer: keep the longest text of the other replies.
        if not followed_up and len(reply.text) > len(answer):
            answer = reply.text
        if reply.tool_calls:
            before = len(saved)
            results = _handle_tools(
                session,
                user_id,
                reply,
                question=question,
                held=held,
                usage_id=usage.id,
                saved=saved,
            )
            messages.append({"role": "assistant", "content": reply.raw_content})
            messages.append({"role": "user", "content": results})
            failed = len(reply.tool_calls) - (len(saved) - before)
            continue
        failed = 0
        if not saved and not followed_up and RECOMMENDS.search(reply.text):
            followed_up = True
            messages.append({"role": "assistant", "content": reply.raw_content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "If your answer recommends buying, selling or holding any security, call "
                        "record_pick for each one now. If it does not, reply with the single "
                        "word: none."
                    ),
                }
            )
            continue
        break
    result = _result(answer or "The AI gave no answer.", saved, points, models, settings, False)
    result.notes.extend(notes)
    if failed:
        result.notes.append(
            f"{failed} pick(s) in this answer could not be recorded in the pick log. "
            "Ask again, or note them yourself."
        )
    return result


def _result(
    answer: str,
    saved: list[AiPick],
    points: list[str],
    models: set[str],
    settings: Any,
    refused: bool,
) -> AskResult:
    # The API may report a dated name for the configured model, e.g. "claude-opus-5-5-2026...".
    fallback = any(
        m != settings.llm_model and not m.startswith(settings.llm_model + "-") for m in models
    )
    return AskResult(
        answer=answer,
        label=consent.LABEL,
        picks=saved,
        data_points=points,
        model=sorted(models)[0] if len(models) == 1 else ", ".join(sorted(models)),
        fallback_used=fallback,
        refused=refused,
    )

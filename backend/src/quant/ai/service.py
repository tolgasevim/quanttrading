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

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from quant.ai import budget, consent, picks, tools
from quant.ai.client import LlmClient, LlmError, LlmReply, default_client
from quant.config import get_settings
from quant.models import AiPick
from quant.portfolio import service
from quant.portfolio.lots import COST_MISSING

MAX_TURNS = 5  # model calls after the first; read tools need a turn each
MAX_HISTORY = 10  # earlier messages of a chat that are sent again
MAX_HISTORY_CHARS = 4000
MAX_POSITIONS = 60
MAX_QUESTION_CHARS = 1000
SUBSTANTIAL = 80  # characters: a reply this long counts as an answer, not as "Done."
RECOMMENDS = re.compile(
    r"\b(buy|sell|hold|accumulate|add to|reduce|trim|avoid|overweight|underweight"
    r"|kaufen|verkaufen|halten)\b",
    re.IGNORECASE,
)

SYSTEM = """You are a careful investing guide for a private investor in Germany. You give \
guidance only. You never place trades. You are not a financial adviser and you say so when it \
matters.

The user message holds a summary of the user's portfolio and a question. You can call read-only \
tools for a holding's details, stored prices, a price-shock scenario, the tax estimate, ECB \
rates and the user's earlier picks. You have no news, no macro data, no live quotes and no \
fundamentals: say when an answer depends on facts you cannot see, and never invent prices or \
figures. Use tools for numbers instead of guessing, and name which data you used. For a \
scenario such as "what if oil rises 30%", call the scenario tool with sensible targets and say \
that it is linear and ignores correlations.

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
    tool_calls: list[tools.ToolTrace] = field(default_factory=list)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def build_context(holdings: service.Holdings, anonymise: bool) -> tuple[str, list[str], set[str]]:
    """The portfolio as compact JSON for the prompt, what it contains, and the held ISINs."""
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


def _pick_key(data: dict[str, Any]) -> tuple[str, str] | None:
    who = data.get("isin") or data.get("name")
    direction = data.get("direction")
    if not isinstance(who, str) or not isinstance(direction, str):
        return None
    return (" ".join(who.split()).upper(), direction.strip().lower())


def _pick_key_of(pick: AiPick) -> tuple[str, str]:
    return ((pick.isin or pick.name).upper(), pick.direction)


def _handle_tools(
    session: Session,
    user_id: uuid.UUID,
    reply: LlmReply,
    *,
    question: str,
    held: set[str],
    usage_id: uuid.UUID,
    saved: list[AiPick],
    ctx: tools.ToolContext,
    traces: list[tools.ToolTrace],
) -> tuple[list[dict[str, Any]], int]:
    """Run the tool calls of one reply. Returns the tool_result blocks and how many record_pick
    calls failed (the other tools' errors are for the model to read, not for the user)."""
    results: list[dict[str, Any]] = []
    failed = 0
    for call in reply.tool_calls:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id}
        if call.name in tools.NAMES:
            text, trace = tools.run(ctx, call.name, call.input)
            traces.append(trace)
            block["content"] = text
            if trace.error:
                block["is_error"] = True
        elif call.name != picks.TOOL_NAME:
            block.update(content="unknown tool", is_error=True)
        else:
            key = _pick_key(call.input)
            if key is not None and key in {_pick_key_of(p) for p in saved}:
                block["content"] = "already recorded"  # the same pick twice is logged once
                results.append(block)
                continue
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
                failed += 1
            except SQLAlchemyError:  # the savepoint is rolled back; the answer is not lost
                block.update(content="the pick could not be stored", is_error=True)
                failed += 1
        results.append(block)
    session.commit()
    return results, failed


def _warn_if_unlogged(answer: str, saved: list[AiPick], notes: list[str]) -> None:
    """The loop stopped early. If the answer reads like advice and nothing is logged, say so."""
    if not saved and RECOMMENDS.search(answer):
        notes.append("This answer may name a recommendation that is not in the pick log.")


def ask(
    session: Session,
    user_id: uuid.UUID,
    question: str,
    client: LlmClient | None = None,
    history: list[dict[str, str]] | None = None,
) -> AskResult:
    """One question, or the next message of a chat: `history` holds the earlier messages as plain
    text (role user or assistant). The portfolio summary goes with the newest question only."""
    question = " ".join(question.split())[:MAX_QUESTION_CHARS]
    agreed = consent.require(session, user_id)
    settings = get_settings()
    budget.check(session, user_id, settings)
    client = client or default_client()

    holdings = service.build_holdings(session, user_id)
    context, points, held = build_context(holdings, agreed.anonymise_amounts)
    ctx = tools.ToolContext(session, user_id, agreed.anonymise_amounts, holdings)
    messages: list[dict[str, Any]] = [
        {"role": m["role"], "content": m["content"][:MAX_HISTORY_CHARS]}
        for m in (history or [])[-MAX_HISTORY:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    # The API wants the first message from the user and the roles to alternate.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    # Two messages of one role in a row (a turn that was never recorded): the later one is kept.
    merged: list[dict[str, Any]] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = m
        else:
            merged.append(m)
    messages = merged
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    messages.append(
        {"role": "user", "content": f"Portfolio summary:\n{context}\n\nQuestion: {question}"}
    )
    traces: list[tools.ToolTrace] = []
    saved: list[AiPick] = []
    models: set[str] = set()
    answer = ""
    notes: list[str] = []
    followed_up = False
    failed = 0  # picks the model got wrong on the last turn, with no turn left to fix them
    asked_tools = False  # the last reply was a tool call: if the loop ends on it, no answer came

    for _turn in range(MAX_TURNS + 1):
        # No transaction stays open while the model thinks: the connection goes back to the pool.
        session.commit()
        if _turn > 0:  # every paid call is checked, not only the first
            try:
                budget.check(session, user_id, settings)
            except budget.BudgetExceeded:
                notes.append("The AI budget ran out, so the answer stops here.")
                _warn_if_unlogged(answer, saved, notes)
                asked_tools = False  # the stop has its own note
                break
        try:
            reply = client.send(
                system=SYSTEM, messages=messages, tools=[picks.TOOL, *tools.SCHEMAS]
            )
        except LlmError:
            if _turn == 0:
                raise
            # The earlier calls were paid for and their picks are stored: keep what we have.
            notes.append("The AI service failed part way, so the answer may be incomplete.")
            _warn_if_unlogged(answer, saved, notes)
            asked_tools = False
            break
        models.add(reply.model)
        usage = budget.record(session, user_id, "ask", reply, settings)
        if reply.refused:
            declined = _result(
                "The AI declined to answer this question.", saved, points, models, settings, True
            )
            declined.notes.extend(notes)
            declined.tool_calls = traces
            return declined
        if reply.stop_reason == "max_tokens":
            notes.append("The answer was cut off at the length limit.")
        # A reply to the follow-up, or a short "Recorded." after the tool results, is not the
        # answer. A later reply of some substance is (it may correct an earlier draft); a shorter
        # one does not replace a longer text.
        if not followed_up and (len(reply.text) >= SUBSTANTIAL or len(reply.text) > len(answer)):
            answer = reply.text
        if reply.tool_calls:
            results, failed = _handle_tools(
                session,
                user_id,
                reply,
                question=question,
                held=held,
                usage_id=usage.id,
                saved=saved,
                ctx=ctx,
                traces=traces,
            )
            messages.append({"role": "assistant", "content": reply.raw_content})
            if _turn == MAX_TURNS - 1:  # the next call is the last: ask for the answer now
                results.append(
                    {
                        "type": "text",
                        "text": "That was the last round of tool calls. Answer now, from what "
                        "you have. Do not call more tools.",
                    }
                )
            messages.append({"role": "user", "content": results})
            asked_tools = True
            continue
        asked_tools = False
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
    if asked_tools:
        notes.append("The AI used all its tool turns and gave no final answer. Ask again, shorter.")
    result = _result(answer or "The AI gave no answer.", saved, points, models, settings, False)
    result.notes.extend(notes)
    result.tool_calls = traces
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

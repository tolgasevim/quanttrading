"""The AI layer: status and consent, a question box, and the pick log (FR-4, FR-52, FR-53,
FR-56, FR-57). Guidance only: nothing here places a trade.

Every query takes the user's own session (row-level security) and also filters by `user_id`.
"""

import importlib.util
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from quant.ai import budget, consent, scoring
from quant.ai import service as ai_service
from quant.ai.client import LlmClient, LlmError, default_client
from quant.api.deps import CurrentUser, UserDb
from quant.config import get_settings
from quant.models import AiPick, AiPickScore, Role

router = APIRouter(prefix="/api/ai", tags=["ai"])

MAX_LIMIT = 200


def llm_client() -> LlmClient:
    try:
        return default_client()
    except LlmError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


Llm = Annotated[LlmClient, Depends(llm_client)]


class BudgetOut(BaseModel):
    month: str
    user_eur: Decimal
    user_cap_eur: Decimal
    total_eur: Decimal | None  # admins only
    total_cap_eur: Decimal | None


class StatusOut(BaseModel):
    configured: bool
    accepted: bool
    anonymise_amounts: bool
    disclaimer: str
    disclaimer_version: int
    label: str
    budget: BudgetOut


class ConsentIn(BaseModel):
    accept: bool
    anonymise_amounts: bool = False


class AskIn(BaseModel):
    question: str = Field(max_length=ai_service.MAX_QUESTION_CHARS)

    @field_validator("question")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        value = " ".join(value.split())  # the same normalising the service does
        if len(value) < 3:
            raise ValueError("the question is too short")
        return value


class PickOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    question: str
    name: str
    isin: str | None
    ticker: str | None
    direction: str
    horizon_months: int
    rationale: str
    held: bool
    price: Decimal | None
    price_currency: str | None
    price_date: date | None


class AskOut(BaseModel):
    answer: str
    label: str
    model: str
    fallback_used: bool
    refused: bool
    picks: list[PickOut]
    data_points: list[str]
    notes: list[str]


def _pick(p: AiPick) -> PickOut:
    return PickOut(
        id=p.id,
        created_at=p.created_at,
        question=p.question,
        name=p.name,
        isin=p.isin,
        ticker=p.ticker,
        direction=p.direction,
        horizon_months=p.horizon_months,
        rationale=p.rationale,
        held=p.held,
        price=p.price,
        price_currency=p.price_currency,
        price_date=p.price_date,
    )


def _budget(db: UserDb, user: CurrentUser) -> BudgetOut:
    admin = user.role == Role.ADMIN
    spend = budget.summary(db, user.id, include_total=admin)
    return BudgetOut(
        month=spend.month,
        user_eur=spend.user_eur.quantize(Decimal("0.01")),
        user_cap_eur=spend.user_cap_eur,
        total_eur=spend.total_eur.quantize(Decimal("0.01"))
        if spend.total_eur is not None
        else None,
        total_cap_eur=spend.total_cap_eur if admin else None,
    )


@router.get("/status", response_model=StatusOut)
def get_status(db: UserDb, user: CurrentUser) -> StatusOut:
    agreed = consent.get(db, user.id)
    key = bool(get_settings().anthropic_api_key)
    return StatusOut(
        configured=key and importlib.util.find_spec("anthropic") is not None,
        accepted=agreed is not None,
        anonymise_amounts=agreed.anonymise_amounts if agreed else False,
        disclaimer=consent.DISCLAIMER,
        disclaimer_version=consent.DISCLAIMER_VERSION,
        label=consent.LABEL,
        budget=_budget(db, user),
    )


@router.post("/consent", response_model=StatusOut)
def post_consent(body: ConsentIn, db: UserDb, user: CurrentUser) -> StatusOut:
    if not body.accept:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the disclaimer must be accepted")
    consent.accept(db, user.id, body.anonymise_amounts)
    return get_status(db, user)


@router.delete("/consent", response_model=StatusOut)
def delete_consent(db: UserDb, user: CurrentUser) -> StatusOut:
    consent.withdraw(db, user.id)
    return get_status(db, user)


@router.post("/ask", response_model=AskOut)
def post_ask(body: AskIn, db: UserDb, user: CurrentUser, client: Llm) -> AskOut:
    try:
        result = ai_service.ask(db, user.id, body.question, client)
    except consent.ConsentRequired as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except budget.BudgetExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except LlmError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return AskOut(
        answer=result.answer,
        label=result.label,
        model=result.model,
        fallback_used=result.fallback_used,
        refused=result.refused,
        picks=[_pick(p) for p in result.picks],
        data_points=result.data_points,
        notes=result.notes,
    )


@router.get("/picks", response_model=list[PickOut])
def list_picks(
    db: UserDb, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100
) -> list[PickOut]:
    rows = db.scalars(
        select(AiPick)
        .where(AiPick.user_id == user.id)
        .order_by(AiPick.created_at.desc(), AiPick.id)
        .limit(limit)
    ).all()
    return [_pick(p) for p in rows]


class ScoreOut(BaseModel):
    window_months: int
    end_date: date
    pick_return_pct: Decimal
    benchmark_return_pct: Decimal
    excess_pct: Decimal
    hit: bool


class ScoredPickOut(BaseModel):
    pick: PickOut
    scores: list[ScoreOut]


class WindowSummary(BaseModel):
    window_months: int
    scored: int
    hits: int
    hit_rate_pct: Decimal | None
    avg_pick_return_pct: Decimal | None
    avg_benchmark_return_pct: Decimal | None
    avg_excess_pct: Decimal | None


class TrackRecordOut(BaseModel):
    benchmark: str
    picks: int
    unscored: int  # picks with no score yet (too young, or no prices)
    windows: list[WindowSummary]
    items: list[ScoredPickOut]


def _avg(values: list[Decimal]) -> Decimal | None:
    return (sum(values, Decimal(0)) / len(values)).quantize(Decimal("0.01")) if values else None


@router.get("/track-record", response_model=TrackRecordOut)
def track_record(
    db: UserDb, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100
) -> TrackRecordOut:
    """How the user's own AI picks did against the benchmark (FR-52)."""
    total = db.scalars(select(AiPick.id).where(AiPick.user_id == user.id)).all()
    scores = db.scalars(
        select(AiPickScore)
        .where(AiPickScore.user_id == user.id)
        .order_by(AiPickScore.window_months)
    ).all()
    by_pick: dict[uuid.UUID, list[AiPickScore]] = {}
    for score in scores:
        by_pick.setdefault(score.pick_id, []).append(score)
    windows = []
    for months in scoring.WINDOWS:
        rows = [s for s in scores if s.window_months == months]
        windows.append(
            WindowSummary(
                window_months=months,
                scored=len(rows),
                hits=sum(1 for s in rows if s.hit),
                hit_rate_pct=(
                    (Decimal(sum(1 for s in rows if s.hit)) * 100 / len(rows)).quantize(
                        Decimal("0.1")
                    )
                    if rows
                    else None
                ),
                avg_pick_return_pct=_avg([s.pick_return_pct for s in rows]),
                avg_benchmark_return_pct=_avg([s.benchmark_return_pct for s in rows]),
                avg_excess_pct=_avg([s.excess_pct for s in rows]),
            )
        )
    picks = db.scalars(
        select(AiPick)
        .where(AiPick.user_id == user.id)
        .order_by(AiPick.created_at.desc(), AiPick.id)
        .limit(limit)
    ).all()
    return TrackRecordOut(
        benchmark=scoring.BENCHMARK_CODE,
        picks=len(total),
        unscored=len([i for i in total if i not in by_pick]),
        windows=windows,
        items=[
            ScoredPickOut(
                pick=_pick(p),
                scores=[
                    ScoreOut(
                        window_months=s.window_months,
                        end_date=s.end_date,
                        pick_return_pct=s.pick_return_pct,
                        benchmark_return_pct=s.benchmark_return_pct,
                        excess_pct=s.excess_pct,
                        hit=s.hit,
                    )
                    for s in by_pick.get(p.id, [])
                ],
            )
            for p in picks
        ],
    )

"""Target allocation, drift, guardrails, rebalancing, the deployment plan and the decision
journal (FR-40 to FR-44, FR-49)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from quant.api.deps import CurrentUser, UserDb
from quant.models import JournalEntry
from quant.portfolio import allocation, journal

router = APIRouter(prefix="/api", tags=["allocation"])
PCT = Decimal("0.01")


class BandIn(BaseModel):
    target: Decimal = Field(ge=0, le=100)
    low: Decimal = Field(ge=0, le=100)
    high: Decimal = Field(ge=0, le=100)


class TargetsIn(BaseModel):
    targets: dict[str, BandIn]


class GuardrailsIn(BaseModel):
    max_single_stock: Decimal | None = Field(None, ge=0, le=100)
    max_sector: Decimal | None = Field(None, ge=0, le=100)
    max_country: Decimal | None = Field(None, ge=0, le=100)
    min_cash: Decimal | None = Field(None, ge=0, le=100)
    max_crypto: Decimal | None = Field(None, ge=0, le=100)
    max_trading_book: Decimal | None = Field(None, ge=0, le=100)
    drawdown_alert: Decimal | None = Field(None, ge=0, le=100)


class BucketOut(BaseModel):
    bucket: str
    label: str
    value: Decimal
    pct: Decimal
    target: Decimal
    low: Decimal
    high: Decimal
    status: str
    gap_pp: Decimal
    gap_eur: Decimal


class RuleOut(BaseModel):
    rule: str
    subject: str
    current_pct: Decimal
    limit_pct: Decimal
    breached: bool
    note: str


class SuggestionOut(BaseModel):
    kind: str
    text: str
    amount: Decimal
    bucket: str
    isin: str | None
    name: str | None
    to_bucket: str | None
    to_isin: str | None
    to_name: str | None
    why: str
    gain_estimate: Decimal | None


class SplitOut(BaseModel):
    bucket: str
    label: str
    amount: Decimal
    instrument: str | None


class TrancheOut(BaseModel):
    number: int
    due: date
    amount: Decimal
    done: bool
    split: list[SplitOut]


class DeploymentOut(BaseModel):
    active: bool
    started: date | None
    tranches: int
    excess: Decimal
    invested_since: Decimal
    plan: list[TrancheOut]
    pull_forward: bool
    pull_forward_note: str


class AllocationOut(BaseModel):
    has_portfolio: bool
    base: Decimal
    free_cash: Decimal
    trading_book: Decimal
    buckets: list[BucketOut]
    rules: list[RuleOut]
    breaches: int
    suggestions: list[SuggestionOut]
    savings_plans: dict[str, Decimal]
    deployment: DeploymentOut | None
    custom_targets: bool
    custom_guardrails: bool
    guardrails: dict[str, Decimal]
    warnings: list[str]


def _q(value: Decimal) -> Decimal:
    return value.quantize(PCT)


def _view(db: UserDb, user: CurrentUser) -> AllocationOut:
    r = allocation.analyse(db, user.id)
    settings = r.settings
    assert settings is not None
    deployment = r.deployment
    g = settings.guardrails
    return AllocationOut(
        has_portfolio=r.has_portfolio,
        base=_q(r.base),
        free_cash=_q(r.free_cash),
        trading_book=_q(r.trading_book),
        buckets=[
            BucketOut(
                bucket=b.bucket,
                label=allocation.LABELS[b.bucket],
                value=_q(b.value),
                pct=_q(b.pct),
                target=b.target,
                low=b.low,
                high=b.high,
                status=b.status,
                gap_pp=_q(b.gap_pp),
                gap_eur=_q(b.gap_eur),
            )
            for b in r.buckets
        ],
        rules=[
            RuleOut(
                rule=x.rule,
                subject=x.subject,
                current_pct=_q(x.current_pct),
                limit_pct=x.limit_pct,
                breached=x.breached,
                note=x.note,
            )
            for x in r.rules
        ],
        breaches=sum(1 for x in r.rules if x.breached),
        suggestions=[
            SuggestionOut(
                kind=s.kind,
                text=s.text,
                amount=_q(s.amount),
                bucket=s.bucket,
                isin=s.isin,
                name=s.name,
                to_bucket=s.to_bucket,
                to_isin=s.to_isin,
                to_name=s.to_name,
                why=s.why,
                gain_estimate=s.gain_estimate,
            )
            for s in r.suggestions
        ],
        savings_plans=r.plans,
        deployment=(
            None
            if deployment is None
            else DeploymentOut(
                active=deployment.active,
                started=deployment.started,
                tranches=deployment.tranches,
                excess=_q(deployment.excess),
                invested_since=_q(deployment.invested_since),
                plan=[
                    TrancheOut(
                        number=t.number,
                        due=t.due,
                        amount=t.amount,
                        done=t.done,
                        split=[
                            SplitOut(
                                bucket=b, label=allocation.LABELS[b], amount=a, instrument=name
                            )
                            for b, a, name in t.split
                        ],
                    )
                    for t in deployment.plan
                ],
                pull_forward=deployment.pull_forward,
                pull_forward_note=deployment.pull_forward_note,
            )
        ),
        custom_targets=settings.custom_targets,
        custom_guardrails=settings.custom_guardrails,
        guardrails={n: getattr(g, n) for n in allocation.GUARDRAIL_FIELDS},
        warnings=r.warnings,
    )


@router.get("/allocation", response_model=AllocationOut)
def get_allocation(user: CurrentUser, db: UserDb) -> AllocationOut:
    return _view(db, user)


@router.put("/allocation/targets", response_model=AllocationOut)
def put_targets(body: TargetsIn, user: CurrentUser, db: UserDb) -> AllocationOut:
    try:
        bands = allocation.validate_targets(
            {b: (v.target, v.low, v.high) for b, v in body.targets.items()}
        )
    except allocation.AllocationError as error:
        raise HTTPException(422, str(error)) from error
    allocation.save_targets(db, user.id, bands)
    return _view(db, user)


@router.delete("/allocation/targets", response_model=AllocationOut)
def reset_targets(user: CurrentUser, db: UserDb) -> AllocationOut:
    allocation.reset_targets(db, user.id)
    return _view(db, user)


@router.put("/allocation/guardrails", response_model=AllocationOut)
def put_guardrails(body: GuardrailsIn, user: CurrentUser, db: UserDb) -> AllocationOut:
    sent = {k: v for k, v in body.model_dump().items() if v is not None}
    current = allocation.load_settings(db, user.id).guardrails
    merged = {n: getattr(current, n) for n in allocation.GUARDRAIL_FIELDS} | sent
    try:
        guardrails = allocation.validate_guardrails(merged)
    except allocation.AllocationError as error:
        raise HTTPException(422, str(error)) from error
    allocation.save_guardrails(db, user.id, guardrails)
    return _view(db, user)


class DeploymentIn(BaseModel):
    tranches: int = Field(allocation.DEFAULT_TRANCHES, ge=1, le=allocation.MAX_TRANCHES)


@router.post("/allocation/deployment", response_model=AllocationOut, status_code=201)
def start_deployment(body: DeploymentIn, user: CurrentUser, db: UserDb) -> AllocationOut:
    try:
        allocation.start_deployment(db, user.id, body.tranches)
    except allocation.AllocationError as error:
        raise HTTPException(422, str(error)) from error
    return _view(db, user)


@router.delete("/allocation/deployment", response_model=AllocationOut)
def cancel_deployment(user: CurrentUser, db: UserDb) -> AllocationOut:
    allocation.cancel_deployment(db, user.id)
    return _view(db, user)


# --- the decision journal --------------------------------------------------------------------


class JournalIn(BaseModel):
    action: Literal["bought", "sold", "ignored"]
    name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    isin: str | None = Field(None, max_length=20)
    guidance: str | None = Field(None, max_length=200)
    pick_id: uuid.UUID | None = None


class JournalOut(BaseModel):
    id: uuid.UUID
    at: datetime
    action: str
    name: str
    isin: str | None
    reason: str
    guidance: str | None
    pick_id: uuid.UUID | None
    price: Decimal | None
    price_currency: str | None
    since_pct: Decimal | None
    guidance_right: bool | None
    verdict: str


class JournalListOut(BaseModel):
    entries: list[JournalOut]
    counts: dict[str, int]
    judged: int  # entries tied to a pick with a price move to judge
    right: int  # of those, how many the pick got right


def _entry(db: UserDb, e: JournalEntry) -> JournalOut:
    o = journal.outcome(db, e)
    return JournalOut(
        id=e.id,
        at=e.at,
        action=e.action,
        name=e.name,
        isin=e.isin,
        reason=e.reason,
        guidance=e.guidance,
        pick_id=e.pick_id,
        price=e.price,
        price_currency=e.price_currency,
        since_pct=o.since_pct,
        guidance_right=o.guidance_right,
        verdict=o.verdict,
    )


@router.get("/journal", response_model=JournalListOut)
def list_journal(user: CurrentUser, db: UserDb) -> JournalListOut:
    items = [_entry(db, e) for e in journal.entries(db, user.id)]
    counts = {a: sum(1 for i in items if i.action == a) for a in journal.ACTIONS}
    judged = [i for i in items if i.guidance_right is not None]
    return JournalListOut(
        entries=items,
        counts=counts,
        judged=len(judged),
        right=sum(1 for i in judged if i.guidance_right),
    )


@router.post("/journal", response_model=JournalOut, status_code=201)
def add_journal(body: JournalIn, user: CurrentUser, db: UserDb) -> JournalOut:
    try:
        entry = journal.add_entry(
            db,
            user.id,
            action=body.action,
            name=body.name,
            reason=body.reason,
            isin=body.isin,
            guidance=body.guidance,
            pick_id=body.pick_id,
        )
    except journal.JournalError as error:
        raise HTTPException(422, str(error)) from error
    return _entry(db, entry)


@router.delete("/journal/{entry_id}", status_code=204)
def delete_journal(entry_id: uuid.UUID, user: CurrentUser, db: UserDb) -> Response:
    entry = db.scalar(
        select(JournalEntry).where(JournalEntry.id == entry_id, JournalEntry.user_id == user.id)
    )
    if entry is None:
        raise HTTPException(404, "no such entry")
    db.delete(entry)
    db.commit()
    return Response(status_code=204)

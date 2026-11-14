"""Tech sub-groups, tags, consolidation, redundancy and the trading book
(FR-45 to FR-48)."""

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from quant.api.deps import CurrentUser, UserDb
from quant.portfolio import (
    allocation,
    book,
    cash,
    consolidation,
    lookthrough,
    service,
    subgroups,
    trading,
)

router = APIRouter(prefix="/api/book", tags=["book"])
PCT = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(PCT)


def _pct(value: Decimal, base: Decimal) -> Decimal:
    return _q(value / base * 100) if base > 0 else Decimal("0.00")


# --- tags ---


class TagIn(BaseModel):
    """Only the fields that are sent change; a field sent as null is cleared."""

    subgroup: str | None = None
    pinned: bool | None = None
    tracker_until: date | None = None
    short_term: bool | None = None
    stop_price: Decimal | None = None
    barrier: Decimal | None = None
    barrier_isin: str | None = Field(None, max_length=20)
    barrier_long: bool | None = None
    note: str | None = Field(None, max_length=200)


class TagOut(BaseModel):
    isin: str
    subgroup: str | None
    pinned: bool
    tracker_until: date | None
    short_term: bool
    stop_price: Decimal | None
    barrier: Decimal | None
    barrier_isin: str | None
    barrier_long: bool | None
    note: str | None


def _tag_out(t: book.Tag) -> TagOut:
    return TagOut(**t.__dict__)


@router.get("/tags", response_model=list[TagOut])
def list_tags(user: CurrentUser, db: UserDb) -> list[TagOut]:
    return [_tag_out(t) for t in book.load_tags(db, user.id).values()]


@router.put("/tags/{isin}", response_model=TagOut)
def put_tag(isin: str, body: TagIn, user: CurrentUser, db: UserDb) -> TagOut:
    try:
        tag = book.save_tag(db, user.id, isin, body.model_dump(exclude_unset=True), service.today())
    except book.BookError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return _tag_out(tag)


@router.delete("/tags/{isin}", status_code=204)
def delete_tag(isin: str, user: CurrentUser, db: UserDb) -> Response:
    if not book.delete_tag(db, user.id, isin):
        raise HTTPException(404, "no such tag")
    return Response(status_code=204)


# --- tech sub-groups (FR-47) ---


class MemberOut(BaseModel):
    isin: str
    name: str
    value: Decimal
    overridden: bool


class SubgroupOut(BaseModel):
    key: str
    label: str
    value: Decimal
    pct: Decimal
    pct_of_tech: Decimal
    target: Decimal | None
    cap: Decimal | None
    breached: bool
    members: list[MemberOut]


class StockOut(BaseModel):
    isin: str
    name: str
    subgroup: str
    label: str
    auto: str
    auto_label: str
    overridden: bool
    value: Decimal


class SubgroupsOut(BaseModel):
    base: Decimal
    groups: list[SubgroupOut]
    non_tech: Decimal
    non_tech_pct: Decimal
    stocks: list[StockOut]
    labels: dict[str, str]


@router.get("/subgroups", response_model=SubgroupsOut)
def get_subgroups(user: CurrentUser, db: UserDb) -> SubgroupsOut:
    r = allocation.analyse(db, user.id)
    return SubgroupsOut(
        base=_q(r.base),
        groups=[
            SubgroupOut(
                key=g.key,
                label=g.label,
                value=_q(g.value),
                pct=_q(g.pct),
                pct_of_tech=_q(g.pct_of_tech),
                target=g.target,
                cap=g.cap,
                breached=g.breached,
                members=[
                    MemberOut(isin=i, name=n, value=_q(v), overridden=o) for i, n, v, o in g.members
                ],
            )
            for g in r.subgroups
        ],
        non_tech=_q(r.non_tech),
        non_tech_pct=_pct(r.non_tech, r.base),
        stocks=[
            StockOut(
                isin=i,
                name=n,
                subgroup=g,
                label=subgroups.LABELS[g],
                auto=a,
                auto_label=subgroups.LABELS[a],
                overridden=o,
                value=_q(v),
            )
            for i, n, g, a, o, v in sorted(r.stocks, key=lambda s: s[5], reverse=True)
        ],
        labels=subgroups.LABELS,
    )


class LimitIn(BaseModel):
    target: Decimal | None = Field(None, ge=0, le=100)
    cap: Decimal | None = Field(None, ge=0, le=100)


class LimitsIn(BaseModel):
    limits: dict[str, LimitIn]


@router.put("/subgroups/limits", response_model=SubgroupsOut)
def put_limits(body: LimitsIn, user: CurrentUser, db: UserDb) -> SubgroupsOut:
    try:
        book.save_subgroup_limits(
            db,
            user.id,
            {g: book.SubgroupLimit(v.target, v.cap) for g, v in body.limits.items()},
        )
    except book.BookError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return get_subgroups(user, db)


# --- consolidation and redundancy (FR-45, FR-46) ---


class SettingsIn(BaseModel):
    min_positions: int | None = Field(None, ge=1, le=1000)
    max_positions: int | None = Field(None, ge=1, le=1000)
    weekly_prompts: int | None = Field(None, ge=0, le=20)
    hold_days: int | None = Field(None, ge=1, le=365)
    barrier_pct: Decimal | None = Field(None, gt=0, le=100)


class SettingsOut(BaseModel):
    min_positions: int
    max_positions: int
    weekly_prompts: int
    hold_days: int
    barrier_pct: Decimal


class StatusOut(BaseModel):
    isin: str
    name: str
    type: str
    value: Decimal | None
    pct: Decimal | None
    status: str
    why: str
    suggestion: str
    gain_estimate: Decimal | None


class PromptOut(BaseModel):
    isin: str
    name: str
    kind: str
    text: str


class OverlapOut(BaseModel):
    a_isin: str
    a_name: str
    b_isin: str
    b_name: str
    overlap_pct: Decimal | None
    basis: str
    intentional: bool
    reconfirm: bool
    suggestion: str


class ConsolidationOut(BaseModel):
    direct: int
    look_through: int
    min_positions: int
    max_positions: int
    state: str
    small_pct: Decimal
    small: list[StatusOut]
    overlaps: list[OverlapOut]
    prompts: list[PromptOut]
    held_back: int
    notes: list[str]
    settings: SettingsOut


def _settings_out(s: book.BookSettings) -> SettingsOut:
    return SettingsOut(
        min_positions=s.min_positions,
        max_positions=s.max_positions,
        weekly_prompts=s.weekly_prompts,
        hold_days=s.hold_days,
        barrier_pct=s.barrier_pct,
    )


def _consolidation(db: UserDb, user: CurrentUser) -> ConsolidationOut:
    held = service.build_holdings(db, user.id)
    conc = lookthrough.concentration(db, held)
    tags = book.load_tags(db, user.id)
    settings = book.load_book_settings(db, user.id)
    plans = allocation.savings_plans(db, user.id, service.today())
    c = consolidation.analyse(db, held, conc, tags, settings, plans, service.today())
    return ConsolidationOut(
        direct=c.direct,
        look_through=c.look_through,
        min_positions=c.min_positions,
        max_positions=c.max_positions,
        state=c.state,
        small_pct=c.small_pct,
        small=[
            StatusOut(
                isin=s.isin,
                name=s.name,
                type=s.type,
                value=None if s.value is None else _q(s.value),
                pct=None if s.pct is None else _q(s.pct),
                status=s.status,
                why=s.why,
                suggestion=s.suggestion,
                gain_estimate=s.gain_estimate,
            )
            for s in c.small
        ],
        overlaps=[
            OverlapOut(
                a_isin=o.a_isin,
                a_name=o.a_name,
                b_isin=o.b_isin,
                b_name=o.b_name,
                overlap_pct=None if o.overlap_pct is None else _q(o.overlap_pct),
                basis=o.basis,
                intentional=o.intentional,
                reconfirm=o.reconfirm,
                suggestion=o.suggestion,
            )
            for o in c.overlaps
        ],
        prompts=[PromptOut(isin=p.isin, name=p.name, kind=p.kind, text=p.text) for p in c.prompts],
        held_back=c.held_back,
        notes=c.notes,
        settings=_settings_out(settings),
    )


@router.get("/consolidation", response_model=ConsolidationOut)
def get_consolidation(user: CurrentUser, db: UserDb) -> ConsolidationOut:
    return _consolidation(db, user)


@router.put("/settings", response_model=ConsolidationOut)
def put_settings(body: SettingsIn, user: CurrentUser, db: UserDb) -> ConsolidationOut:
    try:
        book.change_book_settings(db, user.id, body.model_dump(exclude_none=True))
    except book.BookError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return _consolidation(db, user)


class OverlapIn(BaseModel):
    a: str = Field(min_length=1, max_length=20)
    b: str = Field(min_length=1, max_length=20)
    intentional: bool = True


@router.post("/overlaps", response_model=ConsolidationOut)
def mark_overlap(body: OverlapIn, user: CurrentUser, db: UserDb) -> ConsolidationOut:
    try:
        book.set_intentional(db, user.id, body.a, body.b, body.intentional, service.today())
    except book.BookError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    return _consolidation(db, user)


# --- the trading book (FR-48) ---


class AlertOut(BaseModel):
    code: str
    text: str


class OpenTradeOut(BaseModel):
    isin: str
    name: str
    type: str
    value: Decimal
    days_held: int | None
    price: Decimal | None
    stop_price: Decimal | None
    barrier_distance_pct: Decimal | None
    alerts: list[AlertOut]
    flags: list[str]


class StatsOut(BaseModel):
    closed: int
    wins: int
    hit_rate: Decimal | None
    expired: int
    pnl: Decimal
    avg_days: Decimal | None
    by_year: dict[int, Decimal]


class TradingOut(BaseModel):
    value: Decimal
    pct: Decimal  # of everything held
    cap: Decimal
    over_cap: bool
    open: list[OpenTradeOut]
    stats: StatsOut
    needs_thesis: list[tuple[str, str]]
    tax_note: str
    settings: SettingsOut
    generated_at: datetime


@router.get("/trading", response_model=TradingOut)
def get_trading(user: CurrentUser, db: UserDb) -> TradingOut:
    held = service.build_holdings(db, user.id)
    conc = lookthrough.concentration(db, held)
    tags = book.load_tags(db, user.id)
    settings = book.load_book_settings(db, user.id)
    t = trading.analyse(db, user.id, held, conc, tags, settings, service.today())
    guard = allocation.load_settings(db, user.id).guardrails
    free_cash = cash.cash_status(db, user.id).investable
    whole = conc.total + free_cash
    pct = _pct(t.value, whole)
    return TradingOut(
        value=_q(t.value),
        pct=pct,
        cap=guard.max_trading_book,
        over_cap=pct > guard.max_trading_book,
        open=[
            OpenTradeOut(
                isin=o.isin,
                name=o.name,
                type=o.type,
                value=_q(o.value),
                days_held=o.days_held,
                price=None if o.price is None else _q(o.price),
                stop_price=o.stop_price,
                barrier_distance_pct=None
                if o.barrier_distance_pct is None
                else _q(o.barrier_distance_pct),
                alerts=[AlertOut(code=a.code, text=a.text) for a in o.alerts],
                flags=o.flags,
            )
            for o in t.open
        ],
        stats=StatsOut(
            closed=t.stats.closed,
            wins=t.stats.wins,
            hit_rate=None if t.stats.hit_rate is None else _q(t.stats.hit_rate),
            expired=t.stats.expired,
            pnl=_q(t.stats.pnl),
            avg_days=None
            if t.stats.avg_days is None
            else t.stats.avg_days.quantize(Decimal("0.1")),
            by_year={y: _q(v) for y, v in sorted(t.stats.by_year.items())},
        ),
        needs_thesis=t.needs_thesis,
        tax_note=t.tax_note,
        settings=_settings_out(settings),
        generated_at=datetime.now().astimezone(),
    )

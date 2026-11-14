"""Factor scores, the universe, the watchlist and the fund scorecards (FR-30 to FR-35).

Scores, fundamentals and fund facts are public market data shared by all users; only admins
load them. The watchlist is per user.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from quant.api.deps import AdminUser, CurrentUser, UserDb
from quant.ingest.mapping import valid_isin
from quant.models import (
    FactorScore,
    Fundamental,
    Instrument,
    InstrumentFact,
    UniverseMember,
    WatchlistItem,
)
from quant.portfolio import (
    allocation,
    book,
    consolidation,
    factors,
    instruments,
    lookthrough,
    scorecards,
    service,
)

router = APIRouter(prefix="/api/scores", tags=["scores"])

ISIN = Field(min_length=12, max_length=12, pattern=r"^[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]$")
NOTE = (
    "Information only. A score ranks a share against its sector peers on past data: it is not a "
    "forecast, and the app places no trade. Read the data-quality flag before you trust one."
)


class ScoreOut(BaseModel):
    isin: str
    name: str
    sector: str | None
    day: date
    value: Decimal | None
    quality: Decimal | None
    growth: Decimal | None
    momentum: Decimal | None
    low_vol: Decimal | None
    composite: Decimal | None
    composite_pct: Decimal | None
    data_quality: str
    flags: list[str]
    flag_text: list[str]
    peers: int
    held: bool
    watched: bool


class ScoresOut(BaseModel):
    day: date | None
    scores: list[ScoreOut]
    weights: dict[str, int]
    note: str


class HistoryPoint(BaseModel):
    day: date
    value: Decimal | None
    quality: Decimal | None
    growth: Decimal | None
    momentum: Decimal | None
    low_vol: Decimal | None
    composite: Decimal | None


class DetailOut(BaseModel):
    score: ScoreOut | None
    history: list[HistoryPoint]
    trends: list[str]
    note: str


class WatchIn(BaseModel):
    isin: str = ISIN
    note: str | None = Field(default=None, max_length=200)


class WatchOut(BaseModel):
    isin: str
    note: str | None
    name: str | None
    has_score: bool


def _score_out(
    row: FactorScore, inst: Instrument | None, held: set[str], watched: set[str]
) -> ScoreOut:
    return ScoreOut(
        isin=row.isin,
        name=inst.name if inst else row.isin,
        sector=inst.sector if inst else None,
        day=row.day,
        value=row.value,
        quality=row.quality,
        growth=row.growth,
        momentum=row.momentum,
        low_vol=row.low_vol,
        composite=row.composite,
        composite_pct=row.composite_pct,
        data_quality=row.data_quality,
        flags=list(row.flags or []),
        flag_text=[factors.FLAG_TEXT.get(f, f) for f in row.flags or []],
        peers=row.peers,
        held=row.isin in held,
        watched=row.isin in watched,
    )


def _held(db: UserDb, user: CurrentUser) -> set[str]:
    return {p.isin for p in service.build_holdings(db, user.id).positions}


def _watched(db: UserDb, user: CurrentUser) -> set[str]:
    return set(db.scalars(select(WatchlistItem.isin).where(WatchlistItem.user_id == user.id)))


# --- the watchlist (per user) ---------------------------------------------------------------


@router.get("/watchlist", response_model=list[WatchOut])
def get_watchlist(user: CurrentUser, db: UserDb) -> list[WatchOut]:
    items = db.scalars(
        select(WatchlistItem).where(WatchlistItem.user_id == user.id).order_by(WatchlistItem.isin)
    ).all()
    names = {
        i.isin: i.name
        for i in db.scalars(select(Instrument).where(Instrument.isin.in_([w.isin for w in items])))
    }
    scored = set(
        db.scalars(select(FactorScore.isin).where(FactorScore.isin.in_([w.isin for w in items])))
    )
    return [
        WatchOut(isin=w.isin, note=w.note, name=names.get(w.isin), has_score=w.isin in scored)
        for w in items
    ]


@router.post("/watchlist", response_model=WatchOut, status_code=201)
def add_watch(body: WatchIn, user: CurrentUser, db: UserDb) -> WatchOut:
    isin = body.isin.upper()
    if not valid_isin(isin):
        raise HTTPException(422, "that is not a valid ISIN (the check digit does not match)")
    item = db.get(WatchlistItem, (user.id, isin))
    if item is None:
        item = WatchlistItem(user_id=user.id, isin=isin)
    item.note = body.note
    db.add(item)
    db.commit()
    inst = db.scalar(select(Instrument).where(Instrument.isin == isin))
    scored = db.scalar(
        select(func.count()).select_from(FactorScore).where(FactorScore.isin == isin)
    )
    return WatchOut(
        isin=isin, note=item.note, name=inst.name if inst else None, has_score=bool(scored)
    )


@router.delete("/watchlist/{isin}", status_code=204)
def remove_watch(isin: str, user: CurrentUser, db: UserDb) -> Response:
    db.execute(
        delete(WatchlistItem).where(
            WatchlistItem.user_id == user.id, WatchlistItem.isin == isin.upper()
        )
    )
    db.commit()
    return Response(status_code=204)


# --- the fund scorecards (FR-32, FR-33) ------------------------------------------------------


class LineOut(BaseModel):
    label: str
    value: str
    verdict: str
    note: str


class CardOut(BaseModel):
    isin: str
    name: str
    kind: str
    held: bool
    lines: list[LineOut]
    missing: list[str]
    overlaps: list[str]
    tax_note: str


class FactIn(BaseModel):
    kind: Literal["etf", "etc"]
    ter: Decimal | None = Field(default=None, ge=0, le=10)
    fund_size_eur: Decimal | None = Field(default=None, ge=0)
    replication: Literal["physical", "sampling", "synthetic", "futures"] | None = None
    distribution: Literal["acc", "dist"] | None = None
    domicile: str | None = Field(default=None, min_length=2, max_length=2)
    tracking_difference: Decimal | None = Field(default=None, ge=-10, le=10)
    issuer: str | None = Field(default=None, max_length=100)
    delivery_claim: bool | None = None
    roll_method: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=300)


@router.get("/funds", response_model=list[CardOut])
def get_scorecards(user: CurrentUser, db: UserDb) -> list[CardOut]:
    """A card for every fund and ETC the user holds, and for every one with facts entered."""
    held = service.build_holdings(db, user.id)
    wanted: dict[str, tuple[str, str]] = {}  # isin -> name, kind
    held_isins = {p.isin for p in held.positions}
    for p in held.positions:
        t = instruments.classify(p.asset_class, p.name, p.isin).type
        if t in (instruments.ETF, instruments.ETC):
            wanted[p.isin] = (p.name or p.isin, t)
    facts = {f.isin: f for f in db.scalars(select(InstrumentFact))}
    names = {
        i.isin: i.name
        for i in db.scalars(select(Instrument).where(Instrument.isin.in_(list(facts))))
    }
    for isin, f in facts.items():
        wanted.setdefault(isin, (names.get(isin, isin), f.kind))
    overlaps: dict[str, list[str]] = {}
    held_funds = [i for i, (_, k) in wanted.items() if k == instruments.ETF and i in held_isins]
    if len(held_funds) >= 2:  # overlap needs two funds the user holds
        conc = lookthrough.concentration(db, held)
        settings = book.load_book_settings(db, user.id)
        c = consolidation.analyse(
            db,
            held,
            conc,
            book.load_tags(db, user.id),
            settings,
            allocation.savings_plans(db, user.id, service.today()),
            service.today(),
        )
        for o in c.overlaps:
            share = (
                f"{o.overlap_pct:.0f}% of the holdings"
                if o.overlap_pct is not None
                else "the same index"
            )
            overlaps.setdefault(o.a_isin, []).append(f"{o.b_name}: {share}")
            overlaps.setdefault(o.b_isin, []).append(f"{o.a_name}: {share}")
    out: list[CardOut] = []
    for isin, (name, kind) in sorted(wanted.items(), key=lambda kv: kv[1][0].lower()):
        card = scorecards.build(isin, name, kind, facts.get(isin), overlaps.get(isin, []))
        out.append(
            CardOut(
                isin=isin,
                name=name,
                kind=kind,
                held=isin in held_isins,
                lines=[
                    LineOut(label=x.label, value=x.value, verdict=x.verdict, note=x.note)
                    for x in card.lines
                ],
                missing=card.missing,
                overlaps=card.overlaps,
                tax_note=card.tax_note,
            )
        )
    return out


@router.put("/facts/{isin}", status_code=204)
def put_facts(isin: str, body: FactIn, user: AdminUser, db: UserDb) -> Response:
    isin = isin.upper()
    if not valid_isin(isin):
        raise HTTPException(422, "that is not a valid ISIN (the check digit does not match)")
    fact = db.get(InstrumentFact, isin) or InstrumentFact(isin=isin)
    for name, value in body.model_dump().items():
        setattr(fact, name, value.upper() if name == "domicile" and value else value)
    db.add(fact)
    db.commit()
    return Response(status_code=204)


@router.delete("/facts/{isin}", status_code=204)
def delete_facts(isin: str, user: AdminUser, db: UserDb) -> Response:
    db.execute(delete(InstrumentFact).where(InstrumentFact.isin == isin.upper()))
    db.commit()
    return Response(status_code=204)


# --- the universe and the fundamentals (admin) -----------------------------------------------


class MemberIn(BaseModel):
    isin: str = ISIN
    name: str | None = Field(default=None, max_length=200)


class UniverseIn(BaseModel):
    list_name: str = Field(min_length=1, max_length=30)
    members: list[MemberIn] = Field(max_length=2000)


class UniverseOut(BaseModel):
    list_name: str
    members: int
    rejected: list[str]


class FundamentalIn(BaseModel):
    isin: str = ISIN
    as_of: date
    source: str = Field(default="manual", max_length=40)
    ev_ebit: Decimal | None = None
    fcf_yield: Decimal | None = None
    ev_sales: Decimal | None = None
    roic: Decimal | None = None
    gross_margin: Decimal | None = None
    net_debt_ebitda: Decimal | None = None
    revenue_growth: Decimal | None = None
    revenue_cagr_3y: Decimal | None = None
    eps_revision: Decimal | None = None


class FundamentalsIn(BaseModel):
    rows: list[FundamentalIn] = Field(max_length=5000)


class FundamentalsOut(BaseModel):
    stored: int
    rejected: list[str]


@router.get("/universe", response_model=list[UniverseOut])
def get_universe(user: CurrentUser, db: UserDb) -> list[UniverseOut]:
    rows = db.execute(
        select(UniverseMember.list_name, func.count()).group_by(UniverseMember.list_name)
    ).all()
    return [UniverseOut(list_name=n, members=c, rejected=[]) for n, c in sorted(rows)]


@router.post("/universe", response_model=UniverseOut)
def put_universe(body: UniverseIn, user: AdminUser, db: UserDb) -> UniverseOut:
    """Replace one list (FR-30). The lists refresh monthly: an admin loads the new constituents.
    An ISIN with a bad check digit is left out and named in the answer."""
    rejected: list[str] = []
    good: dict[str, str | None] = {}
    for m in body.members:
        isin = m.isin.upper()
        if valid_isin(isin):
            good[isin] = m.name
        else:
            rejected.append(isin)
    db.execute(delete(UniverseMember).where(UniverseMember.list_name == body.list_name))
    for isin, name in good.items():
        db.add(UniverseMember(list_name=body.list_name, isin=isin, name=name))
    db.commit()
    return UniverseOut(list_name=body.list_name, members=len(good), rejected=rejected)


@router.delete("/universe/{list_name}", status_code=204)
def delete_universe(list_name: str, user: AdminUser, db: UserDb) -> Response:
    db.execute(delete(UniverseMember).where(UniverseMember.list_name == list_name))
    db.commit()
    return Response(status_code=204)


@router.post("/fundamentals", response_model=FundamentalsOut)
def put_fundamentals(body: FundamentalsIn, user: AdminUser, db: UserDb) -> FundamentalsOut:
    """Load fundamentals (FR-31). A row for the same share and day replaces the old one."""
    rejected: list[str] = []
    stored = 0
    for r in body.rows:
        isin = r.isin.upper()
        if not valid_isin(isin):
            rejected.append(isin)
            continue
        row = db.scalar(
            select(Fundamental).where(Fundamental.isin == isin, Fundamental.as_of == r.as_of)
        ) or Fundamental(isin=isin, as_of=r.as_of, source=r.source)
        row.source = r.source
        for name in factors.FUNDAMENTAL_FIELDS:
            setattr(row, name, getattr(r, name))
        db.add(row)
        stored += 1
    db.commit()
    return FundamentalsOut(stored=stored, rejected=rejected)


# --- the scores (FR-31, FR-34, FR-35) --------------------------------------------------------


@router.get("", response_model=ScoresOut)
def get_scores(
    user: CurrentUser,
    db: UserDb,
    scope: Literal["all", "held", "watched"] = "all",
) -> ScoresOut:
    day = db.scalar(select(func.max(FactorScore.day)))
    if day is None:
        return ScoresOut(day=None, scores=[], weights=factors.WEIGHTS, note=NOTE)
    held, watched = _held(db, user), _watched(db, user)
    stmt = select(FactorScore).where(FactorScore.day == day)
    if scope == "held":
        stmt = stmt.where(FactorScore.isin.in_(held))
    elif scope == "watched":
        stmt = stmt.where(FactorScore.isin.in_(watched))
    rows = db.scalars(stmt).all()
    insts = {
        i.isin: i
        for i in db.scalars(select(Instrument).where(Instrument.isin.in_([r.isin for r in rows])))
    }
    out = [_score_out(r, insts.get(r.isin), held, watched) for r in rows]
    out.sort(key=lambda s: (s.composite is None, -(s.composite or Decimal(0)), s.name))
    return ScoresOut(day=day, scores=out, weights=factors.WEIGHTS, note=NOTE)


@router.get("/{isin}", response_model=DetailOut)
def get_score(isin: str, user: CurrentUser, db: UserDb, days: int = 180) -> DetailOut:
    isin = isin.upper()
    days = max(30, min(days, 1500))
    latest = factors.latest(db, isin)
    if latest is None:
        raise HTTPException(404, "no score for that share yet")
    held, watched = _held(db, user), _watched(db, user)
    inst = db.scalar(select(Instrument).where(Instrument.isin == isin))
    rows = factors.history(db, isin, days, latest.day)
    return DetailOut(
        score=_score_out(latest, inst, held, watched),
        history=[
            HistoryPoint(
                day=r.day,
                value=r.value,
                quality=r.quality,
                growth=r.growth,
                momentum=r.momentum,
                low_vol=r.low_vol,
                composite=r.composite,
            )
            for r in rows
        ],
        trends=factors.trend_notes(rows),
        note=NOTE,
    )

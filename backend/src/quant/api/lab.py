"""The backtesting lab and the swing scanner (Phase 3 and 4, PRD §6.9).

Information only: a backtest is hypothetical and a signal is not advice. Nothing here places a
trade, and the swing scanner shows only the signal types whose own backtest passes.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from quant.api.deps import CurrentUser, UserDb
from quant.models import FactorScore, Instrument, SwingSignal, WatchlistItem
from quant.portfolio import backtest, instruments, service, swing

router = APIRouter(prefix="/api/lab", tags=["lab"])

MAX_UNIVERSE = 300
BENCHMARKS = ("SXRV", "EUNL", "SXR8")
HUNDRED = Decimal(100)

STRATEGY_TEXT = {
    "buy_hold": "Buy the benchmark once and hold it.",
    "trend": "Hold one instrument while its close is above its 200-day average, else cash. "
    "Checked at the end of each month.",
    "momentum": "Each month, hold the top N shares of a universe by the return from 12 months "
    "ago to one month ago, equal weight.",
    "score": "Each month, hold the top N shares by the composite factor score as it was stored "
    "that day. Needs a stored score history of at least 60 days.",
}


class BacktestIn(BaseModel):
    strategy: Literal["buy_hold", "trend", "momentum", "score"]
    start: date
    end: date
    capital: Decimal = Field(default=Decimal(10000), gt=0, le=Decimal(1_000_000_000))
    fee: Decimal = Field(default=Decimal(1), ge=0, le=100)
    tax_pct: Decimal = Field(default=Decimal("26.375"), ge=0, le=100)
    top_n: int = Field(default=10, ge=1, le=50)
    benchmark: str = Field(default="SXRV", min_length=1, max_length=40)
    universe: Literal["held", "watchlist", "all", "codes"] = "all"
    codes: list[str] = Field(default_factory=list, max_length=MAX_UNIVERSE)
    instrument: str | None = Field(default=None, max_length=40)  # for the trend rule


class StatsOut(BaseModel):
    final_value: Decimal
    total_return_pct: float
    cagr_pct: float | None
    volatility_pct: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float | None
    trades: int
    fees: Decimal
    tax: Decimal
    turnover_pct_year: float | None


class CurvePoint(BaseModel):
    day: date
    strategy: float
    benchmark: float


class BacktestOut(BaseModel):
    strategy: StatsOut
    benchmark: StatsOut
    curve: list[CurvePoint]
    first_day: date
    last_day: date
    universe_size: int
    notes: list[str]
    warnings: list[str]


class LabOut(BaseModel):
    strategies: dict[str, str]
    benchmarks: list[str]
    held: int
    watchlist: int
    universe: int
    score_first_day: date | None
    score_days: int


def _stats(s: backtest.Stats) -> StatsOut:
    return StatsOut(**s.__dict__)


def _held_codes(db: UserDb, user: CurrentUser) -> list[str]:
    isins = {
        p.isin
        for p in service.build_holdings(db, user.id).positions
        if instruments.classify(p.asset_class, p.name, p.isin).type == instruments.STOCK
    }
    return _codes_of(db, isins)


def _watch_codes(db: UserDb, user: CurrentUser) -> list[str]:
    isins = set(db.scalars(select(WatchlistItem.isin).where(WatchlistItem.user_id == user.id)))
    return _codes_of(db, isins)


def _codes_of(db: UserDb, isins: set[str]) -> list[str]:
    if not isins:
        return []
    return sorted(db.scalars(select(Instrument.code).where(Instrument.isin.in_(isins))))


def _all_codes(db: UserDb) -> list[str]:
    return list(
        db.scalars(
            select(Instrument.code)
            .where(Instrument.asset_class == "stock", Instrument.active.is_(True))
            .order_by(Instrument.code)
            .limit(MAX_UNIVERSE)
        )
    )


@router.get("", response_model=LabOut)
def get_lab(user: CurrentUser, db: UserDb) -> LabOut:
    first, last = db.execute(select(func.min(FactorScore.day), func.max(FactorScore.day))).one()
    return LabOut(
        strategies=STRATEGY_TEXT,
        benchmarks=[
            c for c in db.scalars(select(Instrument.code).where(Instrument.code.in_(BENCHMARKS)))
        ],
        held=len(_held_codes(db, user)),
        watchlist=len(_watch_codes(db, user)),
        universe=len(_all_codes(db)),
        score_first_day=first,
        score_days=(last - first).days if first and last else 0,
    )


@router.post("/backtest", response_model=BacktestOut)
def run(body: BacktestIn, user: CurrentUser, db: UserDb) -> BacktestOut:
    if body.strategy == "trend":
        if not body.instrument:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "pick the instrument to trade"
            )
        universe = (body.instrument,)
    elif body.strategy == "buy_hold":
        universe = (body.benchmark,)
    elif body.universe == "held":
        universe = tuple(_held_codes(db, user))
    elif body.universe == "watchlist":
        universe = tuple(_watch_codes(db, user))
    elif body.universe == "codes":
        universe = tuple(dict.fromkeys(body.codes))
    else:
        universe = tuple(_all_codes(db))
    known = set(
        db.scalars(select(Instrument.code).where(Instrument.code.in_((*universe, body.benchmark))))
    )
    missing = sorted({*universe, body.benchmark} - known)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the app does not know: {', '.join(missing[:5])}",
        )
    params = backtest.Params(
        strategy=body.strategy,
        start=body.start,
        end=body.end,
        capital=body.capital,
        fee=body.fee,
        tax_pct=body.tax_pct,
        top_n=body.top_n,
        benchmark=body.benchmark,
        universe=universe,
    )
    try:
        result = backtest.run_backtest(db, params)
    except backtest.BacktestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return BacktestOut(
        strategy=_stats(result.strategy),
        benchmark=_stats(result.benchmark),
        curve=[CurvePoint(day=d, strategy=a, benchmark=b) for d, a, b in result.curve],
        first_day=result.first_day,
        last_day=result.last_day,
        universe_size=result.universe_size,
        notes=result.notes,
        warnings=list(result.warnings),
    )


# --- the swing scanner ------------------------------------------------------------------------


class GateOut(BaseModel):
    signal: str
    text: str
    computed_on: date
    n_trades: int
    win_rate_pct: Decimal
    expectancy_pct: Decimal
    profit_factor: Decimal | None
    first_half_pct: Decimal | None
    second_half_pct: Decimal | None
    universe: int
    passed: bool


class SignalOut(BaseModel):
    day: date
    code: str
    name: str
    currency: str
    signal: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    atr: Decimal
    max_hold: int
    risk_pct: Decimal
    reward_pct: Decimal
    held: bool
    watched: bool


class SwingOut(BaseModel):
    scanned_on: date | None
    criteria: str
    gates: list[GateOut]
    signals: list[SignalOut]
    hidden: int  # signals of types that failed their backtest: counted, not shown
    notes: list[str]


SWING_NOTES = (
    "Information only, not advice: the app places no trade. The levels are indicative and are "
    "worked from the close of the signal day.",
    "A signal type is shown only while its backtest passes. The backtest uses the history the app "
    "stores (survivorship bias), daily bars, and a cost of 0.4% a round trip. A pass is evidence "
    "about the past, not a forecast.",
)


@router.get("/swing", response_model=SwingOut)
def get_swing(user: CurrentUser, db: UserDb, scope: Literal["mine", "all"] = "mine") -> SwingOut:
    gates = swing.latest_gates(db)
    scanned = db.scalar(select(func.max(SwingSignal.scanned_on)))
    held, watched = set(_held_codes(db, user)), set(_watch_codes(db, user))
    rows = (
        list(db.scalars(select(SwingSignal).where(SwingSignal.scanned_on == scanned)))
        if scanned
        else []
    )
    if scope == "mine":
        rows = [r for r in rows if r.code in held or r.code in watched]
    shown = [r for r in rows if gates.get(r.signal) is not None and gates[r.signal].passed]
    shown.sort(key=lambda r: (r.signal, r.code))
    return SwingOut(
        scanned_on=scanned,
        criteria=swing.CRITERIA_TEXT,
        gates=[
            GateOut(
                signal=g.signal,
                text=swing.SIGNAL_TEXT.get(g.signal, g.signal),
                computed_on=g.computed_on,
                n_trades=g.n_trades,
                win_rate_pct=g.win_rate,
                expectancy_pct=g.expectancy,
                profit_factor=g.profit_factor,
                first_half_pct=g.first_half,
                second_half_pct=g.second_half,
                universe=g.universe,
                passed=g.passed,
            )
            for g in sorted(gates.values(), key=lambda g: g.signal)
        ],
        signals=[
            SignalOut(
                day=r.day,
                code=r.code,
                name=r.name,
                currency=r.currency,
                signal=r.signal,
                entry=r.entry,
                stop=r.stop,
                target=r.target,
                atr=r.atr,
                max_hold=r.max_hold,
                risk_pct=((r.entry - r.stop) / r.entry * HUNDRED).quantize(Decimal("0.01")),
                reward_pct=((r.target - r.entry) / r.entry * HUNDRED).quantize(Decimal("0.01")),
                held=r.code in held,
                watched=r.code in watched,
            )
            for r in shown
        ],
        hidden=len(rows) - len(shown),
        notes=list(SWING_NOTES),
    )

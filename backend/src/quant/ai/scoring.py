"""Score the pick log against the benchmark (FR-52).

A pick is scored after 1, 3, 6 and 12 months. The pick's return is the move of its instrument
from the day of the pick to the close on or just before the end of the window. The benchmark is
the Nasdaq-100 proxy ETF (D35, `SXRV`), over the same days. Both returns are in euros.

Prices are converted to euros at the ECB rate of the day (the benchmark is a euro fund, and the
user's money is euros), so a pick in dollars is judged on what a euro investor got. A day with no
rate within a week leaves the window waiting. The pick's start is the price stored on the pick
when there is one (the close before the recommendation), else the close before the pick's day,
so a move on the day of the recommendation is never credited to it.

A buy or hold is a hit when it beat the benchmark. A sell is a hit when the instrument trailed
the benchmark afterwards (selling it was right). A score is written once and never changed. A
window that cannot be scored yet (no price near the day) is tried again by the next night's run.

Runs for all users, so it reads with row-level security bypassed and writes each row with its own
`user_id`.
"""

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.ai.picks import candidates
from quant.config import get_settings
from quant.ingest.jobs import JobResult
from quant.models import AiPick, AiPickScore, FxRate, Instrument, PriceEOD

log = logging.getLogger(__name__)

WINDOWS = (1, 3, 6, 12)
BENCHMARK_CODE = "SXRV"
MAX_FX_GAP_DAYS = 7
MAX_GAP_DAYS = 4  # a close this far before the day still counts (a long weekend)


@dataclass(frozen=True)
class Outcome:
    start: date
    end: date
    pick_pct: Decimal
    benchmark_pct: Decimal

    @property
    def excess(self) -> Decimal:
        return self.pick_pct - self.benchmark_pct


def add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def to_eur(session: Session, price: Decimal, currency: str, day: date) -> Decimal | None:
    """The price in euros at the ECB rate of the latest day on or before `day` (within a week)."""
    if currency == "EUR":
        return price
    rate = session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.quote == currency,
            FxRate.date <= day,
            FxRate.date >= day - timedelta(days=MAX_FX_GAP_DAYS),
        )
        .order_by(FxRate.date.desc())
        .limit(1)
    )
    return None if rate is None or rate <= 0 else price / rate


def close_on_or_before(
    session: Session, instrument_id: int, day: date
) -> tuple[date, Decimal] | None:
    """The newest close on or up to a few days before `day`, in euros."""
    row = session.execute(
        select(PriceEOD.date, PriceEOD.close, PriceEOD.currency)
        .where(
            PriceEOD.instrument_id == instrument_id,
            PriceEOD.date <= day,
            PriceEOD.date >= day - timedelta(days=MAX_GAP_DAYS),
        )
        .order_by(PriceEOD.date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    value = to_eur(session, row[1], row[2], row[0])
    return None if value is None else (row[0], value)


def covered(session: Session, instrument_id: int, day: date) -> bool:
    """The prices run past `day`, so the close used for it is the last one there will be. Without
    this, an outage of the price job could lock a stale close into a score that is never changed."""
    return (
        session.scalar(
            select(PriceEOD.id)
            .where(PriceEOD.instrument_id == instrument_id, PriceEOD.date > day)
            .limit(1)
        )
        is not None
    )


def pick_day(pick: AiPick) -> date:
    """The user's calendar day of the pick (the configured timezone, like the schedules)."""
    return pick.created_at.astimezone(ZoneInfo(get_settings().timezone)).date()


def start_price(
    session: Session, pick: AiPick, instrument: Instrument
) -> tuple[date, Decimal] | None:
    """Where the pick's return starts: the price stored on the pick, else the close before its
    day. Never the close of the day itself, which the recommendation may have come after."""
    day = pick_day(pick)
    if (
        pick.price is not None
        and pick.price_date is not None
        and day - timedelta(days=MAX_GAP_DAYS) <= pick.price_date <= day
        and pick.price_currency
        # The stored price counts only when this very instrument has that close on that day. The
        # pick's price may have come from another listing with other prices.
        and session.scalar(
            select(PriceEOD.id).where(
                PriceEOD.instrument_id == instrument.id,
                PriceEOD.date == pick.price_date,
                PriceEOD.close == pick.price,
            )
        )
        is not None
    ):
        value = to_eur(session, pick.price, pick.price_currency, pick.price_date)
        if value is not None and value > 0:
            return pick.price_date, value
    return close_on_or_before(session, instrument.id, day - timedelta(days=1))


def is_hit(direction: str, excess: Decimal) -> bool:
    return excess < 0 if direction == "sell" else excess > 0


MAX_PCT = Decimal("1000000")  # a return beyond this is a data error, not a result


def _pct(start: Decimal, end: Decimal) -> Decimal | None:
    value = (end / start - 1) * 100
    return value.quantize(Decimal("0.0001")) if abs(value) <= MAX_PCT else None


def choose(session: Session, pick: AiPick, instruments: list[Instrument]) -> Instrument | None:
    """The one instrument all windows of the pick are scored on: the first candidate with a
    close at the pick day. Chosen once, so the 1- and the 12-month score never differ in it."""
    day = pick_day(pick) - timedelta(days=1)
    for instrument in instruments:
        # The instrument needs prices of its own around the day, not only a price on the pick:
        # the pick's price may have come from another candidate.
        if (
            close_on_or_before(session, instrument.id, day) is not None
            and start_price(session, pick, instrument) is not None
        ):
            return instrument
    return None


def outcome(
    session: Session, pick: AiPick, instrument: Instrument, bench: Instrument, end: date
) -> Outcome | None:
    """The pick's and the benchmark's return from the day of the pick to `end`, each from its own
    nearest close, or None when a price is missing or the prices do not run past `end` yet."""
    if not (covered(session, instrument.id, end) and covered(session, bench.id, end)):
        return None
    first = start_price(session, pick, instrument)
    last = close_on_or_before(session, instrument.id, end)
    if first is None:
        return None
    # The benchmark starts on the same day as the pick, so both cover the same days at the start.
    bench_first = close_on_or_before(session, bench.id, first[0])
    bench_last = close_on_or_before(session, bench.id, end)
    if not (last and bench_first and bench_last):
        return None
    if (
        first[1] <= 0
        or bench_first[1] <= 0
        or last[0] <= first[0]
        or bench_last[0] <= bench_first[0]
    ):
        return None
    pick_pct = _pct(first[1], last[1])
    bench_pct = _pct(bench_first[1], bench_last[1])
    if pick_pct is None or bench_pct is None:
        return None
    return Outcome(first[0], last[0], pick_pct, bench_pct)


def score_picks(session: Session, today: date) -> JobResult:
    result = JobResult()
    bench = session.scalars(select(Instrument).where(Instrument.code == BENCHMARK_CODE)).first()
    if bench is None:
        result.errors["benchmark"] = f"benchmark {BENCHMARK_CODE} is not in the instruments table"
        result.attempted = 1
        return result
    done = {
        (pick_id, window)
        for pick_id, window in session.execute(
            select(AiPickScore.pick_id, AiPickScore.window_months)
        )
    }
    waiting = unscorable = 0
    for pick in session.scalars(select(AiPick).order_by(AiPick.created_at)).all():
        pick_id = pick.id  # kept, so an error handler never has to reload an expired row
        due = [
            w
            for w in WINDOWS
            if (pick_id, w) not in done and add_months(pick_day(pick), w) <= today
        ]
        if not due:
            continue
        try:
            chosen = choose(session, pick, candidates(session, pick.isin, pick.ticker))
            if chosen is None:
                unscorable += 1
                continue
            for window in due:
                result.attempted += 1
                end = add_months(pick_day(pick), window)
                found = outcome(session, pick, chosen, bench, end)
                if found is None:
                    waiting += 1
                    continue
                inserted = session.scalars(
                    insert(AiPickScore)
                    .values(
                        user_id=pick.user_id,
                        pick_id=pick.id,
                        window_months=window,
                        start_date=found.start,
                        end_date=found.end,
                        pick_return_pct=found.pick_pct,
                        benchmark_return_pct=found.benchmark_pct,
                        excess_pct=found.excess,
                        hit=is_hit(pick.direction, found.excess),
                    )
                    .on_conflict_do_nothing(constraint="uq_ai_pick_scores_pick_window")
                    .returning(AiPickScore.id)
                ).all()
                result.rows_written += len(inserted)  # one another run wrote first is not counted
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one bad pick must not stop the others
            session.rollback()
            log.exception("could not score pick %s", pick_id)
            result.errors[str(pick_id)] = type(exc).__name__
    if waiting:
        result.warnings["waiting"] = f"{waiting} window(s) wait for prices"
    if unscorable:
        result.warnings["unscorable"] = f"{unscorable} pick(s) name an instrument with no prices"
    return result

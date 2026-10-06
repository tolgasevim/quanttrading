"""Score the pick log against the benchmark (FR-52).

A pick is scored after 1, 3, 6 and 12 months. The pick's return is the move of its instrument
from the day of the pick to the close on or just before the end of the window. The benchmark is
the Nasdaq-100 proxy ETF (D35, `SXRV`), over the same days. Both returns are in the
instrument's own currency, so the currency does not matter for the comparison.

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

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.ai.picks import candidates
from quant.ingest.jobs import JobResult
from quant.models import AiPick, AiPickScore, Instrument, PriceEOD

log = logging.getLogger(__name__)

WINDOWS = (1, 3, 6, 12)
BENCHMARK_CODE = "SXRV"
MAX_GAP_DAYS = 7  # a close this far before the day still counts (weekends, holidays)


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


def close_on_or_before(
    session: Session, instrument_id: int, day: date
) -> tuple[date, Decimal] | None:
    row = session.execute(
        select(PriceEOD.date, PriceEOD.close)
        .where(
            PriceEOD.instrument_id == instrument_id,
            PriceEOD.date <= day,
            PriceEOD.date >= day - timedelta(days=MAX_GAP_DAYS),
        )
        .order_by(PriceEOD.date.desc())
        .limit(1)
    ).first()
    return None if row is None else (row[0], row[1])


def is_hit(direction: str, excess: Decimal) -> bool:
    return excess < 0 if direction == "sell" else excess > 0


def _pct(start: Decimal, end: Decimal) -> Decimal:
    return ((end / start - 1) * 100).quantize(Decimal("0.0001"))


def outcome(
    session: Session, pick: AiPick, instruments: list[Instrument], bench: Instrument, end: date
) -> Outcome | None:
    """The pick's and the benchmark's return from the day of the pick to `end`, or None when a
    price is missing."""
    start_day = pick.created_at.date()
    for instrument in instruments:
        first = close_on_or_before(session, instrument.id, start_day)
        last = close_on_or_before(session, instrument.id, end)
        bench_first = close_on_or_before(session, bench.id, start_day)
        bench_last = close_on_or_before(session, bench.id, end)
        if not (first and last and bench_first and bench_last):
            continue
        if first[1] <= 0 or bench_first[1] <= 0 or last[0] <= first[0]:
            continue
        return Outcome(
            first[0], last[0], _pct(first[1], last[1]), _pct(bench_first[1], bench_last[1])
        )
    return None


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
    for pick in session.scalars(select(AiPick).order_by(AiPick.created_at)):
        due = [
            w
            for w in WINDOWS
            if (pick.id, w) not in done and add_months(pick.created_at.date(), w) <= today
        ]
        if not due:
            continue
        instruments = candidates(session, pick.isin, pick.ticker)
        if not instruments:
            unscorable += 1
            continue
        for window in due:
            result.attempted += 1
            end = add_months(pick.created_at.date(), window)
            found = outcome(session, pick, instruments, bench, end)
            if found is None:
                waiting += 1
                continue
            session.execute(
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
            )
            result.rows_written += 1
        session.commit()
    if waiting:
        result.warnings["waiting"] = f"{waiting} window(s) wait for prices"
    if unscorable:
        result.warnings["unscorable"] = f"{unscorable} pick(s) name an instrument with no prices"
    return result

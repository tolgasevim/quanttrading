"""Alert rules (FR-72, FR-73), as pure functions: a price move, the limit that applies to it, and
quiet hours. The jobs and the API do the reading and the writing."""

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

TENTH = Decimal("0.1")

# The broker's asset classes that get a daily-move alert, and how an alert names them.
MOVE_CLASSES: dict[str, str] = {"STOCK": "share", "FUND": "fund", "CRYPTO": "coin"}

# The newest price must be this recent to count as "today's" move. A job that stopped must not
# keep alerting on an old day, and a closed market is not news.
MAX_PRICE_AGE_DAYS = 4
# The previous price may be at most this far back (a long weekend, a holiday). A longer gap is a
# hole in the data, not a daily move.
MAX_GAP_DAYS = 7


@dataclass(frozen=True)
class Move:
    pct: Decimal  # the change from the previous close, in percent
    close: Decimal
    previous: Decimal
    day: date
    previous_day: date


def daily_move(
    bars: list[tuple[date, Decimal]], today: date, *, max_age: int = MAX_PRICE_AGE_DAYS
) -> Move | None:
    """The move between the two newest bars (newest first), or None when there is none to report:
    fewer than two bars, a stale newest bar, too big a gap, or a previous close of zero."""
    if len(bars) < 2:
        return None
    (day, close), (previous_day, previous) = bars[0], bars[1]
    if (today - day).days > max_age or (today - day).days < 0:
        return None
    if (day - previous_day).days > MAX_GAP_DAYS or day <= previous_day or previous <= 0:
        return None
    return Move((close - previous) / previous * 100, close, previous, day, previous_day)


def threshold_for(
    asset_class: str | None, stock: Decimal, fund: Decimal, crypto: Decimal
) -> Decimal | None:
    """The daily-move limit for an asset class, or None when that class gets no alert."""
    return {"STOCK": stock, "FUND": fund, "CRYPTO": crypto}.get(asset_class or "")


def breaches(move: Move, limit: Decimal) -> bool:
    return abs(move.pct) >= limit


def in_quiet_hours(start: time | None, end: time | None, now: time) -> bool:
    """Whether `now` is inside the quiet window. The window may cross midnight (22:00 to 07:00).
    Start is inside, end is outside. No window, or an empty one (start = end), is never quiet."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def move_text(
    name: str, asset_class: str, move: Move, limit: Decimal, currency: str | None
) -> tuple[str, str]:
    """The title and body of a daily-move notification."""
    sign = "+" if move.pct > 0 else "-"
    pct = abs(move.pct).quantize(TENTH)
    title = f"{name} {sign}{pct}%"
    money = f" {currency}" if currency else ""
    body = (
        f"{name} closed at {_plain(move.close)}{money} on {move.day.isoformat()}, "
        f"{sign}{pct}% from {_plain(move.previous)}{money} on {move.previous_day.isoformat()}. "
        f"Your limit for a {MOVE_CLASSES.get(asset_class, 'holding')} is {_plain(limit)}%."
    )
    return title, body


def _plain(value: Decimal) -> str:
    """12.500000 -> 12.5, never scientific notation."""
    text = format(value.normalize(), "f")
    return text

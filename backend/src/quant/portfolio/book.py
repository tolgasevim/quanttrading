"""The user's own say about their book: tags, sub-group limits and the settings of the
consolidation and trading-book rules (FR-45 to FR-48). Plain reading and writing, with the
checks that keep a bad value out."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import AllocationSettings, PositionTag
from quant.portfolio import subgroups

ZERO = Decimal(0)


class BookError(ValueError):
    """The request is not valid. The text says why, in plain words."""


# --- tags ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tag:
    isin: str
    subgroup: str | None = None
    pinned: bool = False
    tracker_until: date | None = None
    short_term: bool = False
    stop_price: Decimal | None = None
    barrier: Decimal | None = None
    barrier_isin: str | None = None
    barrier_long: bool | None = None
    note: str | None = None


TAG_FIELDS = (
    "subgroup",
    "pinned",
    "tracker_until",
    "short_term",
    "stop_price",
    "barrier",
    "barrier_isin",
    "barrier_long",
    "note",
)


def _tag(row: PositionTag) -> Tag:
    return Tag(
        row.isin,
        row.subgroup,
        row.pinned,
        row.tracker_until,
        row.short_term,
        row.stop_price,
        row.barrier,
        row.barrier_isin,
        row.barrier_long,
        row.note,
    )


def load_tags(session: Session, user_id: uuid.UUID) -> dict[str, Tag]:
    rows = session.scalars(select(PositionTag).where(PositionTag.user_id == user_id))
    return {r.isin: _tag(r) for r in rows}


def _positive(value: Any, what: str) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation as error:
        raise BookError(f"{what} is not a number") from error
    if not out.is_finite() or out <= 0:
        raise BookError(f"{what} must be more than zero")
    return out


def save_tag(
    session: Session, user_id: uuid.UUID, isin: str, changes: dict[str, Any], today: date
) -> Tag:
    """Change some fields of a tag (the keys of `changes`); the others stay. A field set to
    None is cleared."""
    isin = isin.strip().upper()
    if not isin or len(isin) > 20:
        raise BookError("not a valid ISIN")
    unknown = set(changes) - set(TAG_FIELDS)
    if unknown:
        raise BookError(f"unknown field {sorted(unknown)[0]}")
    row = session.get(PositionTag, (user_id, isin))
    if row is None:
        row = PositionTag(user_id=user_id, isin=isin, pinned=False, short_term=False)
        session.add(row)
    for key, value in changes.items():
        if key == "subgroup":
            if value is not None and value not in subgroups.LABELS:
                raise BookError(f"the sub-group must be one of: {', '.join(subgroups.LABELS)}")
        elif key in ("stop_price", "barrier"):
            value = _positive(value, key.replace("_", " "))
        elif key == "tracker_until":
            if value is not None and value <= today:
                raise BookError("the day to keep a tracker until must be in the future")
        elif key == "barrier_isin":
            value = value.strip().upper() if value and value.strip() else None
        elif key == "note":
            value = (value or "").strip()[:200] or None
        elif key in ("pinned", "short_term"):
            value = bool(value)
        setattr(row, key, value)
    session.commit()
    return _tag(row)


def delete_tag(session: Session, user_id: uuid.UUID, isin: str) -> bool:
    row = session.get(PositionTag, (user_id, isin.strip().upper()))
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# --- settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Overlap:
    a: str
    b: str
    confirmed: date


@dataclass(frozen=True)
class BookSettings:
    min_positions: int = 40  # FR-45: a soft target of 40 to 60 positions
    max_positions: int = 60
    weekly_prompts: int = 3  # at most this many consolidation prompts a week
    hold_days: int = 10  # FR-48: a daily-reset ETP held longer than this raises an alert
    barrier_pct: Decimal = Decimal(5)  # FR-48: a knock-out closer than this to its barrier
    intentional: list[Overlap] = field(default_factory=list)  # FR-46


@dataclass(frozen=True)
class SubgroupLimit:
    target: Decimal | None = None
    cap: Decimal | None = None


def _row(session: Session, user_id: uuid.UUID) -> AllocationSettings:
    row = session.get(AllocationSettings, user_id)
    if row is None:
        row = AllocationSettings(user_id=user_id)
        session.add(row)
    return row


def load_book_settings(session: Session, user_id: uuid.UUID) -> BookSettings:
    row = session.get(AllocationSettings, user_id)
    raw = (row.book_settings if row else None) or {}
    out = BookSettings()
    overlaps = [
        Overlap(str(o["a"]), str(o["b"]), date.fromisoformat(str(o["confirmed"])))
        for o in raw.get("intentional", [])
    ]
    return BookSettings(
        min_positions=int(raw.get("min_positions", out.min_positions)),
        max_positions=int(raw.get("max_positions", out.max_positions)),
        weekly_prompts=int(raw.get("weekly_prompts", out.weekly_prompts)),
        hold_days=int(raw.get("hold_days", out.hold_days)),
        barrier_pct=Decimal(str(raw.get("barrier_pct", out.barrier_pct))),
        intentional=overlaps,
    )


def save_book_settings(session: Session, user_id: uuid.UUID, settings: BookSettings) -> None:
    row = _row(session, user_id)
    row.book_settings = {
        "min_positions": settings.min_positions,
        "max_positions": settings.max_positions,
        "weekly_prompts": settings.weekly_prompts,
        "hold_days": settings.hold_days,
        "barrier_pct": str(settings.barrier_pct),
        "intentional": [
            {"a": o.a, "b": o.b, "confirmed": o.confirmed.isoformat()} for o in settings.intentional
        ],
    }
    session.commit()


def change_book_settings(
    session: Session, user_id: uuid.UUID, changes: dict[str, Any]
) -> BookSettings:
    current = load_book_settings(session, user_id)
    new = replace(current, **changes)
    if not 1 <= new.min_positions <= new.max_positions <= 1000:
        raise BookError("the target must satisfy 1 <= minimum <= maximum <= 1000 positions")
    if not 0 <= new.weekly_prompts <= 20:
        raise BookError("the weekly prompts must be from 0 to 20")
    if not 1 <= new.hold_days <= 365:
        raise BookError("the holding days must be from 1 to 365")
    if not ZERO < new.barrier_pct <= 100:
        raise BookError("the barrier distance must be more than 0 and at most 100 percent")
    save_book_settings(session, user_id, new)
    return new


def pair_key(a: str, b: str) -> tuple[str, str]:
    a, b = a.strip().upper(), b.strip().upper()
    return (a, b) if a <= b else (b, a)


def set_intentional(
    session: Session, user_id: uuid.UUID, a: str, b: str, intentional: bool, today: date
) -> BookSettings:
    """Mark two holdings as an intentional overlap (D28), or take the mark away. Confirming
    again later renews the day: it is asked once a year."""
    if a.strip().upper() == b.strip().upper():
        raise BookError("choose two different instruments")
    key = pair_key(a, b)
    current = load_book_settings(session, user_id)
    kept = [o for o in current.intentional if pair_key(o.a, o.b) != key]
    if intentional:
        kept.append(Overlap(key[0], key[1], today))
    new = replace(current, intentional=kept)
    save_book_settings(session, user_id, new)
    return new


def load_subgroup_limits(session: Session, user_id: uuid.UUID) -> dict[str, SubgroupLimit]:
    row = session.get(AllocationSettings, user_id)
    raw = (row.subgroup_limits if row else None) or {}
    return {
        g: SubgroupLimit(
            None if v.get("target") in (None, "") else Decimal(str(v["target"])),
            None if v.get("cap") in (None, "") else Decimal(str(v["cap"])),
        )
        for g, v in raw.items()
        if g in subgroups.TECH_GROUPS
    }


def save_subgroup_limits(
    session: Session, user_id: uuid.UUID, limits: dict[str, SubgroupLimit]
) -> None:
    for group, limit in limits.items():
        if group not in subgroups.TECH_GROUPS:
            raise BookError(f"unknown sub-group {group}")
        for label, value in (("target", limit.target), ("cap", limit.cap)):
            if value is not None and not ZERO <= value <= 100:
                raise BookError(f"{group}: the {label} must be between 0 and 100")
        if limit.target is not None and limit.cap is not None and limit.target > limit.cap:
            raise BookError(f"{group}: the target is above the cap")
    row = _row(session, user_id)
    row.subgroup_limits = {
        g: {
            "target": "" if lim.target is None else str(lim.target),
            "cap": "" if lim.cap is None else str(lim.cap),
        }
        for g, lim in limits.items()
        if lim.target is not None or lim.cap is not None
    }
    session.commit()

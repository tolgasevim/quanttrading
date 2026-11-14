"""Cash, earmarked reserves and the opportunity unlock (FR-28, FR-28a, FR-28b).

The Depotauszug has no cash account, so cash is what the user types in. The balance rebuilt from
the transaction history (the sum of amount, fee and tax of every row) checks it: a difference of
more than 2% is a warning. A reserve is money set aside from that cash. In normal operation no
reserve counts as investable, so the investable cash is the balance minus the reserves.

An *opportunity* reserve can be unlocked for one investment, and only under the strict rule of
FR-28b. The rule here (`unlock_checks`):

- at least one trigger: the Nasdaq-100 or MSCI World fund is 20% or more below its 52-week high,
  or the user flags the opportunity by hand. The third trigger, a top-10% score with good data
  quality, needs the factor scores (FR-31 to FR-35) and shows as "no data" until they exist;
- at most 50% of the reserve per opportunity, and no more than the cash there is;
- not within six months of the reserve's target date (the money may be needed);
- a liquid share or ETF only (no coin, no other class, no thin trading), and a share may not pass
  the 15% single-stock cap (D17). The cap uses direct weights until ETF look-through exists.

Before confirming, the answer shows a stress test: what the reserve would be short if the money
unlocked (this and earlier unlocks) fell 20% or 35% by the target date. Passing the checks does
not invest anything. The app never places a trade. A confirmed unlock shrinks the reserve and
adds the amount to the reserve's *loan* (money out, to be put back). It becomes investable cash,
writes an event, and is entered in the pick log (FR-52), so it is scored like any other call. The
user puts money back with `refill_reserve`, from investable cash.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quant.models import CashBalance, Instrument, PriceEOD, Reserve, ReserveEvent, Transaction
from quant.portfolio import service

ZERO = Decimal(0)
CENT = Decimal("0.01")

MISMATCH_WARN_PCT = Decimal(2)  # FR-28
KINDS = ("hard", "opportunity")

# The cash bucket, as a share of the investable portfolio (PRD D26 and §6.5). FR-40 will make the
# target user-settable; these are its defaults.
CASH_TARGET_PCT = Decimal(5)
CASH_BAND_LOW_PCT = Decimal(3)
CASH_BAND_HIGH_PCT = Decimal(10)

UNLOCK_MAX_SHARE = Decimal("0.5")
UNLOCK_BLOCK_DAYS = 183  # no unlock within about six months of the target date
STRESS_FALLS = (Decimal(20), Decimal(35))  # percent
LOAN_ALERT_DAYS = 90
DISLOCATION_PCT = Decimal(20)
INSTRUMENT_DISCOUNT_PCT = Decimal(25)
SINGLE_STOCK_CAP_PCT = Decimal(15)
MIN_DAILY_TURNOVER = Decimal(1_000_000)  # in the instrument's own currency: a rough test
TURNOVER_DAYS = 30
INDEX_CODES = ("SXRV", "EUNL")  # Nasdaq-100 and MSCI World funds
LIQUID_CLASSES = ("stock", "etf")
HIGH_WINDOW_DAYS = 365
MAX_PRICE_AGE_DAYS = 7


class CashError(ValueError):
    """The request is not valid. The text says why, in plain words."""


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT)


# --- the balance --------------------------------------------------------------------------


def history_balance(session: Session, user_id: uuid.UUID, until: date | None = None) -> Decimal:
    """The cash the transaction history adds up to (amount + fee + tax of every row), up to and
    including `until` when given."""
    query = select(
        func.coalesce(
            func.sum(
                func.coalesce(Transaction.amount, 0)
                + func.coalesce(Transaction.fee, 0)
                + func.coalesce(Transaction.tax, 0)
            ),
            0,
        )
    ).where(Transaction.user_id == user_id)
    if until is not None:
        query = query.where(Transaction.date <= until)
    return Decimal(session.scalar(query) or 0)


def latest_entered(session: Session, user_id: uuid.UUID) -> CashBalance | None:
    return session.scalar(
        select(CashBalance)
        .where(CashBalance.user_id == user_id)
        .order_by(CashBalance.as_of.desc(), CashBalance.id.desc())
        .limit(1)
    )


def enter_balance(
    session: Session, user_id: uuid.UUID, amount: Decimal, as_of: date, note: str | None
) -> CashBalance:
    row = CashBalance(
        user_id=user_id, as_of=as_of, amount=_cents(amount), note=(note or "").strip()[:200] or None
    )
    session.add(row)
    session.commit()
    return row


def active_reserves(session: Session, user_id: uuid.UUID) -> list[Reserve]:
    return list(
        session.scalars(
            select(Reserve)
            .where(Reserve.user_id == user_id, Reserve.released_at.is_(None))
            .order_by(Reserve.created_at, Reserve.id)
        )
    )


@dataclass(frozen=True)
class CashStatus:
    entered: Decimal | None
    entered_as_of: date | None
    history: Decimal  # rebuilt from the transactions (up to the entered day, if there is one)
    difference: Decimal | None  # entered minus history
    difference_pct: Decimal | None  # of the entered balance
    mismatch: bool  # the two differ by more than 2%
    cash: Decimal  # the balance everything below uses
    cash_source: str  # "entered" or "history"
    reserves_total: Decimal
    shortfall: Decimal  # how far the cash is below the reserves (0 when it is not)
    investable: Decimal  # cash minus reserves, never below zero


def cash_status(session: Session, user_id: uuid.UUID) -> CashStatus:
    entered = latest_entered(session, user_id)
    reserves_total = sum((r.amount for r in active_reserves(session, user_id)), ZERO)
    if entered is None:
        history = history_balance(session, user_id)
        cash, source = history, "history"
        difference = pct = None
        mismatch = False
    else:
        history = history_balance(session, user_id, entered.as_of)
        cash, source = entered.amount, "entered"
        difference = entered.amount - history
        pct = None if entered.amount == 0 else abs(difference) / abs(entered.amount) * 100
        mismatch = (pct is not None and pct > MISMATCH_WARN_PCT) or (
            entered.amount == 0 and difference != 0
        )
    shortfall = max(reserves_total - cash, ZERO)
    return CashStatus(
        entered=None if entered is None else entered.amount,
        entered_as_of=None if entered is None else entered.as_of,
        history=history,
        difference=difference,
        difference_pct=pct,
        mismatch=mismatch,
        cash=cash,
        cash_source=source,
        reserves_total=reserves_total,
        shortfall=shortfall,
        investable=max(cash - reserves_total, ZERO),
    )


@dataclass(frozen=True)
class Band:
    pct: Decimal  # investable cash as a share of securities plus investable cash
    status: str  # low | ok | high
    target_pct: Decimal
    low_pct: Decimal
    high_pct: Decimal
    excess: Decimal  # investable cash above the target (what FR-49 would deploy), 0 unless high


def cash_band(securities: Decimal, investable: Decimal) -> Band | None:
    total = securities + investable
    if total <= 0:
        return None
    pct = investable / total * 100
    status = "low" if pct < CASH_BAND_LOW_PCT else "high" if pct > CASH_BAND_HIGH_PCT else "ok"
    excess = max(investable - total * CASH_TARGET_PCT / 100, ZERO) if status == "high" else ZERO
    return Band(pct, status, CASH_TARGET_PCT, CASH_BAND_LOW_PCT, CASH_BAND_HIGH_PCT, _cents(excess))


# --- reserves -----------------------------------------------------------------------------


def _event(
    session: Session,
    reserve: Reserve,
    action: str,
    before: Decimal | None,
    after: Decimal | None,
    detail: dict[str, object] | None = None,
) -> None:
    session.add(
        ReserveEvent(
            user_id=reserve.user_id,
            reserve_id=reserve.id,
            action=action,
            name=reserve.name,
            amount_before=before,
            amount_after=after,
            detail=detail,
        )
    )


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise CashError("a reserve needs a name")
    return name[:80]


def _check(kind: str, amount: Decimal) -> None:
    if kind not in KINDS:
        raise CashError("the kind must be hard or opportunity")
    if amount <= 0:
        raise CashError("the amount must be more than zero")


def create_reserve(
    session: Session,
    user_id: uuid.UUID,
    name: str,
    kind: str,
    amount: Decimal,
    target_date: date | None,
    note: str | None,
) -> Reserve:
    _check(kind, amount)
    reserve = Reserve(
        user_id=user_id,
        name=_clean_name(name),
        kind=kind,
        amount=_cents(amount),
        target_date=target_date,
        note=(note or "").strip()[:200] or None,
    )
    session.add(reserve)
    session.flush()
    _event(session, reserve, "create", None, reserve.amount, {"kind": kind})
    session.commit()
    return reserve


def change_reserve(
    session: Session,
    reserve: Reserve,
    *,
    name: str | None,
    kind: str | None,
    amount: Decimal | None,
    target_date: date | None,
    clear_target_date: bool,
    note: str | None,
) -> Reserve:
    """Change a reserve. A smaller amount frees the difference as investable cash."""
    if reserve.released_at is not None:
        raise CashError("this reserve was released")
    before = reserve.amount
    new_kind = kind or reserve.kind
    new_amount = _cents(amount) if amount is not None else reserve.amount
    _check(new_kind, new_amount)
    if name is not None:
        reserve.name = _clean_name(name)
    reserve.kind = new_kind
    reserve.amount = new_amount
    if clear_target_date:
        reserve.target_date = None
    elif target_date is not None:
        reserve.target_date = target_date
    if note is not None:
        reserve.note = note.strip()[:200] or None
    _event(session, reserve, "change", before, reserve.amount, {"kind": reserve.kind})
    session.commit()
    return reserve


def release_reserve(session: Session, reserve: Reserve) -> Reserve:
    """Give the whole reserve back: its money becomes investable cash."""
    if reserve.released_at is not None:
        raise CashError("this reserve was already released")
    reserve.released_at = datetime.now(UTC)
    reserve.loaned = ZERO  # releasing the reserve gives up the wish to put the loan back
    _event(session, reserve, "release", reserve.amount, ZERO)
    session.commit()
    return reserve


def events(session: Session, user_id: uuid.UUID, limit: int = 50) -> list[ReserveEvent]:
    return list(
        session.scalars(
            select(ReserveEvent)
            .where(ReserveEvent.user_id == user_id)
            .order_by(ReserveEvent.id.desc())
            .limit(limit)
        )
    )


# --- the opportunity unlock (FR-28b) -------------------------------------------------------


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    passed: bool | None  # None: no data to say
    detail: str
    blocking: bool  # a failed blocking check stops the unlock


@dataclass
class Unlock:
    checks: list[Check] = field(default_factory=list)
    allowed: bool = False
    triggers: list[str] = field(default_factory=list)  # the triggers that fired


def below_high(
    session: Session, instrument_id: int, today: date
) -> tuple[Decimal, Decimal, Decimal] | None:
    """How far the latest close is below the highest close of the last 52 weeks: the fall in
    percent, the high and the latest close. None without a recent close."""
    rows = session.execute(
        select(PriceEOD.date, PriceEOD.close)
        .where(
            PriceEOD.instrument_id == instrument_id,
            PriceEOD.date >= today - timedelta(days=HIGH_WINDOW_DAYS),
            PriceEOD.date <= today,
        )
        .order_by(PriceEOD.date.desc())
    ).all()
    if not rows or (today - rows[0][0]).days > MAX_PRICE_AGE_DAYS:
        return None
    last = rows[0][1]
    high = max(close for _, close in rows)
    if high <= 0:
        return None
    return (high - last) / high * 100, high, last


def average_turnover(session: Session, instrument_id: int, today: date) -> Decimal | None:
    """The average traded value (volume times close) of the last 30 bars with a volume."""
    rows = session.execute(
        select(PriceEOD.close, PriceEOD.volume)
        .where(
            PriceEOD.instrument_id == instrument_id,
            PriceEOD.date <= today,
            PriceEOD.volume.is_not(None),
        )
        .order_by(PriceEOD.date.desc())
        .limit(TURNOVER_DAYS)
    ).all()
    values = [close * volume for close, volume in rows if volume is not None]
    return sum(values, ZERO) / len(values) if values else None


def market_dislocation(session: Session, today: date) -> tuple[bool | None, str]:
    """Is the Nasdaq-100 or MSCI World fund far below its 52-week high?"""
    seen: list[str] = []
    fired = False
    for code in INDEX_CODES:
        inst = session.scalar(select(Instrument).where(Instrument.code == code))
        found = None if inst is None else below_high(session, inst.id, today)
        if found is None:
            continue
        pct = found[0]
        seen.append(f"{code} is {pct:.1f}% below its 52-week high")
        fired = fired or pct >= DISLOCATION_PCT
    if not seen:
        return None, "no recent prices for the index funds"
    return fired, "; ".join(seen)


def unlock_checks(
    session: Session,
    user_id: uuid.UUID,
    reserve: Reserve,
    isin: str,
    amount: Decimal,
    manual: bool,
    today: date,
) -> Unlock:
    """Run the FR-28b rule for investing `amount` of an opportunity reserve in `isin`."""
    out = Unlock()
    add = out.checks.append
    isin = isin.strip().upper()

    add(
        Check(
            "reserve_type",
            "Opportunity reserve",
            reserve.kind == "opportunity" and reserve.released_at is None,
            "an opportunity reserve can be unlocked"
            if reserve.kind == "opportunity"
            else "a hard reserve is never invested",
            True,
        )
    )
    limit = reserve.amount * UNLOCK_MAX_SHARE
    add(
        Check(
            "share",
            "At most half of the reserve",
            0 < amount <= limit,
            f"{amount:.2f} of at most {limit:.2f} (half of {reserve.amount:.2f})",
            True,
        )
    )
    status = cash_status(session, user_id)
    add(
        Check(
            "cash",
            "The cash is there",
            status.cash >= amount,
            f"cash {status.cash:.2f}, to invest {amount:.2f}",
            True,
        )
    )

    if reserve.target_date is not None:
        left = (reserve.target_date - today).days
        add(
            Check(
                "window",
                "Not within six months of the target date",
                left > UNLOCK_BLOCK_DAYS,
                f"{left} days to {reserve.target_date.isoformat()}"
                if left >= 0
                else f"the target date was {-left} days ago",
                True,
            )
        )
        loaned = reserve.loaned + amount
        falls = "; ".join(
            f"a {fall}% fall leaves the reserve {_cents(loaned * fall / 100):.2f} short"
            for fall in STRESS_FALLS
        )
        add(
            Check(
                "stress",
                "Car stress test",
                None,
                f"of {loaned:.2f} unlocked in all, {falls}, by "
                f"{reserve.target_date.isoformat()} ({max(left, 0) // 30} months left)",
                False,
            )
        )

    # Triggers: at least one must fire.
    dislocation, text = market_dislocation(session, today)
    add(Check("market_dislocation", "Market dislocation", dislocation, text, False))
    add(
        Check(
            "manual",
            "You flag it as an opportunity",
            manual,
            "you confirmed this yourself" if manual else "not flagged by you",
            False,
        )
    )
    add(
        Check(
            "instrument_score",
            "Top-10% score with good data quality",
            None,
            "no data: the factor scores are not built yet",
            False,
        )
    )
    if dislocation:
        out.triggers.append("market_dislocation")
    if manual:
        out.triggers.append("manual")

    # The instrument.
    inst = session.scalar(select(Instrument).where(Instrument.isin == isin)) if isin else None
    if inst is None:
        add(
            Check(
                "instrument",
                "A known instrument",
                False,
                "the app has no prices for this ISIN, so it cannot check liquidity",
                True,
            )
        )
    else:
        add(
            Check(
                "class",
                "A share or an ETF",
                inst.asset_class in LIQUID_CLASSES,
                f"{inst.name} is of class {inst.asset_class}",
                True,
            )
        )
        turnover = average_turnover(session, inst.id, today)
        add(
            Check(
                "liquidity",
                "Liquid enough",
                None if turnover is None else turnover >= MIN_DAILY_TURNOVER,
                "no volume data"
                if turnover is None
                else f"about {turnover:,.0f} {inst.currency} traded a day",
                True,
            )
        )
        found = below_high(session, inst.id, today)
        add(
            Check(
                "discount",
                f"At least {INSTRUMENT_DISCOUNT_PCT}% below its 52-week high",
                None if found is None else found[0] >= INSTRUMENT_DISCOUNT_PCT,
                "no recent price" if found is None else f"{found[0]:.1f}% below its high",
                False,
            )
        )
        if inst.asset_class == "stock":
            holdings = service.build_holdings(session, user_id)
            securities = service.weights(holdings)[1]
            current = next(
                (
                    service.mark_value(holdings.marks[p.isin])
                    for p in holdings.positions
                    if p.isin == isin and p.isin in holdings.marks
                ),
                ZERO,
            )
            total = securities + status.cash
            after = (current + amount) / total * 100 if total > 0 else Decimal(100)
            add(
                Check(
                    "single_stock_cap",
                    f"Under the {SINGLE_STOCK_CAP_PCT}% single-stock cap",
                    after <= SINGLE_STOCK_CAP_PCT,
                    f"it would be {after:.1f}% of the portfolio (direct holdings only)",
                    True,
                )
            )

    # A check with no data ("no volume data") does not block on its own. An unknown instrument is
    # a failed check, not a missing one.
    blocked = any(c.blocking and c.passed is False for c in out.checks)
    out.allowed = not blocked and bool(out.triggers)
    return out


def _log_pick(
    session: Session, user_id: uuid.UUID, reserve: Reserve, isin: str, amount: Decimal, day: date
) -> None:
    """An unlock is a call to buy: it goes into the pick log, to be scored (FR-52). The import is
    inside the function because the AI layer itself reads the reserves."""
    from quant.ai import picks

    inst = session.scalar(select(Instrument).where(Instrument.isin == isin.strip().upper()))
    picks.record_pick(
        session,
        user_id,
        question=f"Unlock of the reserve {reserve.name}",
        data={
            "name": inst.name if inst else isin.strip().upper(),
            "isin": isin.strip().upper(),
            "direction": "buy",
            "horizon_months": 12,
            "rationale": f"You unlocked {amount:.2f} EUR of the reserve {reserve.name} on "
            f"{day.isoformat()}.",
        },
        held_isins=set(),
        usage_id=None,
    )


def refill_reserve(session: Session, user_id: uuid.UUID, reserve: Reserve, amount: Decimal) -> None:
    """Put money back into a reserve that lent some (FR-28b), out of the investable cash."""
    amount = _cents(amount)
    if reserve.released_at is not None:
        raise CashError("this reserve was released")
    if amount <= 0:
        raise CashError("the amount must be more than zero")
    if amount > reserve.loaned:
        raise CashError(f"only {reserve.loaned:.2f} is out on loan")
    status = cash_status(session, user_id)
    if amount > status.investable:
        raise CashError(f"only {status.investable:.2f} of your cash is free to put back")
    before = reserve.amount
    reserve.amount = before + amount
    reserve.loaned = reserve.loaned - amount
    _event(session, reserve, "refill", before, reserve.amount, {"loan_left": str(reserve.loaned)})
    session.commit()


def refill_per_month(reserve: Reserve, today: date) -> Decimal | None:
    """What to put back each month to have the loan back by the target date."""
    if reserve.loaned <= 0 or reserve.target_date is None:
        return None
    months = max((reserve.target_date - today).days // 30, 1)
    return _cents(reserve.loaned / months)


def unlock(
    session: Session,
    user_id: uuid.UUID,
    reserve: Reserve,
    isin: str,
    amount: Decimal,
    manual: bool,
    today: date,
) -> Unlock:
    """Confirm an unlock: if the checks allow it, the reserve shrinks by `amount` and an event is
    written. Raises CashError when it is not allowed."""
    amount = _cents(amount)
    result = unlock_checks(session, user_id, reserve, isin, amount, manual, today)
    if not result.allowed:
        failed = [c.title for c in result.checks if c.blocking and c.passed is False]
        if not result.triggers:
            failed.append("no trigger has fired")
        raise CashError("not allowed: " + "; ".join(failed))
    before = reserve.amount
    reserve.amount = before - amount
    reserve.loaned = reserve.loaned + amount
    if reserve.amount <= 0:
        reserve.amount = ZERO
        reserve.released_at = datetime.now(UTC)
    _log_pick(session, user_id, reserve, isin, amount, today)
    _event(
        session,
        reserve,
        "unlock",
        before,
        reserve.amount,
        {
            "isin": isin.strip().upper(),
            "amount": str(amount),
            "triggers": result.triggers,
            "checks": {c.key: c.passed for c in result.checks},
        },
    )
    session.commit()
    return result

"""The read-only tools the chat assistant may call (FR-50), all scoped to the signed-in user.

Every tool returns a small JSON text and a one-line summary that the page shows (FR-53: the tool
calls are visible). A tool never writes, never reaches the internet and never sees another user's
data: it works from the user's own holdings and the shared stored prices. With "hide amounts"
(FR-57) the tools that would give euro values, quantities, costs or tax say so instead.

Not available, and the assistant is told so: news, macro data, live quotes, fundamentals.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.ai.picks import candidates
from quant.models import AiPick, AiPickScore, FxRate, PriceEOD
from quant.portfolio import service
from quant.portfolio.lots import COST_MISSING, realised_by_isin
from quant.portfolio.tax import YearEstimate

MAX_RESULT_CHARS = 6000
MAX_DAYS = 400
HIDDEN = "hidden: the user chose to hide amounts, so only weights are available"

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_position",
        "description": (
            "Details of one holding of the user: weight, sector, and (unless amounts are hidden) "
            "quantity, average cost, value, unrealised and realised profit. Find it by ISIN or "
            "by part of its name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "ISIN or name"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_prices",
        "description": (
            "Stored daily closes of a security (any ISIN, ticker or code the app tracks): the "
            "last close, the change over 1 week, 1 month, 3 months and the year to date, and the "
            "high and low of the period. Closes are end-of-day and may be a few days old."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "ISIN, ticker or the user's holding"},
                "days": {"type": "integer", "description": "Look-back in days (default 90)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "scenario",
        "description": (
            "What a price shock does to the user's portfolio. Each shock names a target and a "
            "percent move: a security (ISIN), a sector (Yahoo's name, e.g. Energy), an asset "
            "class (STOCK, FUND, CRYPTO) or all. The most specific target wins for a holding. "
            "The result is linear: weight times move. It ignores correlations and second-order "
            "effects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "shocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {"type": "string"},
                            "pct": {"type": "number", "description": "e.g. -20 for a fall of 20%"},
                        },
                        "required": ["target", "pct"],
                    },
                }
            },
            "required": ["shocks"],
        },
    },
    {
        "name": "get_tax_summary",
        "description": (
            "The German tax estimate for one year (default: the latest): gains, the tax estimate "
            "and what is still owed or refundable. An estimate for planning only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"year": {"type": "integer"}},
        },
    },
    {
        "name": "get_fx",
        "description": "The latest ECB reference rate: 1 EUR in the given currency (e.g. USD).",
        "input_schema": {
            "type": "object",
            "properties": {"currency": {"type": "string"}},
            "required": ["currency"],
        },
    },
    {
        "name": "list_my_picks",
        "description": (
            "The user's earlier AI picks (buy, sell, hold) with their scores against the "
            "benchmark so far. Use it to stay consistent with earlier advice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Default 10, at most 30"}},
        },
    },
]
NAMES = {s["name"] for s in SCHEMAS}


@dataclass
class ToolTrace:
    """One tool call as the page shows it."""

    name: str
    input: str
    summary: str
    error: bool = False


@dataclass
class ToolContext:
    session: Session
    user_id: uuid.UUID
    anonymise: bool
    holdings: service.Holdings


class ToolError(Exception):
    """A problem the model can read and work around."""


class NoMatch(ToolError):
    """Nothing the user holds fits the query (as opposed to several holdings fitting it)."""


def _dec(value: Decimal | None, places: str = "0.01") -> str | None:
    return None if value is None else str(value.quantize(Decimal(places)))


def _find_position(ctx: ToolContext, query: str) -> Any:
    wanted = " ".join(query.split()).lower()
    if not wanted:
        raise ToolError("query is empty")
    positions = ctx.holdings.positions
    for p in positions:
        if p.isin.lower() == wanted or (p.name and p.name.lower() == wanted):
            return p
    named = [p for p in positions if p.name and wanted in p.name.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise ToolError("several holdings match: " + ", ".join(p.name or p.isin for p in named[:8]))
    raise NoMatch("the user holds nothing that matches")


def _closed_position(ctx: ToolContext, query: str) -> tuple[dict[str, Any], str] | None:
    """A holding the user has sold completely: what is left to say is the realised result."""
    wanted = " ".join(query.split()).lower()
    for p in ctx.holdings.everything.values():
        if p.is_open or not (p.isin.lower() == wanted or (p.name and wanted in p.name.lower())):
            continue
        out: dict[str, Any] = {
            "name": p.name,
            "isin": p.isin,
            "class": p.asset_class,
            "status": "sold completely",
            "first_bought": p.first_date,
            "last_trade": p.last_date,
        }
        if ctx.anonymise:
            out["amounts"] = HIDDEN
        else:
            realised = realised_by_isin(ctx.holdings.book.disposals).get(p.isin)
            if realised is not None:
                out["realised_pnl_eur"] = _dec(realised)
        return out, f"{p.name or p.isin}: sold"
    return None


def get_position(ctx: ToolContext, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    query = str(args.get("query", ""))
    try:
        p = _find_position(ctx, query)
    except NoMatch:
        closed = _closed_position(ctx, query)  # only when no open holding fits at all
        if closed is None:
            raise
        return closed
    h = ctx.holdings
    weights, _, _ = service.weights(h)
    sector, industry = h.sectors.get(p.isin, (None, None))
    out: dict[str, Any] = {
        "name": p.name,
        "isin": p.isin,
        "class": p.asset_class,
        "sector": sector,
        "industry": industry,
        "weight_pct": _dec(weights.get(p.isin)),
        "first_bought": p.first_date,
    }
    if ctx.anonymise:
        out["amounts"] = HIDDEN
        return out, f"{p.name or p.isin}: weight only"
    mark = h.marks.get(p.isin)
    cost = h.costs.get(p.isin)
    out["quantity"] = format(p.quantity.normalize(), "f")  # never 1E+2
    if cost is not None:
        out["average_cost_eur"] = _dec(cost.average_cost, "0.0001")
        out["total_cost_eur"] = _dec(cost.total_cost)
        out["cost_flags"] = sorted(cost.flags)
    if mark is not None:
        value = service.mark_value(mark)
        out["price"] = _dec(mark.price, "0.0001")
        out["price_as_of"] = mark.as_of.isoformat()
        out["value_eur"] = _dec(value)
        if mark.cost is not None and not mark.cost.flags & COST_MISSING and mark.cost.total_cost:
            out["unrealised_pct"] = _dec(
                (value - mark.cost.total_cost) / mark.cost.total_cost * 100
            )
    realised = realised_by_isin(h.book.disposals).get(p.isin)
    if realised is not None:
        out["realised_pnl_eur"] = _dec(realised)
    return out, f"{p.name or p.isin}: position details"


def _change(closes: list[tuple[date, Decimal]], days: int) -> str | None:
    if not closes:
        return None
    last_day, last = closes[-1]
    target = last_day - timedelta(days=days)
    base = next((c for d, c in reversed(closes) if d <= target), None)
    return None if not base else _dec((last / base - 1) * 100)


def get_prices(ctx: ToolContext, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    query = " ".join(str(args.get("query", "")).split())
    if not query:
        raise ToolError("query is empty")
    days = args.get("days", 90)
    days = days if isinstance(days, int) and not isinstance(days, bool) else 90
    days = max(7, min(days, MAX_DAYS))
    try:  # a holding named in words resolves to its ISIN
        held = _find_position(ctx, query)
        isin: str | None = held.isin
        ticker: str | None = None
        name = held.name
    except NoMatch:  # not a holding: an ISIN or a ticker the app tracks
        isin = query.upper() if len(query) == 12 else None
        ticker, name = query, None
    options = []
    for candidate in candidates(ctx.session, isin, ticker):
        got = ctx.session.execute(
            select(PriceEOD.date, PriceEOD.close, PriceEOD.currency)
            .where(
                PriceEOD.instrument_id == candidate.id,
                PriceEOD.date >= date.today() - timedelta(days=max(days, 370)),
            )
            .order_by(PriceEOD.date)
        ).all()
        if got:
            options.append((got[-1][0], candidate, got))
    # Several listings can fit: the one with the newest close is the one the app keeps current.
    for _, instrument, rows in sorted(options, key=lambda o: o[0], reverse=True)[:1]:
        closes = [(d, c) for d, c, _ in rows]
        last_day = closes[-1][0]
        # The periods run back from the last stored close, which may be a few days old.
        window = [c for d, c in closes if d >= last_day - timedelta(days=days)]
        prior_year = [c for d, c in closes if d.year < last_day.year]
        ytd_base = prior_year[-1] if prior_year else None  # the close of the year before
        out = {
            "name": name or instrument.name,
            "code": instrument.code,
            "currency": rows[-1][2],
            "last_close": _dec(closes[-1][1], "0.0001"),
            "last_date": closes[-1][0].isoformat(),
            "change_1w_pct": _change(closes, 7),
            "change_1m_pct": _change(closes, 30),
            "change_3m_pct": _change(closes, 91),
            "change_ytd_pct": _dec((closes[-1][1] / ytd_base - 1) * 100) if ytd_base else None,
            "last_close_age_days": (date.today() - last_day).days,
            f"high_{days}d": _dec(max(window), "0.0001"),
            f"low_{days}d": _dec(min(window), "0.0001"),
            "note": "end-of-day closes, price only, not adjusted for dividends",
        }
        return out, f"{out['name']}: last close {out['last_close']} ({out['last_date']})"
    raise ToolError("the app has no stored prices for that (it tracks held and recommended shares)")


def scenario(ctx: ToolContext, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    shocks = args.get("shocks")
    if not isinstance(shocks, list) or not shocks or len(shocks) > 20:
        raise ToolError("give between 1 and 20 shocks")
    parsed: list[tuple[str, Decimal]] = []
    for item in shocks:
        try:
            target = " ".join(str(item["target"]).split()).lower()
            pct = Decimal(str(item["pct"]))
            if not pct.is_finite():
                raise ValueError("pct is not a number")
        except (KeyError, TypeError, ArithmeticError, ValueError):
            raise ToolError("each shock needs a target and a number pct") from None
        if not target or not -100 <= pct <= 1000:
            raise ToolError("pct must be between -100 and 1000")
        parsed.append((target, pct))
    targets = [t for t, _ in parsed]
    if len(set(targets)) != len(targets):
        raise ToolError("each target may appear once; send one shock per target")
    h = ctx.holdings
    weights, total, _ = service.weights(h)
    if not weights:
        raise ToolError("none of the holdings has a price, so no scenario can be computed")
    impact = Decimal(0)
    rows = []
    used: set[str] = set()
    for p in h.positions:
        weight = weights.get(p.isin)
        if weight is None:
            continue
        sector = (h.sectors.get(p.isin, (None, None))[0] or "").lower()
        cls = (p.asset_class or "").lower()
        # The most specific target wins: the security, then its sector, then its class, then all.
        move = None
        for rank in (p.isin.lower(), sector, cls, "all"):
            hit = next((pct for target, pct in parsed if rank and target == rank), None)
            if hit is not None:
                used.add(rank)  # a target counts as matched even when a more specific one wins
                if move is None:
                    move = hit
        if move is None:
            continue
        part = weight * move / 100
        impact += part
        rows.append(
            {
                "name": p.name or p.isin,
                "weight_pct": _dec(weight),
                "move_pct": _dec(move),
                "portfolio_effect_pct": _dec(part),
            }
        )
    ignored = sorted({t for t, _ in parsed} - used)
    out: dict[str, Any] = {
        "portfolio_change_pct": _dec(impact),
        "positions_hit": sorted(
            rows, key=lambda r: abs(Decimal(r["portfolio_effect_pct"] or 0)), reverse=True
        )[:15],
        "targets_that_matched_nothing": ignored,
        "note": "linear, valued holdings only, no correlations",
    }
    if not ctx.anonymise:
        out["portfolio_change_eur"] = _dec(total * impact / 100)
    return out, f"portfolio {out['portfolio_change_pct']}% for {len(parsed)} shock(s)"


def get_tax_summary(ctx: ToolContext, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if ctx.anonymise:
        raise ToolError(HIDDEN)
    years = service.build_tax(ctx.session, ctx.user_id)
    if not years:
        raise ToolError("no tax data: there are no sales or income in the history")
    wanted = args.get("year")
    year: YearEstimate | None
    if wanted is None:
        year = max(years, key=lambda y: y.year)
    elif isinstance(wanted, int) and not isinstance(wanted, bool):
        year = next((y for y in years if y.year == wanted), None)
    else:
        raise ToolError("year must be a whole number such as 2024")
    if year is None:
        raise ToolError("no data for that year; years: " + ", ".join(str(y.year) for y in years))
    out = {
        "year": year.year,
        "share_gains_eur": _dec(year.stock_pnl),
        "fund_gains_after_teilfreistellung_eur": _dec(year.fund_pnl),
        "other_gains_eur": _dec(year.other_pnl),
        "income_eur": _dec(year.income),
        "taxable_eur": _dec(year.taxable),
        "tax_estimate_eur": _dec(year.tax),
        "withheld_by_broker_eur": _dec(year.withheld),
        "still_owed_eur_negative_is_refund": _dec(year.to_settle),
        "note": "an estimate for planning, not tax advice; crypto tax is not estimated",
    }
    return out, f"tax estimate {year.year}: {out['tax_estimate_eur']} EUR"


def get_fx(ctx: ToolContext, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    code = str(args.get("currency", "")).strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ToolError("currency is a three letter code such as USD")
    row = ctx.session.scalars(
        select(FxRate).where(FxRate.quote == code).order_by(FxRate.date.desc()).limit(1)
    ).first()
    if row is None:
        raise ToolError(f"no stored ECB rate for {code}")
    return (
        {"currency": code, "per_eur": str(row.rate), "date": row.date.isoformat(), "source": "ECB"},
        f"1 EUR = {row.rate} {code} ({row.date.isoformat()})",
    )


def list_my_picks(ctx: ToolContext, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    limit = args.get("limit", 10)
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else 10
    limit = max(1, min(limit, 30))
    picks = ctx.session.scalars(
        select(AiPick)
        .where(AiPick.user_id == ctx.user_id)
        .order_by(AiPick.created_at.desc(), AiPick.id)
        .limit(limit)
    ).all()
    scores = ctx.session.scalars(
        select(AiPickScore).where(
            AiPickScore.user_id == ctx.user_id, AiPickScore.pick_id.in_([p.id for p in picks])
        )
    ).all()
    by_pick: dict[uuid.UUID, list[AiPickScore]] = {}
    for s in scores:
        by_pick.setdefault(s.pick_id, []).append(s)
    items = [
        {
            "date": p.created_at.date().isoformat(),
            "name": p.name,
            "direction": p.direction,
            "horizon_months": p.horizon_months,
            "scores": [
                {"months": s.window_months, "excess_pct": _dec(s.excess_pct), "hit": s.hit}
                for s in sorted(by_pick.get(p.id, []), key=lambda s: s.window_months)
            ],
        }
        for p in picks
    ]
    return {"picks": items}, f"{len(items)} earlier pick(s)"


RUNNERS = {
    "get_position": get_position,
    "get_prices": get_prices,
    "scenario": scenario,
    "get_tax_summary": get_tax_summary,
    "get_fx": get_fx,
    "list_my_picks": list_my_picks,
}


def run(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[str, ToolTrace]:
    """Run one tool. Returns the text for the model and the trace for the page. An error is text
    the model can read; it never raises."""
    shown = json.dumps(args, ensure_ascii=False, default=str)[:300]
    try:
        # A savepoint: a database error inside a tool must not abort the user's transaction.
        with ctx.session.begin_nested():
            out, summary = RUNNERS[name](ctx, args)
    except ToolError as exc:
        return f"error: {exc}", ToolTrace(name, shown, f"error: {exc}"[:200], True)
    except Exception as exc:  # noqa: BLE001 - a broken tool must not end the answer
        return "error: the tool failed", ToolTrace(
            name, shown, f"failed: {type(exc).__name__}", True
        )
    text = json.dumps(out, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        # Still valid JSON, with a flag, so the model knows the data is partial.
        text = json.dumps({"truncated": True, "partial_text": text[:MAX_RESULT_CHARS]})
    return text, ToolTrace(name, shown, summary[:200])

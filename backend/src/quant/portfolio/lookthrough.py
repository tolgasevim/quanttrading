"""ETF look-through and concentration (FR-23, FR-24, FR-27).

An ETF's holdings come from the issuer's holdings file, which an admin uploads for each fund
(`parse_holdings`, then `replace_holdings`). The files are public data about the fund, so the
table has no user column. Each file is a snapshot: a new upload replaces the old one.

The exposure view puts every euro of the portfolio on the underlying names:

- a share counts for itself (`direct`);
- a fund with a holdings file counts for each name in the file, at the file's weight
  (`indirect`, with the fund named in `via`). The same name held directly and through funds is
  one line, so hidden overlap shows;
- a fund without a file stays one line, "not looked through", and is listed, so the user knows
  how much of the picture is a guess. `covered_pct` is the share of fund value that was looked
  through.

Sector, country and currency use the same view. Money is `Decimal`.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from quant.models import EtfHolding, Instrument
from quant.portfolio import instruments as kinds
from quant.portfolio import service

ZERO = Decimal(0)
HUNDRED = Decimal(100)
NOT_LOOKED_THROUGH = "Funds not looked through"
OTHER_IN_FUNDS = "Other and cash inside funds"
MAX_ROWS = 5000
MIN_TOTAL_WEIGHT = Decimal(20)
MAX_TOTAL_WEIGHT = Decimal("105")


class HoldingsFormatError(ValueError):
    """The file is not a holdings file we understand. Nothing is stored."""


@dataclass(frozen=True)
class ParsedHolding:
    name: str
    ticker: str | None
    isin: str | None
    weight: Decimal  # percent of the fund
    sector: str | None
    country: str | None
    currency: str | None
    asset_class: str | None


# Header names (lower case) the parser accepts for each field.
ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "holding", "security name", "description", "issuer", "holding name"),
    "ticker": ("ticker", "symbol", "issuer ticker"),
    "isin": ("isin",),
    "weight": (
        "weight (%)", "weight", "% of net assets", "weighting", "% weight", "weight %",
        "% of fund", "fund weight",
    ),
    "sector": ("sector", "gics sector"),
    "country": ("location", "country", "location of risk", "country of risk"),
    "currency": ("currency", "market currency", "trading currency"),
    "asset_class": ("asset class", "type", "security type"),
}  # fmt: skip


def _split(line: str, delimiter: str) -> list[str]:
    return next(csv.reader([line], delimiter=delimiter), [])


def _decimal(raw: str, comma_decimal: bool) -> Decimal | None:
    text = raw.strip().replace("%", "").replace(" ", "")
    if not text or text in {"-", "--"}:
        return None
    text = text.replace(",", ".") if comma_decimal else text.replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_holdings(text: str) -> list[ParsedHolding]:
    """Read an issuer holdings file: the iShares layout (a few lines of preamble, then a header
    row) or any CSV with a name or ticker column and a weight column, comma or semicolon
    separated. Rows without a name or a weight are skipped (footers, blank lines)."""
    lines = text.lstrip("﻿").splitlines()
    header_index: int | None = None
    delimiter = ","
    columns: dict[str, int] = {}
    for delim in (",", ";", "\t"):
        for i, line in enumerate(lines[:60]):
            cells = [c.strip().lower() for c in _split(line, delim)]
            found: dict[str, int] = {}
            for field_name, names in ALIASES.items():
                for j, cell in enumerate(cells):
                    if cell in names and field_name not in found:
                        found[field_name] = j
                        break
            if "weight" in found and ({"name", "ticker", "isin"} & found.keys()):
                header_index, delimiter, columns = i, delim, found
                break
        if header_index is not None:
            break
    if header_index is None:
        raise HoldingsFormatError(
            "no header row with a weight column and a name, ticker or ISIN column"
        )
    comma_decimal = delimiter == ";"
    rows: list[ParsedHolding] = []
    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        cells = _split(line, delimiter)

        def cell(key: str, cells: list[str] = cells) -> str | None:
            j = columns.get(key)
            if j is None or j >= len(cells):
                return None
            value = cells[j].strip()
            return value if value and value != "-" else None

        weight = _decimal(cell("weight") or "", comma_decimal)
        name = cell("name") or cell("ticker") or cell("isin")
        if weight is None or weight <= 0 or not name:
            continue
        isin = (cell("isin") or "").upper()
        rows.append(
            ParsedHolding(
                name=name[:200],
                ticker=(cell("ticker") or "")[:30].upper() or None,
                isin=isin if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) else None,
                weight=weight,
                sector=(cell("sector") or "")[:60] or None,
                country=(cell("country") or "")[:60] or None,
                currency=(cell("currency") or "").upper()[:3] or None,
                asset_class=(cell("asset_class") or "")[:30] or None,
            )
        )
        if len(rows) > MAX_ROWS:
            raise HoldingsFormatError(f"more than {MAX_ROWS} holdings")
    if not rows:
        raise HoldingsFormatError("the file has no holdings with a weight")
    total = sum((r.weight for r in rows), ZERO)
    if not MIN_TOTAL_WEIGHT <= total <= MAX_TOTAL_WEIGHT:
        raise HoldingsFormatError(
            f"the weights add up to {total:.1f}%, not about 100%: is this the weight column?"
        )
    return rows


def replace_holdings(
    session: Session, etf_isin: str, rows: list[ParsedHolding], source: str, as_of: date
) -> int:
    """Store a fund's holdings, replacing the earlier file."""
    session.execute(delete(EtfHolding).where(EtfHolding.etf_isin == etf_isin))
    for r in rows:
        session.add(
            EtfHolding(
                etf_isin=etf_isin,
                name=r.name,
                ticker=r.ticker,
                holding_isin=r.isin,
                weight=r.weight,
                sector=r.sector,
                country=r.country,
                currency=r.currency,
                asset_class=r.asset_class,
                as_of=as_of,
                source=source[:40],
            )
        )
    session.commit()
    return len(rows)


# --- the exposure view ---------------------------------------------------------------------


@dataclass
class Underlying:
    key: str
    name: str
    isin: str | None
    type: str
    sector: str
    country: str
    direct: Decimal = ZERO
    via: dict[str, Decimal] = field(default_factory=dict)  # fund name -> value

    @property
    def indirect(self) -> Decimal:
        return sum(self.via.values(), ZERO)

    @property
    def total(self) -> Decimal:
        return self.direct + self.indirect


@dataclass(frozen=True)
class PositionView:
    isin: str
    name: str
    type: str
    flags: list[str]
    value: Decimal
    trading_book: bool
    crypto_linked: bool
    looked_through: bool | None  # None for a position that is not a fund


@dataclass
class Concentration:
    total: Decimal = ZERO
    unvalued: int = 0
    positions: list[PositionView] = field(default_factory=list)
    underlying: list[Underlying] = field(default_factory=list)
    by_class: dict[str, Decimal] = field(default_factory=dict)
    by_sector: dict[str, Decimal] = field(default_factory=dict)
    by_country: dict[str, Decimal] = field(default_factory=dict)
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    unlooked: list[str] = field(default_factory=list)
    fund_value: Decimal = ZERO
    looked_value: Decimal = ZERO
    crypto_value: Decimal = ZERO  # coins plus crypto-linked shares, direct and through funds

    @property
    def covered_pct(self) -> Decimal | None:
        return None if self.fund_value <= 0 else self.looked_value / self.fund_value * HUNDRED


FUND_TYPES = frozenset({kinds.ETF, kinds.BOND})


def _base_ticker(symbol: str) -> str:
    return symbol.split(".")[0].upper()


def holdings_by_fund(session: Session, isins: list[str]) -> dict[str, list[EtfHolding]]:
    out: dict[str, list[EtfHolding]] = {}
    if not isins:
        return out
    for row in session.scalars(select(EtfHolding).where(EtfHolding.etf_isin.in_(isins))):
        out.setdefault(row.etf_isin, []).append(row)
    return out


def _add(bucket: dict[str, Decimal], label: str, value: Decimal) -> None:
    bucket[label] = bucket.get(label, ZERO) + value


def concentration(session: Session, held: service.Holdings) -> Concentration:
    """The exposure view of the open, valued positions (see the module text)."""
    result = Concentration()
    valued = [
        (p, service.mark_value(held.marks[p.isin]))
        for p in held.positions
        if p.isin in held.marks and service.mark_value(held.marks[p.isin]) > 0
    ]
    result.unvalued = len(held.positions) - len(valued)
    result.total = sum((v for _, v in valued), ZERO)
    if not valued:
        return result
    isins = [p.isin for p, _ in valued]
    inst_by_isin = {
        i.isin: i for i in session.scalars(select(Instrument).where(Instrument.isin.in_(isins)))
    }
    kind_by_isin = {p.isin: kinds.classify(p.asset_class, p.name, p.isin) for p, _ in valued}
    fund_isins = [i for i in isins if kind_by_isin[i].type in FUND_TYPES]
    funds = holdings_by_fund(session, fund_isins)
    # A holdings file with no ISIN names a share by its ticker: match it to a held share by the
    # ticker of the price feed (NVDA, SAP.DE -> SAP).
    ticker_to_isin: dict[str, str] = {}
    for isin, inst in inst_by_isin.items():
        symbol = (inst.symbols or {}).get("yahoo")
        if symbol and isin:
            ticker_to_isin.setdefault(_base_ticker(symbol), isin)
    names = {p.isin: p.name or p.isin for p, _ in valued}
    lines: dict[str, Underlying] = {}

    def line(
        key: str, name: str, isin: str | None, type_: str, sector: str, country: str
    ) -> Underlying:
        found = lines.get(key)
        if found is None:
            found = lines[key] = Underlying(key, name, isin, type_, sector, country)
        elif found.sector == kinds.UNKNOWN and sector != kinds.UNKNOWN:
            found.sector = sector
        return found

    for position, value in sorted(valued, key=lambda pv: pv[1], reverse=True):
        kind = kind_by_isin[position.isin]
        name = names[position.isin]
        looked: bool | None = None
        _add(result.by_class, kind.type, value)
        if kind.crypto_linked:
            result.crypto_value += value
        if kind.type in FUND_TYPES:
            result.fund_value += value
            rows = funds.get(position.isin)
            looked = bool(rows)
            if rows:
                result.looked_value += value
                covered = ZERO
                for h in rows:
                    part = value * h.weight / HUNDRED
                    covered += part
                    isin = h.holding_isin or (
                        ticker_to_isin.get(h.ticker or "") if h.ticker else None
                    )
                    key = isin or (f"ticker:{h.ticker}" if h.ticker else f"name:{h.name.upper()}")
                    country = kinds.country_code(h.country)
                    if country == "??" and isin:
                        country = kinds.country_of_isin(isin)
                    sector = kinds.canonical_sector(h.sector)
                    crypto = (isin or "") in kinds.CRYPTO_LINKED_ISINS
                    under = line(
                        key, h.name, isin, kinds.CRYPTO if crypto else kinds.STOCK, sector, country
                    )
                    under.via[name] = under.via.get(name, ZERO) + part
                    if crypto:
                        result.crypto_value += part
                    currency = (h.currency or "").upper() or kinds.currency_of_country(country)
                    _add(result.by_sector, sector, part)
                    _add(result.by_country, country, part)
                    _add(result.by_currency, currency, part)
                rest = value - covered
                if rest > 0:
                    _add(result.by_sector, OTHER_IN_FUNDS, rest)
                    _add(result.by_country, OTHER_IN_FUNDS, rest)
                    _add(result.by_currency, OTHER_IN_FUNDS, rest)
            else:
                result.unlooked.append(name)
                under = line(
                    position.isin,
                    name,
                    position.isin,
                    kind.type,
                    NOT_LOOKED_THROUGH,
                    NOT_LOOKED_THROUGH,
                )
                under.direct += value
                for bucket in (result.by_sector, result.by_country, result.by_currency):
                    _add(bucket, NOT_LOOKED_THROUGH, value)
        else:
            sector_raw = held.sectors.get(position.isin, (None, None))[0]
            if kind.type == kinds.CRYPTO:
                sector, country, currency = kinds.CRYPTO_SECTOR, "n/a", kinds.CRYPTO_SECTOR
            elif kind.type == kinds.ETC:
                sector, country, currency = "Commodities", "n/a", "USD"
            else:
                sector = kinds.canonical_sector(sector_raw)
                country = kinds.country_of_isin(position.isin)
                currency = kinds.currency_of_country(country)
            under = line(position.isin, name, position.isin, kind.type, sector, country)
            under.name = name  # the name of the share held directly beats a file's spelling
            under.direct += value
            _add(result.by_sector, sector, value)
            _add(result.by_country, country, value)
            _add(result.by_currency, currency, value)
        result.positions.append(
            PositionView(
                position.isin, name, kind.type, kind.flags, value, kind.trading_book,
                kind.crypto_linked, looked,
            )
        )  # fmt: skip
    result.underlying = sorted(lines.values(), key=lambda u: u.total, reverse=True)
    return result

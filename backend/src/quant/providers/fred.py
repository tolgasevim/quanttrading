"""FRED (St. Louis Fed) data series as a price provider: the macro data of FR-61 and FR-63.

The public CSV download needs no key: one date column and one value column. A day with no
observation has "." as its value and is skipped. The value goes in as `close`; there is no
currency, so the instrument carries its own unit (a percent, an index).
"""

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from quant.providers.base import Bar, Fetcher, PriceSeries, ProviderError

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class FredProvider:
    name = "fred"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        body = self._fetcher.get_text(
            self.name,
            CSV_URL,
            {"id": symbol, "cosd": start.isoformat(), "coed": end.isoformat()},
        )
        return parse_csv(body, start, end)


def parse_csv(body: str, start: date, end: date) -> PriceSeries:
    text = body.strip()
    if not text:
        return PriceSeries(currency=None, bars=[])
    rows = list(csv.reader(io.StringIO(text)))
    header = [c.strip().lower() for c in rows[0]] if rows else []
    if len(header) < 2 or header[0] not in {"date", "observation_date"}:
        raise ProviderError(f"fred: unexpected payload: {text[:80]!r}")
    bars: list[Bar] = []
    for row in rows[1:]:
        if len(row) < 2 or row[1].strip() in {"", "."}:
            continue
        try:
            day = date.fromisoformat(row[0].strip())
            value = Decimal(row[1].strip())
        except (ValueError, InvalidOperation) as exc:
            raise ProviderError(f"fred: malformed row: {exc}") from exc
        if start <= day <= end:
            bars.append(Bar(date=day, close=value))
    return PriceSeries(currency=None, bars=sorted(bars, key=lambda b: b.date))

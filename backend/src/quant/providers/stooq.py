"""Stooq daily CSV download: fallback EOD source (PRD §8). No currency is reported."""

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from quant.providers.base import Bar, Fetcher, PriceSeries, ProviderError

CSV_URL = "https://stooq.com/q/d/l/"


class StooqProvider:
    name = "stooq"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        body = self._fetcher.get_text(
            self.name,
            CSV_URL,
            {"s": symbol, "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d"), "i": "d"},
        )
        return parse_csv(body)


def parse_csv(body: str) -> PriceSeries:
    text = body.strip()
    if not text or text.lower().startswith("no data"):
        return PriceSeries(currency=None, bars=[])
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "Close" not in reader.fieldnames:
        raise ProviderError(f"stooq: unexpected payload: {text[:80]!r}")
    bars = []
    try:
        for row in reader:
            volume = row.get("Volume")
            bars.append(
                Bar(
                    date=date.fromisoformat(row["Date"]),
                    open=Decimal(row["Open"]),
                    high=Decimal(row["High"]),
                    low=Decimal(row["Low"]),
                    close=Decimal(row["Close"]),
                    volume=int(float(volume)) if volume else None,
                )
            )
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise ProviderError(f"stooq: malformed row: {exc}") from exc
    return PriceSeries(currency=None, bars=sorted(bars, key=lambda b: b.date))

"""Yahoo Finance chart API (unofficial, personal-use terms; PRD §8 and §11).

Called directly over HTTP rather than through the yfinance package: the same endpoint with far
fewer dependencies, and the raw JSON is cached like every other provider response.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from quant.providers.base import Bar, Fetcher, PriceSeries, ProviderError

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class YahooProvider:
    name = "yahoo"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        period1 = int(datetime.combine(start, time.min, tzinfo=UTC).timestamp())
        period2 = int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC).timestamp())
        body = self._fetcher.get_text(
            self.name,
            CHART_URL.format(symbol=symbol),
            {
                "period1": str(period1),
                "period2": str(period2),
                "interval": "1d",
                "events": "div,split",
                "includeAdjustedClose": "true",
            },
        )
        return parse_chart(body, start, end)


def parse_chart(body: str, start: date, end: date) -> PriceSeries:
    try:
        payload = json.loads(body)
        chart = payload["chart"]
    except (ValueError, KeyError) as exc:
        raise ProviderError(f"yahoo: malformed response: {exc}") from exc
    if chart.get("error"):
        raise ProviderError(f"yahoo: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        return PriceSeries(currency=None, bars=[])
    result = results[0]
    meta = result.get("meta", {})
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    timestamps: list[int] = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")

    bars: dict[date, Bar] = {}
    for i, ts in enumerate(timestamps):
        close = _dec(quote.get("close", [None] * len(timestamps))[i])
        if close is None:  # holidays and halted days come back as nulls
            continue
        day = datetime.fromtimestamp(ts, tz).date()
        if not start <= day <= end:
            continue
        volume = quote.get("volume", [None] * len(timestamps))[i]
        # The last element wins, so an intraday snapshot for the same day is replaced
        # by the final bar when both are present.
        bars[day] = Bar(
            date=day,
            close=close,
            open=_dec(quote.get("open", [None] * len(timestamps))[i]),
            high=_dec(quote.get("high", [None] * len(timestamps))[i]),
            low=_dec(quote.get("low", [None] * len(timestamps))[i]),
            adj_close=_dec(adj[i]) if adj else None,
            volume=int(volume) if volume is not None else None,
        )
    return PriceSeries(
        currency=meta.get("currency"), bars=sorted(bars.values(), key=lambda b: b.date)
    )

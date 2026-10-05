"""Provider interfaces, shared data types and the HTTP fetcher with retries and raw caching."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

import httpx


class ProviderError(Exception):
    """A provider could not deliver data (network, HTTP error, bad payload)."""


@dataclass(frozen=True)
class Bar:
    date: date
    close: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    adj_close: Decimal | None = None
    volume: int | None = None


@dataclass(frozen=True)
class PriceSeries:
    currency: str | None  # None when the provider doesn't report one
    bars: list[Bar]


@dataclass(frozen=True)
class FxObservation:
    quote: str  # 1 EUR = rate units of quote
    date: date
    rate: Decimal


class PriceProvider(Protocol):
    name: str

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries: ...


class FxProvider(Protocol):
    name: str

    def fetch_rates(self, quotes: list[str], start: date, end: date) -> list[FxObservation]: ...


# (provider, request_key, status_code, body) -> None
Recorder = Callable[[str, str, int, str], None]

USER_AGENT = "Mozilla/5.0 (compatible; QuantTrading/0.1; personal use)"


class Fetcher:
    """GET with retries and exponential backoff. Every response body is handed to `recorder`."""

    def __init__(
        self,
        client: httpx.Client,
        recorder: Recorder | None = None,
        retries: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._retries = retries
        self._backoff = backoff_seconds
        self._sleep = sleep

    def close(self) -> None:
        """Release the HTTP connections. Call it when the job or request is done."""
        self._client.close()

    def get_text(self, provider: str, url: str, params: Mapping[str, str] | None = None) -> str:
        request_key = str(httpx.URL(url, params=params))[:300]
        last_error: Exception | None = None
        for attempt in range(self._retries):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                response = self._client.get(url, params=params, headers={"User-Agent": USER_AGENT})
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if self._recorder is not None:
                self._recorder(provider, request_key, response.status_code, response.text)
            if response.status_code == 200:
                return response.text
            last_error = ProviderError(f"{provider}: HTTP {response.status_code}")
            # Client errors other than rate limiting won't fix themselves on retry.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                break
        raise ProviderError(f"{provider}: request failed for {request_key}: {last_error}")

    def post_json(
        self, provider: str, url: str, body: object, headers: Mapping[str, str] | None = None
    ) -> str:
        """POST a JSON body, with the same retries and raw-response recording as `get_text`."""
        request_key = f"POST {url}"
        last_error: Exception | None = None
        for attempt in range(self._retries):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                response = self._client.post(
                    url,
                    json=body,
                    headers={"User-Agent": USER_AGENT, **(headers or {})},
                )
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if self._recorder is not None:
                self._recorder(provider, request_key, response.status_code, response.text)
            if response.status_code == 200:
                return response.text
            last_error = ProviderError(f"{provider}: HTTP {response.status_code}")
            if 400 <= response.status_code < 500 and response.status_code != 429:
                break
        raise ProviderError(f"{provider}: request failed for {request_key}: {last_error}")


# Quotes in minor units (e.g. London prices in pence) are normalised to the major currency.
MINOR_UNITS: dict[str, tuple[str, Decimal]] = {
    "GBp": ("GBP", Decimal(100)),
    "GBX": ("GBP", Decimal(100)),
    "ZAc": ("ZAR", Decimal(100)),
    "ILA": ("ILS", Decimal(100)),
}


def normalise_minor_units(series: PriceSeries) -> PriceSeries:
    if series.currency not in MINOR_UNITS:
        return series
    major, factor = MINOR_UNITS[series.currency]

    def scale(value: Decimal | None) -> Decimal | None:
        return None if value is None else value / factor

    bars = [
        Bar(
            date=b.date,
            close=b.close / factor,
            open=scale(b.open),
            high=scale(b.high),
            low=scale(b.low),
            adj_close=scale(b.adj_close),
            volume=b.volume,
        )
        for b in series.bars
    ]
    return PriceSeries(currency=major, bars=bars)

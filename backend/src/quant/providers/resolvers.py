"""ISIN -> ticker resolvers (FR-12). A resolver answers with the symbol the price providers use.

Two are built in, tried in the order of `QT_ISIN_RESOLVERS`:
- `yahoo`: Yahoo Finance's search, which accepts an ISIN and returns the primary listing with a
  symbol Yahoo's price API understands. Unofficial, like the price feed that uses the symbol.
- `openfigi`: the OpenFIGI mapping API (free, rate limited). It returns a ticker and an
  exchange code, translated here to a Yahoo symbol.
Anything a resolver cannot find is left for a manual entry.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

from quant.providers.base import Fetcher, ProviderError

YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# Yahoo's quote types that are listed securities with a daily price.
YAHOO_TYPES = {"EQUITY", "ETF", "MUTUALFUND"}


@dataclass(frozen=True)
class Listing:
    """One tradable listing of an ISIN, with the Yahoo symbol to price it."""

    symbol: str
    name: str | None
    exchange: str | None
    source: str  # the resolver's name


class IsinResolver(Protocol):
    name: str

    def resolve(self, isin: str) -> Listing | None:
        """The listing for `isin`, or None when this resolver does not know it."""
        ...


class YahooSearchResolver:
    name = "yahoo"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def resolve(self, isin: str) -> Listing | None:
        body = self._fetcher.get_text(
            "yahoo_search", YAHOO_SEARCH_URL, {"q": isin, "quotesCount": "6", "newsCount": "0"}
        )
        return parse_yahoo_search(body)


def parse_yahoo_search(body: str) -> Listing | None:
    try:
        quotes = json.loads(body).get("quotes", [])
        if not isinstance(quotes, list):
            raise TypeError("quotes is not a list")
    except (ValueError, AttributeError, TypeError) as exc:
        raise ProviderError(f"yahoo_search: unreadable response: {exc}") from exc
    for quote in quotes:
        if not isinstance(quote, dict):
            continue  # a malformed entry must not stop the lookup of the others
        if quote.get("quoteType") in YAHOO_TYPES and quote.get("symbol"):
            return Listing(
                symbol=str(quote["symbol"]),
                name=quote.get("longname") or quote.get("shortname"),
                exchange=quote.get("exchange"),
                source="yahoo",
            )
    return None


# OpenFIGI exchange code -> Yahoo symbol suffix. Order is the preference when an ISIN has
# several listings: the US composite first, then the larger European venues.
FIGI_SUFFIX: dict[str, str] = {
    "US": "",
    "UN": "",  # NYSE
    "UQ": "",  # Nasdaq Global Select
    "UW": "",  # Nasdaq Global Market
    "UR": "",  # Nasdaq Capital Market
    "UA": "",  # NYSE American
    "UP": "",  # NYSE Arca
    "GY": ".DE",  # Xetra
    "GF": ".F",  # Frankfurt
    "LN": ".L",
    "NA": ".AS",
    "FP": ".PA",
    "IM": ".MI",
    "SM": ".MC",
    "SW": ".SW",
    "DC": ".CO",
    "SS": ".ST",
    "NO": ".OL",
    "FH": ".HE",
    "AV": ".VI",
    "BB": ".BR",
    "PL": ".LS",
    "JT": ".T",
    "HK": ".HK",
    "CT": ".TO",
}
FIGI_SECTORS = {"Equity"}


class OpenFigiResolver:
    name = "openfigi"

    def __init__(self, fetcher: Fetcher, api_key: str | None = None) -> None:
        self._fetcher = fetcher
        self._api_key = api_key

    def resolve(self, isin: str) -> Listing | None:
        headers = {"X-OPENFIGI-APIKEY": self._api_key} if self._api_key else {}
        body = self._fetcher.post_json(
            "openfigi", OPENFIGI_URL, [{"idType": "ID_ISIN", "idValue": isin}], headers
        )
        return parse_openfigi(body)


def _figi_symbol(ticker: str, exchange: str) -> str:
    symbol = ticker.replace("/", "-").replace(" ", "-")
    suffix = FIGI_SUFFIX[exchange]
    if suffix == ".HK" and symbol.isdigit():
        symbol = symbol.zfill(4)  # Yahoo writes Hong Kong tickers with four digits: 0700.HK
    return symbol + suffix


def parse_openfigi(body: str) -> Listing | None:
    try:
        results: Any = json.loads(body)
        if not isinstance(results, list):
            raise TypeError(f"expected a list, got {type(results).__name__}")
        first = results[0] if results else {}
        if not isinstance(first, dict):
            raise TypeError("the first result is not an object")
        data = first.get("data", [])
        if not isinstance(data, list):
            raise TypeError("data is not a list")
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"openfigi: unreadable response: {exc}") from exc
    order = list(FIGI_SUFFIX)
    candidates = [
        d
        for d in data
        if isinstance(d, dict)  # skip malformed entries instead of failing the whole lookup
        and d.get("ticker")
        and d.get("exchCode") in FIGI_SUFFIX
        and d.get("marketSector") in FIGI_SECTORS
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda d: order.index(d["exchCode"]))
    return Listing(
        symbol=_figi_symbol(str(best["ticker"]), best["exchCode"]),
        name=best.get("name"),
        exchange=best.get("exchCode"),
        source="openfigi",
    )

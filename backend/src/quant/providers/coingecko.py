"""CoinGecko: daily euro prices for crypto, and the lookup of a coin from its name (PRD §8).

The free public API needs no key (a free "demo" key raises the rate limit) and quotes coins in
euros directly, so no FX rate is involved. It serves only the last 365 days of history, which is
why a longer backfill is cut to fit. Free use asks for a credit to CoinGecko: the Prices page
shows one.

Trade Republic's crypto ISINs (XF000...) are not known to CoinGecko, so a coin is found by the
name TR gives it ("Bitcoin") and the app keeps CoinGecko's own id ("bitcoin").
"""

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from quant.providers.base import Bar, Fetcher, PriceSeries, ProviderError
from quant.providers.resolvers import Listing

BASE_URL = "https://api.coingecko.com/api/v3"
KEY_HEADER = "x-cg-demo-api-key"
# The public API serves at most this many days back from now.
MAX_HISTORY_DAYS = 364
# CoinGecko ids are lower-case words with hyphens. The id goes into a URL path, so anything else
# is refused before a request is made.
COIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def _headers(api_key: str | None) -> dict[str, str]:
    return {KEY_HEADER: api_key} if api_key else {}


class CoinGeckoProvider:
    name = "coingecko"

    def __init__(self, fetcher: Fetcher, api_key: str | None = None) -> None:
        self._fetcher = fetcher
        self._api_key = api_key

    def fetch_eod(self, symbol: str, start: date, end: date) -> PriceSeries:
        if not COIN_ID.match(symbol):
            raise ProviderError(f"coingecko: not a coin id: {symbol!r}")
        start = max(start, end - timedelta(days=MAX_HISTORY_DAYS))
        body = self._fetcher.get_text(
            self.name,
            f"{BASE_URL}/coins/{symbol}/market_chart/range",
            {
                "vs_currency": "eur",
                "from": str(int(datetime.combine(start, time.min, tzinfo=UTC).timestamp())),
                "to": str(
                    int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC).timestamp())
                ),
            },
            headers=_headers(self._api_key),
        )
        return parse_market_chart(body, start, end)


def parse_market_chart(body: str, start: date, end: date) -> PriceSeries:
    """Daily bars in EUR from a `market_chart/range` answer: `{"prices": [[ms, price], ...]}`.

    The points are prices at a moment, not closes. A point at exactly 00:00 UTC is the price at the
    turn of the day, so it is the close of the day before. Any other point belongs to its own UTC
    day, and the last one of a day wins. That makes the newest bar the latest price of today.
    """
    try:
        points = json.loads(body)["prices"]
        if not isinstance(points, list):
            raise TypeError("prices is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderError(f"coingecko: unreadable response: {exc}") from exc
    closes: dict[date, tuple[int, Decimal]] = {}
    for point in points:
        try:
            millis, price = point[0], point[1]
            if price is None:
                continue
            moment = datetime.fromtimestamp(int(millis) / 1000, UTC)
            value = Decimal(str(price))
        except (TypeError, ValueError, IndexError, ArithmeticError, OverflowError, OSError):
            continue  # one bad point must not lose the others
        if value <= 0:
            continue
        day = moment.date()
        if moment.time() == time.min:
            day -= timedelta(days=1)
        if not start <= day <= end:
            continue
        if day not in closes or int(millis) >= closes[day][0]:
            closes[day] = (int(millis), value)
    bars = [Bar(date=day, close=close) for day, (_, close) in sorted(closes.items())]
    return PriceSeries(currency="EUR", bars=bars)


class CoinGeckoResolver:
    """Finds the CoinGecko id of a coin from the name the broker uses for it."""

    name = "coingecko"

    def __init__(self, fetcher: Fetcher, api_key: str | None = None) -> None:
        self._fetcher = fetcher
        self._api_key = api_key

    def resolve(self, isin: str, name: str | None) -> Listing | None:
        if not name or not name.strip():
            return None
        body = self._fetcher.get_text(
            "coingecko_search",
            f"{BASE_URL}/search",
            {"query": name.strip()},
            headers=_headers(self._api_key),
        )
        found = parse_search(body, name, isin)
        if found is None:
            return None
        coin_id, coin_name = found
        return Listing(
            symbol=coin_id, name=coin_name, exchange=None, source=self.name, provider=self.name
        )


def parse_search(body: str, name: str, isin: str) -> tuple[str, str] | None:
    """The (id, coin name) of the coin `name` stands for, or None.

    A coin counts when its name or symbol is the broker's name. Search also returns copies and
    scam tokens with the same name, and they have no market rank, so only ranked coins count and
    the best rank wins. A symbol that the broker's ISIN carries after its XF000 prefix
    (XF000BTC0017 holds BTC) comes first when it is there.
    """
    try:
        coins = json.loads(body)["coins"]
        if not isinstance(coins, list):
            raise TypeError("coins is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderError(f"coingecko: unreadable search response: {exc}") from exc
    wanted = name.strip().casefold()
    ranked: list[tuple[int, str, str, str]] = []
    for coin in coins:
        if not isinstance(coin, dict):
            continue
        coin_id, coin_name = coin.get("id"), coin.get("name")
        rank, symbol = coin.get("market_cap_rank"), str(coin.get("symbol") or "")
        if not (isinstance(coin_id, str) and COIN_ID.match(coin_id) and isinstance(coin_name, str)):
            continue
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
            continue
        if wanted in (coin_name.strip().casefold(), symbol.casefold()):
            ranked.append((rank, coin_id, coin_name, symbol.upper()))
    if not ranked:
        return None
    in_isin = [c for c in ranked if c[3] and isin.upper()[5:].startswith(c[3])]
    best = min(in_isin or ranked)
    return best[1], best[2]

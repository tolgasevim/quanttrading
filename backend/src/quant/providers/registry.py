"""Build providers by name, so swapping a provider is a configuration change (PRD §8)."""

from quant.providers.base import Fetcher, FxProvider, PriceProvider
from quant.providers.coingecko import CoinGeckoProvider, CoinGeckoResolver
from quant.providers.ecb import EcbProvider
from quant.providers.resolvers import (
    CryptoResolver,
    IsinResolver,
    OpenFigiResolver,
    YahooSearchResolver,
)
from quant.providers.stooq import StooqProvider
from quant.providers.yahoo import YahooProvider

PRICE_PROVIDERS = ("yahoo", "stooq", "coingecko")
FX_PROVIDERS: dict[str, type[EcbProvider]] = {"ecb": EcbProvider}


def price_providers(
    names: list[str], fetcher: Fetcher, coingecko_key: str | None = None
) -> list[PriceProvider]:
    unknown = [n for n in names if n not in PRICE_PROVIDERS]
    if unknown:
        raise ValueError(f"unknown price provider(s): {', '.join(unknown)}")
    built: dict[str, PriceProvider] = {
        "yahoo": YahooProvider(fetcher),
        "stooq": StooqProvider(fetcher),
        "coingecko": CoinGeckoProvider(fetcher, coingecko_key),
    }
    return [built[n] for n in names]


def crypto_resolvers(
    price_names: list[str], fetcher: Fetcher, coingecko_key: str | None
) -> list[CryptoResolver]:
    """Coin lookup is on when CoinGecko is one of the price providers: a coin is only found to be
    priced from there."""
    return [CoinGeckoResolver(fetcher, coingecko_key)] if "coingecko" in price_names else []


def fx_provider(name: str, fetcher: Fetcher) -> FxProvider:
    if name not in FX_PROVIDERS:
        raise ValueError(f"unknown FX provider: {name}")
    return FX_PROVIDERS[name](fetcher)


RESOLVER_NAMES = ("yahoo", "openfigi")


def isin_resolvers(
    names: list[str], fetcher: Fetcher, openfigi_key: str | None
) -> list[IsinResolver]:
    if not names:
        raise ValueError("no ISIN resolvers configured (QT_ISIN_RESOLVERS is empty)")
    unknown = [n for n in names if n not in RESOLVER_NAMES]
    if unknown:
        raise ValueError(f"unknown ISIN resolver(s): {', '.join(unknown)}")
    built: dict[str, IsinResolver] = {
        "yahoo": YahooSearchResolver(fetcher),
        "openfigi": OpenFigiResolver(fetcher, openfigi_key),
    }
    return [built[n] for n in names]

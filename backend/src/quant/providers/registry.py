"""Build providers by name, so swapping a provider is a configuration change (PRD §8)."""

from quant.providers.base import Fetcher, FxProvider, PriceProvider
from quant.providers.ecb import EcbProvider
from quant.providers.stooq import StooqProvider
from quant.providers.yahoo import YahooProvider

PRICE_PROVIDERS: dict[str, type[YahooProvider] | type[StooqProvider]] = {
    "yahoo": YahooProvider,
    "stooq": StooqProvider,
}
FX_PROVIDERS: dict[str, type[EcbProvider]] = {"ecb": EcbProvider}


def price_providers(names: list[str], fetcher: Fetcher) -> list[PriceProvider]:
    unknown = [n for n in names if n not in PRICE_PROVIDERS]
    if unknown:
        raise ValueError(f"unknown price provider(s): {', '.join(unknown)}")
    return [PRICE_PROVIDERS[n](fetcher) for n in names]


def fx_provider(name: str, fetcher: Fetcher) -> FxProvider:
    if name not in FX_PROVIDERS:
        raise ValueError(f"unknown FX provider: {name}")
    return FX_PROVIDERS[name](fetcher)

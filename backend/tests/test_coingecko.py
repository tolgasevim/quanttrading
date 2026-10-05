import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from quant.providers.base import ProviderError
from quant.providers.coingecko import (
    KEY_HEADER,
    MAX_HISTORY_DAYS,
    CoinGeckoProvider,
    CoinGeckoResolver,
    parse_market_chart,
    parse_search,
)
from quant.providers.registry import crypto_resolvers, price_providers


def ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def chart(points: list[object]) -> str:
    return json.dumps({"prices": points, "market_caps": [], "total_volumes": []})


class FakeFetcher:
    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[tuple[str, str, Mapping[str, str] | None, Mapping[str, str] | None]] = []

    def get_text(
        self,
        provider: str,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        self.calls.append((provider, url, params, headers))
        return self.body


START, END = date(2026, 9, 1), date(2026, 10, 5)


def test_a_midnight_point_is_the_close_of_the_day_before() -> None:
    body = chart([[ms(2026, 10, 2), 100.5], [ms(2026, 10, 3), 101], [ms(2026, 10, 4), "102.25"]])
    series = parse_market_chart(body, START, END)
    assert series.currency == "EUR"
    assert [(b.date, b.close) for b in series.bars] == [
        (date(2026, 10, 1), D("100.5")),
        (date(2026, 10, 2), D("101")),
        (date(2026, 10, 3), D("102.25")),
    ]


def test_hourly_points_give_one_bar_per_day_with_the_last_point_of_the_day() -> None:
    body = chart(
        [
            [ms(2026, 10, 2), 99],  # the close of 1 October
            [ms(2026, 10, 2, 9), 100],
            [ms(2026, 10, 2, 23), 105],
            [ms(2026, 10, 2, 14), 103],  # out of order: the later time still wins
            [ms(2026, 10, 3, 6, 30), 107],  # the latest price of today
        ]
    )
    bars = {b.date: b.close for b in parse_market_chart(body, START, END).bars}
    assert bars == {date(2026, 10, 1): D(99), date(2026, 10, 2): D(105), date(2026, 10, 3): D(107)}


def test_only_the_asked_range_is_kept() -> None:
    body = chart([[ms(2026, 8, 20, 5), 1], [ms(2026, 9, 10, 5), 2], [ms(2026, 10, 9, 5), 3]])
    assert [b.close for b in parse_market_chart(body, START, END).bars] == [D(2)]


def test_a_bad_point_is_skipped_and_the_others_are_kept() -> None:
    body = chart(
        [
            [ms(2026, 9, 10, 5), 10],
            [ms(2026, 9, 11, 5), None],
            [ms(2026, 9, 12, 5), 0],
            [ms(2026, 9, 13, 5), -4],
            ["x", 1],
            [1],
            "junk",
            [ms(2026, 9, 14, 5), 11],
        ]
    )
    assert [b.close for b in parse_market_chart(body, START, END).bars] == [D(10), D(11)]


@pytest.mark.parametrize("body", ["", "not json", "{}", '{"prices": 3}', "[]", '{"prices": null}'])
def test_a_wrong_overall_shape_is_a_provider_error(body: str) -> None:
    with pytest.raises(ProviderError):
        parse_market_chart(body, START, END)


def test_an_empty_answer_is_an_empty_series() -> None:
    assert parse_market_chart(chart([]), START, END).bars == []


def test_the_request_asks_for_euros_and_cuts_the_range_to_what_the_free_api_serves() -> None:
    fetcher = FakeFetcher(chart([[ms(2026, 10, 3, 6), 7]]))
    provider = CoinGeckoProvider(fetcher)  # type: ignore[arg-type]
    series = provider.fetch_eod("bitcoin", date(2024, 1, 1), END)
    [(name, url, params, headers)] = fetcher.calls
    assert name == "coingecko" and url.endswith("/coins/bitcoin/market_chart/range")
    assert params is not None and params["vs_currency"] == "eur"
    first = datetime.fromtimestamp(int(params["from"]), UTC).date()
    assert first == END - timedelta(days=MAX_HISTORY_DAYS)
    assert datetime.fromtimestamp(int(params["to"]), UTC).date() == END + timedelta(days=1)
    assert not headers  # no key, no header
    assert series.currency == "EUR" and series.bars[0].close == D(7)


def test_a_demo_key_goes_in_the_header() -> None:
    fetcher = FakeFetcher(chart([]))
    CoinGeckoProvider(fetcher, "CG-secret").fetch_eod("bitcoin", START, END)  # type: ignore[arg-type]
    assert fetcher.calls[0][3] == {KEY_HEADER: "CG-secret"}


@pytest.mark.parametrize("bad", ["", "Bitcoin", "../bitcoin", "bit coin", "a/b", "x" * 101, "-x"])
def test_a_coin_id_that_is_not_one_never_reaches_the_url(bad: str) -> None:
    fetcher = FakeFetcher(chart([]))
    with pytest.raises(ProviderError, match="not a coin id"):
        CoinGeckoProvider(fetcher).fetch_eod(bad, START, END)  # type: ignore[arg-type]
    assert fetcher.calls == []


def coin(id_: str, name: str, symbol: str, rank: object) -> dict[str, object]:
    return {"id": id_, "name": name, "api_symbol": id_, "symbol": symbol, "market_cap_rank": rank}


def search(*coins: object) -> str:
    return json.dumps({"coins": list(coins), "exchanges": [], "categories": []})


def test_the_best_ranked_coin_with_the_name_wins_and_copies_are_ignored() -> None:
    body = search(
        coin("fake-bitcoin", "Bitcoin", "BTC", None),  # a copy has no rank
        coin("bitcoin-cash", "Bitcoin Cash", "BCH", 20),
        coin("wrapped-bitcoin", "Wrapped Bitcoin", "WBTC", 15),
        coin("bitcoin", "Bitcoin", "BTC", 1),
        coin("bitcoin-clone", "Bitcoin", "BTC2", 900),
    )
    assert parse_search(body, "Bitcoin", "XF000BTC0017") == ("bitcoin", "Bitcoin")


def test_the_symbol_in_the_isin_decides_between_coins_with_the_same_name() -> None:
    body = search(coin("alpha-one", "Alpha", "AAA", 5), coin("alpha-two", "Alpha", "BBB", 50))
    assert parse_search(body, "Alpha", "XF000BBB0011") == ("alpha-two", "Alpha")
    assert parse_search(body, "Alpha", "XF000ZZZ0011") == ("alpha-one", "Alpha")  # best rank


def test_the_broker_may_name_a_coin_by_its_symbol() -> None:
    assert parse_search(search(coin("ripple", "XRP", "XRP", 4)), "XRP", "XF000XRP0011") == (
        "ripple",
        "XRP",
    )
    assert parse_search(search(coin("ripple", "Ripple", "XRP", 4)), "xrp", "XF000XRP0011")


def test_no_ranked_exact_match_is_not_found() -> None:
    body = search(
        coin("bitcoin-cash", "Bitcoin Cash", "BCH", 20), coin("x", "Bitcoin", "BTC", None)
    )
    assert parse_search(body, "Bitcoin", "XF000BTC0017") is None
    assert parse_search(search(), "Bitcoin", "XF000BTC0017") is None


def test_malformed_search_entries_are_skipped_and_a_wrong_shape_is_an_error() -> None:
    body = search(
        "junk",
        {"id": "../x", "name": "Bitcoin", "symbol": "BTC", "market_cap_rank": 1},  # an unsafe id
        {"id": "bitcoin", "name": "Bitcoin", "symbol": "BTC", "market_cap_rank": True},
        {"id": "bitcoin", "name": "Bitcoin", "symbol": "BTC", "market_cap_rank": "1"},
        coin("bitcoin", "Bitcoin", "BTC", 1),
    )
    assert parse_search(body, "Bitcoin", "XF000BTC0017") == ("bitcoin", "Bitcoin")
    for bad in ["", "nope", "{}", '{"coins": 3}']:
        with pytest.raises(ProviderError):
            parse_search(bad, "Bitcoin", "XF000BTC0017")


def test_the_resolver_searches_by_the_brokers_name() -> None:
    fetcher = FakeFetcher(search(coin("cardano", "Cardano", "ADA", 9)))
    found = CoinGeckoResolver(fetcher, "k").resolve("XF000ADA0010", " Cardano ")  # type: ignore[arg-type]
    assert found is not None
    assert (found.symbol, found.name, found.source, found.provider) == (
        "cardano",
        "Cardano",
        "coingecko",
        "coingecko",
    )
    [(name, url, params, headers)] = fetcher.calls
    assert name == "coingecko_search" and url.endswith("/search")
    assert params == {"query": "Cardano"} and headers == {KEY_HEADER: "k"}


def test_a_coin_without_a_name_is_not_searched() -> None:
    fetcher = FakeFetcher(search())
    assert CoinGeckoResolver(fetcher).resolve("XF000ADA0010", None) is None  # type: ignore[arg-type]
    assert CoinGeckoResolver(fetcher).resolve("XF000ADA0010", "  ") is None  # type: ignore[arg-type]
    assert fetcher.calls == []


def test_the_registry_builds_coingecko_and_coin_lookup_follows_the_price_providers() -> None:
    fetcher = FakeFetcher("")
    built = price_providers(["yahoo", "coingecko"], fetcher, "key")  # type: ignore[arg-type]
    assert [p.name for p in built] == ["yahoo", "coingecko"]
    assert len(crypto_resolvers(["yahoo", "coingecko"], fetcher, None)) == 1  # type: ignore[arg-type]
    assert crypto_resolvers(["yahoo", "stooq"], fetcher, None) == []  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown price provider"):
        price_providers(["coingeko"], fetcher)  # type: ignore[arg-type]


def test_coingecko_is_a_default_price_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from quant.config import Settings

    monkeypatch.delenv("QT_PRICE_PROVIDERS", raising=False)
    assert "coingecko" in Settings().price_providers
    monkeypatch.setenv("QT_PRICE_PROVIDERS", "yahoo,stooq")
    assert Settings().price_providers == ["yahoo", "stooq"]

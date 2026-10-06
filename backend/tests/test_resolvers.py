import json

import httpx
import pytest

from quant.providers.base import Fetcher, ProviderError
from quant.providers.resolvers import (
    OpenFigiResolver,
    YahooSearchResolver,
    parse_openfigi,
    parse_yahoo_search,
)

APPLE = json.dumps(
    {
        "explains": [],
        "count": 1,
        "quotes": [
            {
                "exchange": "NMS",
                "shortname": "Apple Inc.",
                "quoteType": "EQUITY",
                "symbol": "AAPL",
                "longname": "Apple Inc.",
                "exchDisp": "NASDAQ",
            }
        ],
        "news": [],
    }
)
ETF = json.dumps(
    {
        "quotes": [
            {
                "exchange": "LSE",
                "shortname": "ISHARES III PLC ISHRS CORE MSCI",
                "quoteType": "ETF",
                "symbol": "IWDA.L",
                "longname": "iShares Core MSCI World UCITS ETF USD (Acc)",
            }
        ]
    }
)


def test_yahoo_search_takes_the_symbol_and_the_long_name() -> None:
    listing = parse_yahoo_search(APPLE)
    assert listing is not None
    assert (listing.symbol, listing.name, listing.exchange, listing.source) == (
        "AAPL",
        "Apple Inc.",
        "NMS",
        "yahoo",
    )
    etf = parse_yahoo_search(ETF)
    assert etf is not None and etf.symbol == "IWDA.L"


def test_yahoo_search_ignores_what_is_not_a_listed_security() -> None:
    assert parse_yahoo_search(json.dumps({"quotes": []})) is None
    other = {"quotes": [{"quoteType": "CURRENCY", "symbol": "EURUSD=X"}]}
    assert parse_yahoo_search(json.dumps(other)) is None
    no_symbol = {"quotes": [{"quoteType": "EQUITY"}]}
    assert parse_yahoo_search(json.dumps(no_symbol)) is None


def test_yahoo_search_garbage_is_a_provider_error() -> None:
    with pytest.raises(ProviderError):
        parse_yahoo_search("<html>blocked</html>")


def figi(*listings: tuple[str, str, str]) -> str:
    """OpenFIGI's documented response: one result per request, with its listings under `data`."""
    return json.dumps(
        [
            {
                "data": [
                    {"ticker": t, "exchCode": ex, "marketSector": sector, "name": "ACME CORP"}
                    for t, ex, sector in listings
                ]
            }
        ]
    )


def test_openfigi_prefers_the_us_listing_then_germany() -> None:
    both = parse_openfigi(figi(("ACME", "GY", "Equity"), ("ACME", "US", "Equity")))
    assert both is not None and both.symbol == "ACME" and both.source == "openfigi"
    german = parse_openfigi(figi(("SAP", "GF", "Equity"), ("SAP", "GY", "Equity")))
    assert german is not None and german.symbol == "SAP.DE"  # Xetra before Frankfurt


def test_openfigi_translates_share_classes_and_suffixes() -> None:
    brk = parse_openfigi(figi(("BRK/B", "UN", "Equity")))
    assert brk is not None and brk.symbol == "BRK-B"
    ams = parse_openfigi(figi(("ASML", "NA", "Equity")))
    assert ams is not None and ams.symbol == "ASML.AS"


def test_openfigi_skips_unknown_venues_and_other_sectors() -> None:
    assert parse_openfigi(figi(("ACME", "ZZ", "Equity"))) is None  # an exchange we cannot translate
    assert parse_openfigi(figi(("ACME", "US", "Corp"))) is None  # a bond, not a share
    assert parse_openfigi(json.dumps([{"warning": "No identifier found."}])) is None


def test_openfigi_garbage_is_a_provider_error() -> None:
    with pytest.raises(ProviderError):
        parse_openfigi("not json")
    with pytest.raises(ProviderError):
        parse_openfigi(json.dumps({"error": "bad"}))


def make_fetcher(handler: httpx.MockTransport, recorded: list[tuple[str, str, int]]) -> Fetcher:
    return Fetcher(
        httpx.Client(transport=handler),
        recorder=lambda provider, key, status, body: recorded.append((provider, key, status)),
        retries=3,
        backoff_seconds=0,
        sleep=lambda _: None,
    )


def test_the_yahoo_resolver_asks_for_the_isin_and_records_the_answer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=APPLE)

    recorded: list[tuple[str, str, int]] = []
    listing = YahooSearchResolver(make_fetcher(httpx.MockTransport(handler), recorded)).resolve(
        "US0378331005"
    )
    assert listing is not None and listing.symbol == "AAPL"
    assert seen[0].url.params["q"] == "US0378331005"
    assert recorded and recorded[0][0] == "yahoo_search"


def test_the_openfigi_resolver_posts_the_isin_with_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=figi(("SAP", "GY", "Equity")))

    resolver = OpenFigiResolver(make_fetcher(httpx.MockTransport(handler), []), api_key="k123")
    listing = resolver.resolve("DE0007164600")
    assert listing is not None and listing.symbol == "SAP.DE"
    request = seen[0]
    assert json.loads(request.content) == [{"idType": "ID_ISIN", "idValue": "DE0007164600"}]
    assert request.headers["X-OPENFIGI-APIKEY"] == "k123"
    keyless = OpenFigiResolver(make_fetcher(httpx.MockTransport(handler), []))
    keyless.resolve("DE0007164600")
    assert "X-OPENFIGI-APIKEY" not in seen[-1].headers


def test_post_retries_rate_limits_but_not_client_errors() -> None:
    calls = {"n": 0}

    def limited(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] < 3 else 200, text="[]")

    fetcher = make_fetcher(httpx.MockTransport(limited), [])
    assert fetcher.post_json("openfigi", "https://x.test/v3/mapping", []) == "[]"
    assert calls["n"] == 3

    bad = {"n": 0}

    def forbidden(request: httpx.Request) -> httpx.Response:
        bad["n"] += 1
        return httpx.Response(401, text="no")

    with pytest.raises(ProviderError):
        make_fetcher(httpx.MockTransport(forbidden), []).post_json("openfigi", "https://x.test", [])
    assert bad["n"] == 1  # a 401 will not fix itself


def test_malformed_entries_are_skipped_not_fatal() -> None:
    mixed = {"quotes": ["x", 5, None, {"quoteType": "EQUITY", "symbol": "OK"}]}
    listing = parse_yahoo_search(json.dumps(mixed))
    assert listing is not None and listing.symbol == "OK"
    assert parse_yahoo_search(json.dumps({"quotes": ["x", None]})) is None
    broken = [
        {"data": ["x", 5, None, {"ticker": "ACME", "exchCode": "US", "marketSector": "Equity"}]}
    ]
    figi_listing = parse_openfigi(json.dumps(broken))
    assert figi_listing is not None and figi_listing.symbol == "ACME"
    assert parse_openfigi(json.dumps([{"data": ["x"]}])) is None


def test_a_wrong_overall_shape_is_a_provider_error_not_a_crash() -> None:
    for body in ('["x"]', '[{"data": "x"}]', "[5]", '{"quotes": 5}', "null"):
        with pytest.raises(ProviderError):
            parse_openfigi(body) if body != '{"quotes": 5}' else parse_yahoo_search(body)
    with pytest.raises(ProviderError):
        parse_yahoo_search("null")


def test_hong_kong_tickers_get_four_digits() -> None:
    tencent = parse_openfigi(figi(("700", "HK", "Equity")))
    assert tencent is not None and tencent.symbol == "0700.HK"
    alibaba = parse_openfigi(figi(("9988", "HK", "Equity")))
    assert alibaba is not None and alibaba.symbol == "9988.HK"
    tokyo = parse_openfigi(figi(("7203", "JT", "Equity")))
    assert tokyo is not None and tokyo.symbol == "7203.T"  # other markets keep their digits


def test_the_fetcher_can_release_its_connection() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    fetcher = Fetcher(client)
    assert not client.is_closed
    fetcher.close()
    assert client.is_closed


def test_yahoo_search_gives_the_sector_and_industry_of_a_share() -> None:
    body = json.dumps(
        {
            "quotes": [
                {
                    "quoteType": "EQUITY",
                    "symbol": "SAP.DE",
                    "sector": "Technology",
                    "industry": " Software—Application ",
                }
            ]
        }
    )
    found = parse_yahoo_search(body)
    assert found is not None
    assert (found.sector, found.industry, found.sector_known) == (
        "Technology",
        "Software—Application",
        True,
    )


def test_a_fund_or_an_odd_value_has_no_sector_but_the_source_still_answered() -> None:
    etf = parse_yahoo_search(json.dumps({"quotes": [{"quoteType": "ETF", "symbol": "IWDA.L"}]}))
    assert etf is not None and (etf.sector, etf.industry, etf.sector_known) == (None, None, True)
    odd = parse_yahoo_search(
        json.dumps(
            {"quotes": [{"quoteType": "EQUITY", "symbol": "X", "sector": 5, "industry": "x" * 300}]}
        )
    )
    assert odd is not None and odd.sector is None and odd.industry == "x" * 100  # cut to fit
    assert odd.sector_known


def test_openfigi_gives_no_sector() -> None:
    found = parse_openfigi(figi(("AAPL", "US", "Equity")))
    assert found is not None and found.sector is None and not found.sector_known

from datetime import date
from decimal import Decimal

import httpx
import pytest

from quant.providers import ecb, stooq, yahoo
from quant.providers.base import Fetcher, PriceSeries, ProviderError, normalise_minor_units

from .conftest import FIXTURES

WEEK = (date(2026, 9, 21), date(2026, 9, 25))


def test_yahoo_parses_bars_skips_nulls_and_uses_exchange_dates() -> None:
    series = yahoo.parse_chart((FIXTURES / "yahoo_chart_nvda.json").read_text(), *WEEK)
    assert series.currency == "USD"
    assert [b.date for b in series.bars] == [
        date(2026, 9, 21),
        date(2026, 9, 22),
        date(2026, 9, 24),
        date(2026, 9, 25),
    ]
    last = series.bars[-1]
    assert last.close == Decimal("185.43") and last.volume == 210556400
    assert series.bars[0].adj_close == Decimal("181.19")


def test_yahoo_filters_to_requested_range() -> None:
    series = yahoo.parse_chart(
        (FIXTURES / "yahoo_chart_nvda.json").read_text(), date(2026, 9, 24), date(2026, 9, 24)
    )
    assert [b.date for b in series.bars] == [date(2026, 9, 24)]


def test_yahoo_prices_are_rounded_to_4_decimals() -> None:
    body = (
        '{"chart":{"error":null,"result":[{"meta":{"currency":"USD","exchangeTimezoneName":"UTC"},'
        '"timestamp":[1790899200],"indicators":{"quote":[{"close":[233.9499969482422],'
        '"open":[1.00005],"high":[2.5],"low":[1.2],"volume":[10]}],'
        '"adjclose":[{"adjclose":[233.94999694]}]}}]}}'
    )
    bar = yahoo.parse_chart(body, date(2026, 10, 1), date(2026, 10, 3)).bars[0]
    assert bar.close == Decimal("233.9500")
    assert bar.adj_close == Decimal("233.9500")
    assert bar.open == Decimal("1.0000")  # ties round half-even
    assert bar.volume == 10


def test_yahoo_error_payload_raises() -> None:
    with pytest.raises(ProviderError, match="delisted"):
        yahoo.parse_chart((FIXTURES / "yahoo_chart_error.json").read_text(), *WEEK)


def test_pence_quotes_are_converted_to_pounds() -> None:
    raw = yahoo.parse_chart((FIXTURES / "yahoo_chart_gbp_pence.json").read_text(), *WEEK)
    series = normalise_minor_units(raw)
    assert series.currency == "GBP"
    assert series.bars[0].close == Decimal("52.355")


def test_stooq_csv() -> None:
    series = stooq.parse_csv((FIXTURES / "stooq_nvda.csv").read_text())
    assert series.currency is None and len(series.bars) == 4
    assert series.bars[-1].close == Decimal("185.43")
    assert stooq.parse_csv("No data") == PriceSeries(currency=None, bars=[])
    with pytest.raises(ProviderError):
        stooq.parse_csv("<html>blocked</html>")


def test_ecb_csv_skips_missing_observations() -> None:
    rates = ecb.parse_csv((FIXTURES / "ecb_exr.csv").read_text())
    assert len(rates) == 4
    usd = [r for r in rates if r.quote == "USD"]
    assert usd[-1].date == date(2026, 9, 25) and usd[-1].rate == Decimal("1.1698")
    assert ecb.parse_csv("") == []


def _fetcher(handler: httpx.MockTransport, recorded: list[tuple[str, int]]) -> Fetcher:
    return Fetcher(
        httpx.Client(transport=handler),
        recorder=lambda provider, key, status, body: recorded.append((provider, status)),
        retries=3,
        sleep=lambda _: None,
    )


def test_fetcher_retries_server_errors_and_records_every_response() -> None:
    calls = iter([503, 503, 200])
    transport = httpx.MockTransport(lambda req: httpx.Response(next(calls), text="ok"))
    recorded: list[tuple[str, int]] = []
    assert _fetcher(transport, recorded).get_text("yahoo", "https://example.test/x") == "ok"
    assert recorded == [("yahoo", 503), ("yahoo", 503), ("yahoo", 200)]


def test_fetcher_does_not_retry_client_errors() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(404, text="nope"))
    recorded: list[tuple[str, int]] = []
    with pytest.raises(ProviderError, match="404"):
        _fetcher(transport, recorded).get_text("stooq", "https://example.test/x")
    assert len(recorded) == 1


def test_yahoo_provider_end_to_end_over_http() -> None:
    body = (FIXTURES / "yahoo_chart_nvda.json").read_text()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=body)

    provider = yahoo.YahooProvider(_fetcher(httpx.MockTransport(handler), []))
    series = provider.fetch_eod("NVDA", *WEEK)
    assert len(series.bars) == 4
    assert seen[0].url.path.endswith("/NVDA") and seen[0].url.params["interval"] == "1d"
    assert "User-Agent" in seen[0].headers

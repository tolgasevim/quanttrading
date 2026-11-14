"""Instrument types, sector and country names (FR-23, FR-27)."""

import pytest

from quant.portfolio import instruments as k


@pytest.mark.parametrize(
    ("asset_class", "name", "isin", "type_"),
    [
        ("STOCK", "NVIDIA Corp.", "US67066G1040", k.STOCK),
        ("FUND", "iShares Core MSCI World UCITS ETF (Acc)", "IE00B4L5Y983", k.ETF),
        ("FUND", "VanEck Gold Miners UCITS ETF", None, k.ETF),  # miners: an equity fund
        ("FUND", "Xetra-Gold", None, k.ETC),
        ("FUND", "Invesco Physical Gold ETC", None, k.ETC),
        ("FUND", "WisdomTree WTI Crude Oil 3x Daily Short", None, k.LEVERAGED),
        ("FUND", "Franklin 2x Leveraged Nasdaq", None, k.LEVERAGED),
        ("FUND", "iShares Short Duration Bond ETF", None, k.BOND),  # "short" alone is not leverage
        ("FUND", "Amundi Euro Government Bond", None, k.BOND),
        ("FUND", "Some ELTIF Infrastructure", None, k.PRIVATE_FUND),
        ("DERIVATIVE", "Turbo Long DAX", None, k.KNOCKOUT),
        ("STOCK", "Mini Future Short Apple", None, k.KNOCKOUT),
        ("CRYPTO", "Bitcoin", "XF000BTC0017", k.CRYPTO),
        ("", "Mystery", "XF000ETH0019", k.CRYPTO),  # the XF prefix marks a coin
        (None, "No class given", None, k.STOCK),
        ("SOMETHING", "Odd", None, k.OTHER),
    ],
)
def test_classify(asset_class: str | None, name: str, isin: str | None, type_: str) -> None:
    assert k.classify(asset_class, name, isin).type == type_


def test_the_trading_book_and_the_flags() -> None:
    lev = k.classify("FUND", "Lyxor 3x Short Oil", None)
    assert lev.trading_book is True and "decays" in lev.flags[0]
    knock = k.classify("DERIVATIVE", "Turbo Long DAX", None)
    assert knock.trading_book is True and "barrier" in knock.flags[0]
    private = k.classify("FUND", "ELTIF Private Equity", None)
    assert private.trading_book is False and "hard to sell" in private.flags[0]
    etc = k.classify("FUND", "Gold ETC", None)
    assert etc.type == k.ETC and "issuer risk" in etc.flags[0]
    assert k.classify("STOCK", "Plain", None).flags == []
    assert {k.LEVERAGED, k.KNOCKOUT} == k.TRADING_BOOK_TYPES


def test_crypto_linked_shares_count_with_crypto() -> None:
    strategy = k.classify("STOCK", "Strategy", "US5949724083")
    assert strategy.type == k.STOCK and strategy.crypto_linked is True
    assert k.classify("CRYPTO", "Bitcoin", "XF000BTC0017").crypto_linked is True
    assert k.classify("STOCK", "Apple", "US0378331005").crypto_linked is False


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("Technology", "Information Technology"),
        ("Information Technology", "Information Technology"),
        ("Financial Services", "Financials"),
        ("Healthcare", "Health Care"),
        ("Consumer Cyclical", "Consumer Discretionary"),
        ("Communication Services", "Communication"),
        ("Real  Estate", "Real Estate"),
        ("Industrials & more", "Industrials & More"),
        ("", "Unknown"),
        (None, "Unknown"),
        ("   ", "Unknown"),
    ],
)
def test_sector_names_are_folded(raw: str | None, label: str) -> None:
    assert k.canonical_sector(raw) == label


def test_countries() -> None:
    assert k.country_code("United States") == "US"
    assert k.country_code("germany") == "DE"
    assert k.country_code("de") == "DE"
    assert k.country_code("Atlantis") == "??" and k.country_code(None) == "??"
    assert k.country_of_isin("US67066G1040") == "US"
    assert k.country_of_isin("XF000BTC0017") == "??"
    assert k.country_of_isin(None) == "??" and k.country_of_isin("1") == "??"
    assert k.currency_of_country("US") == "USD" and k.currency_of_country("DE") == "EUR"
    assert k.currency_of_country("??") == "Unknown"

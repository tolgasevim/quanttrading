"""What kind of instrument something is, and where it is exposed (FR-23, FR-27).

The broker's asset class (`STOCK`, `FUND`, `CRYPTO`, ...) is too coarse: a leveraged ETP and a
plain index fund are both a `FUND`. `classify` reads the name as well. It is a heuristic, shown
as a type and flags, never as a fact the user cannot see.

Country comes from the first two letters of the ISIN, which for a share is where it is
registered. That is a fair proxy for a share and a wrong answer for a fund (an Irish ETF holds
no Irish economy), so funds are looked through (`lookthrough.py`) or left as "fund, not looked
through". Sector names from different sources are folded onto one list (`canonical_sector`).
"""

import re
from dataclasses import dataclass, field

# Types, in the order the page lists them.
STOCK = "stock"
ETF = "etf"
ETC = "etc"  # a commodity ETC or an ETN
CRYPTO = "crypto"
LEVERAGED = "leveraged"  # a leveraged or inverse ETP: decays with its daily reset
KNOCKOUT = "knockout"  # a turbo or knock-out certificate
BOND = "bond"
PRIVATE_FUND = "private_fund"  # an ELTIF or another private-market fund
OTHER = "other"

# FR-27: these belong to the trading book (FR-48), not the long-term portfolio.
TRADING_BOOK_TYPES = frozenset({LEVERAGED, KNOCKOUT})

LEVERAGED_NAME = re.compile(
    r"(?<![\w.])(?:[1-5]x|x[1-5])(?![\w])"
    r"|\b(?:leveraged|leverage|inverse|ultrashort|ultra short)\b",
    re.IGNORECASE,
)
KNOCKOUT_NAME = re.compile(
    r"\b(knock[- ]?out|turbo|mini[- ]?future|open[- ]?end(?:ed)? (?:long|short))\b",
    re.IGNORECASE,
)
# An ETC or ETN, or a name that says the metal itself is held. A gold *miners* fund is an equity
# fund and does not match.
ETC_NAME = re.compile(
    r"\b(etc|etn|xetra-gold|bullion|physical (?:gold|silver|platinum|palladium))\b", re.IGNORECASE
)
BOND_NAME = re.compile(r"\b(bond|treasury|anleihe|aggregate|gilt|bund)\b", re.IGNORECASE)
PRIVATE_NAME = re.compile(r"\b(eltif|private (?:equity|markets?)|infrastructure fund)\b", re.I)

# Shares whose business is crypto: they count towards the combined crypto exposure (FR-27, FR-29).
CRYPTO_LINKED_ISINS = frozenset(
    {
        "US5949724083",  # Strategy (MicroStrategy)
        "US19260Q1076",  # Coinbase
        "US5657881067",  # Marathon Digital
        "US7672921050",  # Riot Platforms
    }
)


@dataclass(frozen=True)
class Kind:
    type: str
    flags: list[str] = field(default_factory=list)  # plain-words notes shown beside the position
    trading_book: bool = False
    crypto_linked: bool = False


def classify(asset_class: str | None, name: str | None, isin: str | None = None) -> Kind:
    """The type of an instrument from the broker's class, its name and its ISIN."""
    broker = (asset_class or "").upper()
    label = name or ""
    flags: list[str] = []
    if broker == "CRYPTO" or (isin or "").startswith("XF"):
        return Kind(CRYPTO, crypto_linked=True)
    if KNOCKOUT_NAME.search(label) or broker in {"DERIVATIVE", "WARRANT", "KNOCKOUT"}:
        flags.append("A knock-out product is worth nothing if the price touches its barrier.")
        return Kind(KNOCKOUT, flags, trading_book=True)
    if broker in {"FUND", "ETF", "ETP"} and LEVERAGED_NAME.search(label):
        flags.append("Leveraged or inverse: it resets daily, so it decays over time.")
        return Kind(LEVERAGED, flags, trading_book=True)
    if PRIVATE_NAME.search(label) or broker in {"PRIVATE_FUND", "ELTIF", "PRIVATE_MARKET"}:
        flags.append("Private-market fund: hard to sell, and its value is updated rarely.")
        return Kind(PRIVATE_FUND, flags)
    if broker in {"FUND", "ETF", "ETP"}:
        if ETC_NAME.search(label):
            flags.append("A commodity ETC: it carries issuer risk, and some roll futures.")
            return Kind(ETC, flags)
        if BOND_NAME.search(label):
            return Kind(BOND)
        return Kind(ETF)
    if broker == "BOND":
        return Kind(BOND)
    if broker in {"STOCK", "EQUITY", ""}:
        return Kind(STOCK, crypto_linked=(isin or "") in CRYPTO_LINKED_ISINS)
    return Kind(OTHER)


# --- sectors and countries -----------------------------------------------------------------

SECTOR_ALIASES: dict[str, str] = {
    "technology": "Information Technology",
    "information technology": "Information Technology",
    "it": "Information Technology",
    "financial services": "Financials",
    "financials": "Financials",
    "healthcare": "Health Care",
    "health care": "Health Care",
    "consumer cyclical": "Consumer Discretionary",
    "consumer discretionary": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "consumer staples": "Consumer Staples",
    "basic materials": "Materials",
    "materials": "Materials",
    "communication services": "Communication",
    "communication": "Communication",
    "communications": "Communication",
    "telecommunication": "Communication",
    "industrials": "Industrials",
    "energy": "Energy",
    "utilities": "Utilities",
    "real estate": "Real Estate",
}
CRYPTO_SECTOR = "Crypto"
UNKNOWN = "Unknown"


def canonical_sector(raw: str | None) -> str:
    """One name for a sector, whichever source gave it. Unknown stays "Unknown"."""
    if not raw or not raw.strip():
        return UNKNOWN
    key = " ".join(raw.lower().replace("&", "and").split())
    return SECTOR_ALIASES.get(key, raw.strip().title())


COUNTRY_NAMES: dict[str, str] = {
    "united states": "US",
    "usa": "US",
    "germany": "DE",
    "ireland": "IE",
    "luxembourg": "LU",
    "netherlands": "NL",
    "france": "FR",
    "united kingdom": "GB",
    "uk": "GB",
    "switzerland": "CH",
    "japan": "JP",
    "taiwan": "TW",
    "south korea": "KR",
    "korea": "KR",
    "china": "CN",
    "hong kong": "HK",
    "canada": "CA",
    "australia": "AU",
    "sweden": "SE",
    "denmark": "DK",
    "finland": "FI",
    "norway": "NO",
    "spain": "ES",
    "italy": "IT",
    "belgium": "BE",
    "austria": "AT",
    "israel": "IL",
    "singapore": "SG",
    "india": "IN",
    "brazil": "BR",
    "argentina": "AR",
    "cayman islands": "KY",
    "bermuda": "BM",
    "jersey": "JE",
    "uruguay": "UY",
}


def country_code(raw: str | None) -> str:
    """A country as two letters. A two-letter value passes through. Unknown stays "??"."""
    if not raw or not raw.strip():
        return "??"
    text = raw.strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return COUNTRY_NAMES.get(text.lower(), "??")


def country_of_isin(isin: str | None) -> str:
    """Where a share is registered, from the ISIN. Not for funds, and not for coins."""
    if not isin or len(isin) < 2 or not isin[:2].isalpha():
        return "??"
    prefix = isin[:2].upper()
    return "??" if prefix == "XF" else prefix


# The currency a country's shares are exposed to (not the currency a German exchange quotes them
# in: a US share bought in euros is still a dollar asset).
COUNTRY_CURRENCY: dict[str, str] = {
    "US": "USD", "CA": "CAD", "GB": "GBP", "CH": "CHF", "JP": "JPY", "CN": "CNY", "HK": "HKD",
    "TW": "TWD", "KR": "KRW", "AU": "AUD", "SE": "SEK", "DK": "DKK", "NO": "NOK", "IN": "INR",
    "BR": "BRL", "IL": "ILS", "SG": "SGD", "KY": "USD", "BM": "USD", "UY": "USD", "AR": "ARS",
    "DE": "EUR", "FR": "EUR", "NL": "EUR", "IE": "EUR", "LU": "EUR", "ES": "EUR", "IT": "EUR",
    "BE": "EUR", "AT": "EUR", "FI": "EUR", "PT": "EUR", "GR": "EUR",
}  # fmt: skip


def currency_of_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(country, UNKNOWN)

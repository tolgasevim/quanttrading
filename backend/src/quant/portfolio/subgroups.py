"""Tech sub-groups (FR-47).

Every tech share is in exactly one of nine sub-groups. The automatic tag reads, in this order:
a well-known name, the industry a data source gives, and the sector. The user can override it
for any instrument (`position_tags.subgroup`). A share outside tech is `non_tech`: it is reported
separately and has no cap (D31). Clean-tech is a tech-adjacent group (D31).

The weights here are direct holdings only: the constituents of a fund are not tagged, because a
holdings file has sectors but no industries.
"""

from dataclasses import dataclass
from decimal import Decimal

from quant.portfolio import instruments as kinds

SEMIS = "semis"
SOFTWARE = "software"
INTERNET = "internet"
HARDWARE = "hardware"
FINTECH = "fintech"
IT_SERVICES = "it_services"
MEDIA = "media"
CRYPTO_EQUITIES = "crypto_equities"
CLEANTECH = "cleantech"
NON_TECH = "non_tech"

LABELS: dict[str, str] = {
    SEMIS: "Semiconductors and equipment",
    SOFTWARE: "Software and cloud",
    INTERNET: "Internet platforms and consumer internet",
    HARDWARE: "Hardware and devices",
    FINTECH: "Fintech and payments",
    IT_SERVICES: "IT services",
    MEDIA: "Media and streaming",
    CRYPTO_EQUITIES: "Crypto-linked equities",
    CLEANTECH: "Clean-tech and energy tech",
    NON_TECH: "Not tech",
}
TECH_GROUPS = tuple(k for k in LABELS if k != NON_TECH)

# A word in the name that settles it. Lower case; the first group that matches wins.
NAME_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        CRYPTO_EQUITIES,
        (
            "coinbase",
            "microstrategy",
            "strategy inc",
            "marathon digital",
            "riot platforms",
            "bitcoin miner",
            "galaxy digital",
        ),
    ),
    (
        SEMIS,
        (
            "nvidia",
            "taiwan semiconductor",
            "tsmc",
            "asml",
            "infineon",
            "advanced micro",
            "intel corp",
            "qualcomm",
            "broadcom",
            "micron",
            "applied materials",
            "lam research",
            "kla corp",
            "arm holdings",
            "marvell",
            "texas instruments",
            "stmicro",
            "nxp",
            "aixtron",
            "sk hynix",
            "tokyo electron",
            "teradyne",
            "on semiconductor",
        ),
    ),
    (
        SOFTWARE,
        (
            "microsoft",
            "sap se",
            "oracle",
            "salesforce",
            "adobe",
            "atlassian",
            "servicenow",
            "palantir",
            "snowflake",
            "datadog",
            "intuit",
            "workday",
            "crowdstrike",
            "shopify",
            "cloudflare",
            "synopsys",
            "cadence design",
            "dassault",
            "software ag",
            "nemetschek",
        ),
    ),
    (
        INTERNET,
        (
            "alphabet",
            "meta platforms",
            "amazon",
            "alibaba",
            "uber",
            "booking",
            "airbnb",
            "tencent",
            "pinduoduo",
            "mercadolibre",
            "baidu",
            "doordash",
            "zalando",
            "delivery hero",
            "just eat",
        ),
    ),
    (
        HARDWARE,
        (
            "apple inc",
            "dell tech",
            "hewlett",
            "cisco",
            "xiaomi",
            "samsung electronics",
            "sony group",
            "logitech",
            "garmin",
            "super micro",
        ),
    ),
    (
        FINTECH,
        (
            "adyen",
            "paypal",
            "block inc",
            "klarna",
            "affirm",
            "wise plc",
            "nu holdings",
            "sofi",
            "toast",
        ),
    ),
    (
        IT_SERVICES,
        (
            "accenture",
            "epam",
            "capgemini",
            "cognizant",
            "infosys",
            "globant",
            "ibm",
            "international business machines",
            "atos",
            "bechtle",
        ),
    ),
    (MEDIA, ("netflix", "spotify", "walt disney", "warner", "roku", "live nation")),
    (
        CLEANTECH,
        (
            "sma solar",
            "enphase",
            "solaredge",
            "first solar",
            "vestas",
            "plug power",
            "nordex",
            "siemens energy",
            "meyer burger",
        ),
    ),
)

# A word in the industry name (Yahoo's spelling, lower case).
INDUSTRY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SEMIS, ("semiconductor",)),
    (SOFTWARE, ("software",)),
    (CLEANTECH, ("solar", "renewable")),
    (IT_SERVICES, ("information technology services",)),
    (HARDWARE, ("consumer electronics", "computer hardware", "communication equipment",
                "electronic components", "scientific & technical instruments")),
    (FINTECH, ("credit services", "financial data")),
    (MEDIA, ("entertainment", "broadcasting", "publishing")),
    (INTERNET, ("internet content", "internet retail", "internet")),
)  # fmt: skip


def auto_subgroup(
    sector: str | None, industry: str | None, name: str | None, isin: str | None
) -> str:
    """The sub-group of a share, or `non_tech`. `sector` is the canonical name."""
    if (isin or "") in kinds.CRYPTO_LINKED_ISINS:
        return CRYPTO_EQUITIES
    label = (name or "").lower()
    for group, words in NAME_HINTS:
        if any(w in label for w in words):
            return group
    ind = (industry or "").lower()
    for group, words in INDUSTRY_HINTS:
        if any(w in ind for w in words):
            return group
    if sector == "Information Technology":
        return SOFTWARE  # tech with no better clue: the biggest group
    if sector == "Communication":
        return INTERNET
    return NON_TECH


@dataclass(frozen=True)
class Tagged:
    isin: str
    name: str
    subgroup: str
    auto: str
    overridden: bool
    value: Decimal


def is_tech(subgroup: str) -> bool:
    return subgroup in TECH_GROUPS

"""ETF and ETC scorecards (FR-32, FR-33).

A scorecard lists the facts that matter for a fund or a commodity ETC and says what each one
means. The free price sources do not give these facts reliably, so an admin enters them from the
factsheet (`InstrumentFact`). A fact that is not entered is shown as unknown and listed under
`missing`: the card never fills a gap with a guess (FR-34).

It is information, not advice. Tax lines say "verify with your tax adviser".
"""

from dataclasses import dataclass, field
from decimal import Decimal

from quant.models import InstrumentFact

# Countries whose funds can be UCITS. A UCITS fund is required (FR-32): an ETF from elsewhere
# is not sold to German retail investors under the PRIIPs rules, and its tax is worse.
UCITS_DOMICILES = frozenset({"IE", "LU", "DE", "FR", "NL", "AT", "FI", "SE", "DK", "ES", "IT"})
HIGH_TER_ETF = Decimal("0.50")  # percent a year
HIGH_TER_ETC = Decimal("0.60")
SMALL_FUND_EUR = Decimal(100_000_000)

ETF_FIELDS = (
    "ter",
    "tracking_difference",
    "fund_size_eur",
    "replication",
    "distribution",
    "domicile",
)
ETC_FIELDS = ("ter", "issuer", "replication", "delivery_claim")


@dataclass
class Line:
    label: str
    value: str  # what the fact is, or "unknown"
    verdict: str  # "good" | "watch" | "bad" | "info" | "unknown"
    note: str


@dataclass
class Scorecard:
    isin: str
    name: str
    kind: str  # "etf" | "etc"
    lines: list[Line] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    overlaps: list[str] = field(default_factory=list)
    tax_note: str = ""


def _pct(v: Decimal | None) -> str:
    return "unknown" if v is None else f"{v.normalize():f}%"


def build(
    isin: str, name: str, kind: str, fact: InstrumentFact | None, overlaps: list[str]
) -> Scorecard:
    card = Scorecard(isin, name, kind, overlaps=overlaps)
    f = fact
    if kind == "etc":
        _etc(card, f)
    else:
        _etf(card, f)
    return card


def _etf(card: Scorecard, f: InstrumentFact | None) -> None:
    lines = card.lines
    ter = f.ter if f else None
    if ter is None:
        lines.append(Line("Cost (TER)", "unknown", "unknown", "Not entered."))
        card.missing.append("ter")
    else:
        high = ter > HIGH_TER_ETF
        lines.append(
            Line(
                "Cost (TER)",
                _pct(ter),
                "watch" if high else "good",
                "Above 0.50% a year: check for a cheaper fund on the same index."
                if high
                else "A low yearly cost.",
            )
        )
    td = f.tracking_difference if f else None
    if td is None:
        lines.append(Line("Tracking difference", "unknown", "unknown", "Not entered."))
        card.missing.append("tracking_difference")
    else:
        lines.append(
            Line(
                "Tracking difference",
                _pct(td),
                "good" if abs(td) <= Decimal("0.30") else "watch",
                "How far the fund trailed (or beat) its index over a year, after cost.",
            )
        )
    size = f.fund_size_eur if f else None
    if size is None:
        lines.append(Line("Fund size", "unknown", "unknown", "Not entered."))
        card.missing.append("fund_size_eur")
    else:
        small = size < SMALL_FUND_EUR
        lines.append(
            Line(
                "Fund size",
                f"{size / 1_000_000:.0f} million EUR",
                "watch" if small else "good",
                "Under 100 million EUR: small funds are sometimes closed."
                if small
                else "Large enough that closure is unlikely.",
            )
        )
    rep = f.replication if f else None
    if rep is None:
        lines.append(Line("Replication", "unknown", "unknown", "Not entered."))
        card.missing.append("replication")
    else:
        notes = {
            "physical": "Holds the shares of the index.",
            "sampling": "Holds a sample of the index: it can drift a little more.",
            "synthetic": "Follows the index through a swap: there is counterparty risk.",
        }
        lines.append(
            Line(
                "Replication",
                rep,
                "watch" if rep == "synthetic" else "good",
                notes.get(rep, "Check how it follows the index."),
            )
        )
    dist = f.distribution if f else None
    if dist is None:
        lines.append(Line("Pays out or accumulates", "unknown", "unknown", "Not entered."))
        card.missing.append("distribution")
    else:
        lines.append(
            Line(
                "Pays out or accumulates",
                "accumulating" if dist == "acc" else "distributing",
                "info",
                "An accumulating fund pays no cash, but the German tax office still taxes a "
                "notional gain each year (Vorabpauschale). A distributing fund pays taxable "
                "dividends. Verify with your tax adviser.",
            )
        )
        card.tax_note = (
            "Funds are taxed under the Investmentsteuergesetz: 30% of the gain is exempt for an "
            "equity fund (Teilfreistellung). Verify with your tax adviser."
        )
    dom = f.domicile if f else None
    if dom is None:
        lines.append(Line("Domicile", "unknown", "unknown", "Not entered."))
        card.missing.append("domicile")
    else:
        ucits = dom.upper() in UCITS_DOMICILES
        lines.append(
            Line(
                "Domicile",
                dom.upper(),
                "good" if ucits else "bad",
                "A UCITS country."
                if ucits
                else "Not a usual UCITS country. A UCITS fund is required here.",
            )
        )


def _etc(card: Scorecard, f: InstrumentFact | None) -> None:
    lines = card.lines
    rep = f.replication if f else None
    if rep is None:
        lines.append(Line("Physical or synthetic", "unknown", "unknown", "Not entered."))
        card.missing.append("replication")
    else:
        notes = {
            "physical": "Backed by metal in a vault. Issuer and custodian risk remain.",
            "synthetic": "Follows the price through a swap: there is counterparty risk.",
        }
        lines.append(
            Line(
                "Physical or synthetic",
                rep,
                "good" if rep == "physical" else "watch",
                notes.get(rep, "Futures-based: roll cost and contango can lose money."),
            )
        )
    claim = f.delivery_claim if f else None
    if claim is None:
        lines.append(Line("Delivery claim", "unknown", "unknown", "Not entered."))
        card.missing.append("delivery_claim")
    else:
        lines.append(
            Line(
                "Delivery claim",
                "yes" if claim else "no",
                "good" if claim else "info",
                "A right to have the metal delivered. Verify with your tax adviser: a gold ETC "
                "with a delivery claim may be tax-free after one year of holding."
                if claim
                else "No right to the metal itself.",
            )
        )
        card.tax_note = (
            "An ETC is not a fund. Gains on a gold ETC with a delivery claim may be tax-free "
            "after a year; others are taxed as capital gains. Verify with your tax adviser."
        )
    ter = f.ter if f else None
    if ter is None:
        lines.append(Line("Cost (TER)", "unknown", "unknown", "Not entered."))
        card.missing.append("ter")
    else:
        lines.append(
            Line(
                "Cost (TER)",
                _pct(ter),
                "watch" if ter > HIGH_TER_ETC else "good",
                "Above 0.60% a year." if ter > HIGH_TER_ETC else "A fair yearly cost.",
            )
        )
    issuer = f.issuer if f else None
    if issuer is None:
        lines.append(Line("Issuer", "unknown", "unknown", "Not entered."))
        card.missing.append("issuer")
    else:
        lines.append(
            Line("Issuer", issuer, "info", "An ETC is a debt note: you also depend on the issuer.")
        )
    roll = f.roll_method if f else None
    if rep not in (None, "physical"):
        if roll is None:
            lines.append(Line("Roll method", "unknown", "unknown", "Not entered."))
            card.missing.append("roll_method")
        else:
            lines.append(
                Line("Roll method", roll, "info", "How it moves from one future to the next.")
            )

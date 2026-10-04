import csv
import io
from collections.abc import Sequence
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant.models import User

from .conftest import login, make_user
from .pdfutil import crypto_statement_pdf

FIELDS = [
    "datetime", "date", "category", "type", "asset_class", "name", "symbol", "shares", "price",
    "amount", "fee", "tax", "currency", "transaction_id", "description",
]  # fmt: skip

A, B, BTC, ETH = "US0000000001", "US0000000002", "XF000BTC0017", "XF000ETH0019"
SPIN, ORPHAN = "US0000000003", "US0000000009"


def row(
    n: int, day: str, category: str, type_: str, cls: str, name: str, isin: str, **kw: str
) -> dict[str, str]:
    base = dict.fromkeys(FIELDS, "")
    base.update(
        datetime=f"{day}T09:00:00.000000Z", date=day, category=category, type=type_,
        asset_class=cls, name=name, symbol=isin, currency="EUR",
        transaction_id=f"00000000-0000-4000-8000-{n:012d}",
    )  # fmt: skip
    base.update(kw)
    return base


def history(extra: list[dict[str, str]] | None = None) -> str:
    rows = [
        # Stock A: buy 10 for 1000 (fee 1), sell 4 for 600 (fee 1, tax withheld 10) in 2025.
        row(1, "2024-02-01", "TRADING", "BUY", "STOCK", "Alpha Corp", A, shares="10", price="100", amount="-1000", fee="-1"),
        row(2, "2025-03-01", "TRADING", "SELL", "STOCK", "Alpha Corp", A, shares="-4", price="150", amount="600", fee="-1", tax="-10"),
        # Stock B: bought for 500 (fee 1), sold for 400 (fee 1) in 2025: a loss.
        row(3, "2024-02-02", "TRADING", "BUY", "STOCK", "Beta Inc", B, shares="5", price="100", amount="-500", fee="-1"),
        row(4, "2025-04-01", "TRADING", "SELL", "STOCK", "Beta Inc", B, shares="-5", price="80", amount="400", fee="-1"),
        # Crypto: bitcoin arrived as a free receipt worth 4000; ether was bought for 4000 (fee 2).
        row(5, "2024-03-01", "DELIVERY", "FREE_RECEIPT", "CRYPTO", "Bitcoin", BTC, shares="0.1", price="40000"),
        row(6, "2024-03-02", "TRADING", "BUY", "CRYPTO", "Ethereum", ETH, shares="2", price="2000", amount="-4000", fee="-2"),
        # A spin-off: units arrive, the broker gives no cost.
        row(7, "2025-06-01", "CORPORATE_ACTION", "SPIN_OFF", "STOCK", "Spin Co", SPIN, shares="6"),
        # Corporate-action cash that fits no action.
        row(8, "2025-07-01", "CASH", "EXCHANGE", "STOCK", "", ORPHAN, amount="5.8"),
        *(extra or []),
    ]  # fmt: skip
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# Printed the way the broker does: trailing zeros dropped, so whole amounts have no comma.
STATEMENT_ROWS = [
    ("0,1", "Bitcoin", "50.000", "4.000", "1.000", "5.000"),
    ("2", "Ethereum", "2.500", "4.000", "1.000", "5.000"),
]


def import_history(client: TestClient, text: str) -> None:
    preview = client.post(
        "/api/imports", files={"file": ("t.csv", text.encode(), "text/csv")}
    ).json()
    assert client.post(f"/api/imports/{preview['id']}/commit").status_code == 200


def upload(
    client: TestClient, rows: Sequence[tuple[str, ...]] = STATEMENT_ROWS, total: str = "10.000"
) -> dict:  # type: ignore[type-arg]
    pdf = crypto_statement_pdf(list(rows), total=total)  # type: ignore[arg-type]
    response = client.post(
        "/api/holdings/statements/crypto", files={"file": ("s.pdf", pdf, "application/pdf")}
    )
    assert response.status_code == 201, response.text
    body: dict = response.json()  # type: ignore[type-arg]
    return body


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    import_history(client, history())
    return client


def by_name(body: dict) -> dict[str, dict]:  # type: ignore[type-arg]
    return {p["name"]: p for p in body["positions"]}


def test_cost_basis_per_position(owner: TestClient) -> None:
    alpha = by_name(owner.get("/api/holdings").json())["Alpha Corp"]
    # 6 of 10 units left: 600 of the 1000 purchase value and 0.60 of the 1.00 fee.
    assert (alpha["purchase_value"], alpha["acquisition_costs"], alpha["average_cost"]) == (
        "600.00",
        "0.60",
        "100.1000",
    )
    assert (
        alpha["cost_flags"] == [] and alpha["market_value"] is None
    )  # no price source for stocks yet


def test_realised_profit_and_loss_by_year_and_instrument(owner: TestClient) -> None:
    realised = owner.get("/api/holdings").json()["realised"]
    [year] = realised["by_year"]
    # Alpha: 600 - 1 fee - 400 - 0.40 fee share = 198.60. Beta: 400 - 1 - 500 - 1 = -102.
    assert (year["year"], year["gains"], year["losses"], year["net"]) == (
        2025,
        "198.60",
        "-102.00",
        "96.60",
    )
    assert (year["fees"], year["tax_withheld"], year["disposals"]) == ("2.00", "10.00", 2)
    assert realised["net_total"] == "96.60"
    assert [(i["name"], i["realised_pnl"]) for i in realised["best"]] == [("Alpha Corp", "198.60")]
    assert [(i["name"], i["realised_pnl"]) for i in realised["worst"]] == [("Beta Inc", "-102.00")]


def test_market_value_and_unrealised_pnl_come_from_the_statement_price(owner: TestClient) -> None:
    body = upload(owner)
    positions = by_name(body)
    btc, eth = positions["Bitcoin"], positions["Ethereum"]
    assert (btc["price"], btc["market_value"], btc["unrealised_pnl"], btc["unrealised_pct"]) == (
        "50000", "5000.00", "1000.00", "25.00",
    )  # fmt: skip
    # Ether's 2.00 of fees count against the gain: 5000 - 4002.
    assert (eth["market_value"], eth["unrealised_pnl"], eth["unrealised_pct"]) == (
        "5000.00",
        "998.00",
        "24.94",
    )
    assert btc["price_as_of"] == "2026-09-27"
    assert positions["Alpha Corp"]["market_value"] is None  # only priced positions get a value
    assert "price_derived" in btc["cost_flags"] and "price_derived" not in eth["cost_flags"]


def test_statement_purchase_value_is_checked_against_the_rebuilt_cost(owner: TestClient) -> None:
    [recon] = upload(owner)["reconciliations"]
    assert all(check["ok"] for check in recon["cost_checks"]) and len(recon["cost_checks"]) == 2
    assert {c["name"] for c in recon["cost_checks"]} == {
        "Bitcoin",
        "Ethereum",
    }  # fees excluded: 4000 each


def test_a_wrong_statement_cost_is_reported_with_the_difference(owner: TestClient) -> None:
    rows = [("0,1", "Bitcoin", "50.000", "3.500", "1.500", "5.000"), STATEMENT_ROWS[1]]
    [recon] = upload(owner, rows)["reconciliations"]
    bad = [c for c in recon["cost_checks"] if not c["ok"]]
    assert [(c["name"], c["statement_cost"], c["computed_cost"], c["difference"]) for c in bad] == [
        ("Bitcoin", "3500.00", "4000.00", "500.00")
    ]


def test_a_purchase_after_the_statement_date_does_not_break_the_cost_check(
    client: TestClient, admin: User
) -> None:
    login(client, admin.email)
    later = row(
        9,
        "2026-10-02",
        "TRADING",
        "BUY",
        "CRYPTO",
        "Ethereum",
        ETH,
        shares="1",
        price="2500",
        amount="-2500",
        fee="-1",
    )
    import_history(client, history([later]))
    body = upload(client)
    assert all(c["ok"] for c in body["reconciliations"][0]["cost_checks"])
    assert (
        by_name(body)["Ethereum"]["purchase_value"] == "6500.00"
    )  # the current holding includes it


def test_items_that_need_the_owner_are_listed(owner: TestClient) -> None:
    review = owner.get("/api/holdings").json()["review"]
    assert review["cost_unknown"] == 1  # the spin-off
    [cash] = review["unattributed_cash"]
    assert (cash["isin"], cash["type"], cash["amount"], cash["date"]) == (
        ORPHAN,
        "EXCHANGE",
        "5.80",
        "2025-07-01",
    )
    spin = by_name(owner.get("/api/holdings").json())["Spin Co"]
    assert spin["cost_flags"] == ["cost_unknown"] and spin["purchase_value"] == "0.00"


def test_other_users_see_none_of_it(owner: TestClient, db: Session) -> None:
    make_user(db, "sister@example.com")
    other = TestClient(owner.app)
    login(other, "sister@example.com")
    body = other.get("/api/holdings").json()
    assert (
        body["positions"] == []
        and body["realised"]["by_year"] == []
        and body["realised"]["net_total"] == "0.00"
    )
    assert body["review"] == {"cost_unknown": 0, "unattributed_cash": []}


def test_money_is_serialised_as_exact_decimals(owner: TestClient) -> None:
    raw = owner.get("/api/holdings").text
    assert '"net_total":"96.60"' in raw.replace(" ", "")
    assert D("96.60") == D(owner.get("/api/holdings").json()["realised"]["net_total"])

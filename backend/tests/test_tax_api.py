import pytest
from fastapi.testclient import TestClient

from quant.models import User

from .conftest import login
from .test_holdings_pnl_api import A, history, import_history, row


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    extra = [
        # A dividend of 20 gross with 5 withheld, and a 3 refund from a loss-pot adjustment.
        row(20, "2025-05-01", "CASH", "DIVIDEND", "STOCK", "Alpha Corp", A, amount="20", tax="-5"),
        row(21, "2025-12-20", "CASH", "TAX_OPTIMIZATION", "", "", "", tax="3"),
    ]
    import_history(client, history(extra))
    return client


def test_tax_estimate_per_year(owner: TestClient) -> None:
    body = owner.get("/api/tax").json()
    [y2025] = [y for y in body["years"] if y["year"] == 2025]
    # Realised: Alpha +198.60 and Beta -102.00 are both shares: 96.60, plus a 20 dividend.
    assert (y2025["stock_pnl"], y2025["income"]) == ("96.60", "20.00")
    assert (y2025["taxable_before_allowance"], y2025["allowance_used"]) == ("116.60", "116.60")
    assert (y2025["taxable"], y2025["tax"]) == ("0.00", "0.00")
    # Withheld: 10 on the sale and 5 on the dividend, less the 3 refund.
    assert (y2025["withheld"], y2025["to_settle"]) == ("12.00", "-12.00")
    assert y2025["crypto"]["taxable_gain"] == "0.00"  # nothing crypto was sold


def test_assumptions_are_listed(owner: TestClient) -> None:
    assumptions = owner.get("/api/tax").json()["assumptions"]
    assert any("Teilfreistellung" in a for a in assumptions)
    assert any("tax advisor" in a for a in assumptions)


def test_tax_needs_a_login(client: TestClient) -> None:
    assert client.get("/api/tax").status_code == 401


def test_income_uses_the_instrument_class_and_refunds_may_sit_in_amount(
    client: TestClient, admin: User
) -> None:
    login(client, admin.email)
    fund = "IE0000000001"
    extra = [
        row(30, "2024-06-01", "TRADING", "BUY", "FUND", "Fund X", fund, shares="10", price="100", amount="-1000"),
        # No asset class on the distribution row: the fund's class comes from its purchase.
        row(31, "2025-05-01", "CASH", "DISTRIBUTION", "", "Fund X", fund, amount="100"),
        row(32, "2025-12-20", "CASH", "TAX_OPTIMIZATION", "", "", "", amount="4"),
    ]  # fmt: skip
    import_history(client, history(extra))
    [y2025] = [y for y in client.get("/api/tax").json()["years"] if y["year"] == 2025]
    assert y2025["income"] == "70.00"  # 100 less the 30% Teilfreistellung
    assert y2025["withheld"] == "6.00"  # 10 on the sale, less the 4 refund

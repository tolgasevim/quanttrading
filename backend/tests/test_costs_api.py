import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant.models import User

from .conftest import login, make_user
from .test_holdings_pnl_api import SPIN, by_name, history, import_history, row


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    import_history(client, history())
    return client


def items(client: TestClient) -> dict[str, dict]:  # type: ignore[type-arg]
    return {i["isin"]: i for i in client.get("/api/costs").json()["items"]}


def test_instruments_without_a_cost_are_listed(owner: TestClient) -> None:
    spin = items(owner)[SPIN]
    assert (spin["name"], spin["open_units"], spin["sold_units"], spin["unit_cost"]) == (
        "Spin Co",
        "6",
        "0",
        None,
    )
    assert len(items(owner)) == 1  # nothing else lacks a cost


def test_entering_a_cost_feeds_the_cost_basis(owner: TestClient) -> None:
    before = owner.get("/api/holdings").json()
    assert before["review"]["cost_unknown"] == 1
    response = owner.put(
        f"/api/costs/{SPIN}", json={"unit_cost": "12.5", "note": "from the broker"}
    )
    assert response.status_code == 200
    entry = {i["isin"]: i for i in response.json()["items"]}[SPIN]
    assert (entry["unit_cost"], entry["note"]) == ("12.5", "from the broker")
    after = owner.get("/api/holdings").json()
    spin = by_name(after)["Spin Co"]
    assert (spin["purchase_value"], spin["average_cost"]) == ("75.00", "12.5000")
    assert "cost_entered" in spin["cost_flags"] and "cost_unknown" not in spin["cost_flags"]
    assert after["review"]["cost_unknown"] == 0


def test_an_entered_cost_can_be_changed_and_removed(owner: TestClient) -> None:
    owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "10"})
    owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "20"})
    assert items(owner)[SPIN]["unit_cost"] == "20"
    assert owner.delete(f"/api/costs/{SPIN}").status_code == 204
    assert items(owner)[SPIN]["unit_cost"] is None
    assert owner.get("/api/holdings").json()["review"]["cost_unknown"] == 1
    assert owner.delete(f"/api/costs/{SPIN}").status_code == 404


def test_an_entered_cost_corrects_sales_already_made(client: TestClient, admin: User) -> None:
    login(client, admin.email)
    sale = row(
        9, "2025-08-01", "TRADING", "SELL", "STOCK", "Spin Co", SPIN,
        shares="-6", price="10", amount="60", fee="-1",
    )  # fmt: skip
    import_history(client, history([sale]))

    def spin_sales() -> tuple[dict, dict]:  # type: ignore[type-arg]
        year = [y for y in client.get("/api/tax").json()["years"] if y["year"] == 2025][0]
        realised = client.get("/api/holdings").json()["realised"]
        spin = [i for i in realised["best"] + realised["worst"] if i["name"] == "Spin Co"][0]
        return year, spin

    year, spin = spin_sales()
    assert year["cost_unknown_sales"] == 1 and spin["cost_unknown"] is True
    assert spin["realised_pnl"] == "59.00"  # no cost: the proceeds less the fee

    client.put(f"/api/costs/{SPIN}", json={"unit_cost": "4"})
    year, spin = spin_sales()
    assert year["cost_unknown_sales"] == 0 and spin["cost_unknown"] is False
    assert spin["realised_pnl"] == "35.00"  # 60 - 1 - 6 x 4


def test_bad_input_is_refused(owner: TestClient) -> None:
    assert owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "-1"}).status_code == 422
    assert owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "abc"}).status_code == 422
    assert owner.put("/api/costs/not-an-isin", json={"unit_cost": "1"}).status_code == 422
    assert (
        owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "1", "note": "x" * 201}).status_code
        == 422
    )
    # An ISIN that is not in the history cannot get an entry.
    assert owner.put("/api/costs/US0000000999", json={"unit_cost": "1"}).status_code == 404


def test_costs_are_private_to_each_user(owner: TestClient, db: Session) -> None:
    owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "12.5"})
    make_user(db, "sister@example.com")
    other = TestClient(owner.app)
    login(other, "sister@example.com")
    assert other.get("/api/costs").json() == {"items": []}
    assert other.put(f"/api/costs/{SPIN}", json={"unit_cost": "1"}).status_code == 404
    assert other.delete(f"/api/costs/{SPIN}").status_code == 404
    assert items(owner)[SPIN]["unit_cost"] == "12.5"  # untouched


def test_costs_need_a_login(client: TestClient) -> None:
    assert client.get("/api/costs").status_code == 401
    assert client.put(f"/api/costs/{SPIN}", json={"unit_cost": "1"}).status_code == 401
    assert client.delete(f"/api/costs/{SPIN}").status_code == 401


def test_a_second_save_that_races_the_first_updates_instead_of_failing(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant.api import costs

    assert owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "10"}).status_code == 200
    real = costs._entry
    calls = {"n": 0}

    def stale_first(db, user_id, isin):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return (
            None if calls["n"] == 1 else real(db, user_id, isin)
        )  # the other save's row is unseen

    monkeypatch.setattr(costs, "_entry", stale_first)
    response = owner.put(f"/api/costs/{SPIN}", json={"unit_cost": "20", "note": "second"})
    assert response.status_code == 200
    entry = {i["isin"]: i for i in response.json()["items"]}[SPIN]
    assert (entry["unit_cost"], entry["note"]) == ("20", "second")

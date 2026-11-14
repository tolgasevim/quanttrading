"""Manual add, edit and delete of transactions, with an audit trail (FR-14)."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from quant import rls
from quant.models import Transaction, TransactionAudit, User
from quant.portfolio.positions import compute_positions

from .conftest import login, make_user
from .test_imports_api import upload

NVDA = "US67066G1040"
BUY = {
    "side": "buy",
    "date": "2025-03-03",
    "isin": NVDA,
    "name": "NVIDIA Corp.",
    "shares": "4",
    "price": "100",
    "fee": "1",
}


def positions(db: Session, user: User) -> dict[str, Decimal]:
    rls.bypass(db)
    rows = db.scalars(select(Transaction).where(Transaction.user_id == user.id)).all()
    return {isin: p.quantity for isin, p in compute_positions(rows).items()}


def test_add_buy_and_sell(owner: TestClient, db: Session, admin: User) -> None:
    buy = owner.post("/api/transactions", json=BUY)
    assert buy.status_code == 201, buy.text
    body = buy.json()
    assert body["source"] == "manual" and body["editable"] is True
    assert Decimal(body["shares"]) == 4 and Decimal(body["amount"]) == -400
    assert Decimal(body["fee"]) == -1
    sell = owner.post(
        "/api/transactions",
        json={**BUY, "side": "sell", "date": "2025-04-01", "shares": "1.5", "price": "120"},
    ).json()
    assert Decimal(sell["shares"]) == Decimal("-1.5") and Decimal(sell["amount"]) == 180
    assert positions(db, admin) == {NVDA: Decimal("2.5")}
    # The cash balance counts both: -400 - 1 + 180 - 1.
    assert Decimal(owner.get("/api/transactions").json()["cash_balance"]) == Decimal("-222")


def test_an_explicit_amount_wins_over_price_times_shares(owner: TestClient) -> None:
    body = owner.post("/api/transactions", json={**BUY, "amount": "399.5"}).json()
    assert Decimal(body["amount"]) == Decimal("-399.5")


@pytest.mark.parametrize(
    ("patch", "why"),
    [
        ({"isin": None}, "ISIN"),
        ({"shares": "0"}, "shares"),
        ({"shares": None}, "shares"),
        ({"price": None}, "price"),
    ],
)
def test_invalid_trades_are_refused(owner: TestClient, patch: dict, why: str) -> None:  # type: ignore[type-arg]
    response = owner.post("/api/transactions", json={**BUY, **patch})
    assert response.status_code == 422 and why in response.text
    assert owner.get("/api/transactions").json()["counts"] == {}


def test_cash_entries(owner: TestClient) -> None:
    deposit = owner.post(
        "/api/transactions", json={"side": "deposit", "date": "2025-01-02", "amount": "1000"}
    ).json()
    withdrawal = owner.post(
        "/api/transactions", json={"side": "withdrawal", "date": "2025-01-03", "amount": "250"}
    ).json()
    dividend = owner.post(
        "/api/transactions",
        json={"side": "dividend", "date": "2025-01-04", "isin": NVDA, "amount": "5", "tax": "1"},
    ).json()
    assert [Decimal(x["amount"]) for x in (deposit, withdrawal, dividend)] == [1000, -250, 5]
    assert Decimal(owner.get("/api/transactions").json()["cash_balance"]) == Decimal("754")
    no_amount = owner.post("/api/transactions", json={"side": "fee", "date": "2025-01-05"})
    assert no_amount.status_code == 422
    negative = owner.post(
        "/api/transactions", json={"side": "deposit", "date": "2025-01-05", "amount": "-5"}
    )
    assert negative.status_code == 422  # the side says which way: numbers are positive


def test_edit_manual_row(owner: TestClient, db: Session, admin: User) -> None:
    tx_id = owner.post("/api/transactions", json=BUY).json()["id"]
    edited = owner.patch(f"/api/transactions/{tx_id}", json={"shares": "5", "price": "90"})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert Decimal(body["shares"]) == 5 and Decimal(body["amount"]) == -450
    assert Decimal(body["fee"]) == -1  # untouched fields stay
    # Changing the side of a manual row turns a buy into a sell.
    sold = owner.patch(f"/api/transactions/{tx_id}", json={"side": "sell"}).json()
    assert Decimal(sold["shares"]) == -5 and Decimal(sold["amount"]) == 450
    moved = owner.patch(f"/api/transactions/{tx_id}", json={"date": "2025-05-05"}).json()
    assert moved["date"] == "2025-05-05"
    assert positions(db, admin) == {NVDA: Decimal("-5")}


def test_edit_validates_the_result(owner: TestClient) -> None:
    tx_id = owner.post("/api/transactions", json=BUY).json()["id"]
    bad = owner.patch(f"/api/transactions/{tx_id}", json={"shares": "0"})
    assert bad.status_code == 422
    # The row did not change.
    items = owner.get("/api/transactions").json()["items"]
    assert Decimal(items[0]["shares"]) == 4


def test_edit_imported_rows(owner: TestClient, db: Session, admin: User) -> None:
    preview = upload(owner)
    owner.post(f"/api/imports/{preview['id']}/commit")
    items = owner.get("/api/transactions?include_card=true&limit=50").json()["items"]
    buy = next(i for i in items if i["type"] == "BUY" and i["isin"] == NVDA)
    assert buy["source"] == "tr" and buy["editable"] is True
    changed = owner.patch(f"/api/transactions/{buy['id']}", json={"price": "55", "amount": "550"})
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert Decimal(body["price"]) == 55 and Decimal(body["amount"]) == -550  # sign kept
    assert Decimal(body["shares"]) == 10 and body["type"] == "BUY"
    # The side of an imported row cannot change.
    assert owner.patch(f"/api/transactions/{buy['id']}", json={"side": "sell"}).status_code == 422
    # A corporate action can be deleted but not edited.
    action = next(i for i in items if i["kind"] == "corporate_action")
    assert action["editable"] is False
    refused = owner.patch(f"/api/transactions/{action['id']}", json={"price": "1"})
    assert refused.status_code == 422 and "corporate_action" in refused.text


def test_delete_and_audit(owner: TestClient, db: Session, admin: User) -> None:
    tx_id = owner.post("/api/transactions", json=BUY).json()["id"]
    owner.patch(f"/api/transactions/{tx_id}", json={"shares": "6"})
    assert owner.delete(f"/api/transactions/{tx_id}").status_code == 204
    assert owner.delete(f"/api/transactions/{tx_id}").status_code == 404
    assert owner.get("/api/transactions").json()["counts"] == {}
    trail = owner.get("/api/transactions/audit").json()
    assert [a["action"] for a in trail] == ["delete", "edit", "add"]
    assert trail[0]["after"] is None and Decimal(trail[0]["before"]["shares"]) == 6
    edit = trail[1]
    assert Decimal(edit["before"]["shares"]) == 4 and Decimal(edit["after"]["shares"]) == 6
    assert trail[2]["before"] is None and trail[2]["source"] == "manual"


def test_a_deleted_imported_row_stays_deleted_after_a_new_import(owner: TestClient) -> None:
    first = upload(owner)
    owner.post(f"/api/imports/{first['id']}/commit")
    items = owner.get("/api/transactions").json()["items"]
    victim = next(i for i in items if i["type"] == "BUY" and i["isin"] == NVDA)
    assert owner.delete(f"/api/transactions/{victim['id']}").status_code == 204
    second = upload(owner)
    committed = owner.post(f"/api/imports/{second['id']}/commit").json()
    assert committed["rows_inserted"] == 0  # the deleted row was not brought back
    after = owner.get("/api/transactions").json()["items"]
    assert all(i["id"] != victim["id"] for i in after)
    assert not any(i["type"] == "BUY" and i["isin"] == NVDA for i in after)
    trail = owner.get("/api/transactions/audit").json()
    assert trail[0]["action"] == "delete" and trail[0]["source"] == "tr"


def test_other_users_cannot_touch_my_rows(
    owner: TestClient, client: TestClient, db: Session
) -> None:
    tx_id = owner.post("/api/transactions", json=BUY).json()["id"]
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert client.patch(f"/api/transactions/{tx_id}", json={"shares": "9"}).status_code == 404
    assert client.delete(f"/api/transactions/{tx_id}").status_code == 404
    assert client.get("/api/transactions/audit").json() == []
    assert client.get("/api/transactions").json()["counts"] == {}


def test_the_audit_trail_cannot_be_changed_by_the_app(
    owner: TestClient, db: Session, admin: User
) -> None:
    owner.post("/api/transactions", json=BUY)
    rls.scope_to_user(db, admin.id)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(TransactionAudit)) == 1
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(text("UPDATE transaction_audits SET action = 'x'"))
    db.rollback()
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(text("DELETE FROM transaction_audits"))
    db.rollback()


def test_the_disclaimer_gate_covers_the_new_endpoints(client: TestClient, db: Session) -> None:
    make_user(db, "new@example.com", accepted=False)
    assert login(client, "new@example.com") == 200
    assert client.post("/api/transactions", json=BUY).status_code == 403
    assert client.get("/api/transactions/audit").status_code == 403

"""Cash, earmarked reserves and the opportunity unlock (FR-28, FR-28a, FR-28b)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from quant import rls
from quant.ai import service as ai_service
from quant.ingest.alerts import create_alerts
from quant.models import Instrument, Notification, PriceEOD, User
from quant.portfolio import cash, service

from .conftest import login, make_user

TODAY = date(2025, 2, 28)
ALPHA = "US0000000001"
ETF = "IE0000000002"
COIN = "XF000BTC0017"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "today", lambda: TODAY)


def instrument(
    db: Session, code: str, isin: str, asset_class: str = "stock", volume: int | None = 100_000
) -> Instrument:
    row = Instrument(code=code, isin=isin, name=code, asset_class=asset_class, currency="EUR")
    db.add(row)
    db.flush()
    return row


def closes(db: Session, row: Instrument, values: dict[int, str], volume: int | None = None) -> None:
    """A close for each given number of days before TODAY (0 is today)."""
    for ago, close in values.items():
        db.add(
            PriceEOD(
                instrument_id=row.id,
                date=TODAY - timedelta(days=ago),
                close=D(close),
                currency="EUR",
                source="t",
                volume=volume,
            )
        )
    db.commit()


def deposit(client: TestClient, amount: str, day: str = "2025-01-02") -> None:
    body = {"side": "deposit", "date": day, "amount": amount}
    assert client.post("/api/transactions", json=body).status_code == 201


def enter(client: TestClient, amount: str, **kw: Any) -> dict[str, Any]:
    response = client.post("/api/cash/balance", json={"amount": amount, **kw})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def get(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/cash")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def reserve(client: TestClient, name: str, kind: str, amount: str, **kw: Any) -> dict[str, Any]:
    response = client.post(
        "/api/cash/reserves", json={"name": name, "kind": kind, "amount": amount, **kw}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# --- FR-28: the balance and its cross-check ---------------------------------------------


def test_without_an_entry_the_history_is_the_balance(owner: TestClient) -> None:
    deposit(owner, "1000")
    body = get(owner)
    assert body["status"]["cash_source"] == "history" and D(body["status"]["cash"]) == 1000
    assert body["status"]["entered"] is None and body["status"]["mismatch"] is False
    assert any("not entered" in w for w in body["warnings"])


def test_a_close_balance_is_fine_and_a_far_one_warns(owner: TestClient) -> None:
    deposit(owner, "1000")
    near = enter(owner, "1015")  # 1.5% off
    assert near["status"]["cash_source"] == "entered"
    assert D(near["status"]["history"]) == 1000 and D(near["status"]["difference"]) == 15
    assert near["status"]["mismatch"] is False and near["warnings"] == []
    far = enter(owner, "1100")  # 9.1% off
    assert far["status"]["mismatch"] is True
    assert any("differs" in w for w in far["warnings"])
    assert [D(b["amount"]) for b in far["balances"]] == [1100, 1015]  # newest first


def test_the_history_is_compared_up_to_the_entered_day(owner: TestClient) -> None:
    deposit(owner, "1000", "2025-01-02")
    deposit(owner, "500", "2025-02-20")
    body = enter(owner, "1000", as_of="2025-02-01")
    assert D(body["status"]["history"]) == 1000 and body["status"]["mismatch"] is False
    assert enter(owner, "1500")["status"]["mismatch"] is False  # as of today: 1500 in history


def test_an_entry_cannot_be_in_the_future(owner: TestClient) -> None:
    response = owner.post("/api/cash/balance", json={"amount": "10", "as_of": "2025-03-01"})
    assert response.status_code == 422


# --- FR-28a: reserves -----------------------------------------------------------------------


def test_reserves_come_off_the_investable_cash(owner: TestClient) -> None:
    deposit(owner, "5000")
    enter(owner, "5000")
    reserve(owner, "Car", "opportunity", "3000", target_date="2026-09-01")
    body = reserve(owner, "Tax", "hard", "500")
    status = body["status"]
    assert D(status["reserves_total"]) == 3500 and D(status["investable"]) == 1500
    assert D(status["cash"]) == 5000 and D(body["net_worth"]) == 5000  # reserves stay in net worth
    assert [r["name"] for r in body["reserves"]] == ["Car", "Tax"]
    assert body["warnings"] == []


def test_cash_below_the_reserves_is_a_shortfall(owner: TestClient) -> None:
    enter(owner, "1000")
    body = reserve(owner, "Car", "hard", "1600")
    assert D(body["status"]["shortfall"]) == 600 and D(body["status"]["investable"]) == 0
    assert any("below your reserves" in w for w in body["warnings"])


def test_change_and_release_a_reserve(owner: TestClient) -> None:
    enter(owner, "5000")
    car = reserve(owner, "Car", "opportunity", "3000")["reserves"][0]
    smaller = owner.patch(
        f"/api/cash/reserves/{car['id']}", json={"amount": "2000", "name": "  New   car "}
    ).json()
    assert smaller["reserves"][0]["name"] == "New car"
    assert D(smaller["status"]["investable"]) == 3000  # the freed money is investable
    assert owner.patch(f"/api/cash/reserves/{car['id']}", json={"kind": "x"}).status_code == 422
    assert owner.patch(f"/api/cash/reserves/{car['id']}", json={"amount": "0"}).status_code == 422
    # A reserve can lose its target date.
    dated = owner.patch(f"/api/cash/reserves/{car['id']}", json={"target_date": "2026-01-01"})
    assert dated.json()["reserves"][0]["target_date"] == "2026-01-01"
    cleared = owner.patch(f"/api/cash/reserves/{car['id']}", json={"clear_target_date": True})
    assert cleared.json()["reserves"][0]["target_date"] is None
    released = owner.post(f"/api/cash/reserves/{car['id']}/release").json()
    assert released["reserves"] == [] and D(released["status"]["investable"]) == 5000
    assert owner.post(f"/api/cash/reserves/{car['id']}/release").status_code == 422
    assert owner.patch(f"/api/cash/reserves/{car['id']}", json={"amount": "5"}).status_code == 422
    actions = [e["action"] for e in get(owner)["events"]]
    assert actions == ["release", "change", "change", "change", "create"]


def test_invalid_reserves_are_refused(owner: TestClient) -> None:
    bad = [
        {"name": "x", "kind": "soft", "amount": "10"},
        {"name": "x", "kind": "hard", "amount": "0"},
        {"name": "x", "kind": "hard", "amount": "-5"},
        {"name": "   ", "kind": "hard", "amount": "5"},
        {"name": "", "kind": "hard", "amount": "5"},
    ]
    for body in bad:
        assert owner.post("/api/cash/reserves", json=body).status_code == 422, body
    assert get(owner)["reserves"] == []


def test_the_band_follows_the_investable_cash(owner: TestClient, db: Session) -> None:
    alpha = instrument(db, "ALPHA", ALPHA)
    closes(db, alpha, {0: "100", 1: "100"})
    deposit(owner, "3000")
    buy = {"side": "buy", "date": "2025-01-06", "isin": ALPHA, "shares": "10", "price": "100"}
    assert owner.post("/api/transactions", json=buy).status_code == 201
    body = enter(owner, "2000")
    assert D(body["securities_value"]) == 1000 and D(body["net_worth"]) == 3000
    band = body["band"]
    assert band["status"] == "high" and D(band["pct"]).quantize(D("0.1")) == D("66.7")
    assert D(band["excess"]) == D("1850.00")  # down to 5% of 3000
    # Reserving most of it brings the investable cash back inside the band.
    body = reserve(owner, "Car", "hard", "1900")
    assert body["band"]["status"] == "ok" and D(body["band"]["excess"]) == 0
    low = reserve(owner, "More", "hard", "100")
    assert low["band"]["status"] == "low"


def test_no_holdings_and_no_cash_has_no_band(owner: TestClient) -> None:
    assert get(owner)["band"] is None


# --- isolation ---------------------------------------------------------------------------


def test_reserves_are_private(owner: TestClient, client: TestClient, db: Session) -> None:
    car = reserve(owner, "Car", "hard", "100")["reserves"][0]
    enter(owner, "100")
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert get(client)["reserves"] == [] and get(client)["balances"] == []
    assert client.patch(f"/api/cash/reserves/{car['id']}", json={"amount": "1"}).status_code == 404
    assert client.post(f"/api/cash/reserves/{car['id']}/release").status_code == 404
    body = {"isin": ALPHA, "amount": "10"}
    assert client.post(f"/api/cash/reserves/{car['id']}/unlock", json=body).status_code == 404


def test_the_history_tables_cannot_be_changed_by_the_app(
    owner: TestClient, db: Session, admin: User
) -> None:
    reserve(owner, "Car", "hard", "100")
    enter(owner, "100")
    rls.scope_to_user(db, admin.id)
    db.rollback()
    statements = (
        "UPDATE cash_balances SET user_id = user_id",
        "DELETE FROM cash_balances",
        "UPDATE reserve_events SET user_id = user_id",
        "DELETE FROM reserve_events",
    )
    for statement in statements:
        with pytest.raises(ProgrammingError, match="permission denied"):
            db.execute(text(statement))
        db.rollback()


def test_the_disclaimer_gate_covers_cash(client: TestClient, db: Session) -> None:
    make_user(db, "new@example.com", accepted=False)
    assert login(client, "new@example.com") == 200
    assert client.get("/api/cash").status_code == 403
    assert client.post("/api/cash/balance", json={"amount": "1"}).status_code == 403


# --- FR-28b: the opportunity unlock ---------------------------------------------------------


@pytest.fixture
def market(db: Session) -> dict[str, Instrument]:
    """Alpha (a liquid share), an ETF, a coin, and the two index funds, all quiet."""
    rows = {
        "alpha": instrument(db, "ALPHA", ALPHA),
        "etf": instrument(db, "ETF", ETF, "etf"),
        "coin": instrument(db, "BTC", COIN, "crypto"),
        "SXRV": instrument(db, "SXRV", "IE00B53SZB19", "etf"),
        "EUNL": instrument(db, "EUNL", "IE00B4L5Y983", "etf"),
    }
    for key in ("alpha", "etf", "coin"):
        closes(db, rows[key], {0: "100", 100: "100"}, volume=100_000)  # 10M a day
    for key in ("SXRV", "EUNL"):
        closes(db, rows[key], {0: "100", 100: "100"})  # no fall from the high
    return rows


def setup_unlock(client: TestClient, cash_amount: str = "5000") -> dict[str, Any]:
    enter(client, cash_amount)
    return reserve(client, "Car", "opportunity", "1000")["reserves"][0]


def check(client: TestClient, rid: str, isin: str, amount: str, manual: bool = False) -> Any:
    response = client.post(
        f"/api/cash/reserves/{rid}/unlock-check",
        json={"isin": isin, "amount": amount, "manual": manual},
    )
    assert response.status_code == 200, response.text
    return response.json()


def passed(result: dict[str, Any], key: str) -> Any:
    return next(c["passed"] for c in result["checks"] if c["key"] == key)


def test_no_trigger_means_no_unlock(owner: TestClient, market: dict[str, Instrument]) -> None:
    car = setup_unlock(owner)
    result = check(owner, car["id"], ALPHA, "400")
    assert result["allowed"] is False and result["triggers"] == []
    assert passed(result, "manual") is False and passed(result, "market_dislocation") is False
    assert passed(result, "instrument_score") is None  # needs the factor scores
    assert passed(result, "share") is True and passed(result, "cash") is True


def test_a_manual_flag_is_a_trigger(owner: TestClient, market: dict[str, Instrument]) -> None:
    car = setup_unlock(owner)
    result = check(owner, car["id"], ALPHA, "400", manual=True)
    assert result["allowed"] is True and result["triggers"] == ["manual"]
    assert passed(result, "class") is True and passed(result, "liquidity") is True
    assert passed(result, "single_stock_cap") is True  # 400 of 5000 = 8%


def test_a_market_fall_is_a_trigger(
    owner: TestClient, db: Session, market: dict[str, Instrument]
) -> None:
    db.add(
        PriceEOD(
            instrument_id=market["SXRV"].id,
            date=TODAY - timedelta(days=50),
            close=D("130"),  # the 52-week high
            currency="EUR",
            source="t",
        )
    )
    db.commit()  # latest 100 is 23% below 130
    car = setup_unlock(owner)
    result = check(owner, car["id"], ALPHA, "400")
    assert result["allowed"] is True and result["triggers"] == ["market_dislocation"]
    text_ = next(c["detail"] for c in result["checks"] if c["key"] == "market_dislocation")
    assert "SXRV is 23.1% below" in text_ and "EUNL is 0.0% below" in text_


def test_a_small_fall_is_not_a_trigger(
    owner: TestClient, db: Session, market: dict[str, Instrument]
) -> None:
    db.add(
        PriceEOD(
            instrument_id=market["SXRV"].id,
            date=TODAY - timedelta(days=50),
            close=D("124"),  # 19.4% below
            currency="EUR",
            source="t",
        )
    )
    db.commit()
    car = setup_unlock(owner)
    assert check(owner, car["id"], ALPHA, "400")["triggers"] == []


def test_the_limits_are_strict(owner: TestClient, market: dict[str, Instrument]) -> None:
    car = setup_unlock(owner)
    too_much = check(owner, car["id"], ALPHA, "600", manual=True)  # over half of 1000
    assert too_much["allowed"] is False and passed(too_much, "share") is False
    assert check(owner, car["id"], ALPHA, "500", manual=True)["allowed"] is True  # exactly half
    coin = check(owner, car["id"], COIN, "100", manual=True)
    assert coin["allowed"] is False and passed(coin, "class") is False  # no crypto
    unknown = check(owner, car["id"], "US9999999999", "100", manual=True)
    assert unknown["allowed"] is False and passed(unknown, "instrument") is False
    etf = check(owner, car["id"], ETF, "100", manual=True)
    assert etf["allowed"] is True
    assert all(c["key"] != "single_stock_cap" for c in etf["checks"])  # the cap is for shares


def test_a_hard_reserve_is_never_unlocked(owner: TestClient, market: dict[str, Instrument]) -> None:
    enter(owner, "5000")
    hard = reserve(owner, "Tax", "hard", "1000")["reserves"][0]
    result = check(owner, hard["id"], ALPHA, "100", manual=True)
    assert result["allowed"] is False and passed(result, "reserve_type") is False
    response = owner.post(
        f"/api/cash/reserves/{hard['id']}/unlock",
        json={"isin": ALPHA, "amount": "100", "manual": True},
    )
    assert response.status_code == 422 and "Opportunity reserve" in response.text


def test_the_single_stock_cap_uses_the_whole_portfolio(
    owner: TestClient, market: dict[str, Instrument]
) -> None:
    car = setup_unlock(owner, cash_amount="1500")  # 400 of 1500 would be 26.7%
    result = check(owner, car["id"], ALPHA, "400", manual=True)
    assert passed(result, "single_stock_cap") is False and result["allowed"] is False
    assert check(owner, car["id"], ALPHA, "100", manual=True)["allowed"] is True  # 6.7%


def test_the_cash_must_be_there(owner: TestClient, market: dict[str, Instrument]) -> None:
    enter(owner, "300")
    car = reserve(owner, "Car", "opportunity", "1000")["reserves"][0]
    result = check(owner, car["id"], ETF, "400", manual=True)
    assert passed(result, "cash") is False and result["allowed"] is False


def test_thin_trading_blocks_and_missing_volume_does_not(
    owner: TestClient, db: Session, market: dict[str, Instrument]
) -> None:
    thin = instrument(db, "THIN", "US0000000003")
    closes(db, thin, {0: "10"}, volume=10)  # 100 a day
    novol = instrument(db, "NOVOL", "US0000000004")
    closes(db, novol, {0: "10"})
    car = setup_unlock(owner)
    blocked = check(owner, car["id"], "US0000000003", "100", manual=True)
    assert passed(blocked, "liquidity") is False and blocked["allowed"] is False
    unknown_volume = check(owner, car["id"], "US0000000004", "100", manual=True)
    assert passed(unknown_volume, "liquidity") is None and unknown_volume["allowed"] is True


def test_confirming_an_unlock_shrinks_the_reserve(
    owner: TestClient, market: dict[str, Instrument]
) -> None:
    car = setup_unlock(owner)
    refused = owner.post(
        f"/api/cash/reserves/{car['id']}/unlock",
        json={"isin": ALPHA, "amount": "400", "manual": False},
    )
    assert refused.status_code == 422 and "no trigger" in refused.text
    assert D(get(owner)["reserves"][0]["amount"]) == 1000  # nothing changed
    ok = owner.post(
        f"/api/cash/reserves/{car['id']}/unlock",
        json={"isin": ALPHA, "amount": "400", "manual": True},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert D(body["reserves"][0]["amount"]) == 600
    assert D(body["reserves"][0]["loaned"]) == 400  # out on loan until it is put back
    assert D(body["status"]["investable"]) == 4400
    unlock = body["events"][0]
    assert unlock["action"] == "unlock" and D(unlock["amount_before"]) == 1000
    assert D(unlock["amount_after"]) == 600
    assert unlock["detail"]["isin"] == ALPHA and unlock["detail"]["triggers"] == ["manual"]
    # Half of what is left is 300: the same 400 is now too much.
    again = owner.post(
        f"/api/cash/reserves/{car['id']}/unlock",
        json={"isin": ALPHA, "amount": "400", "manual": True},
    )
    assert again.status_code == 422 and "half" in again.text


def dated_reserve(client: TestClient, target: str) -> dict[str, Any]:
    enter(client, "5000")
    return reserve(client, "Car", "opportunity", "1000", target_date=target)["reserves"][0]


def test_no_unlock_within_six_months_of_the_target_date(
    owner: TestClient, market: dict[str, Instrument]
) -> None:
    near = dated_reserve(owner, "2025-06-01")  # 93 days away
    result = check(owner, near["id"], ALPHA, "400", manual=True)
    assert passed(result, "window") is False and result["allowed"] is False
    blocked = owner.post(
        f"/api/cash/reserves/{near['id']}/unlock",
        json={"isin": ALPHA, "amount": "400", "manual": True},
    )
    assert blocked.status_code == 422 and "six months" in blocked.text
    past = check(owner, near["id"], ALPHA, "400", manual=True)
    assert "93 days to 2025-06-01" in next(
        c["detail"] for c in past["checks"] if c["key"] == "window"
    )


def test_the_stress_test_shows_the_shortfall(
    owner: TestClient, market: dict[str, Instrument]
) -> None:
    far = dated_reserve(owner, "2026-09-01")
    result = check(owner, far["id"], ALPHA, "400", manual=True)
    assert passed(result, "window") is True and result["allowed"] is True
    stress = next(c for c in result["checks"] if c["key"] == "stress")
    assert stress["passed"] is None and stress["blocking"] is False
    assert "a 20% fall leaves the reserve 80.00 short" in stress["detail"]
    assert "a 35% fall leaves the reserve 140.00 short" in stress["detail"]
    assert "2026-09-01" in stress["detail"]


def test_a_confirmed_unlock_is_a_loan_a_pick_and_can_be_refilled(
    owner: TestClient, db: Session, admin: User, market: dict[str, Instrument]
) -> None:
    from quant.models import AiPick

    far = dated_reserve(owner, "2026-09-01")
    unlock = {"isin": ALPHA, "amount": "400", "manual": True}
    body = owner.post(f"/api/cash/reserves/{far['id']}/unlock", json=unlock).json()
    car = body["reserves"][0]
    assert D(car["amount"]) == 600 and D(car["loaned"]) == 400
    assert D(car["refill_per_month"]) == D("22.22")  # 400 over the 18 months left
    # The unlock is in the pick log, as a buy, to be scored like any other call.
    rls.bypass(db)
    [pick] = db.scalars(select(AiPick).where(AiPick.user_id == admin.id))
    assert (pick.direction, pick.isin, pick.question) == ("buy", ALPHA, "Unlock of the reserve Car")
    assert pick.price == D("100") and pick.held is False
    # Putting money back.
    back = owner.post(f"/api/cash/reserves/{far['id']}/refill", json={"amount": "150"}).json()
    assert D(back["reserves"][0]["amount"]) == 750 and D(back["reserves"][0]["loaned"]) == 250
    assert back["events"][0]["action"] == "refill"
    too_much = owner.post(f"/api/cash/reserves/{far['id']}/refill", json={"amount": "300"})
    assert too_much.status_code == 422 and "on loan" in too_much.text
    assert (
        owner.post(f"/api/cash/reserves/{far['id']}/refill", json={"amount": "0"}).status_code
        == 422
    )


def test_a_refill_needs_free_cash(owner: TestClient, market: dict[str, Instrument]) -> None:
    far = dated_reserve(owner, "2026-09-01")
    unlock = {"isin": ALPHA, "amount": "400", "manual": True}
    assert owner.post(f"/api/cash/reserves/{far['id']}/unlock", json=unlock).status_code == 200
    # The money left the broker's cash when the user bought: the user enters the new balance.
    enter(owner, "100")
    reserve(owner, "Tax", "hard", "100")
    refused = owner.post(f"/api/cash/reserves/{far['id']}/refill", json={"amount": "50"})
    assert refused.status_code == 422 and "free to put back" in refused.text


def test_a_loan_that_cannot_come_back_raises_an_alert(
    owner: TestClient, db: Session, market: dict[str, Instrument]
) -> None:
    far = dated_reserve(owner, "2026-09-01")
    unlock = {"isin": ALPHA, "amount": "400", "manual": True}
    assert owner.post(f"/api/cash/reserves/{far['id']}/unlock", json=unlock).status_code == 200
    enter(owner, "700")  # 600 reserved, 100 free: cannot refill 400
    assert run_alerts(db, date(2026, 5, 1)) == 0  # 4 months out: not yet
    assert run_alerts(db, date(2026, 6, 15)) == 1  # under 90 days
    rls.bypass(db)
    kinds = {n.kind for n in db.scalars(select(Notification))}
    assert "reserve_loan" in kinds
    assert run_alerts(db, date(2026, 6, 20)) == 0  # once a month
    enter(owner, "2000")  # now there is cash to put it back
    assert run_alerts(db, date(2026, 7, 20)) == 0


# --- the alert and the AI ------------------------------------------------------------------


def run_alerts(db: Session, today: date) -> int:
    rls.bypass(db)
    result = create_alerts(db, today, datetime(today.year, today.month, today.day, 12, tzinfo=UTC))
    assert result.errors == {} and result.warnings == {}
    return result.rows_written


def test_a_reserve_breach_raises_one_alert_a_week(owner: TestClient, db: Session) -> None:
    enter(owner, "500")
    reserve(owner, "Car", "hard", "800")
    assert run_alerts(db, date(2025, 2, 26)) == 1
    assert run_alerts(db, date(2025, 2, 27)) == 0  # the same week
    rls.bypass(db)
    [n] = db.scalars(select(Notification).where(Notification.kind == "reserve_breach"))
    assert n.severity == "warning" and "300.00 EUR below" in n.body
    assert run_alerts(db, date(2025, 3, 5)) == 1  # the next week


def test_no_breach_no_alert(owner: TestClient, db: Session) -> None:
    enter(owner, "900")
    reserve(owner, "Car", "hard", "800")
    assert run_alerts(db, date(2025, 2, 26)) == 0


def test_the_ai_sees_reserves_as_not_investable() -> None:
    class R:
        def __init__(self, name: str, kind: str, amount: D) -> None:
            self.name, self.kind, self.amount = name, kind, amount

    state = cash.CashStatus(
        entered=D(5000), entered_as_of=TODAY, history=D(5000), difference=D(0),
        difference_pct=D(0), mismatch=False, cash=D(5000), cash_source="entered",
        reserves_total=D(3000), shortfall=D(0), investable=D(2000),
    )  # fmt: skip
    reserves = [R("Car", "opportunity", D(3000))]
    shown = ai_service._cash_context(state, reserves, anonymise=False)
    assert shown["investable_cash_eur"] == "2000.00"
    assert shown["reserves"] == [{"name": "Car", "kind": "opportunity", "amount_eur": "3000.00"}]
    hidden = ai_service._cash_context(state, reserves, anonymise=True)
    assert "3000" not in str(hidden) and "2000" not in str(hidden)
    assert hidden["reserves"] == [{"name": "Car", "kind": "opportunity"}]
    context, _, _ = ai_service.build_context(service.Holdings(positions=[]), False, shown)
    assert '"cash":{"investable_cash_eur":"2000.00"' in context
    assert "Never suggest investing a reserve" in ai_service.SYSTEM

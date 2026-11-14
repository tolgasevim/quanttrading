"""Target allocation, drift, guardrails, rebalancing, the deployment plan and the journal
(FR-40 to FR-44, FR-49)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from quant import rls
from quant.ingest.alerts import create_alerts
from quant.models import AiPick, Instrument, Notification, PriceEOD, Transaction, User
from quant.portfolio import allocation, service

from .conftest import login, make_user

TODAY = date(2025, 2, 28)
NVDA = "US67066G1040"
WORLD = "IE0000000002"
LEV = "IE0000000004"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "today", lambda: TODAY)


def instrument(db: Session, isin: str, name: str, cls: str = "stock", **kw: Any) -> Instrument:
    row = Instrument(code=isin, isin=isin, name=name, asset_class=cls, currency="EUR", **kw)
    db.add(row)
    db.flush()
    for ago in (0, 1):
        db.add(
            PriceEOD(
                instrument_id=row.id, date=TODAY - timedelta(days=ago), close=D("100"),
                currency="EUR", source="t",
            )
        )  # fmt: skip
    db.commit()
    return row


def buy(
    client: TestClient, isin: str, name: str, cls: str, shares: str, day: str = "2025-01-06"
) -> None:
    body = {
        "side": "buy", "date": day, "isin": isin, "name": name, "asset_class": cls,
        "shares": shares, "price": "100",
    }  # fmt: skip
    assert client.post("/api/transactions", json=body).status_code == 201


def enter_cash(client: TestClient, amount: str) -> None:
    deposit = {"side": "deposit", "date": "2025-01-02", "amount": "5000"}
    assert client.post("/api/transactions", json=deposit).status_code == 201
    assert client.post("/api/cash/balance", json={"amount": amount}).status_code == 201


@pytest.fixture
def book(owner: TestClient, db: Session) -> TestClient:
    """5000 in all: NVIDIA 2000 (a tech share), a world ETF 1000, free cash 2000."""
    instrument(db, NVDA, "NVIDIA Corp.", sector="Technology", symbols={"yahoo": "NVDA"})
    instrument(db, WORLD, "World ETF", "etf")
    buy(owner, NVDA, "NVIDIA Corp.", "STOCK", "20")
    buy(owner, WORLD, "World ETF", "FUND", "10")
    enter_cash(owner, "2000")
    return owner


def get(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/allocation")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def buckets(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {b["bucket"]: b for b in body["buckets"]}


# --- FR-40 and FR-41: targets and drift -----------------------------------------------------------


def test_the_default_targets_and_the_drift(book: TestClient) -> None:
    body = get(book)
    assert body["has_portfolio"] and D(body["base"]) == 5000
    assert body["custom_targets"] is False
    b = buckets(body)
    assert [D(b[k]["pct"]) for k in ("tech_stocks", "etfs", "cash")] == [40, 20, 40]
    assert D(b["tech_stocks"]["target"]) == 35 and D(b["tech_stocks"]["low"]) == 30
    assert D(b["etfs"]["high"]) == 50 and D(b["crypto"]["high"]) == 10
    # Tech 40% sits on the edge of its 30-40 band; ETFs are below, cash is above.
    status = {k: v["status"] for k, v in b.items()}
    assert status == {
        "tech_stocks": "ok", "etfs": "below", "crypto": "ok",
        "commodities": "ok", "cash": "above", "other": "ok",
    }  # fmt: skip
    assert D(b["etfs"]["gap_eur"]) == 1250 and D(b["tech_stocks"]["gap_eur"]) == -250
    assert D(b["cash"]["gap_eur"]) == -1750 and D(b["etfs"]["gap_pp"]) == -25


def test_reserves_are_not_part_of_the_cash_bucket(book: TestClient) -> None:
    reserve = {"name": "Car", "kind": "hard", "amount": "1500"}
    assert book.post("/api/cash/reserves", json=reserve).status_code == 201
    body = get(book)
    assert D(body["free_cash"]) == 500 and D(body["base"]) == 3500
    assert D(buckets(body)["cash"]["pct"]).quantize(D("0.1")) == D("14.3")


def test_set_validate_and_reset_the_targets(book: TestClient) -> None:
    wide = {
        "tech_stocks": {"target": "25", "low": "20", "high": "30"},
        "etfs": {"target": "45", "low": "40", "high": "50"},
        "crypto": {"target": "7", "low": "0", "high": "10"},
        "commodities": {"target": "3", "low": "0", "high": "5"},
        "cash": {"target": "5", "low": "3", "high": "10"},
        "other": {"target": "5", "low": "0", "high": "10"},
    }
    ok = book.put("/api/allocation/targets", json={"targets": wide})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["custom_targets"] is True
    assert buckets(body)["tech_stocks"]["status"] == "above"  # 40% against a band of 20-30
    too_many = {**wide, "etfs": {"target": "90", "low": "40", "high": "95"}}
    refused = book.put("/api/allocation/targets", json={"targets": too_many})
    assert refused.status_code == 422 and "more than 100%" in refused.text
    inverted = {**wide, "etfs": {"target": "45", "low": "50", "high": "40"}}
    assert book.put("/api/allocation/targets", json={"targets": inverted}).status_code == 422
    missing = {k: v for k, v in wide.items() if k != "other"}
    assert book.put("/api/allocation/targets", json={"targets": missing}).status_code == 422
    unknown = {**wide, "bonds": {"target": "1", "low": "0", "high": "2"}}
    assert book.put("/api/allocation/targets", json={"targets": unknown}).status_code == 422
    reset = book.delete("/api/allocation/targets").json()
    assert reset["custom_targets"] is False
    assert buckets(reset)["tech_stocks"]["status"] == "ok"


def test_an_empty_portfolio_has_nothing_to_say(owner: TestClient) -> None:
    body = get(owner)
    assert body["has_portfolio"] is False and body["buckets"] == [] and body["suggestions"] == []


# --- FR-43: guardrails ---------------------------------------------------------------------------


def rules(body: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["rule"], r["subject"]): r for r in body["rules"]}


def test_a_single_share_above_the_cap_is_a_breach(book: TestClient) -> None:
    body = get(book)
    nvda = rules(body)[("single_stock", "NVIDIA Corp.")]
    assert D(nvda["current_pct"]) == 40 and D(nvda["limit_pct"]) == 15 and nvda["breached"]
    assert body["breaches"] == 1 and body["rules"][0]["breached"] is True  # breaches come first
    assert rules(body)[("min_cash", "Free cash")]["breached"] is False
    assert rules(body)[("crypto", "Coins and crypto-linked shares")]["breached"] is False
    assert D(rules(body)[("sector", "Information Technology")]["current_pct"]) == 40


def test_change_the_guardrails(book: TestClient) -> None:
    body = book.put("/api/allocation/guardrails", json={"max_single_stock": "50"}).json()
    assert body["custom_guardrails"] is True and body["breaches"] == 0
    assert D(body["guardrails"]["max_single_stock"]) == 50
    assert D(body["guardrails"]["max_crypto"]) == 10  # the others keep their value
    tight = book.put("/api/allocation/guardrails", json={"min_cash": "60", "max_sector": "30"})
    after = tight.json()
    assert after["breaches"] == 2
    breached = {r["rule"] for r in after["rules"] if r["breached"]}
    assert breached == {"min_cash", "sector"}
    assert book.put("/api/allocation/guardrails", json={"max_sector": "101"}).status_code == 422


def test_the_trading_book_is_apart_and_capped(book: TestClient, db: Session) -> None:
    instrument(db, LEV, "WisdomTree Oil 3x Daily Short", "etf")
    buy(book, LEV, "WisdomTree Oil 3x Daily Short", "FUND", "3")
    body = get(book)
    assert D(body["trading_book"]) == 300
    assert D(body["base"]) == 5000  # not part of the drift maths
    cap = rules(body)[("trading_book", "Leveraged products and knock-outs")]
    assert cap["breached"] is True and D(cap["limit_pct"]) == 3  # 300 of 5300 is 5.7%


# --- FR-42: rebalancing suggestions ---------------------------------------------------------------


def test_new_money_goes_to_the_buckets_that_are_short(book: TestClient) -> None:
    body = get(book)
    new_money = {s["bucket"]: s for s in body["suggestions"] if s["kind"] == "new_money"}
    # 2000 free cash is 1750 above its 5% target: it covers every gap in full.
    assert {k: D(v["amount"]) for k, v in new_money.items()} == {
        "etfs": 1250, "crypto": 350, "commodities": 150,
    }  # fmt: skip
    etfs = new_money["etfs"]
    assert "1,250.00 EUR" in etfs["text"] and "World ETF" in etfs["text"]
    assert etfs["to_isin"] == WORLD
    assert new_money["crypto"]["to_isin"] is None  # nothing held yet: no example
    assert not any(s["kind"] == "sell" for s in body["suggestions"])  # no sale needed


@pytest.fixture
def plan_book(owner: TestClient, db: Session) -> TestClient:
    """The same 5000, but NVIDIA was bought partly through a savings plan (100 a month)."""
    instrument(db, NVDA, "NVIDIA Corp.", sector="Technology", symbols={"yahoo": "NVDA"})
    instrument(db, WORLD, "World ETF", "etf")
    buy(owner, NVDA, "NVIDIA Corp.", "STOCK", "17")
    buy(owner, WORLD, "World ETF", "FUND", "10")
    user = db.scalar(select(User).where(User.email == "owner@example.com"))
    assert user is not None
    rls.bypass(db)
    for n, day in enumerate((date(2024, 12, 5), date(2025, 1, 5), date(2025, 2, 5))):
        db.add(
            Transaction(
                user_id=user.id,
                broker="tr",
                external_id=f"plan-{n}",
                executed_at=datetime(day.year, day.month, day.day, 9, tzinfo=UTC),
                date=day,
                kind="trade",
                category="TRADING",
                type="BUY",
                asset_class="STOCK",
                isin=NVDA,
                name="NVIDIA Corp.",
                shares=D(1),
                price=D(100),
                amount=D(-100),
                currency="EUR",
                savings_plan=True,
            )  # fmt: skip
        )
    db.commit()
    enter_cash(owner, "2000")
    return owner


def test_a_savings_plan_is_turned_down_when_its_bucket_is_too_big(
    plan_book: TestClient,
) -> None:
    body = get(plan_book)
    assert D(body["savings_plans"][NVDA]) == D("100.00")
    tight = {
        "tech_stocks": {"target": "25", "low": "20", "high": "30"},
        "etfs": {"target": "45", "low": "40", "high": "50"},
        "crypto": {"target": "7", "low": "0", "high": "10"},
        "commodities": {"target": "3", "low": "0", "high": "5"},
        "cash": {"target": "5", "low": "3", "high": "10"},
        "other": {"target": "5", "low": "0", "high": "10"},
    }
    body = plan_book.put("/api/allocation/targets", json={"targets": tight}).json()
    kinds = {s["kind"]: s for s in body["suggestions"]}
    plan = kinds["redirect_plan"]
    assert D(plan["amount"]) == 100 and plan["isin"] == NVDA and plan["to_isin"] == WORLD
    assert "from 100 to 0 EUR a month" in plan["text"] and "World ETF" in plan["text"]
    # 750 over the target; plan changes work off 600 in six months; the rest is a sale, last.
    sell = kinds["sell"]
    assert D(sell["amount"]) == 150 and sell["isin"] == NVDA
    assert "Tax page" in sell["text"] and D(sell["gain_estimate"]) == 0
    order = [s["kind"] for s in body["suggestions"]]
    assert order.index("redirect_plan") < order.index("sell")  # cash flow first


# --- FR-49: the deployment plan --------------------------------------------------------------------


def test_the_excess_cash_is_split_into_tranches(book: TestClient) -> None:
    plan = get(book)["deployment"]
    assert plan["active"] is False and D(plan["excess"]) == D("1750.00")
    assert plan["tranches"] == 6 and len(plan["plan"]) == 6
    amounts = [D(t["amount"]) for t in plan["plan"]]
    assert amounts[0] == D("291.67") and sum(amounts) == D("1750.00")
    first = {s["bucket"]: D(s["amount"]) for s in plan["plan"][0]["split"]}
    assert first == {"etfs": D("208.34"), "crypto": D("58.33"), "commodities": D("25.00")}
    assert plan["plan"][1]["due"] == "2025-03-28" and not any(t["done"] for t in plan["plan"])


def test_start_follow_and_cancel_a_plan(book: TestClient, db: Session) -> None:
    started = book.post("/api/allocation/deployment", json={"tranches": 4})
    assert started.status_code == 201, started.text
    plan = started.json()["deployment"]
    assert plan["active"] is True and plan["started"] == "2025-02-28"
    assert D(plan["excess"]) == D("1750.00") and [D(t["amount"]) for t in plan["plan"]] == [
        D("437.50")
    ] * 4  # fmt: skip
    assert D(plan["invested_since"]) == 0 and not any(t["done"] for t in plan["plan"])
    # A purchase made since the start ticks the first tranche off.
    buy(book, WORLD, "World ETF", "FUND", "5", day="2025-02-28")
    after = get(book)["deployment"]
    assert D(after["invested_since"]) == 500
    assert [t["done"] for t in after["plan"]] == [True, False, False, False]
    assert D(after["excess"]) == D("1750.00")  # the plan keeps the amount it started with
    cancelled = book.delete("/api/allocation/deployment").json()["deployment"]
    assert cancelled["active"] is False


def test_a_market_fall_offers_to_pull_the_rest_forward(book: TestClient, db: Session) -> None:
    index = instrument(db, "IE00B53SZB19", "Nasdaq 100 fund", "etf")
    index.code = "SXRV"
    db.commit()
    assert book.post("/api/allocation/deployment", json={"tranches": 3}).status_code == 201
    assert get(book)["deployment"]["pull_forward"] is False
    db.add(
        PriceEOD(
            instrument_id=index.id, date=TODAY + timedelta(days=1), close=D("85"),
            currency="EUR", source="t",
        )
    )  # fmt: skip
    db.commit()
    offer = get(book)["deployment"]
    assert offer["pull_forward"] is True and "15.0% below" in offer["pull_forward_note"]
    assert "offer" in offer["pull_forward_note"]


def test_no_plan_without_cash_above_the_band(owner: TestClient, db: Session) -> None:
    instrument(db, NVDA, "NVIDIA Corp.", sector="Technology")
    buy(owner, NVDA, "NVIDIA Corp.", "STOCK", "20")
    enter_cash(owner, "50")  # 50 of 2050 is inside nothing: far below the band
    refused = owner.post("/api/allocation/deployment", json={"tranches": 6})
    assert refused.status_code == 422 and "no cash above" in refused.text
    assert owner.post("/api/allocation/deployment", json={"tranches": 13}).status_code == 422


# --- alerts (FR-41, FR-43) ------------------------------------------------------------------------------


def run_alerts(db: Session, today: date) -> int:
    rls.bypass(db)
    result = create_alerts(db, today, datetime(today.year, today.month, today.day, 12, tzinfo=UTC))
    assert result.errors == {} and result.warnings == {}
    return result.rows_written


def test_drift_and_breaches_raise_one_alert_a_week(book: TestClient, db: Session) -> None:
    assert run_alerts(db, date(2025, 2, 26)) == 3  # ETFs below, cash above, NVIDIA over 15%
    assert run_alerts(db, date(2025, 2, 27)) == 0  # the same week
    rls.bypass(db)
    made = list(db.scalars(select(Notification).order_by(Notification.dedupe_key)))
    kinds = sorted(n.kind for n in made)
    assert kinds == ["allocation_drift", "allocation_drift", "guardrail_breach"]
    drift = {n.title for n in made if n.kind == "allocation_drift"}
    assert drift == {"Broad and tech ETFs is below its band", "Cash is above its band"}
    breach = next(n for n in made if n.kind == "guardrail_breach")
    assert breach.severity == "warning" and "40.0% against your limit of 15%" in breach.body
    assert run_alerts(db, date(2025, 3, 5)) == 3  # the next week


# --- privacy -------------------------------------------------------------------------------------------


def test_settings_are_private(book: TestClient, client: TestClient, db: Session) -> None:
    book.put("/api/allocation/guardrails", json={"max_single_stock": "50"})
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    body = get(client)
    assert body["custom_guardrails"] is False and D(body["guardrails"]["max_single_stock"]) == 15
    assert body["has_portfolio"] is False


def test_the_disclaimer_gate_covers_allocation(client: TestClient, db: Session) -> None:
    make_user(db, "new@example.com", accepted=False)
    assert login(client, "new@example.com") == 200
    assert client.get("/api/allocation").status_code == 403
    assert client.get("/api/journal").status_code == 403


# --- FR-44: the decision journal -------------------------------------------------------------------------


def entry(client: TestClient, **kw: Any) -> Any:
    body = {"action": "bought", "name": "NVIDIA", "reason": "The AI said buy", **kw}
    return client.post("/api/journal", json=body)


def test_write_list_and_delete_journal_entries(owner: TestClient) -> None:
    created = entry(owner, isin="US67066G1040", guidance="Buy more NVIDIA")
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["action"] == "bought" and row["guidance"] == "Buy more NVIDIA"
    assert row["since_pct"] is None and "No price was stored" in row["verdict"]
    ignored = entry(owner, action="ignored", name="Gold ETC", reason="Too early in the year")
    assert ignored.status_code == 201
    listing = owner.get("/api/journal").json()
    assert [e["name"] for e in listing["entries"]] == ["Gold ETC", "NVIDIA"] or [
        e["name"] for e in listing["entries"]
    ] == ["NVIDIA", "Gold ETC"]
    assert listing["counts"] == {"bought": 1, "sold": 0, "ignored": 1}
    assert owner.delete(f"/api/journal/{row['id']}").status_code == 204
    assert owner.delete(f"/api/journal/{row['id']}").status_code == 404
    assert len(owner.get("/api/journal").json()["entries"]) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"reason": "   "},
        {"name": "  "},
        {"action": "maybe"},
        {"pick_id": "00000000-0000-4000-8000-000000000000"},  # not a pick of theirs
    ],
)
def test_bad_entries_are_refused(owner: TestClient, bad: dict[str, Any]) -> None:
    assert entry(owner, **bad).status_code == 422
    assert owner.get("/api/journal").json()["entries"] == []


def test_the_journal_shows_what_followed(owner: TestClient, db: Session, admin: User) -> None:
    inst = Instrument(code="NVDA", isin=NVDA, name="NVIDIA", asset_class="stock", currency="EUR")
    db.add(inst)
    db.flush()
    bar = PriceEOD(
        instrument_id=inst.id, date=date.today(), close=D("100"), currency="EUR", source="t"
    )
    db.add(bar)
    rls.bypass(db)
    pick = AiPick(
        user_id=admin.id, question="q", name="NVIDIA", isin=NVDA, direction="buy",
        horizon_months=12, rationale="r", held=False,
    )  # fmt: skip
    db.add(pick)
    db.commit()
    created = entry(owner, isin=NVDA, pick_id=str(pick.id), action="ignored").json()
    assert created["price"] is not None and D(created["price"]) == 100
    bar.close = D("110")  # the price moves up afterwards
    db.commit()
    shown = owner.get("/api/journal").json()
    one = shown["entries"][0]
    assert D(one["since_pct"]) == D("10.0") and one["guidance_right"] is True
    assert "+10.0%" in one["verdict"] and "direction was right" in one["verdict"]
    assert shown["judged"] == 1 and shown["right"] == 1
    bar.close = D("90")
    db.commit()
    worse = owner.get("/api/journal").json()
    assert worse["entries"][0]["guidance_right"] is False and worse["right"] == 0


def test_a_sale_that_was_followed_by_a_fall_looks_right(owner: TestClient, db: Session) -> None:
    inst = Instrument(code="X", isin="US0000000099", name="X", asset_class="stock", currency="EUR")
    db.add(inst)
    db.flush()
    bar = PriceEOD(
        instrument_id=inst.id, date=date.today(), close=D("100"), currency="EUR", source="t"
    )
    db.add(bar)
    db.commit()
    made = entry(owner, action="sold", name="X", isin="US0000000099", reason="Too big").json()
    bar.close = D("80")
    db.commit()
    shown = owner.get("/api/journal").json()["entries"][0]
    assert made["id"] == shown["id"] and "Selling looks right so far" in shown["verdict"]


def test_journal_entries_cannot_be_rewritten_by_the_app(
    owner: TestClient, db: Session, admin: User
) -> None:
    entry(owner)
    rls.scope_to_user(db, admin.id)
    db.rollback()
    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(text("UPDATE journal_entries SET reason = 'x'"))
    db.rollback()


def test_the_journal_is_private(owner: TestClient, client: TestClient, db: Session) -> None:
    made = entry(owner).json()
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert client.get("/api/journal").json()["entries"] == []
    assert client.delete(f"/api/journal/{made['id']}").status_code == 404


def test_the_band_maths_helpers() -> None:
    assert allocation.add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert allocation.add_months(date(2025, 11, 30), 3) == date(2026, 2, 28)
    assert allocation.bucket_of("stock", "software", False) == "tech_stocks"
    assert allocation.bucket_of("stock", "non_tech", False) == "other"
    assert allocation.bucket_of("stock", "non_tech", True) == "tech_stocks"  # a crypto-linked share
    assert allocation.bucket_of("etf", "Unknown", False) == "etfs"
    assert allocation.bucket_of("etc", "Unknown", False) == "commodities"
    assert allocation.bucket_of("crypto", "Crypto", True) == "crypto"
    assert allocation.bucket_of("leveraged", "Unknown", False) == "trading_book"
    assert allocation.bucket_of("bond", "Unknown", False) == "other"

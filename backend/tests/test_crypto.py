"""Crypto: per-lot tax-free dates and the Freigrenze (FR-29)."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant.models import Instrument, PriceEOD
from quant.portfolio import crypto, service, tax

from .conftest import login, make_user

TODAY = date(2025, 2, 28)
BTC = "XF000BTC0017"
ETH = "XF000ETH0019"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "today", lambda: TODAY)


# --- the rule ------------------------------------------------------------------------------------


def test_a_lot_is_tax_free_the_day_after_its_first_anniversary() -> None:
    assert crypto.tax_free_from(date(2024, 3, 1)) == date(2025, 3, 2)
    assert crypto.tax_free_from(date(2024, 2, 29)) == date(2025, 3, 1)  # no 29 February in 2025
    assert crypto.tax_free_from(date(2023, 12, 31)) == date(2025, 1, 1)


@pytest.mark.parametrize(
    "acquired", [date(2024, 3, 1), date(2024, 2, 29), date(2023, 12, 31), date(2020, 2, 29)]
)
def test_it_agrees_with_the_tax_module(acquired: date) -> None:
    free = crypto.tax_free_from(acquired)
    assert tax._held_over_a_year(acquired, free) is True
    assert tax._held_over_a_year(acquired, free - timedelta(days=1)) is False


# --- the page ------------------------------------------------------------------------------------


def instrument(db: Session, isin: str, name: str) -> None:
    row = Instrument(code=isin, isin=isin, name=name, asset_class="crypto", currency="EUR")
    db.add(row)
    db.flush()
    for ago, close in ((0, "100000" if isin == BTC else "3000"), (1, "100000")):
        db.add(
            PriceEOD(
                instrument_id=row.id, date=TODAY - timedelta(days=ago), close=D(close),
                currency="EUR", source="t",
            )
        )  # fmt: skip
    db.commit()


def trade(
    client: TestClient, side: str, isin: str, name: str, shares: str, price: str, day: str
) -> None:
    body = {
        "side": side, "date": day, "isin": isin, "name": name, "asset_class": "CRYPTO",
        "shares": shares, "price": price,
    }  # fmt: skip
    assert client.post("/api/transactions", json=body).status_code == 201


def get(client: TestClient) -> dict:  # type: ignore[type-arg]
    response = client.get("/api/crypto")
    assert response.status_code == 200, response.text
    body: dict = response.json()  # type: ignore[type-arg]
    return body


@pytest.fixture
def coins(owner: TestClient, db: Session) -> TestClient:
    """Three Bitcoin lots, one sold in part after it turned tax-free, and a quick Ether trade."""
    instrument(db, BTC, "Bitcoin")
    instrument(db, ETH, "Ethereum")
    trade(owner, "buy", BTC, "Bitcoin", "0.1", "40000", "2024-02-01")  # tax-free from 2025-02-02
    trade(owner, "buy", BTC, "Bitcoin", "0.1", "60000", "2024-12-15")  # from 2025-12-16
    trade(owner, "buy", BTC, "Bitcoin", "0.05", "90000", "2025-02-10")  # from 2026-02-12
    trade(owner, "sell", BTC, "Bitcoin", "0.02", "80000", "2025-02-20")  # first lot, now free
    trade(owner, "buy", ETH, "Ethereum", "1", "2000", "2025-01-10")
    trade(owner, "sell", ETH, "Ethereum", "1", "2600", "2025-02-01")  # held 22 days: taxable
    deposit = {"side": "deposit", "date": "2025-01-02", "amount": "50000"}
    assert owner.post("/api/transactions", json=deposit).status_code == 201
    # 50000 - 4000 - 6000 - 4500 - 2000 + 1600 + 2600 = 37700
    assert owner.post("/api/cash/balance", json={"amount": "37700"}).status_code == 201
    return owner


def test_each_lot_shows_when_it_turns_tax_free(coins: TestClient) -> None:
    body = get(coins)
    assert [c["name"] for c in body["coins"]] == ["Bitcoin"]  # the Ether is sold
    [btc] = body["coins"]
    assert D(btc["quantity"]) == D("0.23") and D(btc["value"]) == 23000
    lots = btc["lots"]
    assert [(lot["acquired"], lot["tax_free_from"], lot["free"]) for lot in lots] == [
        ("2024-02-01", "2025-02-02", True),
        ("2024-12-15", "2025-12-16", False),
        ("2025-02-10", "2026-02-12", False),
    ]
    assert D(lots[0]["quantity"]) == D("0.08")  # 0.02 was sold from it
    assert lots[0]["days_left"] == 0 and lots[1]["days_left"] == 291
    assert D(lots[1]["gain"]) == 4000 and D(lots[2]["gain"]) == 500  # value less cost


def test_the_share_of_a_coin_that_is_already_tax_free(coins: TestClient) -> None:
    [btc] = get(coins)["coins"]
    assert D(btc["free_quantity"]) == D("0.08")
    assert D(btc["free_share_pct"]) == D("34.78")  # 0.08 of 0.23
    assert btc["next_free_date"] == "2025-12-16" and D(btc["next_free_quantity"]) == D("0.1")
    assert D(btc["taxable_gain_if_sold"]) == 4500  # the two lots that are not free yet
    body = get(coins)
    assert D(body["free_value"]) == 8000 and D(body["taxable_gain_if_sold"]) == 4500


def test_the_freigrenze_and_what_was_sold_this_year(coins: TestClient) -> None:
    body = get(coins)
    assert body["year"] == 2025 and D(body["freigrenze"]) == 1000
    assert D(body["realised_taxable"]) == 600  # the Ether sale, held a year or less
    assert D(body["realised_free"]) == 800  # the Bitcoin sale from a lot held over a year
    assert D(body["headroom"]) == 400 and body["over_limit"] is False
    assert "Verify with a tax adviser" in body["note"]


def test_over_the_freigrenze_the_headroom_is_gone(coins: TestClient) -> None:
    trade(coins, "buy", ETH, "Ethereum", "1", "2000", "2025-02-12")
    trade(coins, "sell", ETH, "Ethereum", "1", "2500", "2025-02-14")  # another 500: 1100 in all
    body = get(coins)
    assert D(body["realised_taxable"]) == 1100
    assert body["over_limit"] is True and D(body["headroom"]) == 0


def test_the_crypto_share_of_the_portfolio_against_its_cap(coins: TestClient) -> None:
    body = get(coins)
    # 23000 of 23000 (the coins) + 37700 (free cash): 37.89%.
    assert D(body["exposure_pct"]) == D("37.89") and D(body["cap_pct"]) == 10
    coins.put("/api/allocation/guardrails", json={"max_crypto": "50"})
    assert D(get(coins)["cap_pct"]) == 50


def test_without_coins_there_is_nothing_to_show(owner: TestClient) -> None:
    body = get(owner)
    assert body["coins"] == [] and D(body["value"]) == 0 and D(body["headroom"]) == 1000
    assert body["over_limit"] is False


def test_crypto_is_private_and_gated(coins: TestClient, client: TestClient, db: Session) -> None:
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert get(client)["coins"] == []
    make_user(db, "new@example.com", accepted=False)
    client.cookies.clear()
    assert login(client, "new@example.com") == 200
    assert client.get("/api/crypto").status_code == 403

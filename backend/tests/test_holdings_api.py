from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.models import Snapshot, User

from .conftest import login, make_user
from .pdfutil import crypto_statement_pdf
from .test_tr_crypto_pdf import ROWS, TOTAL

HEADER = (
    '"datetime","date","account_type","category","type","asset_class","name","symbol","shares",'
    '"price","amount","fee","tax","currency","original_amount","original_currency","fx_rate",'
    '"description","transaction_id","counterparty_name","counterparty_iban","payment_reference",'
    '"mcc_code"'
)

# (id, category, type, asset_class, name, isin, shares)
HISTORY = [
    (1, "TRADING", "BUY", "CRYPTO", "Ethereum", "XF000ETH0019", "4.944466"),
    (2, "TRADING", "BUY", "CRYPTO", "Cardano", "XF000ADA0010", "12000"),
    (3, "DELIVERY", "FREE_RECEIPT", "CRYPTO", "Cardano", "XF000ADA0010", "423.854702"),
    (4, "TRADING", "BUY", "CRYPTO", "XRP", "XF000XRP0011", "2030.784567"),
    (5, "TRADING", "BUY", "CRYPTO", "Bitcoin", "XF000BTC0017", "0.093152"),
    (6, "TRADING", "BUY", "STOCK", "NVIDIA Corp.", "US67066G1040", "10"),
    (7, "CORPORATE_ACTION", "SPLIT", "STOCK", "NVIDIA Corp.", "US67066G1040", "90"),
    (8, "CASH", "DIVIDEND", "STOCK", "NVIDIA Corp.", "US67066G1040", "100"),  # not a movement
    (9, "TRADING", "BUY", "STOCK", "Old Corp", "US0000000001", "5"),
    (10, "TRADING", "SELL", "STOCK", "Old Corp", "US0000000001", "-5"),
]


def csv_text(
    history: list[tuple[int, str, str, str, str, str, str]] = HISTORY,
    dates: dict[int, str] | None = None,
) -> str:
    """`dates` overrides the day of individual rows (by id); the default is 2024."""
    rows = [HEADER]
    for n, category, type_, cls, name, isin, shares in history:
        day = (dates or {}).get(n, f"2024-0{min(n, 9)}-01")
        rows.append(
            f'"{day}T09:00:00.000000Z","{day}","DEFAULT","{category}",'
            f'"{type_}","{cls}","{name}","{isin}","{shares}","","","","","EUR","","","","",'
            f'"00000000-0000-4000-8000-{n:012d}","","","",""'
        )
    return "\n".join(rows) + "\n"


def import_history(client: TestClient, text: str | None = None) -> None:
    files = {"file": ("t.csv", (text or csv_text()).encode(), "text/csv")}
    preview = client.post("/api/imports", files=files).json()
    assert client.post(f"/api/imports/{preview['id']}/commit").status_code == 200


def upload_statement(client: TestClient, pdf: bytes) -> Any:
    # Any: the response class is httpx's or httpx2's, depending on which the test client picked
    # (the anthropic package brings httpx2).
    return client.post(
        "/api/holdings/statements/crypto", files={"file": ("s.pdf", pdf, "application/pdf")}
    )


@pytest.fixture
def owner_client(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    return client


def test_holdings_from_history_without_a_statement(owner_client: TestClient) -> None:
    assert owner_client.get("/api/holdings").json()["positions"] == []
    import_history(owner_client)
    body = owner_client.get("/api/holdings").json()
    by_name = {p["name"]: p for p in body["positions"]}
    assert set(by_name) == {
        "Bitcoin",
        "Cardano",
        "Ethereum",
        "NVIDIA Corp.",
        "XRP",
    }  # sold-out one gone
    assert by_name["NVIDIA Corp."]["quantity"] == "100"  # 10 + split 90, dividend ignored
    assert by_name["NVIDIA Corp."]["corporate_action"] is True
    assert by_name["Cardano"]["quantity"] == "12423.854702"
    assert body["by_class"] == {"CRYPTO": 4, "STOCK": 1}
    assert body["verified"] == 0 and body["reconciliations"] == []


def test_matching_statement_verifies_the_crypto_positions(
    owner_client: TestClient, db: Session
) -> None:
    import_history(owner_client)
    response = upload_statement(owner_client, crypto_statement_pdf(ROWS, total=TOTAL))
    assert response.status_code == 201, response.text
    body = response.json()
    [recon] = body["reconciliations"]
    assert recon["as_of"] == "2026-09-27" and recon["counts"]["match"] == 4
    assert recon["review"] == [] and body["verified"] == 4
    verified = {p["name"]: p["verified"] for p in body["positions"]}
    assert (
        verified["Bitcoin"] and not verified["NVIDIA Corp."]
    )  # stocks need the securities statement

    rls.bypass(db)
    stored = db.scalar(select(Snapshot))
    assert stored is not None and stored.footer_count == 4 and len(stored.lines) == 4


def test_mismatch_and_ghost_land_in_the_review_queue(owner_client: TestClient) -> None:
    history = [h for h in HISTORY if h[4] != "XRP"] + [
        (11, "TRADING", "BUY", "CRYPTO", "Dogecoin", "XF000DOG0000", "1000")
    ]
    history[2] = (3, "DELIVERY", "FREE_RECEIPT", "CRYPTO", "Cardano", "XF000ADA0010", "400")
    import_history(owner_client, csv_text(history))
    body = upload_statement(owner_client, crypto_statement_pdf(ROWS, total=TOTAL)).json()
    review = {f["name"]: f for f in body["reconciliations"][0]["review"]}
    assert review["Cardano"]["status"] == "quantity_mismatch"
    flags = {p["name"]: (p["verified"], p["differs"]) for p in body["positions"]}
    assert flags["Cardano"] == (False, True) and flags["Bitcoin"] == (True, False)
    assert review["Cardano"]["difference"] == "-23.854702"
    assert review["XRP"]["status"] == "missing_in_history"
    assert review["Dogecoin"]["status"] == "not_on_statement"
    assert body["reconciliations"][0]["counts"]["match"] == 2


def test_bad_uploads_are_rejected_with_a_reason(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_pdf = owner_client.post(
        "/api/holdings/statements/crypto", files={"file": ("s.pdf", b"hello", "application/pdf")}
    )
    assert not_pdf.status_code == 422 and "not a PDF" in not_pdf.json()["detail"]

    wrong_footer = crypto_statement_pdf(ROWS, count=5, total=TOTAL)
    bad = upload_statement(owner_client, wrong_footer)
    assert bad.status_code == 422 and "says 5 positions, 4 rows were read" in bad.json()["detail"]

    from quant.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
    assert (
        upload_statement(owner_client, crypto_statement_pdf(ROWS, total=TOTAL)).status_code == 413
    )


def test_nothing_is_stored_for_a_rejected_statement(owner_client: TestClient, db: Session) -> None:
    upload_statement(owner_client, crypto_statement_pdf(ROWS, count=9, total=TOTAL))
    rls.bypass(db)
    assert db.scalar(select(Snapshot)) is None


def test_the_newest_statement_wins(owner_client: TestClient) -> None:
    import_history(owner_client)
    upload_statement(owner_client, crypto_statement_pdf(ROWS[:1], total="11.743,9"))
    first = owner_client.get("/api/holdings").json()["reconciliations"][0]
    assert first["counts"]["match"] == 1 and first["counts"]["not_on_statement"] == 3
    upload_statement(owner_client, crypto_statement_pdf(ROWS, total=TOTAL))
    second = owner_client.get("/api/holdings").json()["reconciliations"]
    assert len(second) == 1 and second[0]["counts"]["match"] == 4


def test_users_only_see_their_own_holdings_and_statements(
    owner_client: TestClient, db: Session
) -> None:
    import_history(owner_client)
    upload_statement(owner_client, crypto_statement_pdf(ROWS, total=TOTAL))
    make_user(db, "sister@example.com")
    other = TestClient(owner_client.app)
    login(other, "sister@example.com")
    body = other.get("/api/holdings").json()
    assert body["positions"] == [] and body["reconciliations"] == []
    assert owner_client.get("/api/holdings").json()["verified"] == 4


def test_holdings_require_sign_in(client: TestClient) -> None:
    assert client.get("/api/holdings").status_code == 401
    assert upload_statement(client, b"%PDF-").status_code == 401


def test_a_trade_after_the_statement_date_is_not_a_discrepancy(owner_client: TestClient) -> None:
    """The statement is dated 2026-09-27: buying more Bitcoin on 2026-10-02 must not turn a
    correct history into a mismatch."""
    history = [*HISTORY, (11, "TRADING", "BUY", "CRYPTO", "Bitcoin", "XF000BTC0017", "0.5")]
    import_history(owner_client, csv_text(history, dates={11: "2026-10-02"}))
    body = upload_statement(owner_client, crypto_statement_pdf(ROWS, total=TOTAL)).json()
    recon = body["reconciliations"][0]
    assert recon["counts"]["match"] == 4 and recon["review"] == []

    by_name = {p["name"]: p for p in body["positions"]}
    assert by_name["Bitcoin"]["quantity"] == "0.593152"  # the current holding includes the trade
    # Not confirmed any more, since it moved after the statement, but not flagged either.
    assert (by_name["Bitcoin"]["verified"], by_name["Bitcoin"]["differs"]) == (False, False)
    assert by_name["XRP"]["verified"] and body["verified"] == 3


def test_a_trade_before_the_statement_date_still_counts(owner_client: TestClient) -> None:
    history = [*HISTORY, (11, "TRADING", "BUY", "CRYPTO", "Bitcoin", "XF000BTC0017", "0.5")]
    import_history(owner_client, csv_text(history, dates={11: "2026-09-27"}))  # same day
    body = upload_statement(owner_client, crypto_statement_pdf(ROWS, total=TOTAL)).json()
    review = {f["name"]: f for f in body["reconciliations"][0]["review"]}
    assert review["Bitcoin"]["status"] == "quantity_mismatch"
    assert review["Bitcoin"]["difference"] == "0.5"


def test_upload_handlers_run_in_the_threadpool() -> None:
    """A coroutine handler would parse files on the event loop and stall every other request."""
    import inspect

    from quant.api import holdings, imports

    assert not inspect.iscoroutinefunction(holdings.upload_crypto_statement)
    assert not inspect.iscoroutinefunction(imports.upload)


@pytest.mark.parametrize(
    "payload",
    [
        b"%PDF-1.4\n",
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 99 0 R >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n",
        b"%PDF-1.7\n" + b"\x00\xff" * 500,
        b"%PDF-1.4\n%%EOF\n",
    ],
)
def test_malformed_pdfs_are_a_422_not_a_500(owner_client: TestClient, payload: bytes) -> None:
    response = upload_statement(owner_client, payload)
    assert response.status_code == 422, response.text

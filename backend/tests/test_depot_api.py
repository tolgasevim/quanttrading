from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quant import rls
from quant.cli import check_holdings
from quant.models import Snapshot, User

from .conftest import login
from .pdfutil import crypto_statement_pdf, depot_statement_pdf
from .test_holdings_pnl_api import SPIN, STATEMENT_ROWS, A, history, import_history
from .test_tr_crypto_pdf import PII

# Alpha: 6 units left after the sale; the spin-off gave 6 units. Beta is sold, so it is not listed.
DEPOT_ROWS = [
    ("6", "Alpha Corp Registered Shares", A, "Vereinigte Staaten", "150", "900"),
    ("6", "Spin Co", SPIN, "Deutschland", "10", "60"),
]
DEPOT_TOTAL = "960"


def post(client: TestClient, path: str, pdf: bytes, expect: int = 201) -> dict:  # type: ignore[type-arg]
    response = client.post(
        f"/api/holdings/statements{path}", files={"file": ("s.pdf", pdf, "application/pdf")}
    )
    assert response.status_code == expect, response.text
    body: dict = response.json()  # type: ignore[type-arg]
    return body


def depot(rows: Sequence[tuple[str, ...]] = DEPOT_ROWS, total: str = DEPOT_TOTAL) -> bytes:
    return depot_statement_pdf(rows, total=total)


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    import_history(client, history())
    return client


def recon(body: dict, source: str) -> dict:  # type: ignore[type-arg]
    [found] = [r for r in body["reconciliations"] if r["source"] == source]
    result: dict = found  # type: ignore[type-arg]
    return result


def test_a_matching_depot_statement_verifies_shares_and_leaves_coins_alone(
    owner: TestClient,
) -> None:
    body = post(owner, "/depot", depot())
    r = recon(body, "tr_depot_statement")
    assert r["as_of"] == "2026-09-27" and r["review"] == [] and r["counts"]["match"] == 2
    verified = {p["name"] for p in body["positions"] if p["verified"]}
    assert verified == {"Alpha Corp", "Spin Co"}
    # The coins are on the crypto statement, not a depot ghost.
    assert not any(f["name"] in ("Bitcoin", "Ethereum") for f in r["review"])


def test_the_statement_price_becomes_the_market_value(owner: TestClient) -> None:
    body = post(owner, "/depot", depot())
    alpha = {p["name"]: p for p in body["positions"]}["Alpha Corp"]
    # 6 units at 150, bought for 600 plus 0.60 of fees.
    assert (alpha["price"], alpha["market_value"], alpha["unrealised_pnl"]) == (
        "150",
        "900.00",
        "299.40",
    )
    assert body["review"]["unpriced"] == 0


def test_mismatch_ghost_and_unknown_position_go_to_the_review_queue(owner: TestClient) -> None:
    rows = [
        ("7", "Alpha Corp", A, "Vereinigte Staaten", "150", "1.050"),  # history says 6
        ("3", "Mystery Fund", "IE0000000009", "Irland", "10", "30"),  # not in the history
    ]
    r = recon(post(owner, "/depot", depot(rows, "1.080")), "tr_depot_statement")
    assert {f["name"]: f["status"] for f in r["review"]} == {
        "Alpha Corp": "quantity_mismatch",
        "Mystery Fund": "missing_in_history",
        "Spin Co": "not_on_statement",
    }


def test_both_statements_are_kept_side_by_side(owner: TestClient) -> None:
    post(owner, "/depot", depot())
    body = post(owner, "/crypto", crypto_statement_pdf(list(STATEMENT_ROWS), total="10.000"))
    assert {r["source"] for r in body["reconciliations"]} == {
        "tr_depot_statement",
        "tr_crypto_statement",
    }
    assert body["verified"] == 4


def test_the_upload_tells_the_two_statements_apart(owner: TestClient) -> None:
    assert recon(post(owner, "", depot()), "tr_depot_statement")["counts"]["match"] == 2
    crypto = crypto_statement_pdf(list(STATEMENT_ROWS), total="10.000")
    assert recon(post(owner, "", crypto), "tr_crypto_statement")["counts"]["match"] == 2


def test_a_crypto_statement_sent_as_a_depot_is_refused_and_nothing_is_stored(
    owner: TestClient, db: Session
) -> None:
    crypto = crypto_statement_pdf(list(STATEMENT_ROWS), total="10.000")
    post(owner, "/depot", crypto, expect=422)
    post(owner, "/depot", depot(total="1"), expect=422)  # the footer total is wrong
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 0


def test_only_the_table_is_stored_never_the_header(owner: TestClient, db: Session) -> None:
    post(owner, "/depot", depot())
    rls.bypass(db)
    stored = str([s.lines for s in db.scalars(select(Snapshot))])
    for secret in PII:
        assert secret not in stored, secret
    assert "Vereinigte Staaten" in stored  # the custody country is kept


def test_the_cli_check_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = tmp_path / "tx.csv"
    pdf = tmp_path / "depot.pdf"
    csv.write_text(history())
    pdf.write_bytes(depot())
    assert check_holdings(csv, None, pdf) == 0
    out = capsys.readouterr().out
    assert "PASS securities quantities match the statement" in out and "match" in out
    assert "2 rows, 2 positions, 2 with a usable price, 2 with a custody country" in out
    for secret in [*PII, "Alpha", "Spin", A, SPIN]:
        assert secret not in out


def test_the_cli_check_fails_on_a_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = tmp_path / "tx.csv"
    pdf = tmp_path / "depot.pdf"
    csv.write_text(history())
    pdf.write_bytes(
        depot([("7", "Alpha Corp", A, "Deutschland", "150", "1.050"), DEPOT_ROWS[1]], "1.110")
    )
    assert check_holdings(csv, None, pdf) == 1
    out = capsys.readouterr().out
    assert "FAIL securities quantities match the statement" in out
    assert "quantity_mismatch" in out


def test_a_holding_split_over_two_lines_is_one_position(owner: TestClient) -> None:
    rows = [
        ("4", "Alpha Corp", A, "Deutschland", "150", "600"),
        ("2", "Alpha Corp", A, "Vereinigte Staaten", "150", "300"),
        DEPOT_ROWS[1],
    ]
    r = recon(post(owner, "/depot", depot(rows, "960")), "tr_depot_statement")
    assert r["review"] == [] and r["counts"]["match"] == 2

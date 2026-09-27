from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from quant.ingest.jobs import JobResult, run_job
from quant.models import FxRate, Instrument, PriceEOD, User

from .conftest import login


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_market_status_and_fx(client: TestClient, admin: User, db: Session) -> None:
    inst = Instrument(code="NVDA", name="NVIDIA", asset_class="stock", currency="USD", symbols={})
    empty = Instrument(code="SAP", name="SAP", asset_class="stock", currency="EUR", symbols={})
    db.add_all([inst, empty])
    db.flush()
    db.add_all(
        [
            PriceEOD(
                instrument_id=inst.id,
                date=date(2026, 9, 24),
                close=Decimal("179.9"),
                currency="USD",
                source="yahoo",
            ),
            PriceEOD(
                instrument_id=inst.id,
                date=date(2026, 9, 25),
                close=Decimal("185.43"),
                currency="USD",
                source="yahoo",
            ),
            FxRate(quote="USD", date=date(2026, 9, 24), rate=Decimal("1.1721"), source="ecb"),
            FxRate(quote="USD", date=date(2026, 9, 25), rate=Decimal("1.1698"), source="ecb"),
        ]
    )
    db.commit()

    assert client.get("/api/market/status").status_code == 401
    login(client, admin.email)
    status = {row["code"]: row for row in client.get("/api/market/status").json()}
    assert status["NVDA"]["last_date"] == "2026-09-25" and status["NVDA"]["rows"] == 2
    assert Decimal(status["NVDA"]["last_close"]) == Decimal("185.43")
    assert status["SAP"]["last_date"] is None and status["SAP"]["rows"] == 0

    fx = client.get("/api/market/fx").json()
    assert fx == [{"quote": "USD", "date": "2026-09-25", "rate": "1.16980000"}]


def test_admin_job_list(client: TestClient, admin: User, db: Session) -> None:
    run_job(db, "ingest_prices", lambda s: JobResult(rows_written=5, attempted=1))
    login(client, admin.email)
    jobs = client.get("/api/admin/jobs").json()
    assert jobs[0]["job"] == "ingest_prices" and jobs[0]["status"] == "success"

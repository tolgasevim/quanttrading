"""Import workflow: parse → stage for preview (FR-15) → commit or discard. Idempotent (FR-13)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quant.importers import tr_csv
from quant.models import Import, ImportStatus, Transaction

DECIMAL_FIELDS = (
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "original_amount",
    "fx_rate",
)


def stage_tr_csv(session: Session, user_id: uuid.UUID, text: str) -> Import:
    """Parse and stage. Raises tr_csv.ImportFormatError for files that aren't TR exports."""
    result = tr_csv.parse(text)
    ids = [t.external_id for t in result.transactions]
    existing = set(
        session.scalars(
            select(Transaction.external_id).where(
                Transaction.user_id == user_id,
                Transaction.broker == tr_csv.BROKER,
                Transaction.external_id.in_(ids),
            )
        )
    )
    summary = result.summary()
    summary["already_imported"] = len(existing)
    summary["new"] = len(ids) - len(existing)
    record = Import(
        user_id=user_id,
        source=tr_csv.SOURCE,
        status=ImportStatus.PREVIEW,
        summary=summary,
        staged=[t.to_json() for t in result.transactions],
    )
    session.add(record)
    session.commit()
    return record


def _row(user_id: uuid.UUID, import_id: uuid.UUID, staged: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "user_id": user_id,
        "import_id": import_id,
        "broker": tr_csv.BROKER,
        "external_id": staged["external_id"],
        "executed_at": datetime.fromisoformat(staged["executed_at"]),
        "date": date.fromisoformat(staged["date"]),
        "kind": staged["kind"],
        "category": staged["category"],
        "type": staged["type"],
        "asset_class": staged["asset_class"],
        "isin": staged["isin"],
        "name": staged["name"],
        "currency": staged["currency"],
        "original_currency": staged["original_currency"],
        "savings_plan": staged["savings_plan"],
    }
    for key in DECIMAL_FIELDS:
        value = staged[key]
        row[key] = Decimal(value) if value is not None else None
    return row


def commit(session: Session, record: Import) -> Import:
    if record.status != ImportStatus.PREVIEW or record.staged is None:
        raise ValueError(f"import is {record.status}, not awaiting confirmation")
    rows = [_row(record.user_id, record.id, s) for s in record.staged]
    inserted = 0
    # Chunked to stay well below Postgres' bind-parameter limit.
    for start in range(0, len(rows), 1000):
        stmt = (
            insert(Transaction)
            .values(rows[start : start + 1000])
            .on_conflict_do_nothing(constraint="uq_transactions_user_external")
            .returning(Transaction.id)
        )
        inserted += len(session.execute(stmt).all())
    record.status = ImportStatus.COMMITTED
    record.committed_at = datetime.now(UTC)
    record.rows_inserted = inserted
    record.staged = None  # staged copy no longer needed
    session.commit()
    return record


def discard(session: Session, record: Import) -> Import:
    if record.status != ImportStatus.PREVIEW:
        raise ValueError(f"import is {record.status}, not awaiting confirmation")
    record.status = ImportStatus.DISCARDED
    record.staged = None
    session.commit()
    return record

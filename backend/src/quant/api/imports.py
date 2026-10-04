"""Statement import endpoints: upload → preview → commit/discard (FR-10, FR-13, FR-15)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select

from quant.api.deps import CurrentUser, UserDb
from quant.config import get_settings
from quant.importers import service, tr_csv
from quant.models import Import

router = APIRouter(prefix="/api/imports", tags=["imports"])


class ImportOut(BaseModel):
    id: str
    source: str
    status: str
    summary: dict[str, object]
    created_at: datetime
    committed_at: datetime | None
    rows_inserted: int

    @classmethod
    def of(cls, record: Import) -> "ImportOut":
        return cls(
            id=str(record.id),
            source=record.source,
            status=record.status,
            summary=record.summary,
            created_at=record.created_at,
            committed_at=record.committed_at,
            rows_inserted=record.rows_inserted,
        )


async def _read_upload(file: UploadFile) -> str:
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "file is not UTF-8 text"
        ) from exc


@router.post("", response_model=ImportOut, status_code=status.HTTP_201_CREATED)
async def upload(file: UploadFile, user: CurrentUser, db: UserDb) -> ImportOut:
    text = await _read_upload(file)
    try:
        record = service.stage_tr_csv(db, user.id, text)
    except tr_csv.ImportFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return ImportOut.of(record)


@router.get("", response_model=list[ImportOut])
def list_imports(user: CurrentUser, db: UserDb) -> list[ImportOut]:
    records = db.scalars(
        select(Import).where(Import.user_id == user.id).order_by(Import.created_at.desc()).limit(50)
    )
    return [ImportOut.of(r) for r in records]


def _get(db: UserDb, user: CurrentUser, import_id: uuid.UUID) -> Import:
    # RLS already hides other users' imports; a foreign id looks exactly like a missing one.
    record = db.get(Import, import_id)
    if record is None or record.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    return record


@router.get("/{import_id}", response_model=ImportOut)
def get_import(import_id: uuid.UUID, user: CurrentUser, db: UserDb) -> ImportOut:
    return ImportOut.of(_get(db, user, import_id))


@router.post("/{import_id}/commit", response_model=ImportOut)
def commit_import(import_id: uuid.UUID, user: CurrentUser, db: UserDb) -> ImportOut:
    record = _get(db, user, import_id)
    try:
        return ImportOut.of(service.commit(db, record))
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/{import_id}", response_model=ImportOut)
def discard_import(import_id: uuid.UUID, user: CurrentUser, db: UserDb) -> ImportOut:
    record = _get(db, user, import_id)
    try:
        return ImportOut.of(service.discard(db, record))
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

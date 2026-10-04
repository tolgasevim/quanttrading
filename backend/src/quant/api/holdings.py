"""Holdings and statement reconciliation (FR-10c, FR-11b, FR-19)."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel

from quant.api.deps import CurrentUser, UserDb
from quant.config import get_settings
from quant.importers import tr_crypto_pdf
from quant.models import Snapshot
from quant.portfolio import service
from quant.portfolio.positions import Position
from quant.portfolio.reconcile import Finding, Status

router = APIRouter(prefix="/api/holdings", tags=["holdings"])

PDF_MAGIC = b"%PDF-"


class PositionOut(BaseModel):
    isin: str
    name: str | None
    asset_class: str | None
    quantity: Decimal
    first_date: str
    last_date: str
    corporate_action: bool  # a split/merger/... touched it; cost basis needs a closer look
    verified: bool  # confirmed by a statement
    differs: bool  # a statement disagrees with the history


class FindingOut(BaseModel):
    status: str
    isin: str | None
    name: str
    history_quantity: Decimal | None
    statement_quantity: Decimal | None
    difference: Decimal | None


class ReconciliationOut(BaseModel):
    source: str
    as_of: str
    counts: dict[str, int]
    review: list[FindingOut]  # everything that is not a clean match


class HoldingsOut(BaseModel):
    positions: list[PositionOut]
    by_class: dict[str, int]
    verified: int
    reconciliations: list[ReconciliationOut]


def _trim(value: Decimal | None) -> Decimal | None:
    """12423.8547020000 → 12423.854702 (the database keeps ten decimals)."""
    return None if value is None else Decimal(format(value.normalize(), "f"))


def _finding(f: Finding) -> FindingOut:
    return FindingOut(
        status=f.status,
        isin=f.isin,
        name=f.name,
        history_quantity=_trim(f.history_quantity),
        statement_quantity=_trim(f.statement_quantity),
        difference=_trim(f.difference),
    )


def _position(p: Position, holdings: service.Holdings) -> PositionOut:
    return PositionOut(
        isin=p.isin,
        name=p.name,
        asset_class=p.asset_class,
        quantity=_trim(p.quantity) or Decimal(0),
        first_date=p.first_date,
        last_date=p.last_date,
        corporate_action=p.corporate_action_touched,
        verified=p.isin in holdings.verified,
        differs=p.isin in holdings.differs,
    )


def _out(holdings: service.Holdings) -> HoldingsOut:
    by_class: dict[str, int] = {}
    for p in holdings.positions:
        key = p.asset_class or "UNKNOWN"
        by_class[key] = by_class.get(key, 0) + 1
    return HoldingsOut(
        positions=[_position(p, holdings) for p in holdings.positions],
        by_class=dict(sorted(by_class.items())),
        verified=len(holdings.verified),
        reconciliations=[
            ReconciliationOut(
                source=r.source,
                as_of=r.as_of,
                counts=r.counts,
                review=[_finding(f) for f in r.findings if f.status != Status.MATCH],
            )
            for r in holdings.reconciliations
        ],
    )


@router.get("", response_model=HoldingsOut)
def get_holdings(user: CurrentUser, db: UserDb) -> HoldingsOut:
    return _out(service.build_holdings(db, user.id))


@router.post("/statements/crypto", response_model=HoldingsOut, status_code=status.HTTP_201_CREATED)
def upload_crypto_statement(file: UploadFile, user: CurrentUser, db: UserDb) -> HoldingsOut:
    # A plain `def`: FastAPI runs it in a worker thread, so parsing a PDF never blocks other
    # requests.
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file is too large")
    if not data.startswith(PDF_MAGIC):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "this file is not a PDF")
    try:
        statement = tr_crypto_pdf.parse_text(tr_crypto_pdf.extract_text(data))
    except tr_crypto_pdf.StatementFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    db.add(
        Snapshot(
            user_id=user.id,
            source=tr_crypto_pdf.SOURCE,
            as_of=statement.as_of,
            lines=[line.to_json() for line in statement.lines],
            footer_count=statement.footer_count,
            footer_total=statement.footer_total,
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    return _out(service.build_holdings(db, user.id))

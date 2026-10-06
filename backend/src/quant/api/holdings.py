"""Holdings and statement reconciliation (FR-10c, FR-11, FR-11b, FR-19)."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel

from quant.api.deps import CurrentUser, UserDb
from quant.config import get_settings
from quant.importers import tr_crypto_pdf, tr_depot_pdf
from quant.importers.statement_pdf import StatementFormatError, detect_kind, extract_text
from quant.ingest.mapping import PRICEABLE
from quant.models import Snapshot
from quant.portfolio import service
from quant.portfolio.lots import (
    COST_MISSING,
    FLAG_COST_UNKNOWN,
    cost_unknown_isins,
    history_gap_isins,
    realised_by_isin,
    realised_by_year,
)
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
    # Cost basis (FIFO). Purchase value is what broker statements quote; acquisition costs are
    # the fees and transaction taxes on top. The average cost per unit includes both.
    purchase_value: Decimal | None
    acquisition_costs: Decimal | None
    total_cost: Decimal | None  # purchase value plus acquisition costs
    average_cost: Decimal | None
    cost_flags: list[str]  # cost_unknown | price_derived | carried
    # Market value from the latest broker statement that prices this position, if any.
    price: Decimal | None
    price_as_of: str | None
    valued_quantity: Decimal | None  # the quantity the value is for: the one on the statement date
    market_value: Decimal | None
    unrealised_pnl: Decimal | None  # market value less purchase value and acquisition costs
    unrealised_pct: Decimal | None
    # Share of the portfolio value, in percent: this position's market value over the total of all
    # positions that have one. None when it has no value.
    weight_pct: Decimal | None
    # The sector and industry a data source gives a share (Yahoo's names). Funds and coins have
    # none.
    sector: str | None
    industry: str | None


class FindingOut(BaseModel):
    status: str
    isin: str | None
    name: str
    history_quantity: Decimal | None
    statement_quantity: Decimal | None
    difference: Decimal | None


class CostCheckOut(BaseModel):
    name: str
    statement_cost: Decimal
    computed_cost: Decimal
    difference: Decimal
    ok: bool


class ReconciliationOut(BaseModel):
    source: str
    as_of: str
    counts: dict[str, int]
    review: list[FindingOut]  # everything that is not a clean match
    cost_checks: list[CostCheckOut]  # the statement's purchase value against the history's


class YearOut(BaseModel):
    year: int
    gains: Decimal
    losses: Decimal
    net: Decimal
    fees: Decimal
    tax_withheld: Decimal
    disposals: int
    cost_unknown_sales: int  # sales of units received with no cost: enter it on the Costs page
    history_gap_sales: int  # sales of units the history never bought: an import is missing


class InstrumentPnlOut(BaseModel):
    isin: str
    name: str | None
    realised_pnl: Decimal
    cost_unknown: bool  # includes a sale of units received with no cost
    history_gap: bool  # includes a sale of units the history never bought


class RealisedOut(BaseModel):
    """Gains and losses on everything sold so far, before tax, after fees (FIFO)."""

    by_year: list[YearOut]
    best: list[InstrumentPnlOut]
    worst: list[InstrumentPnlOut]
    net_total: Decimal


class UnattributedOut(BaseModel):
    isin: str
    name: str | None
    date: str
    type: str
    amount: Decimal


class ReviewSummaryOut(BaseModel):
    unpriced: int  # open shares and funds with no usable price (see the Prices page)
    cost_unknown: int  # open positions whose cost the broker does not give
    unattributed_cash: list[UnattributedOut]  # corporate-action cash that fits no action


class HoldingsOut(BaseModel):
    positions: list[PositionOut]
    by_class: dict[str, int]
    valued_total: Decimal  # market value of the positions that have one, in EUR
    valued_positions: int  # how many positions that is
    verified: int
    reconciliations: list[ReconciliationOut]
    realised: RealisedOut
    review: ReviewSummaryOut


def _trim(value: Decimal | None) -> Decimal | None:
    """12423.8547020000 → 12423.854702 (the database keeps ten decimals)."""
    return None if value is None else Decimal(format(value.normalize(), "f"))


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.01"))


def _cents(value: Decimal) -> Decimal:
    """Always a Decimal with two places, including zero (`0.00`, which is falsy in Python)."""
    return value.quantize(Decimal("0.01"))


def _finding(f: Finding) -> FindingOut:
    return FindingOut(
        status=f.status,
        isin=f.isin,
        name=f.name,
        history_quantity=_trim(f.history_quantity),
        statement_quantity=_trim(f.statement_quantity),
        difference=_trim(f.difference),
    )


def _position(p: Position, holdings: service.Holdings, weights: dict[str, Decimal]) -> PositionOut:
    cost = holdings.costs.get(p.isin)
    mark = holdings.marks.get(p.isin)
    value = pnl = pct = None
    if mark is not None:
        # Price, quantity and cost all belong to the statement date.
        value = service.mark_value(mark)
        known = mark.cost is not None and not mark.cost.flags & COST_MISSING
        if mark.cost is not None and known:
            pnl = value - mark.cost.total_cost
            pct = pnl / mark.cost.total_cost * 100 if mark.cost.total_cost else None
    average = cost.average_cost if cost else None
    sector, industry = holdings.sectors.get(p.isin, (None, None))
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
        purchase_value=_money(cost.cost) if cost else None,
        acquisition_costs=_money(cost.costs) if cost else None,
        total_cost=_money(cost.total_cost) if cost else None,
        average_cost=average.quantize(Decimal("0.0001")) if average is not None else None,
        cost_flags=sorted(cost.flags) if cost else [],
        price=_trim(mark.price) if mark else None,
        price_as_of=mark.as_of.isoformat() if mark else None,
        valued_quantity=_trim(mark.quantity) if mark else None,
        market_value=_money(value),
        unrealised_pnl=_money(pnl),
        unrealised_pct=pct.quantize(Decimal("0.01")) if pct is not None else None,
        weight_pct=weights[p.isin].quantize(Decimal("0.01")) if p.isin in weights else None,
        sector=sector,
        industry=industry,
    )


def _name(holdings: service.Holdings, isin: str) -> str | None:
    known = holdings.everything.get(isin)
    return known.name if known else None


def _realised(holdings: service.Holdings) -> RealisedOut:
    disposals = holdings.book.disposals
    years = realised_by_year(disposals)
    unknown = cost_unknown_isins(disposals)
    gaps = history_gap_isins(disposals)
    per_isin = sorted(realised_by_isin(disposals).items(), key=lambda kv: kv[1])
    losing = [kv for kv in per_isin if kv[1] < 0][:10]
    winning = [kv for kv in reversed(per_isin) if kv[1] > 0][:10]

    def entry(kv: tuple[str, Decimal]) -> InstrumentPnlOut:
        return InstrumentPnlOut(
            isin=kv[0],
            name=_name(holdings, kv[0]),
            realised_pnl=_cents(kv[1]),
            cost_unknown=kv[0] in unknown,
            history_gap=kv[0] in gaps,
        )

    return RealisedOut(
        by_year=[
            YearOut(
                year=y.year,
                gains=_cents(y.gains),
                losses=_cents(y.losses),
                net=_cents(y.net),
                fees=_cents(y.fees),
                tax_withheld=_cents(y.tax_withheld),
                disposals=y.disposals,
                cost_unknown_sales=y.cost_unknown_disposals,
                history_gap_sales=y.history_gap_disposals,
            )
            for y in years
        ],
        best=[entry(kv) for kv in winning],
        worst=[entry(kv) for kv in losing],
        net_total=_cents(sum((y.net for y in years), Decimal(0))),
    )


def _out(holdings: service.Holdings) -> HoldingsOut:
    coins_priced = "coingecko" in get_settings().price_providers
    weights, valued_total, valued_positions = service.weights(holdings)
    by_class: dict[str, int] = {}
    for p in holdings.positions:
        key = p.asset_class or "UNKNOWN"
        by_class[key] = by_class.get(key, 0) + 1
    return HoldingsOut(
        positions=[_position(p, holdings, weights) for p in holdings.positions],
        by_class=dict(sorted(by_class.items())),
        valued_total=_cents(valued_total),
        valued_positions=valued_positions,
        verified=len(holdings.verified),
        reconciliations=[
            ReconciliationOut(
                source=r.source,
                as_of=r.as_of,
                counts=r.counts,
                review=[_finding(f) for f in r.findings if f.status != Status.MATCH],
                cost_checks=[
                    CostCheckOut(
                        name=c.name,
                        statement_cost=_cents(c.statement_cost),
                        computed_cost=_cents(c.computed_cost),
                        difference=_cents(c.difference),
                        ok=ok,
                    )
                    for c, ok in r.cost_checks
                ],
            )
            for r in holdings.reconciliations
        ],
        realised=_realised(holdings),
        review=ReviewSummaryOut(
            # Shares, funds and coins can get a market price; bonds and the rest are not counted.
            # A coin counts only while CoinGecko is among the price providers.
            unpriced=sum(
                1
                for p in holdings.positions
                if p.asset_class in PRICEABLE
                and (p.asset_class != "CRYPTO" or coins_priced)
                and p.isin not in holdings.marks
            ),
            cost_unknown=sum(1 for c in holdings.costs.values() if FLAG_COST_UNKNOWN in c.flags),
            unattributed_cash=[
                UnattributedOut(
                    isin=u.isin,
                    name=_name(holdings, u.isin),
                    date=u.date.isoformat(),
                    type=u.type,
                    amount=_cents(u.amount),
                )
                for u in holdings.book.unattributed
            ],
        ),
    )


@router.get("", response_model=HoldingsOut)
def get_holdings(user: CurrentUser, db: UserDb) -> HoldingsOut:
    return _out(service.build_holdings(db, user.id))


def _read_pdf(file: UploadFile) -> bytes:
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file is too large")
    if not data.startswith(PDF_MAGIC):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "this file is not a PDF")
    return data


def _snapshot(
    source: str, statement: tr_crypto_pdf.CryptoStatement | tr_depot_pdf.DepotStatement
) -> Snapshot:
    return Snapshot(
        source=source,
        as_of=statement.as_of,
        lines=[line.to_json() for line in statement.lines],
        footer_count=statement.footer_count,
        footer_total=statement.footer_total,
        created_at=datetime.now(UTC),
    )


def _import_statement(
    kind: str | None, file: UploadFile, user: CurrentUser, db: UserDb
) -> HoldingsOut:
    """Parse an uploaded statement and keep its table rows. `kind` None: tell from the text."""
    data = _read_pdf(file)
    try:
        text = extract_text(data)
        if kind is None:
            kind = detect_kind(text)
            if kind is None:
                raise StatementFormatError(
                    "this is neither a Trade Republic securities statement (Depotauszug) "
                    "nor a crypto statement (Crypto-Übersicht)"
                )
        if kind == "depot":
            snapshot = _snapshot(tr_depot_pdf.SOURCE, tr_depot_pdf.parse_text(text))
        else:
            snapshot = _snapshot(tr_crypto_pdf.SOURCE, tr_crypto_pdf.parse_text(text))
    except StatementFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    snapshot.user_id = user.id
    db.add(snapshot)
    db.commit()
    return _out(service.build_holdings(db, user.id))


# Plain `def` handlers: FastAPI runs them in a worker thread, so parsing a PDF never blocks other
# requests.
@router.post("/statements", response_model=HoldingsOut, status_code=status.HTTP_201_CREATED)
def upload_statement(file: UploadFile, user: CurrentUser, db: UserDb) -> HoldingsOut:
    """A Depotauszug or a Crypto-Übersicht: the text says which."""
    return _import_statement(None, file, user, db)


@router.post("/statements/crypto", response_model=HoldingsOut, status_code=status.HTTP_201_CREATED)
def upload_crypto_statement(file: UploadFile, user: CurrentUser, db: UserDb) -> HoldingsOut:
    return _import_statement("crypto", file, user, db)


@router.post("/statements/depot", response_model=HoldingsOut, status_code=status.HTTP_201_CREATED)
def upload_depot_statement(file: UploadFile, user: CurrentUser, db: UserDb) -> HoldingsOut:
    return _import_statement("depot", file, user, db)

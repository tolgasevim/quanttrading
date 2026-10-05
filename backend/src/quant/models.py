"""ORM models for Phase 0: users and auth, instruments, EOD prices, FX, raw cache, job runs.

Portfolio tables (user-scoped, protected by row-level security per FR-3) arrive in Phase 1.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Store enum values ("admin"), not member names ("ADMIN")."""
    return [str(member.value) for member in enum_cls]


class Role(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-case
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=_enum_values), default=Role.MEMBER
    )
    # TOTP secret is set at setup and only enforced once confirmed with a valid code.
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    """Server-side session. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class Invite(Base):
    """Invite link (FR-1). Only the SHA-256 of the token is stored."""

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stable internal key, e.g. "NVDA", "NDX", "BTC". Instruments without an ISIN
    # (indices, coins) still need one.
    code: Mapped[str] = mapped_column(String(40), unique=True)
    isin: Mapped[str | None] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_class: Mapped[str] = mapped_column(String(20))  # stock, etf, etc, crypto, index
    currency: Mapped[str] = mapped_column(String(3))
    # Provider-specific symbols, e.g. {"yahoo": "SXRV.DE", "stooq": "sxrv.de"}.
    symbols: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PriceEOD(Base):
    __tablename__ = "prices_eod"
    __table_args__ = (UniqueConstraint("instrument_id", "date", name="uq_prices_eod_instr_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    adj_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(20))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FxRate(Base):
    """ECB reference rate: 1 EUR = rate units of `quote` (FR-25)."""

    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("quote", "date", name="uq_fx_rates_quote_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quote: Mapped[str] = mapped_column(String(3))
    date: Mapped[date] = mapped_column(Date)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    source: Mapped[str] = mapped_column(String(20))


class RawResponse(Base):
    """Every raw provider response is cached (PRD §8 rules) for replay and debugging."""

    __tablename__ = "raw_responses"
    __table_args__ = (Index("ix_raw_responses_provider_key", "provider", "request_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    request_key: Mapped[str] = mapped_column(String(300))
    status_code: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(50))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=_enum_values)
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


# --- User-owned data (Phase 1). Every table here has a user_id and a row-level security
# policy (FR-3, see quant.rls); the API only reaches it through a user-scoped session.


class ImportStatus(enum.StrEnum):
    PREVIEW = "preview"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class Import(Base):
    """One uploaded file. The file itself is never stored (FR-19a); only the parsed, stripped
    rows are staged here until the user confirms the preview (FR-15)."""

    __tablename__ = "imports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30))  # e.g. "tr_transactions_csv"
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, name="import_status", values_callable=_enum_values)
    )
    summary: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    staged: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)


class Transaction(Base):
    """A normalised broker transaction (FR-10). Personal data is removed before it gets here."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "external_id", name="uq_transactions_user_external"),
        Index("ix_transactions_user_date", "user_id", "date"),
        Index("ix_transactions_user_isin", "user_id", "isin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("imports.id", ondelete="SET NULL")
    )
    broker: Mapped[str] = mapped_column(String(20))  # "tr"
    external_id: Mapped[str] = mapped_column(String(100))  # broker's transaction id (FR-10b)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(20))  # normalised, see importers.tr_csv.Kind
    category: Mapped[str] = mapped_column(String(40))  # broker's raw category
    type: Mapped[str] = mapped_column(String(60))  # broker's raw type
    asset_class: Mapped[str | None] = mapped_column(String(20))
    isin: Mapped[str | None] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(200))  # instrument name only
    shares: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    original_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    original_currency: Mapped[str | None] = mapped_column(String(3))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    savings_plan: Mapped[bool] = mapped_column(Boolean, default=False)


class Snapshot(Base):
    """A broker holdings statement (e.g. the Crypto-Übersicht), reduced to its table rows.

    The file itself is never stored (FR-19a). The newest snapshot per source is what holdings
    are reconciled against; older ones are kept as history.
    """

    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30))
    as_of: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lines: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    footer_count: Mapped[int] = mapped_column(Integer)
    footer_total: Mapped[Decimal] = mapped_column(Numeric(20, 2))


class UnitCost(Base):
    """A cost per unit the owner entered for an instrument whose cost the broker does not give
    (spin-offs, rights, units transferred in; FR-20). It applies to every unit of that ISIN that
    has no cost in the history, whether still held or already sold."""

    __tablename__ = "unit_costs"
    __table_args__ = (UniqueConstraint("user_id", "isin", name="uq_unit_costs_user_isin"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    isin: Mapped[str] = mapped_column(String(20))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(28, 10))  # EUR per unit
    note: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

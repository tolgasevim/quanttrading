"""ORM models for Phase 0: users and auth, instruments, EOD prices, FX, raw cache, job runs.

Portfolio tables (user-scoped, protected by row-level security per FR-3) arrive in Phase 1.
"""

import enum
import uuid
from datetime import date, datetime, time
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
    Time,
    UniqueConstraint,
    func,
    true,
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
    # How the symbols were found: NULL for the seed list, else "yahoo", "openfigi", "manual", or
    # "none" when no resolver knew the ISIN (the row is inactive until a symbol is entered, FR-12).
    mapping_source: Mapped[str | None] = mapped_column(String(20))
    mapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The sector and industry a data source gives a share (Yahoo's names). Funds and coins have
    # none. `sector_checked_at` is when a source was last asked, so a share it does not know is not
    # asked about every night.
    sector: Mapped[str | None] = mapped_column(String(60))
    industry: Mapped[str | None] = mapped_column(String(100))
    sector_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


# The daily-move limits of decision D39, in percent. The columns below, the API and the alert job
# all take their defaults from here.
DEFAULT_DAILY_MOVES = True
DEFAULT_STOCK_PCT = Decimal(5)
DEFAULT_FUND_PCT = Decimal(3)
DEFAULT_CRYPTO_PCT = Decimal(10)


class Notification(Base):
    """An item in a user's notification centre (FR-70). `dedupe_key` makes an alert idempotent:
    the same event (a price move on a day, a failed job run) is stored once per user."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_user_key"),
        # The list is read newest first for one user.
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))  # daily_move | job_failed | ai_budget
    severity: Mapped[str] = mapped_column(String(10))  # info | warning
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(1000))
    isin: Mapped[str | None] = mapped_column(String(20))
    dedupe_key: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertSettings(Base):
    """A user's alert settings (FR-73). No row means the defaults. The move thresholds are the
    daily-move limits of decision D39. Quiet hours are for the channels that push (email,
    Telegram); the in-app centre always keeps every alert."""

    __tablename__ = "alert_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    daily_moves_enabled: Mapped[bool] = mapped_column(
        Boolean, default=DEFAULT_DAILY_MOVES, server_default=true()
    )
    move_stock_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=DEFAULT_STOCK_PCT, server_default=str(DEFAULT_STOCK_PCT)
    )
    move_fund_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=DEFAULT_FUND_PCT, server_default=str(DEFAULT_FUND_PCT)
    )
    move_crypto_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=DEFAULT_CRYPTO_PCT, server_default=str(DEFAULT_CRYPTO_PCT)
    )
    quiet_start: Mapped[time | None] = mapped_column(Time)
    quiet_end: Mapped[time | None] = mapped_column(Time)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AiConsent(Base):
    """A user's consent to the AI layer (FR-57) and the disclaimer they accepted (FR-4). One row
    per user. `version` is the disclaimer version accepted: after a change to the text it no
    longer equals the current one, and the user is asked again."""

    __tablename__ = "ai_consents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Send weights only: no quantities, values or costs reach the model (FR-57).
    anonymise_amounts: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class LlmUsage(Base):
    """One call to the language model: what it used and what it cost (FR-56). The ledger the
    spend caps are checked against."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        Index("ix_llm_usage_user_created", "user_id", "created_at"),
        Index("ix_llm_usage_created", "created_at"),  # the month's total across all users
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    purpose: Mapped[str] = mapped_column(String(30))  # ask
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    cost_eur: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    stop_reason: Mapped[str | None] = mapped_column(String(30))
    request_id: Mapped[str | None] = mapped_column(String(100))


class AiPick(Base):
    """The pick log (FR-52): every AI recommendation that names an instrument and a direction,
    with the time, the price then, the horizon and the reason. Scored later against a benchmark."""

    __tablename__ = "ai_picks"
    __table_args__ = (Index("ix_ai_picks_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    usage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_usage.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(String(1000))  # what the user asked
    name: Mapped[str] = mapped_column(String(200))
    isin: Mapped[str | None] = mapped_column(String(20))
    ticker: Mapped[str | None] = mapped_column(String(30))
    direction: Mapped[str] = mapped_column(String(10))  # buy | sell | hold
    horizon_months: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(String(2000))
    held: Mapped[bool] = mapped_column(Boolean, default=False)  # in the user's portfolio then
    # The price when the pick was made, from the stored prices. None when the instrument has none.
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    price_currency: Mapped[str | None] = mapped_column(String(3))
    price_date: Mapped[date | None] = mapped_column(Date)

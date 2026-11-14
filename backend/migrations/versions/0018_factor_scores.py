"""Universe, watchlist, fundamentals, factor scores and fund facts (FR-30 to FR-35)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-14 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUM = sa.Numeric(14, 4)
SCORE = sa.Numeric(6, 2)
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "universe_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("list_name", sa.String(length=30), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("list_name", "isin", name="uq_universe_members"),
    )
    op.create_table(
        "watchlist",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "isin"),
    )
    op.create_table(
        "fundamentals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("ev_ebit", NUM, nullable=True),
        sa.Column("fcf_yield", NUM, nullable=True),
        sa.Column("ev_sales", NUM, nullable=True),
        sa.Column("roic", NUM, nullable=True),
        sa.Column("gross_margin", NUM, nullable=True),
        sa.Column("net_debt_ebitda", NUM, nullable=True),
        sa.Column("revenue_growth", NUM, nullable=True),
        sa.Column("revenue_cagr_3y", NUM, nullable=True),
        sa.Column("eps_revision", NUM, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("isin", "as_of", name="uq_fundamentals_isin_as_of"),
    )
    op.create_table(
        "factor_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("value", SCORE, nullable=True),
        sa.Column("quality", SCORE, nullable=True),
        sa.Column("growth", SCORE, nullable=True),
        sa.Column("momentum", SCORE, nullable=True),
        sa.Column("low_vol", SCORE, nullable=True),
        sa.Column("composite", SCORE, nullable=True),
        sa.Column("composite_pct", SCORE, nullable=True),
        sa.Column("data_quality", sa.String(length=10), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("peers", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("isin", "day", name="uq_factor_scores_isin_day"),
    )
    op.create_index("ix_factor_scores_day", "factor_scores", ["day"])
    op.create_table(
        "instrument_facts",
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=3), nullable=False),
        sa.Column("ter", sa.Numeric(6, 3), nullable=True),
        sa.Column("fund_size_eur", sa.Numeric(18, 2), nullable=True),
        sa.Column("replication", sa.String(length=12), nullable=True),
        sa.Column("distribution", sa.String(length=4), nullable=True),
        sa.Column("domicile", sa.String(length=2), nullable=True),
        sa.Column("tracking_difference", sa.Numeric(6, 3), nullable=True),
        sa.Column("issuer", sa.String(length=100), nullable=True),
        sa.Column("delivery_claim", sa.Boolean(), nullable=True),
        sa.Column("roll_method", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.String(length=300), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("isin"),
    )
    # Public data shared by all users (no row-level security); only admins write it via the API.
    # The daily score rows are history: the job may add and correct today's, never delete.
    for table in ("universe_members", "fundamentals", "instrument_facts"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON factor_scores TO {APP_ROLE}")
    for seq in ("universe_members_id_seq", "fundamentals_id_seq", "factor_scores_id_seq"):
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON watchlist TO {APP_ROLE}")
    for statement in policy_sql("watchlist"):
        op.execute(statement)


def downgrade() -> None:
    for table in (
        "instrument_facts",
        "factor_scores",
        "fundamentals",
        "watchlist",
        "universe_members",
    ):
        op.drop_table(table)

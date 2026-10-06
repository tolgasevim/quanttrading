"""AI layer: consent, usage ledger and pick log

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE, policy_sql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "ai_consents",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anonymise_amounts", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("cost_eur", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("stop_reason", sa.String(length=30), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_user_created", "llm_usage", ["user_id", "created_at"])
    op.create_index("ix_llm_usage_created", "llm_usage", ["created_at"])
    op.create_table(
        "ai_picks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("usage_id", sa.UUID(), nullable=True),
        sa.Column("question", sa.String(length=1000), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("isin", sa.String(length=20), nullable=True),
        sa.Column("ticker", sa.String(length=30), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("horizon_months", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("held", sa.Boolean(), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("price_currency", sa.String(length=3), nullable=True),
        sa.Column("price_date", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["usage_id"], ["llm_usage.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_picks_user_created", "ai_picks", ["user_id", "created_at"])
    # Same access model as the other user-owned tables (see migration 0002).
    for table in ("ai_consents", "llm_usage", "ai_picks"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        for statement in policy_sql(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_ai_picks_user_created", table_name="ai_picks")
    op.drop_table("ai_picks")
    op.drop_index("ix_llm_usage_created", table_name="llm_usage")
    op.drop_index("ix_llm_usage_user_created", table_name="llm_usage")
    op.drop_table("llm_usage")
    op.drop_table("ai_consents")

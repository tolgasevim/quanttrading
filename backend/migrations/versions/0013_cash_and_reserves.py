"""Cash balance and earmarked reserves (FR-28, FR-28a, FR-28b)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "cash_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cash_balances_user_asof", "cash_balances", ["user_id", "as_of"])
    op.create_table(
        "reserves",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("loaned", sa.Numeric(20, 2), server_default="0", nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reserves_user", "reserves", ["user_id"])
    op.create_table(
        "reserve_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reserve_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("action", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("amount_before", sa.Numeric(20, 2), nullable=True),
        sa.Column("amount_after", sa.Numeric(20, 2), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reserve_events_user_at", "reserve_events", ["user_id", "at"])
    for table in ("cash_balances", "reserves", "reserve_events"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    # Entries and events are history: the app role may add them but not change or remove them
    # (the default privileges of migration 0002 grant all four).
    for table in ("cash_balances", "reserve_events"):
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {APP_ROLE}")
    for table in ("cash_balances", "reserves", "reserve_events"):
        for statement in policy_sql(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("reserve_events")
    op.drop_table("reserves")
    op.drop_table("cash_balances")

"""Target allocation, guardrails, deployment plan and the decision journal (FR-40 to FR-44, FR-49)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-14 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "allocation_settings",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=True),
        sa.Column("guardrails", sa.JSON(), nullable=True),
        sa.Column("deployment", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("isin", sa.String(length=20), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("guidance", sa.String(length=200), nullable=True),
        sa.Column("pick_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("price", sa.Numeric(20, 6), nullable=True),
        sa.Column("price_currency", sa.String(length=3), nullable=True),
        sa.Column("price_date", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_journal_entries_user_at", "journal_entries", ["user_id", "at"])
    for table in ("allocation_settings", "journal_entries"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    # A journal entry is a record: it can be added and removed, never rewritten.
    op.execute(f"REVOKE UPDATE ON journal_entries FROM {APP_ROLE}")
    for table in ("allocation_settings", "journal_entries"):
        for statement in policy_sql(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("journal_entries")
    op.drop_table("allocation_settings")

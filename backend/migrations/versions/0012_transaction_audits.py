"""Audit trail for manual transaction changes (FR-14)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transaction_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("broker", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transaction_audits_user_at", "transaction_audits", ["user_id", "at"])
    # An audit trail is append-only (the default privileges of migration 0002 grant all four).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON transaction_audits TO {APP_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON transaction_audits FROM {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE transaction_audits_id_seq TO {APP_ROLE}")
    for statement in policy_sql("transaction_audits"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("transaction_audits")

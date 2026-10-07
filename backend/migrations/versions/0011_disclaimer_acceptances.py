"""App-wide disclaimer acceptance (FR-4)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "disclaimer_acceptances",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON disclaimer_acceptances TO {APP_ROLE}")
    for statement in policy_sql("disclaimer_acceptances"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("disclaimer_acceptances")

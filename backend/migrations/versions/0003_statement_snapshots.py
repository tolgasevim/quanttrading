"""statement snapshots

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04 12:08:38.973769
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE, policy_sql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("footer_count", sa.Integer(), nullable=False),
        sa.Column("footer_total", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_snapshots_user_id"), "snapshots", ["user_id"], unique=False)
    # Same access model as the other user-owned tables (see migration 0002).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON snapshots TO {APP_ROLE}")
    for statement in policy_sql("snapshots"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(op.f("ix_snapshots_user_id"), table_name="snapshots")
    op.drop_table("snapshots")

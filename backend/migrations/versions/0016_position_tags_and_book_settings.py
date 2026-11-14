"""Position tags and book settings (FR-45 to FR-48)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("allocation_settings", sa.Column("subgroup_limits", sa.JSON(), nullable=True))
    op.add_column("allocation_settings", sa.Column("book_settings", sa.JSON(), nullable=True))
    op.create_table(
        "position_tags",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("isin", sa.String(length=20), nullable=False),
        sa.Column("subgroup", sa.String(length=30), nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("tracker_until", sa.Date(), nullable=True),
        sa.Column("short_term", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("stop_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("barrier", sa.Numeric(20, 6), nullable=True),
        sa.Column("barrier_isin", sa.String(length=20), nullable=True),
        sa.Column("barrier_long", sa.Boolean(), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "isin"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON position_tags TO {APP_ROLE}")
    for statement in policy_sql("position_tags"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("position_tags")
    op.drop_column("allocation_settings", "book_settings")
    op.drop_column("allocation_settings", "subgroup_limits")

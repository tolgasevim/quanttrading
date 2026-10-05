"""owner-entered cost per unit

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE, policy_sql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unit_costs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("isin", sa.String(length=20), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "isin", name="uq_unit_costs_user_isin"),
    )
    op.create_index(op.f("ix_unit_costs_user_id"), "unit_costs", ["user_id"], unique=False)
    # Same access model as the other user-owned tables (see migration 0002).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON unit_costs TO {APP_ROLE}")
    for statement in policy_sql("unit_costs"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(op.f("ix_unit_costs_user_id"), table_name="unit_costs")
    op.drop_table("unit_costs")

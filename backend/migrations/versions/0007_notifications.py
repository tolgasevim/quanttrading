"""notification centre and alert settings

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE, policy_sql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=False),
        sa.Column("isin", sa.String(length=20), nullable=True),
        sa.Column("dedupe_key", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_user_key"),
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)
    op.create_table(
        "alert_settings",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("daily_moves_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "move_stock_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="5",
            nullable=False,
        ),
        sa.Column(
            "move_fund_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="3",
            nullable=False,
        ),
        sa.Column(
            "move_crypto_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="10",
            nullable=False,
        ),
        sa.Column("quiet_start", sa.Time(), nullable=True),
        sa.Column("quiet_end", sa.Time(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    # Same access model as the other user-owned tables (see migration 0002).
    for table in ("notifications", "alert_settings"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        for statement in policy_sql(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("alert_settings")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_table("notifications")

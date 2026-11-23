"""Delivery settings, the Telegram read position and the pushed mark on alerts (FR-71, FR-72)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-23 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Alerts that exist now were never meant for Telegram: mark them pushed.
    op.add_column(
        "notifications", sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE notifications SET pushed_at = created_at")
    op.create_table(
        "delivery_settings",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_mode", sa.String(length=10), server_default="off", nullable=False),
        sa.Column("email_to", sa.String(length=320), nullable=True),
        sa.Column("digest_sent_on", sa.Date(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("link_code_hash", sa.String(length=64), nullable=True),
        sa.Column("link_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON delivery_settings TO {APP_ROLE}")
    for statement in policy_sql("delivery_settings"):
        op.execute(statement)
    op.create_table(
        "telegram_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("next_offset", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON telegram_state TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("telegram_state")
    op.drop_table("delivery_settings")
    op.drop_column("notifications", "pushed_at")

"""Weekly AI commentary (FR-54)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_commentaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("why_template", sa.String(length=200), nullable=True),
        sa.Column("model", sa.String(length=60), nullable=True),
        sa.Column("text", sa.String(length=10000), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "week_start", "kind", name="uq_ai_commentaries_week"),
    )
    # Written once per week and never edited: the app role may read and insert only (the default
    # privileges of migration 0002 give it all four).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ai_commentaries TO {APP_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON ai_commentaries FROM {APP_ROLE}")
    for statement in policy_sql("ai_commentaries"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("ai_commentaries")

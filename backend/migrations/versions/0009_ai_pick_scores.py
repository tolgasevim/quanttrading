"""AI pick scores (FR-52)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from quant.rls import APP_ROLE, policy_sql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_pick_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pick_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("window_months", sa.Integer(), nullable=False),
        sa.Column(
            "scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("pick_return_pct", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("benchmark_return_pct", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("excess_pct", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("hit", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pick_id"], ["ai_picks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pick_id", "window_months", name="uq_ai_pick_scores_pick_window"),
    )
    op.create_index("ix_ai_pick_scores_user", "ai_pick_scores", ["user_id"])
    # Written once by the nightly job, so the app role may read and insert, nothing else (the
    # default privileges of migration 0002 give it all four).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ai_pick_scores TO {APP_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON ai_pick_scores FROM {APP_ROLE}")
    for statement in policy_sql("ai_pick_scores"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_ai_pick_scores_user", table_name="ai_pick_scores")
    op.drop_table("ai_pick_scores")

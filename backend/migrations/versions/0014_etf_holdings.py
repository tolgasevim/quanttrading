"""ETF holdings files for look-through (FR-24)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-14 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "etf_holdings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("etf_isin", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("ticker", sa.String(length=30), nullable=True),
        sa.Column("holding_isin", sa.String(length=12), nullable=True),
        sa.Column("weight", sa.Numeric(10, 6), nullable=False),
        sa.Column("sector", sa.String(length=60), nullable=True),
        sa.Column("country", sa.String(length=60), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("asset_class", sa.String(length=30), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_etf_holdings_etf", "etf_holdings", ["etf_isin"])
    # Public data, shared by all users (no row-level security). Only admins write it, through the
    # API; the app role needs all four rights because a new file replaces the old rows.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON etf_holdings TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("etf_holdings")

"""Swing signal gates and signals (Phase 4)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "swing_gates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("signal", sa.String(length=20), nullable=False),
        sa.Column("computed_on", sa.Date(), nullable=False),
        sa.Column("n_trades", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Numeric(6, 2), nullable=False),
        sa.Column("expectancy", sa.Numeric(10, 4), nullable=False),
        sa.Column("profit_factor", sa.Numeric(10, 3), nullable=True),
        sa.Column("first_half", sa.Numeric(10, 4), nullable=True),
        sa.Column("second_half", sa.Numeric(10, 4), nullable=True),
        sa.Column("universe", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signal", "computed_on", name="uq_swing_gates_signal_day"),
    )
    op.create_table(
        "swing_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scanned_on", sa.Date(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("signal", sa.String(length=20), nullable=False),
        sa.Column("entry", sa.Numeric(20, 6), nullable=False),
        sa.Column("stop", sa.Numeric(20, 6), nullable=False),
        sa.Column("target", sa.Numeric(20, 6), nullable=False),
        sa.Column("atr", sa.Numeric(20, 6), nullable=False),
        sa.Column("max_hold", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scanned_on", "code", "signal", name="uq_swing_signals_day_code"),
    )
    # Public data derived from public prices, written by the nightly job only.
    for table in ("swing_gates", "swing_signals"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("swing_signals")
    op.drop_table("swing_gates")

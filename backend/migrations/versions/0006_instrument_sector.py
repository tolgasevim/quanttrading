"""sector and industry of an instrument

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("sector", sa.String(length=60), nullable=True))
    op.add_column("instruments", sa.Column("industry", sa.String(length=100), nullable=True))
    op.add_column(
        "instruments", sa.Column("sector_checked_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("instruments", "sector_checked_at")
    op.drop_column("instruments", "industry")
    op.drop_column("instruments", "sector")

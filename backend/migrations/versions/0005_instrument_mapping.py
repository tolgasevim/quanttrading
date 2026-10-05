"""how an instrument's symbols were found

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("mapping_source", sa.String(length=20), nullable=True))
    op.add_column("instruments", sa.Column("mapped_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("instruments", "mapped_at")
    op.drop_column("instruments", "mapping_source")

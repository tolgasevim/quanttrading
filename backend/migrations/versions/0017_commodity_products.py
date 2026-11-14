"""Tradable ETCs for the commodities (FR-62)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-14 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A short starting list of physically backed metal ETCs. Whether a broker offers them, and their
# cost, are not known here: an admin confirms those on the page. Other commodities start empty.
SEED = [
    ("gold", "DE000A0S9GB0", "Xetra-Gold", "Deutsche Börse Commodities", "physical",
     "Physical gold with a delivery claim. Holding it over a year may be tax-free in Germany: "
     "verify with a tax adviser."),
    ("gold", "IE00B4ND3602", "iShares Physical Gold ETC", "iShares", "physical", None),
    ("gold", "IE00B579F325", "Invesco Physical Gold ETC", "Invesco", "physical", None),
    ("silver", "IE00B4NCWG09", "iShares Physical Silver ETC", "iShares", "physical", None),
    ("silver", "JE00B1VS3333", "WisdomTree Physical Silver", "WisdomTree", "physical", None),
]  # fmt: skip


def upgrade() -> None:
    table = op.create_table(
        "commodity_products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("commodity", sa.String(length=12), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("issuer", sa.String(length=100), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("ter", sa.Numeric(6, 3), nullable=True),
        sa.Column("tr_available", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.String(length=300), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commodity", "isin", name="uq_commodity_products"),
    )
    op.bulk_insert(
        table,
        [
            {"commodity": c, "isin": i, "name": n, "issuer": iss, "kind": k, "notes": note}
            for c, i, n, iss, k, note in SEED
        ],
    )
    # Public data, shared by all users (no row-level security); only admins write it via the API.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON commodity_products TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE commodity_products_id_seq TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("commodity_products")

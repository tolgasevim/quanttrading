"""transactions, imports, row-level security

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27 14:46:53.457694
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from quant.rls import APP_ROLE, policy_sql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "imports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column(
            "status",
            sa.Enum("preview", "committed", "discarded", name="import_status"),
            nullable=False,
        ),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("staged", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_inserted", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_imports_user_id"), "imports", ["user_id"], unique=False)
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("import_id", sa.UUID(), nullable=True),
        sa.Column("broker", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("type", sa.String(length=60), nullable=False),
        sa.Column("asset_class", sa.String(length=20), nullable=True),
        sa.Column("isin", sa.String(length=20), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("shares", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("fee", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("tax", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("original_amount", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("original_currency", sa.String(length=3), nullable=True),
        sa.Column("fx_rate", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("savings_plan", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "external_id", name="uq_transactions_user_external"),
    )
    op.create_index("ix_transactions_user_date", "transactions", ["user_id", "date"], unique=False)
    op.create_index("ix_transactions_user_isin", "transactions", ["user_id", "isin"], unique=False)

    # Row-level security (FR-3). The app runs as the non-owner role APP_ROLE (quant.db sets it
    # on connect), so these policies apply to every query it makes. The role is cluster-wide,
    # so it may already exist (e.g. created by another database on the same server).
    op.execute(
        f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END $$"""
    )
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )
    # Listed here, not taken from quant.rls.USER_TABLES, so this migration never changes.
    for table in ("imports", "transactions"):
        for statement in policy_sql(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_transactions_user_isin", table_name="transactions")
    op.drop_index("ix_transactions_user_date", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index(op.f("ix_imports_user_id"), table_name="imports")
    op.drop_table("imports")
    sa.Enum(name="import_status").drop(op.get_bind(), checkfirst=True)
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")

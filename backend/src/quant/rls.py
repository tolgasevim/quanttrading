"""Row-level security plumbing (FR-3).

The app connects as the database owner but immediately switches to the non-owner role
`quant_app`, so Postgres enforces the RLS policies on user-owned tables. The owner itself (used
only by migrations, backups and admin psql sessions) is not subject to the policies.

At the start of every transaction the session writes its user into `app.user_id`
(transaction-local, so pooled connections never leak it). A session without a user sees no
user-owned rows at all: it fails closed. Background jobs that must work across users opt in
explicitly with `app.bypass_rls`.
"""

import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session, SessionTransaction

APP_ROLE = "quant_app"
USER_KEY = "rls_user_id"
BYPASS_KEY = "rls_bypass"

# Tables with a user_id column protected by the policy below. Add new user-owned tables here
# and in a migration that calls `enable_rls_sql`.
USER_TABLES = (
    "imports",
    "transactions",
    "snapshots",
    "unit_costs",
    "notifications",
    "alert_settings",
    "ai_consents",
    "llm_usage",
    "ai_picks",
    "ai_pick_scores",
    "ai_commentaries",
)


def policy_sql(table: str) -> list[str]:
    return [
        # ENABLE, not FORCE: the app never runs as the owner (it switches to APP_ROLE), and the
        # owner must see everything for migrations and pg_dump backups.
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"""CREATE POLICY {table}_owner ON {table}
            USING (
                user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                OR current_setting('app.bypass_rls', true) = 'on'
            )
            WITH CHECK (
                user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                OR current_setting('app.bypass_rls', true) = 'on'
            )""",
    ]


def scope_to_user(session: Session, user_id: uuid.UUID) -> Session:
    session.info[USER_KEY] = str(user_id)
    session.info.pop(BYPASS_KEY, None)
    # Apply to a transaction that may already be open (e.g. after the auth lookup).
    if session.in_transaction():
        _apply(session)
    return session


def bypass(session: Session) -> Session:
    """For trusted background jobs and admin CLI only."""
    session.info[BYPASS_KEY] = True
    if session.in_transaction():
        _apply(session)
    return session


_SET_SQL = text(
    "SELECT set_config('app.user_id', :uid, true), set_config('app.bypass_rls', :bp, true)"
)


def _params(info: dict[Any, Any]) -> dict[str, str]:
    return {"uid": info.get(USER_KEY, ""), "bp": "on" if info.get(BYPASS_KEY) else "off"}


def _apply(session: Session) -> None:
    session.execute(_SET_SQL, _params(session.info))


@event.listens_for(Session, "after_begin")
def _after_begin(session: Session, transaction: SessionTransaction, connection: Any) -> None:
    connection.execute(_SET_SQL, _params(session.info))

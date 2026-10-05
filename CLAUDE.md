# QuantTrading — notes for Claude Code

Personal quant investing guide for the owner and family. The spec is `docs/PRD.md`: read the
relevant FR-x before building anything, and cite it in the PR description.

## Layout
- `backend/`: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, APScheduler. Package `quant` in `backend/src/quant`.
  - `providers/`: market-data providers behind `PriceProvider` / `FxProvider` (PRD §8). A new source is a new class plus a registry entry.
  - `ingest/`: scheduled jobs. Every run is recorded in `job_runs`.
  - `importers/`: broker file parsers (pure functions) and the preview/commit workflow.
  - `portfolio/`: position engine (`positions.py`), FIFO lots, cost basis and realised P&L (`lots.py`), statement reconciliation (`reconcile.py`) the German tax estimate (`tax.py`) and the holdings read model (`service.py`).
  - `api/`: HTTP routers. `worker.py`: the scheduler. `cli.py`: admin commands.
- `frontend/`: Next.js (App Router). The browser only talks to Next, which proxies `/api/*` to FastAPI.
- `docker-compose.yml`: db, api (runs the migrations), worker, web.

## Commands
```sh
# backend (needs Postgres; tests use the quant_test database)
cd backend && uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
uv run pytest -q
uv run alembic revision --autogenerate -m "..."   # after model changes, then review the file
# frontend
cd frontend && npm ci && npm run build && npm run typecheck
```

## Non-negotiables
- **Never commit real broker data** (statements, exports, anything with names, IBANs or amounts).
  Fixtures are synthetic. Checks against real exports run on the owner's Mac mini and print only
  pass/fail (PRD §9a).
- Parsers drop personal data at parse time (FR-19a, FR-10a).
- Every user-owned table is scoped by `user_id` and protected by Postgres row-level security (FR-3):
  add it to `quant.rls.USER_TABLES` and apply `policy_sql` to it in its migration. API handlers use
  the `UserDb` session *and* filter by `user_id` explicitly.
- The app runs as the non-owner role `quant_app` (see `quant.db`); migrations run as the owner.
- Guidance only: never place trades or store broker credentials (D2).
- Every AI recommendation goes into the pick log (FR-52).
- Money is `Decimal`/`Numeric`, never float.
- A `shares` value on a dividend or distribution row is the record-date quantity, not a movement: positions come only from `TRADING`, `DELIVERY` and `CORPORATE_ACTION` rows.
- One PR per coherent slice, with tests. CI must be green.

## PR workflow
- Never merge a PR without the owner's explicit approval.
- When the owner asks for a PR review: review the diff against the non-negotiables above, check CI,
  merge conflicts and open threads, show the results, then ask for permission to merge and which
  merge method to use. Merge only after a clear yes.

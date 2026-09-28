# QuantTrading — notes for Claude Code

Personal quant investing guide for the owner and family. The spec is `docs/PRD.md`: read the
relevant FR-x before building anything, and cite it in the PR description.

## Layout
- `backend/`: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, APScheduler. Package `quant` in `backend/src/quant`.
  - `providers/`: market-data providers behind `PriceProvider` / `FxProvider` (PRD §8). A new source is a new class plus a registry entry.
  - `ingest/`: scheduled jobs. Every run is recorded in `job_runs`.
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
- Every user-owned table is scoped by `user_id` and protected by Postgres row-level security (FR-3).
- Guidance only: never place trades or store broker credentials (D2).
- Every AI recommendation goes into the pick log (FR-52).
- Money is `Decimal`/`Numeric`, never float.
- One PR per coherent slice, with tests. CI must be green.

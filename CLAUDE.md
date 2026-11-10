# CLAUDE.md

Conventions for working in this repo. Scope, data model, API and milestones live in `docs/SPEC.md`, which is the source of truth. If code and spec disagree, fix the spec first (or flag it), then the code.

## Layout (target)

```
backend/
  app/          FastAPI app: main.py (routes), momentum.py (SQL queries), schemas.py, settings.py
  ingest/       gharchive.py + dailyfile.py + load.py (M1), enrich.py, rollup.py (M2)
  scripts/      one-off analysis (GH Archive coverage scan, score checks)
  tests/        pytest; fixtures/ holds small .json.gz samples
db/migrations/  0001_init.sql, 0002_... (plain SQL, applied in order)
frontend/       Vite + React + TS + Recharts
docs/SPEC.md
docker-compose.yml
```

## Python

- Python 3.12. Format and lint with ruff (`ruff format`, `ruff check`), line length 100.
- Type hints on all public functions. Prefer `dataclass`/`TypedDict` over loose dicts for structured data.
- psycopg 3 with **raw SQL**, no ORM. Always use parameters (`%s` / `%(name)s`), never f-strings or string concatenation for values. Keep longer queries in module-level constants or `.sql` files.
- Use bulk writes for ingestion: `COPY` into a temp table, then `INSERT ... ON CONFLICT` upsert.
- Read config from env vars through one settings module: `DATABASE_URL`, `GITHUB_TOKEN`, plus scoring constants. Keep defaults in code and secrets out of it.
- Logging through `logging`, not `print`, in library code. Never log tokens or full request headers.

## Ingestion rules

- Stream GH Archive files: download → gzip decompress → parse line by line → aggregate. Never write raw events to disk or the DB.
- Every step must be idempotent and resumable. Daily manifests record progress (a day is done when its manifest says all 24 hours are done or missing), and re-running a range must not double count.
- Ingest writes daily aggregate files (`data/daily/`, git-ignored); only `ingest.load` writes activity to Postgres.
- All days are UTC.
- Treat missing GH Archive hours as expected: record them as `missing`, and don't crash.

## Testing

- `pytest` from `backend/`. Unit tests must not hit the network. Use fixture files and pure functions for parsing and aggregation.
- DB tests run against the docker-compose Postgres using a separate test database, created and migrated by a session fixture.
- The momentum math gets hand-computed expected values in tests.
- Before calling something done: `ruff check`, `ruff format --check` and `pytest` all pass.

## Frontend

- TypeScript strict mode. Keep API response types in one `api.ts` that mirrors SPEC §5.
- Recharts for all charts. Every view needs loading, error and empty states.

## Secrets and git

- `.env` is git-ignored. Commit `.env.example` with placeholder values only.
- Never commit `GITHUB_TOKEN`, DB passwords or downloaded GH Archive data.
- Small commits with imperative messages ("Add hourly file parser").

## Working style

- Work milestone by milestone (M1 → M5) and meet each milestone's "done when" before moving on.
- When a decision changes behavior (weights, thresholds, tracked-set size), add a row to the SPEC §8 decisions log.

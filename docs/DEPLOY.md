# Deploying

The whole app is one Docker image (API + built dashboard) plus a Postgres database holding the
fixed 2025 dataset. Nothing runs on a schedule: the data is loaded once.

## What it needs

| Piece | Requirement | Measured |
|---|---|---|
| Web service | Runs a Docker image, sets `PORT`, about 256 MB of RAM | image is 231 MB |
| Postgres 16+ | About 520 MB of storage | 519 MB freshly restored (571 MB on the laptop after reloads) |
| Dump file | Custom-format `pg_dump` | 42 MB; dump 2 min 41 s, restore 14 s locally |

## Hosting options (checked 2026-11-10)

These limits come from web searches on that day, mostly from third-party summaries. Check each
provider's own pricing page before relying on them.

| Option | Cost | Limits that matter here |
|---|---|---|
| **Render web service (free)** | $0 | Sleeps when idle, so the first visit after a quiet spell takes a while to wake; 750 instance hours a month |
| **Neon Postgres (free)** | $0 | Neon's FAQ says 1 GB per project and 100 compute-hours a month, with no expiry; some third-party pages still say 0.5 GB, which would be too small |
| Render Postgres (free) | $0 | 1 GB, but the database is deleted 30 days after creation unless upgraded |
| Fly.io | from about $2 a month | No free tier for new accounts |

**Recommended: Render (free) for the web service and Neon (free) for the database.** Both are
free without an expiry date. If Neon's limit turns out to be 0.5 GB, lower `TRACKED_REPOS`
(20,000 repos is about 460 MB), reload, rebuild the rollups and dump again.

## Steps

1. **Push the repo to GitHub.** CI (`.github/workflows/ci.yml`) runs on every push to `main`
   and on pull requests.
2. **Create the database.** In Neon, create a project with Postgres 16 or newer and copy its
   connection string (it ends in `?sslmode=require`). Keep it out of git and out of chat; put
   it in `.env` as `PROD_DATABASE_URL`.
3. **Export the local database** (Docker Desktop running, from the repo root):
   ```bash
   docker compose exec -T db pg_dump -U gea -Fc --no-owner --no-acl gea > data/gea.dump
   ```
4. **Load it into the hosted database** (bash; reads `PROD_DATABASE_URL` from `.env`):
   ```bash
   export PROD_DATABASE_URL="$(grep '^PROD_DATABASE_URL=' .env | cut -d= -f2-)"
   docker run --rm -e PROD_DATABASE_URL -v "$(pwd)/data:/data:ro" postgres:18 sh -c 'pg_restore --no-owner --no-acl --jobs 2 -d "$PROD_DATABASE_URL" /data/gea.dump'
   ```
   Use a `postgres` image at least as new as the hosted server, and the direct connection
   string, not the pooled one. The dump includes `schema_migrations`, so the app sees the schema
   as up to date. Then run `ANALYZE` once on the hosted database so the planner has statistics.
5. **Create the web service.** In Render, choose New → Blueprint and pick the repo; it reads
   `render.yaml`. Set `DATABASE_URL` to the Neon connection string when asked.
6. **Check it.** `/health` should return `{"status":"ok"}`, `/` the dashboard, and `/docs` the
   API reference.

## First deployment (2026-11-10)

- Neon free project on Postgres 18.6 in `us-east-2`. The restore took 52 s and every table's
  row count matches the local database (4,860,979 activity rows). Size on Neon: 519 MB.
- Query times against Neon from the laptop, uncached, on one open connection: language
  leaderboard 0.4 s (30 days) to 0.9 s (90 days); topic leaderboard 1.6 s to 4.0 s. The API
  caches each result after the first request.
- The API opens a new database connection per request, which adds a TLS handshake each time.
  A connection pool (`psycopg_pool`) is the next improvement if responses feel slow.

## Try the production image locally

```bash
docker compose --profile app up --build
```

Then open http://localhost:8080. This is the same image Render builds, pointed at the local
database.

## Updating the data

Re-run the pipeline locally (`ingest.load`, `ingest.enrich`, `ingest.rollup`), then repeat steps
3 and 4 with `pg_restore --clean --if-exists`, and restart the web service: it caches query
results in memory.

# GitHub Ecosystem Analyzer — Spec

Status: draft v0.1 (2026-11-04). This is the source of truth for scope, data model, API and milestones. Change this file first, then change the code.

## 1. Purpose

Show which technologies on GitHub are gaining or losing momentum. The unit of analysis is a **language** or a **topic**, not a repo, user or org. Repos appear only as the top drivers of a trend.

### Questions it answers

1. Which languages and topics gained the most activity in the last 30, 60 or 90 days, compared with the period before?
2. Which ones are declining?
3. For a given language or topic, what does daily activity (stars, forks, pushes, PRs, issues) look like over time?
4. Which repos drove that change?
5. How fresh is the data? (date of the latest day ingested)

### Non-goals

- User accounts, auth or saved views
- Real-time or streaming updates (data is refreshed daily)
- ML forecasting
- Covering every GH Archive event type
- Analyzing one repo's dependency tree, a single user or org, package dependents, or contributor overlap (possible later phase)
- CSV export (easy to add later, not in scope now)

## 2. Data sources

### GH Archive (no token)

Hourly files at `https://data.gharchive.org/YYYY-MM-DD-H.json.gz` (H = 0..23, no zero-padding, UTC). Each line is one public event.

| Event type | Filter | Counted as |
|---|---|---|
| `WatchEvent` | `payload.action == "started"` | star |
| `ForkEvent` | none | fork (credited to the source repo, `repo.id`) |
| `PushEvent` | none | push |
| `PullRequestEvent` | `payload.action == "opened"` | PR opened |
| `IssuesEvent` | `payload.action == "opened"` | issue opened |

All other event types are skipped. A repo is keyed by `repo.id`, which survives renames. `repo.name` is kept as the latest known `owner/name`.

**Volume:** one hourly file is roughly 50–150 MB gzipped. 180 days is over 4,000 files and on the order of 100+ GB downloaded. Ingestion must stream each file (download, decompress, filter and aggregate in a single pass) and never store raw events. Check the per-hour size during M1 and adjust the backfill plan to match.

### GitHub GraphQL API (`GITHUB_TOKEN`)

GH Archive events don't include a repo's language or topics. The enrichment step fetches `primaryLanguage`, `repositoryTopics` and `stargazerCount` for **tracked repos only** (see §3.2), batching about 50 repos per GraphQL query with aliases. It must respect `rateLimit.remaining` and back off when the limit runs low. The token comes from the `GITHUB_TOKEN` env var and is never committed or logged.

## 3. Data model (PostgreSQL 16)

All dates are UTC days.

### 3.1 Tables

```sql
-- One row per repo we have seen in a tracked set
repos (
  repo_id        BIGINT PRIMARY KEY,      -- GitHub repo id from GH Archive
  full_name      TEXT NOT NULL,           -- latest known owner/name
  language       TEXT,                    -- NULL until enriched or if none
  topics         TEXT[] NOT NULL DEFAULT '{}',
  stars_total    INTEGER,                 -- from enrichment, for display only
  enriched_at    TIMESTAMPTZ,
  enrich_status  TEXT                     -- 'ok' | 'not_found' | 'error'
)

-- Daily counts per repo (the core fact table)
repo_daily_activity (
  repo_id        BIGINT NOT NULL,
  day            DATE   NOT NULL,
  stars          INTEGER NOT NULL DEFAULT 0,
  forks          INTEGER NOT NULL DEFAULT 0,
  pushes         INTEGER NOT NULL DEFAULT 0,
  prs_opened     INTEGER NOT NULL DEFAULT 0,
  issues_opened  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (repo_id, day)
)

-- Ingestion bookkeeping, makes re-runs idempotent
ingest_hours (
  hour_ts        TIMESTAMPTZ PRIMARY KEY, -- start of the GH Archive hour
  status         TEXT NOT NULL,           -- 'done' | 'failed' | 'missing'
  events_seen    INTEGER,
  events_kept    INTEGER,
  processed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)

-- Rollups, rebuilt after each ingest + enrichment run
language_daily (language TEXT, day DATE, stars, forks, pushes, prs_opened,
                issues_opened, active_repos, PRIMARY KEY (language, day))
topic_daily    (topic TEXT,    day DATE, ...same columns...,
                PRIMARY KEY (topic, day))
```

A repo counts toward **every** topic it has, so topic totals overlap and must not be summed across topics.

### 3.2 Keeping the table small: the tracked set

Millions of repos have at least one event per day, so storing a row per active repo per day would mean over 100M rows, too much for a free hosted Postgres. Plan:

1. Ingest aggregates each **day** in memory, keyed by `repo_id`.
2. Only repos that meet a daily threshold are written. Default: at least 1 star **or** at least 1 fork that day. That drops repos with only pushes (mostly private-workflow and bot repos).
3. The **tracked set** is the top `N` repos by activity across the retention window (default `N = 25,000`). Only tracked repos are enriched and counted in the rollups. Rows for repos that fall out of the set are pruned.

Both the threshold and `N` are config values, and M1/M2 should measure the row counts they produce before we commit to them.

**Retention:** 180 days, because the 90-day window compares the last 90 days with the 90 days before.

## 4. Momentum score

```
activity(day)  = stars + 2·forks + 0.5·pushes          (weights v1: a guess)
A_cur          = Σ activity over the last W days
A_prev         = Σ activity over the W days before that
momentum       = (A_cur − A_prev) / max(A_prev, FLOOR)
```

- `W` is the range picker value, 30, 60 or 90 (default 30). v1 as originally stated is `W = 30`.
- `FLOOR` (default 100) stops tiny languages or topics from showing +5000% growth.
- Languages or topics with fewer than `MIN_REPOS` (default 20) active tracked repos in the current window are left out of the leaderboard.
- The window ends at the **latest fully ingested day**, never at today.
- PRs and issues are stored and charted but carry no weight in v1.

**Things to check against real data (M3):**
- Do pushes dominate? Bots and CI repos can push hundreds of times a day. Options: cap pushes per repo per day, or drop actors whose login ends in `[bot]`.
- Is the leaderboard stable if the weights change by ±50%? If the ranking flips, the weights matter and need a justification.
- Does a single viral repo dominate a small language? Possibly cap any one repo's share of `A_cur`.
- Record the outcome in §8 (Decisions log).

## 5. API

FastAPI, JSON only, prefix `/api/v1`. Language and topic names are passed as **query params**, because names like `C++`, `C#` and `Jupyter Notebook` are awkward in paths.

Shared params: `dimension` = `language` | `topic`, `window` = `30` | `60` | `90` (default 30).

### `GET /health`
`{"status": "ok"}` and a check that the DB is reachable.

### `GET /api/v1/meta`
```json
{ "latest_day": "2026-11-03", "earliest_day": "2026-05-08",
  "windows": [30, 60, 90], "tracked_repos": 25000,
  "weights": {"stars": 1, "forks": 2, "pushes": 0.5} }
```

### `GET /api/v1/leaderboard?dimension=language&window=30&limit=20&order=desc`
`order=desc` gives gainers and `asc` gives decliners.
```json
{ "dimension": "language", "window": 30, "as_of": "2026-11-03",
  "items": [
    { "rank": 1, "name": "Zig", "momentum": 0.42,
      "activity_current": 18234.5, "activity_previous": 12840.0,
      "active_repos": 311 }
  ] }
```

### `GET /api/v1/timeseries?dimension=language&name=Rust&window=90`
Daily points covering the current window plus the previous one (so 2·W days), letting the chart show both periods.
```json
{ "name": "Rust", "dimension": "language",
  "points": [ { "day": "2026-08-06", "stars": 0, "forks": 0, "pushes": 0,
                "prs_opened": 0, "issues_opened": 0, "activity": 0.0 } ] }
```

### `GET /api/v1/top-repos?dimension=language&name=Rust&window=30&limit=10`
The repos ranked by their contribution to the change (`A_cur − A_prev` for that repo).
```json
{ "items": [ { "full_name": "owner/repo", "activity_current": 0.0,
               "activity_previous": 0.0, "delta": 0.0,
               "stars_total": 0, "url": "https://github.com/owner/repo" } ] }
```

Errors: unknown `dimension` or `window` gives 422 (FastAPI validation). An unknown `name` gives 404.

## 6. Architecture summary

```
GH Archive ──► ingest (Python, streaming) ──► repo_daily_activity
GitHub GraphQL ──► enrich (tracked set) ──► repos.language / topics
                         rollup (SQL) ──► language_daily / topic_daily
                                  FastAPI ──► React dashboard
```

The README has the full diagram. A scheduler (GitHub Actions cron, M5) runs ingest → enrich → rollup once a day.

## 7. Milestones

### M1 — Ingest pipeline
- `docker-compose.yml` with Postgres 16; numbered SQL migrations in `db/migrations/`, plus a tiny runner.
- `ingest` script: `python -m ingest.gharchive --start YYYY-MM-DD --end YYYY-MM-DD`. Streams the hourly files, applies the §2 filters, aggregates per day and upserts. Idempotent via `ingest_hours`, with a retry for hours that failed and a record for hours that are missing (GH Archive has gaps).
- Unit tests on a small fixture `.json.gz`, covering the event filters and per-day aggregation.
- **Done when:** one real day ingests end to end, a re-run changes nothing, and pytest and ruff pass. Record events/hour, MB/hour and rows/day here.

### M2 — Enrichment + rollups
- Tracked-set selection and pruning (§3.2).
- GraphQL enrichment with batching, rate-limit backoff, and handling for not-found or renamed repos.
- Rollup SQL that rebuilds `language_daily` and `topic_daily`.
- Momentum query in SQL, unit-tested on a hand-built dataset with known answers.
- **Done when:** a SQL query returns a believable language leaderboard for a backfilled 60-day range.

### M3 — API + weight validation
- All §5 endpoints. Integration tests against a test database seeded with fixtures.
- Weight analysis script: compare rankings under different weights and push caps (§4). Write the decision in §8.
- **Done when:** the endpoints are documented at `/docs` (OpenAPI), tests pass, and the weights are justified.

### M4 — Dashboard
- Vite + React + TS + Recharts: leaderboard (language/topic toggle, gainers/decliners), time series for the selected item (current vs previous window), top repos table, 30/60/90 picker, "data as of" label.
- Loading, error and empty states; usable on a phone-width screen.
- **Done when:** every §1 question can be answered from the UI.

### M5 — Deploy + schedule
- Dockerfiles (API serves the built frontend, or two services), deploy to Fly.io or Render with a hosted Postgres.
- GitHub Actions: CI (ruff, pytest, frontend build) on PRs, plus a daily cron running ingest → enrich → rollup, with `GITHUB_TOKEN` and the DB URL as repo secrets.
- README: live link, screenshots, architecture, and how momentum is computed.
- **Done when:** the public URL shows data that is at most 2 days old without any manual steps.

## 8. Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-11-04 | Raw SQL with psycopg 3, no ORM | The queries are analytical, and SQL is the interesting part |
| 2026-11-04 | Momentum v1 weights 1 / 2 / 0.5 | Starting guess, to be validated in M3 |
| 2026-11-04 | Names passed as query params, not path segments | `C++`, `C#`, and names with spaces |

## 9. Open questions

- Tracked-set size `N` and the daily threshold: settle after measuring in M1/M2.
- Backfill cost: 180 days of downloads may be slow on a laptop. Fallback: backfill a shorter range first (e.g. 60 days, which only supports the 30-day window) and grow it daily.
- Hosted Postgres size limits on Fly.io or Render free tiers will decide the final `N`.

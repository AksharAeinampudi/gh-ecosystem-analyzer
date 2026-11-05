# GitHub Ecosystem Analyzer — Spec

Status: draft v0.2 (2026-11-04). This is the source of truth for scope, data model, API and milestones. Change this file first, then change the code.

## 1. Purpose

Show which technologies on GitHub are gaining or losing momentum. The unit of analysis is a **language** or a **topic**, not a repo, user or org. Repos appear only as the top drivers of a trend.

**Dataset:** a fixed historical period, **2025-01-01 → 2025-09-30** (UTC). This is the stretch where GH Archive coverage is stable enough to compare (see §8a). Data after that date is not used in v1.

### Questions it answers

1. Which languages and topics grew their **share of GitHub activity** the most over 30, 60 or 90 days, compared with the period before? The windows end on a chosen date, by default the last day of the dataset.
2. Which ones are declining?
3. For a given language or topic, what does daily activity (stars, forks, pushes, PRs, issues) and its share look like over time?
4. Which repos drove that change?
5. Which period does the data cover? (the earliest and latest ingested days)

### Non-goals

- User accounts, auth or saved views
- Real-time or streaming updates. v1 is a fixed 2025 snapshot. A live mode built on daily `stargazerCount` snapshots is a possible Phase 2 (§9).
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

**Volume:** a 2025 day is about 0.6–1 GB gzipped (24 hourly files), so the 273-day dataset is roughly 200 GB downloaded. Ingestion must stream each file (download, decompress, filter and aggregate in a single pass) and never store raw events. The backfill is a one-time job and can run in chunks, because finished days are skipped.

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
  name_seen_day  DATE NOT NULL,           -- day full_name was seen; out-of-order backfills
                                          -- never overwrite a newer name with an older one
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

**Retention:** the whole dataset (2025-01-01 → 2025-09-30). Nothing rolls off, because the period is fixed.

## 4. Momentum score

Momentum is measured by **share of total activity**, not by raw totals. GH Archive's collection volume changed during 2025: every event type dropped about 38% at once in June. Raw growth would read that as every language collapsing. Shares cancel a uniform change like that out, and "gaining relative to the rest of GitHub" is the more meaningful question anyway.

```
activity(repo, day) = stars + 2·forks + 0.5·pushes        (weights v1: a guess)

For a language or topic x, a window of W days ending on as_of, and the W days before it:
A_cur(x), A_prev(x) = Σ activity of tracked repos in x over each window
T_cur, T_prev       = Σ activity of ALL tracked repos over each window (each repo counted once)

share_cur(x)  = A_cur(x)  / T_cur
share_prev(x) = max(A_prev(x), FLOOR) / T_prev
momentum(x)   = share_cur(x) / share_prev(x) − 1
```

- `W` is the range picker value: 30, 60 or 90 (default 30).
- `as_of` defaults to the last ingested day. Both windows must fit inside the dataset, so the earliest `as_of` is the dataset start + 2·W − 1 days (2025-06-29 for W = 90).
- `FLOOR` (default 100) stops tiny languages or topics from showing +5000% growth.
- Languages or topics with fewer than `MIN_REPOS` (default 20) active tracked repos in the current window are left out of the leaderboard.
- Repos with no language (`NULL`) still count toward `T`. Topic totals overlap (one repo, many topics), but `T` is still the plain total over repos.
- PRs and issues are stored and charted but carry no weight in v1.

**Things to check against real data (M3):**
- **Is the share series continuous across 2025-06?** If the June drop was not uniform across languages, a step in a language's share would show up there. If it does, restrict comparisons to windows that don't cross the step (Jan–May or Jun–Sep).
- Do pushes dominate? Bots and CI repos can push hundreds of times a day. Options: cap pushes per repo per day, or drop actors whose login ends in `[bot]`.
- Is the leaderboard stable if the weights change by ±50%? If the ranking flips, the weights matter and need a justification.
- Does a single viral repo dominate a small language? Possibly cap any one repo's share of `A_cur`.
- Record the outcome in §8 (Decisions log).

## 5. API

FastAPI, JSON only, prefix `/api/v1`. Language and topic names are passed as **query params**, because names like `C++`, `C#` and `Jupyter Notebook` are awkward in paths.

Shared params: `dimension` = `language` | `topic`, `window` = `30` | `60` | `90` (default 30), `as_of` = a date (optional, defaults to the latest day; see §4 for the allowed range).

### `GET /health`
`{"status": "ok"}` and a check that the DB is reachable.

### `GET /api/v1/meta`
```json
{ "earliest_day": "2025-01-01", "latest_day": "2025-09-30",
  "windows": [30, 60, 90], "tracked_repos": 25000,
  "weights": {"stars": 1, "forks": 2, "pushes": 0.5} }
```

### `GET /api/v1/leaderboard?dimension=language&window=30&limit=20&order=desc`
`order=desc` gives gainers and `asc` gives decliners.
```json
{ "dimension": "language", "window": 30, "as_of": "2025-09-30",
  "items": [
    { "rank": 1, "name": "Zig", "momentum": 0.42,
      "share_current": 0.0031, "share_previous": 0.0022,
      "activity_current": 18234.5, "activity_previous": 12840.0,
      "active_repos": 311 }
  ] }
```

### `GET /api/v1/timeseries?dimension=language&name=Rust&window=90`
Daily points covering the current window plus the previous one (so 2·W days), letting the chart show both periods. `share` is that day's activity divided by the day's total over all tracked repos; the dashboard charts it by default.
```json
{ "name": "Rust", "dimension": "language", "as_of": "2025-09-30",
  "points": [ { "day": "2025-04-04", "stars": 0, "forks": 0, "pushes": 0,
                "prs_opened": 0, "issues_opened": 0, "activity": 0.0,
                "share": 0.0 } ] }
```

### `GET /api/v1/top-repos?dimension=language&name=Rust&window=30&limit=10`
The repos ranked by their contribution to the change (`A_cur − A_prev` for that repo).
```json
{ "items": [ { "full_name": "owner/repo", "activity_current": 0.0,
               "activity_previous": 0.0, "delta": 0.0,
               "stars_total": 0, "url": "https://github.com/owner/repo" } ] }
```

Errors: an unknown `dimension` or `window`, or an `as_of` outside the allowed range, gives 422. An unknown `name` gives 404.

## 6. Architecture summary

```
GH Archive ──► ingest (Python, streaming) ──► repo_daily_activity
GitHub GraphQL ──► enrich (tracked set) ──► repos.language / topics
                         rollup (SQL) ──► language_daily / topic_daily
                                  FastAPI ──► React dashboard
```

The README has the full diagram. For the fixed 2025 dataset, ingest → enrich → rollup runs once, as a backfill. A daily schedule only comes back if Phase 2 (live mode) is built.

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
- Backfill 2025-01-01 → 2025-09-30. It can start with Mar–May (enough for W = 30 and 60) and grow from there.
- **Done when:** a SQL query returns a believable share-based language leaderboard for the backfilled range.

### M3 — API + weight validation
- All §5 endpoints. Integration tests against a test database seeded with fixtures.
- Weight analysis script: compare rankings under different weights and push caps, and check that shares stay continuous across June 2025 (§4). Write the decisions in §8.
- **Done when:** the endpoints are documented at `/docs` (OpenAPI), tests pass, and the weights are justified.

### M4 — Dashboard
- Vite + React + TS + Recharts: leaderboard (language/topic toggle, gainers/decliners), time series for the selected item (share by default, raw counts as a toggle, current vs previous window), top repos table, 30/60/90 picker, an "as of" date control limited to the dataset, and a visible note that the data is a 2025 snapshot and why.
- Loading, error and empty states; usable on a phone-width screen.
- **Done when:** every §1 question can be answered from the UI.

### M5 — Deploy
- Dockerfiles (API serves the built frontend, or two services), deploy to Fly.io or Render with a hosted Postgres loaded from the local backfill (`pg_dump` of the slim tables).
- GitHub Actions: CI (ruff, pytest, frontend build) on PRs.
- README: live link, screenshots, architecture, how momentum is computed, and the GH Archive coverage finding (§8a), which shows real data-quality work.
- **Done when:** the public URL serves the full 2025 dataset, and CI runs on every PR.

## 8. Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-11-04 | Raw SQL with psycopg 3, no ORM | The queries are analytical, and SQL is the interesting part |
| 2026-11-04 | Momentum v1 weights 1 / 2 / 0.5 | Starting guess, to be validated in M3 |
| 2026-11-04 | Names passed as query params, not path segments | `C++`, `C#`, and names with spaces |
| 2026-11-04 | Ingest unit of work = one UTC day, written by replacing that day's rows | The daily threshold needs the whole day; replacing makes re-runs idempotent |
| 2026-11-04 | A 404 for an hour that ended less than 6h ago is `failed`, not `missing` | GH Archive publishes with a delay; don't mark an hour missing too early |
| 2026-11-04 | Dataset fixed to 2025-01-01 → 2025-09-30; no daily refresh in v1 | Coverage scan (§8a): stars unreliable from Oct 2025, erratic from Aug 2026 |
| 2026-11-04 | Momentum = relative change in share of total activity, not raw growth | Cancels the uniform June 2025 volume drop; measures growth relative to the ecosystem |
| 2026-11-04 | API takes an optional `as_of` date | With a fixed dataset, exploring different end dates is the main way to look at trends |

## 8a. M1 measurements (2026-11-04)

Single 15:00 UTC hours sampled with stdlib `json`. Parsing one 19 MB hour took about 2.6 s.

| Hour | Events | WatchEvent (stars) | PushEvent | PRs opened |
|---|---|---|---|---|
| 2024-10-01 | 260,103 | 8,496 | 149,741 | 10,280 |
| 2025-06-02 | 158,437 | 5,278 | 90,755 | 7,110 |
| 2025-10-06 | 168,794 | 4,458 | 95,872 | — |
| 2026-01-05 | 153,215 | 3,062 | 107,569 | — |
| 2026-04-06 | 158,506 | 3,146 | 113,325 | — |
| 2026-07-06 | 164,328 | 57 | 147,029 | — |
| 2026-10-01 | 85,569 | 361 | 64,617 | 1,217 |
| 2026-10-20 | 55,085 | 226 | 42,730 | — |
| 2026-10-26 | 2,780 | 194 | 0 | — |
| 2026-11-02 | 4,630 | 313 | 0 | — |

On 2026-11-04, 2026-10-26-03 and 2026-11-03-09 returned 404. A HEAD scan a few hours later found both present, so GH Archive publishes some hours late (see §9).

### Coverage scan (2026-11-04)

Made with `backend/scripts/coverage_scan.py`; `coverage_report.py` prints the full tables. The scan downloaded the 16:00 UTC hour once a week from 2025-01-06 to 2026-05-31, then every day to 2026-11-03 (229 hours), plus HEAD requests for every hour since 2026-06-01. Monthly medians per sampled hour:

| Period | Events | Stars | Forks | Pushes | PRs opened | What changed |
|---|---|---|---|---|---|---|
| 2025-01 → 05 | ~265k | ~8.8k | ~2.5k | ~155k | ~11.3k | Stable baseline |
| 2025-06 → 09 | ~163k | ~5.0k | ~1.3k | ~94k | ~7.3k | Everything fell ~38% at once, so the mix stayed the same |
| 2025-10 → 2026-01 | ~150k | ~2.4k | ~0.6k | ~100k | ~3.9k | Stars, forks and PRs fell ~50% more while pushes held steady, so the mix shifted |
| 2026-02 → 05 | ~153k | 280–1.8k | 60–440 | 110–127k | 0.4–2.5k | Stars keep decaying; pushes grow |
| 2026-06 → 08 | ~160k | ~50–150 | ~10–40 | ~140k | ~100–200 | Stars down ~99% from the baseline; push payloads no longer carry `commits` |
| 2026-08-18 → now | 1k–135k | 100–8.6k | erratic | often **0** | erratic | Unstable: pushes vanish for days, bursty days run up to 2 GB |

Conclusions:
1. The only window where both the volume and the mix of events are stable is **2025-01 → 2025-05**. 2025-06 → 2025-09 has the same mix at a lower volume.
2. Since about October 2025, star and fork counts no longer track real GitHub activity. Momentum built on GH Archive stars is not valid from then on.
3. A uniform drop like June 2025's breaks absolute growth but not **share of total**. If momentum compares a language's share of all activity in each window instead of raw totals, the comparison survives across the June 2025 step, which gives a usable range of 2025-01 → 2025-09.
4. A live daily dashboard can't rely on GH Archive for stars. It would need another source, such as option (b) in §9.
5. Late Oct 2026 files are about 100 MB/day, so a daily pull is cheap. Jan 2025 days are about 0.8–1 GB/day, so a 9-month backfill is roughly 200 GB of downloads.

### Full-day dry run: 2025-03-12

`python -m ingest.gharchive --dry-run`, all 24 hours on a laptop:

| Metric | Value |
|---|---|
| Wall time | 272 s, so the 273-day backfill is **~21 h** sequential |
| Events seen / counted | 6,005,191 / 4,374,348 (0 bad lines) |
| Repos with any counted event | 895,712 |
| Rows kept (≥1 star or fork) | 131,108 → **~36M rows** for the dataset |
| Rows with ≥2 / ≥5 / ≥10 / ≥50 stars | 24,398 / 6,685 / 3,361 / 307 |
| Share of each kind the kept rows cover | stars 100%, forks 100%, **pushes 3.5%**, PRs 13.7%, issues 21.2% |

Peak memory was not captured; the measurement call returned 0. A rough estimate for 0.9M repo entries is a few hundred MB.

**Problem found: the per-day threshold distorts pushes, PRs and issues.** A tracked repo's pushes only count on days it also gets a star or fork, so 96.5% of pushes are dropped and the push term in `activity` is biased toward star days. See §9 for the fix.

## 9. Open questions

- ~~Data source after the GH Archive drop~~ Resolved 2026-11-04: a fixed 2025 dataset with share-based momentum (§1, §4, §8).
- **Phase 2 (optional): live mode.** Take daily `stargazerCount` snapshots of the tracked repos through GraphQL, about 500 queries a day for 25k repos, and use the day-to-day difference as stars per day. History starts the day the job does, so the 90-day comparison would only work after about 6 months.
- Hours marked `missing` can appear later. Should ingest re-check `missing` hours for a few days instead of treating them as final? (Mostly a Phase 2 concern; the 2025 backfill can simply be re-run.)
- **Per-day threshold drops most pushes, PRs and issues (§8a dry run). Blocks M2.** Proposed fix: ingest writes a compact daily aggregate file for *all* repos, `data/daily/YYYY-MM-DD.csv.gz` (repo_id, stars, forks, pushes, prs, issues; names only for star/fork repos). That's about 0.9M lines and a few MB per day, ~1.5 GB in total. These are aggregates, not raw events. A separate `load` step then picks the tracked set from the whole period and loads only those repos' full daily rows into Postgres. Bonus: changing `N`, the threshold or the weights never needs the 21-hour download again.
- Tracked-set size `N`: settle after the backfill, using the daily files.
- Backfill time: roughly 200 GB and ~21 h sequential. Two or three days processed in parallel should cut that substantially if bandwidth allows. Run it in monthly chunks; finished days are skipped on re-run.
- Hosted Postgres size limits on Fly.io or Render free tiers will decide the final `N`.

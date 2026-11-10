# GitHub Ecosystem Analyzer

Which languages and topics on GitHub are gaining or losing momentum?

This project reads all public GitHub activity from [GH Archive](https://www.gharchive.org/) (stars, forks, pushes, PRs and issues opened), aggregates it per repo per day, rolls it up by language and topic, and serves a dashboard that ranks technologies by momentum: how much their share of GitHub activity grew over 30, 60 or 90 days compared with the period before.

The dataset is a fixed snapshot, **January–September 2025**. A coverage scan of GH Archive found that from late 2025 its star and fork counts no longer reflect real GitHub activity, and from mid-2026 the data is erratic. The details are in [SPEC §8a](docs/SPEC.md#8a-m1-measurements-2026-11-04).

> Status: M1 done (2025 data loaded); M2 next. See [docs/SPEC.md](docs/SPEC.md) for scope, data model, API and milestones.

## Architecture

```mermaid
flowchart LR
    subgraph Sources
        GHA[(GH Archive<br/>hourly .json.gz)]
        GQL[GitHub GraphQL API]
    end

    subgraph Pipeline["Pipeline (one-time backfill)"]
        ING[ingest<br/>stream + filter + aggregate]
        FILES[(daily aggregate files<br/>all repos, ~1 per day)]
        LOAD[load<br/>pick top-N tracked repos]
        ENR[enrich<br/>language + topics<br/>for tracked repos]
        ROL[rollup<br/>SQL]
    end

    subgraph DB["PostgreSQL 16"]
        RDA[(repo_daily_activity)]
        REP[(repos)]
        AGG[(language_daily<br/>topic_daily)]
    end

    API[FastAPI<br/>/api/v1]
    UI[React + Recharts<br/>dashboard]

    GHA --> ING --> FILES --> LOAD --> RDA
    GQL --> ENR --> REP
    RDA --> ROL
    REP --> ROL
    ROL --> AGG
    AGG --> API
    RDA --> API
    REP --> API
    API --> UI
```

## How momentum is computed

```
activity  = stars + 2·forks + 0.5·pushes          (per repo per day)
share     = activity of a language / activity of all tracked repos, over a window
momentum  = share in the last W days / share in the W days before − 1
```

`W` is 30, 60 or 90 days. Using shares rather than raw totals cancels out changes in how much GH Archive collected (it dropped about 38% across the board in June 2025). The weights are a v1 starting point and get validated against real data in milestone M3 (see [SPEC §4](docs/SPEC.md#4-momentum-score)).

## Stack

| Layer | Tech |
|---|---|
| Ingestion | Python 3.12 scripts (streaming, resumable backfill) |
| Database | PostgreSQL 16 (Docker locally) |
| API | FastAPI, psycopg 3, raw SQL |
| Frontend | React, Vite, TypeScript, Recharts |
| Quality | pytest, ruff |
| Deploy | Docker → Fly.io or Render |

## Local development

_Setup instructions land with M1._ Prerequisites: Python 3.12, Docker Desktop, Node 20+.

The enrichment step needs a GitHub token in the `GITHUB_TOKEN` environment variable. A fine-grained token with public read-only access is enough. Put it in `.env`, which is git-ignored, and never commit it.

## Roadmap

- [x] **M1** Ingest pipeline: GH Archive → `repo_daily_activity`
- [ ] **M2** Enrichment + language/topic rollups + momentum query
- [ ] **M3** JSON API + validating the momentum weights
- [ ] **M4** Dashboard
- [ ] **M5** Deploy + CI
- [ ] Phase 2 (optional): live mode from daily star snapshots

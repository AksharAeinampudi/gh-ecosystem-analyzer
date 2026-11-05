-- M1: core tables for ingestion. Rollup tables arrive with M2.

CREATE TABLE repos (
    repo_id        BIGINT PRIMARY KEY,
    full_name      TEXT NOT NULL,
    name_seen_day  DATE NOT NULL,            -- day full_name was observed; guards out-of-order backfills
    language       TEXT,
    topics         TEXT[] NOT NULL DEFAULT '{}',
    stars_total    INTEGER,
    enriched_at    TIMESTAMPTZ,
    enrich_status  TEXT CHECK (enrich_status IN ('ok', 'not_found', 'error'))
);

CREATE TABLE repo_daily_activity (
    repo_id        BIGINT  NOT NULL REFERENCES repos (repo_id) ON DELETE CASCADE,
    day            DATE    NOT NULL,
    stars          INTEGER NOT NULL DEFAULT 0,
    forks          INTEGER NOT NULL DEFAULT 0,
    pushes         INTEGER NOT NULL DEFAULT 0,
    pr_events      INTEGER NOT NULL DEFAULT 0,
    issue_events   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, day)
);

CREATE INDEX repo_daily_activity_day_idx ON repo_daily_activity (day);

CREATE TABLE ingest_hours (
    hour_ts       TIMESTAMPTZ PRIMARY KEY,
    status        TEXT NOT NULL CHECK (status IN ('done', 'failed', 'missing')),
    events_seen   INTEGER,
    events_kept   INTEGER,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

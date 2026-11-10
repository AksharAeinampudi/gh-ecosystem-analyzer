-- M2: rollups rebuilt by ingest.rollup (SPEC §3.1).
-- pushes_capped = sum over repos of LEAST(pushes, PUSH_CAP) per day (SPEC §4).

CREATE TABLE language_daily (
    language       TEXT    NOT NULL,
    day            DATE    NOT NULL,
    stars          BIGINT  NOT NULL,
    forks          BIGINT  NOT NULL,
    pushes         BIGINT  NOT NULL,
    pushes_capped  BIGINT  NOT NULL,
    pr_events      BIGINT  NOT NULL,
    issue_events   BIGINT  NOT NULL,
    active_repos   INTEGER NOT NULL,
    PRIMARY KEY (language, day)
);

CREATE TABLE topic_daily (
    topic          TEXT    NOT NULL,
    day            DATE    NOT NULL,
    stars          BIGINT  NOT NULL,
    forks          BIGINT  NOT NULL,
    pushes         BIGINT  NOT NULL,
    pushes_capped  BIGINT  NOT NULL,
    pr_events      BIGINT  NOT NULL,
    issue_events   BIGINT  NOT NULL,
    active_repos   INTEGER NOT NULL,
    PRIMARY KEY (topic, day)
);

-- Every tracked repo counted once, with or without a language: the denominator for shares.
CREATE TABLE total_daily (
    day            DATE    PRIMARY KEY,
    stars          BIGINT  NOT NULL,
    forks          BIGINT  NOT NULL,
    pushes         BIGINT  NOT NULL,
    pushes_capped  BIGINT  NOT NULL,
    pr_events      BIGINT  NOT NULL,
    issue_events   BIGINT  NOT NULL,
    active_repos   INTEGER NOT NULL
);

CREATE INDEX language_daily_day_idx ON language_daily (day);
CREATE INDEX topic_daily_day_idx ON topic_daily (day);
CREATE INDEX repos_language_idx ON repos (language);
CREATE INDEX repos_topics_idx ON repos USING GIN (topics);

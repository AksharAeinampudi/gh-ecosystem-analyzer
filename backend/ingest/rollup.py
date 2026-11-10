"""Rebuild the language, topic and total rollups from repo_daily_activity (SPEC §3.1).

Usage (from backend/):  python -m ingest.rollup

Run it after every load or enrichment run. All three tables are rebuilt in one transaction, so
readers never see a half-built state, and running it twice gives the same result.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass

import psycopg

from app.db import connect
from app.settings import Settings

log = logging.getLogger(__name__)

# The same aggregate list for all three tables. %(push_cap)s is the only parameter.
_SUMS = """
    sum(a.stars), sum(a.forks), sum(a.pushes), sum(LEAST(a.pushes, %(push_cap)s)),
    sum(a.pr_events), sum(a.issue_events), count(*)
"""
_COLUMNS = "stars, forks, pushes, pushes_capped, pr_events, issue_events, active_repos"

_LANGUAGE = f"""
INSERT INTO language_daily (language, day, {_COLUMNS})
SELECT r.language, a.day, {_SUMS}
FROM repo_daily_activity a JOIN repos r USING (repo_id)
WHERE r.language IS NOT NULL
GROUP BY r.language, a.day
"""

# A repo counts toward every one of its topics, so topic rows overlap (SPEC §3.1).
_TOPIC = f"""
INSERT INTO topic_daily (topic, day, {_COLUMNS})
SELECT t.topic, a.day, {_SUMS}
FROM repo_daily_activity a
JOIN repos r USING (repo_id)
CROSS JOIN LATERAL (SELECT DISTINCT unnest(r.topics) AS topic) t
GROUP BY t.topic, a.day
"""

_TOTAL = f"""
INSERT INTO total_daily (day, {_COLUMNS})
SELECT a.day, {_SUMS}
FROM repo_daily_activity a
GROUP BY a.day
"""


@dataclass(frozen=True)
class RollupSummary:
    language_rows: int
    topic_rows: int
    total_rows: int


def rebuild(conn: psycopg.Connection, push_cap: int) -> RollupSummary:
    """Truncate and refill all rollup tables in one transaction."""
    params = {"push_cap": push_cap}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("TRUNCATE language_daily, topic_daily, total_daily")
        cur.execute(_LANGUAGE, params)
        language_rows = cur.rowcount
        cur.execute(_TOPIC, params)
        topic_rows = cur.rowcount
        cur.execute(_TOTAL, params)
        total_rows = cur.rowcount
    return RollupSummary(language_rows, topic_rows, total_rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    t0 = time.monotonic()
    with connect(settings.database_url) as conn:
        s = rebuild(conn, settings.push_cap)
    log.info(
        "rollups rebuilt: %d language rows, %d topic rows, %d days (%.0fs)",
        s.language_rows,
        s.topic_rows,
        s.total_rows,
        time.monotonic() - t0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

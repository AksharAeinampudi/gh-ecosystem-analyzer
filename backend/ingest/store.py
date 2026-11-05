"""Database writes for ingestion. One UTC day is the unit of work (written atomically)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import psycopg

from ingest.events import RepoDayRow


@dataclass(frozen=True)
class HourStatus:
    hour_ts: datetime
    status: str  # 'done' | 'failed' | 'missing'
    events_seen: int | None = None
    events_kept: int | None = None


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + timedelta(days=1)


_COMPLETE_HOURS = """
SELECT count(*) FROM ingest_hours
WHERE hour_ts >= %s AND hour_ts < %s AND status IN ('done', 'missing')
"""


def day_is_complete(conn: psycopg.Connection, day: date) -> bool:
    """True when all 24 hours of the day were processed (done) or confirmed missing."""
    row = conn.execute(_COMPLETE_HOURS, day_bounds(day)).fetchone()
    return row is not None and row[0] == 24


_UPSERT_REPOS = """
INSERT INTO repos (repo_id, full_name, name_seen_day)
SELECT repo_id, full_name, %(day)s FROM tmp_names
ON CONFLICT (repo_id) DO UPDATE
    SET full_name = EXCLUDED.full_name, name_seen_day = EXCLUDED.name_seen_day
    WHERE repos.name_seen_day <= EXCLUDED.name_seen_day
"""

_INSERT_ACTIVITY = """
INSERT INTO repo_daily_activity
    (repo_id, day, stars, forks, pushes, prs_opened, issues_opened)
SELECT repo_id, %(day)s, stars, forks, pushes, prs_opened, issues_opened FROM tmp_activity
"""

_UPSERT_HOUR = """
INSERT INTO ingest_hours (hour_ts, status, events_seen, events_kept, processed_at)
VALUES (%s, %s, %s, %s, now())
ON CONFLICT (hour_ts) DO UPDATE
    SET status = EXCLUDED.status, events_seen = EXCLUDED.events_seen,
        events_kept = EXCLUDED.events_kept, processed_at = EXCLUDED.processed_at
"""


def write_day(
    conn: psycopg.Connection,
    day: date,
    rows: Iterable[RepoDayRow],
    names: Mapping[int, str],
    hours: Iterable[HourStatus],
) -> int:
    """Replace all activity for `day` and record hour statuses, in one transaction.

    Replacing (not adding to) the day is what makes re-runs idempotent.
    Returns the number of activity rows written.
    """
    rows = list(rows)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE tmp_names (repo_id BIGINT, full_name TEXT) ON COMMIT DROP")
        cur.execute(
            """CREATE TEMP TABLE tmp_activity (
                   repo_id BIGINT, stars INT, forks INT, pushes INT,
                   prs_opened INT, issues_opened INT
               ) ON COMMIT DROP"""
        )
        with cur.copy("COPY tmp_names (repo_id, full_name) FROM STDIN") as copy:
            for r in rows:
                copy.write_row((r.repo_id, names[r.repo_id]))
        with cur.copy(
            "COPY tmp_activity (repo_id, stars, forks, pushes, prs_opened, issues_opened)"
            " FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row(r)
        cur.execute(_UPSERT_REPOS, {"day": day})
        cur.execute("DELETE FROM repo_daily_activity WHERE day = %s", (day,))
        cur.execute(_INSERT_ACTIVITY, {"day": day})
        cur.executemany(
            _UPSERT_HOUR,
            [(h.hour_ts, h.status, h.events_seen, h.events_kept) for h in hours],
        )
    return len(rows)

"""Load daily aggregate files into Postgres: pick the tracked set, then load its full history.

Usage (from backend/):
    python -m ingest.load [--start 2025-01-01] [--end 2025-09-30] [--top 25000] [--allow-gaps]

Pass 1 (files only): score every repo by stars and forks over the range and keep the top
    N * candidate_factor as candidates. Only repos with a star or fork can be tracked (SPEC §3.2);
    pushes are left out of this cheap first ranking, and the factor leaves room for them to
    reorder the final ranking.
Pass 2 (Postgres): COPY the candidates' full daily rows (push-only days included) into a temp
    table, rank them by weighted activity in SQL, and keep the top N.

A load rebuilds repo_daily_activity and ingest_hours from scratch, in one transaction. The repos
table is kept as a cache, so enrichment results (language, topics) survive reloads.
"""

from __future__ import annotations

import argparse
import heapq
import logging
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import psycopg

from app.db import connect
from app.settings import DATASET_END, DATASET_START, Settings, Weights
from ingest.dailyfile import DayManifest, iter_day_rows, read_manifest
from ingest.gharchive import daterange

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadSummary:
    days: int
    candidates: int
    tracked: int
    activity_rows: int


class GapsError(RuntimeError):
    """Some days in the range have no complete daily file."""


def complete_manifests(
    data_dir: Path, start: date, end: date
) -> tuple[list[DayManifest], list[date]]:
    """Split the range into complete days (with manifests) and days that are not ready."""
    ready, not_ready = [], []
    for day in daterange(start, end):
        m = read_manifest(data_dir, day)
        if m is not None and m.complete:
            ready.append(m)
        else:
            not_ready.append(day)
    return ready, not_ready


def score_stars_forks(data_dir: Path, days: Iterable[date], weights: Weights) -> dict[int, float]:
    """Pass 1: weighted stars + forks per repo, for repos with at least one of either."""
    scores: dict[int, float] = defaultdict(float)
    for day in days:
        for row in iter_day_rows(data_dir, day):
            if row.stars or row.forks:
                scores[row.repo_id] += row.stars * weights.stars + row.forks * weights.forks
    return scores


def pick_top(scores: dict[int, float], k: int) -> set[int]:
    """The k highest-scoring repo ids (ties broken by lower repo_id, for determinism)."""
    return set(heapq.nlargest(k, scores, key=lambda r: (scores[r], -r)))


_CREATE_STAGE = """
CREATE TEMP TABLE stage_activity (
    repo_id BIGINT, day DATE, stars INT, forks INT, pushes INT, pr_events INT, issue_events INT
) ON COMMIT DROP;
CREATE TEMP TABLE stage_names (repo_id BIGINT, day DATE, full_name TEXT) ON COMMIT DROP;
CREATE TEMP TABLE tracked (repo_id BIGINT PRIMARY KEY, score DOUBLE PRECISION) ON COMMIT DROP;
"""

_PICK_TRACKED = """
INSERT INTO tracked (repo_id, score)
SELECT repo_id,
       sum(stars * %(w_stars)s + forks * %(w_forks)s + pushes * %(w_pushes)s) AS score
FROM stage_activity
GROUP BY repo_id
ORDER BY score DESC, repo_id
LIMIT %(n)s
"""

# Latest name per tracked repo; never overwrite a name seen on a later day by an earlier one.
_UPSERT_REPOS = """
INSERT INTO repos (repo_id, full_name, name_seen_day)
SELECT DISTINCT ON (n.repo_id) n.repo_id, n.full_name, n.day
FROM stage_names n JOIN tracked t USING (repo_id)
ORDER BY n.repo_id, n.day DESC
ON CONFLICT (repo_id) DO UPDATE
    SET full_name = EXCLUDED.full_name, name_seen_day = EXCLUDED.name_seen_day
    WHERE repos.name_seen_day <= EXCLUDED.name_seen_day
"""

_INSERT_ACTIVITY = """
INSERT INTO repo_daily_activity
    (repo_id, day, stars, forks, pushes, pr_events, issue_events)
SELECT s.repo_id, s.day, s.stars, s.forks, s.pushes, s.pr_events, s.issue_events
FROM stage_activity s JOIN tracked t USING (repo_id)
"""

_INSERT_HOUR = """
INSERT INTO ingest_hours (hour_ts, status, events_seen, events_kept)
VALUES (%s, %s, %s, %s)
"""


def load_into_db(
    conn: psycopg.Connection,
    data_dir: Path,
    manifests: list[DayManifest],
    candidates: set[int],
    settings: Settings,
) -> LoadSummary:
    """Pass 2: stage candidate rows, pick the top N, and rebuild the activity tables."""
    w = settings.weights
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_CREATE_STAGE)
        names: list[tuple[int, date, str]] = []
        with cur.copy(
            "COPY stage_activity (repo_id, day, stars, forks, pushes, pr_events, issue_events)"
            " FROM STDIN"
        ) as copy:
            for m in manifests:
                for r in iter_day_rows(data_dir, m.day):
                    if r.repo_id not in candidates:
                        continue
                    copy.write_row(
                        (
                            r.repo_id,
                            m.day,
                            r.stars,
                            r.forks,
                            r.pushes,
                            r.pr_events,
                            r.issue_events,
                        )
                    )
                    if r.full_name:
                        names.append((r.repo_id, m.day, r.full_name))
        with cur.copy("COPY stage_names (repo_id, day, full_name) FROM STDIN") as copy:
            for row in names:
                copy.write_row(row)
        cur.execute(
            _PICK_TRACKED,
            {
                "w_stars": w.stars,
                "w_forks": w.forks,
                "w_pushes": w.pushes,
                "n": settings.tracked_repos,
            },
        )
        tracked = cur.rowcount
        cur.execute(_UPSERT_REPOS)
        cur.execute("TRUNCATE repo_daily_activity, ingest_hours")
        cur.execute(_INSERT_ACTIVITY)
        activity_rows = cur.rowcount
        cur.executemany(
            _INSERT_HOUR,
            [
                (h.hour_ts, h.status, h.events_seen, h.events_kept)
                for m in manifests
                for h in m.hours
            ],
        )
    return LoadSummary(len(manifests), len(candidates), tracked, activity_rows)


def run(
    start: date,
    end: date,
    settings: Settings,
    *,
    allow_gaps: bool = False,
    conn: psycopg.Connection | None = None,
) -> LoadSummary:
    manifests, not_ready = complete_manifests(settings.data_dir, start, end)
    if not_ready and not allow_gaps:
        shown = ", ".join(d.isoformat() for d in not_ready[:10])
        more = f" (+{len(not_ready) - 10} more)" if len(not_ready) > 10 else ""
        raise GapsError(
            f"{len(not_ready)} day(s) not ingested or incomplete: {shown}{more}. "
            "Run ingest.gharchive for them, or pass --allow-gaps."
        )
    if not manifests:
        raise GapsError("no complete days in range")
    t0 = time.monotonic()
    days = [m.day for m in manifests]
    scores = score_stars_forks(settings.data_dir, days, settings.weights)
    candidates = pick_top(scores, settings.tracked_repos * settings.candidate_factor)
    log.info(
        "pass 1: %d repos with stars/forks over %d days, %d candidates (%.0fs)",
        len(scores),
        len(days),
        len(candidates),
        time.monotonic() - t0,
    )
    del scores
    own_conn = conn is None
    conn = conn or connect(settings.database_url)
    try:
        summary = load_into_db(conn, settings.data_dir, manifests, candidates, settings)
    finally:
        if own_conn:
            conn.close()
    log.info(
        "pass 2: %d tracked repos, %d activity rows loaded (%.0fs total)",
        summary.tracked,
        summary.activity_rows,
        time.monotonic() - t0,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, default=DATASET_START)
    p.add_argument("--end", type=date.fromisoformat, default=DATASET_END)
    p.add_argument("--top", type=int, help="tracked repos (default: TRACKED_REPOS or 25000)")
    p.add_argument("--allow-gaps", action="store_true", help="load even if some days are missing")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if args.top:
        settings = replace(settings, tracked_repos=args.top)
    try:
        run(args.start, args.end, settings, allow_gaps=args.allow_gaps)
    except GapsError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

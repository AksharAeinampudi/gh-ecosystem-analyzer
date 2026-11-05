"""Build daily aggregate files from the GH Archive BigQuery dataset (alternative to gharchive.py).

Usage (from backend/, after `gcloud auth application-default login`):
    python -m ingest.bigquery --dry-run          # free: prints bytes each query would scan
    python -m ingest.bigquery [--start 2025-01-01] [--end 2025-09-30] [--force]

Instead of downloading ~200 GB of hourly files, BigQuery does the counting (SPEC §8a):
  1. One query aggregates per-repo daily counts and keeps only the top BQ_CANDIDATES repos by
     weighted stars + forks over the whole range, saved to a table in your project
     (~66 GB scanned; free tier is 1 TB/month). Re-runs reuse that table unless --force.
  2. A small query counts events per hour, which becomes each day's manifest (0 events = missing).
  3. The candidate table is read back month by month and written as daily files, so
     ingest.load and everything after it work unchanged.

Every query is dry-run first and refused if it would scan more than BQ_MAX_GB; the same cap is
also set as maximum_bytes_billed, so BigQuery itself refuses anything larger.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.settings import DATASET_END, DATASET_START, Settings
from ingest import dailyfile
from ingest.dailyfile import HourStatus, day_bounds
from ingest.events import ActivityCounter

log = logging.getLogger(__name__)

EVENT_TYPES = ("WatchEvent", "ForkEvent", "PushEvent", "PullRequestEvent", "IssuesEvent")
_TYPES_SQL = ", ".join(f"'{t}'" for t in EVENT_TYPES)  # constants, not user input

# WatchEvent's only action is "started", so no payload read is needed for stars. PR and issue
# events are counted for every action: reading `payload` would scan ~3.6 TB instead of ~66 GB.
CANDIDATE_DAILY_SQL = """
WITH daily AS (
  SELECT
    DATE(created_at) AS day,
    repo.id AS repo_id,
    COUNTIF(type = 'WatchEvent') AS stars,
    COUNTIF(type = 'ForkEvent') AS forks,
    COUNTIF(type = 'PushEvent') AS pushes,
    COUNTIF(type = 'PullRequestEvent') AS pr_events,
    COUNTIF(type = 'IssuesEvent') AS issue_events,
    ARRAY_AGG(IF(type IN ('WatchEvent', 'ForkEvent'), repo.name, NULL) IGNORE NULLS
              ORDER BY created_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS full_name
  FROM `githubarchive.day.{year}*`
  WHERE _TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix
    AND DATE(created_at) BETWEEN @start AND @end
    AND type IN ({types})
  GROUP BY day, repo_id
),
candidates AS (
  SELECT repo_id
  FROM daily
  GROUP BY repo_id
  HAVING SUM(stars) + SUM(forks) > 0
  ORDER BY SUM(stars) * @w_stars + SUM(forks) * @w_forks DESC, repo_id
  LIMIT @candidates
)
SELECT d.* FROM daily AS d JOIN candidates USING (repo_id)
"""

HOURLY_SQL = """
SELECT
  TIMESTAMP_TRUNC(created_at, HOUR) AS hour,
  COUNT(*) AS events,
  COUNTIF(type IN ({types})) AS kept
FROM `githubarchive.day.{year}*`
WHERE _TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix
  AND DATE(created_at) BETWEEN @start AND @end
GROUP BY hour
"""

READ_MONTH_SQL = """
SELECT day, repo_id, stars, forks, pushes, pr_events, issue_events, full_name
FROM `{table}`
WHERE day BETWEEN @start AND @end
"""


def render(sql: str, year: int, **extra: str) -> str:
    return sql.format(year=year, types=_TYPES_SQL, **extra)


def hours_for_day(day: date, hourly: Mapping[datetime, tuple[int, int]]) -> list[HourStatus]:
    """24 hour statuses for `day`: an hour with no events in BigQuery counts as missing."""
    start, _ = day_bounds(day)
    out = []
    for h in range(24):
        hour = start + timedelta(hours=h)
        events, kept = hourly.get(hour, (0, 0))
        out.append(
            HourStatus(hour, "done", events, kept) if events else HourStatus(hour, "missing")
        )
    return out


def group_rows_by_day(rows: Iterable[Mapping[str, Any]]) -> dict[date, ActivityCounter]:
    """Turn candidate_daily rows into one ActivityCounter per day."""
    by_day: dict[date, ActivityCounter] = {}
    for r in rows:
        c = by_day.setdefault(r["day"], ActivityCounter())
        c.counts[r["repo_id"]] = [
            r["stars"],
            r["forks"],
            r["pushes"],
            r["pr_events"],
            r["issue_events"],
        ]
        if r["full_name"]:
            c.names[r["repo_id"]] = r["full_name"]
    return by_day


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    out, d = [], start
    while d <= end:
        nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
        out.append((d, min(end, nxt - timedelta(days=1))))
        d = nxt
    return out


class BigQueryIngest:
    """Thin wrapper around the BigQuery client; kept separate so the logic above is testable."""

    def __init__(self, settings: Settings, start: date, end: date) -> None:
        from google.cloud import bigquery  # optional dependency: pip install -e ".[bigquery]"

        if start.year != end.year:
            raise ValueError("start and end must be in the same year (one wildcard table set)")
        if not settings.gcp_project:
            raise ValueError("set GOOGLE_CLOUD_PROJECT in .env")
        self.bq = bigquery
        self.settings = settings
        self.start, self.end = start, end
        self.client = bigquery.Client(project=settings.gcp_project)
        self.cap = int(settings.bq_max_gb * 1e9)
        self.table = (
            f"{settings.gcp_project}.{settings.bq_dataset}"
            f".candidate_daily_{start:%Y%m%d}_{end:%Y%m%d}"
        )
        self.params = [
            bigquery.ScalarQueryParameter("start_suffix", "STRING", f"{start:%m%d}"),
            bigquery.ScalarQueryParameter("end_suffix", "STRING", f"{end:%m%d}"),
            bigquery.ScalarQueryParameter("start", "DATE", start),
            bigquery.ScalarQueryParameter("end", "DATE", end),
        ]

    def _config(self, params: list | None = None, **kw: Any):
        return self.bq.QueryJobConfig(
            query_parameters=params if params is not None else self.params,
            maximum_bytes_billed=self.cap,
            **kw,
        )

    def candidate_sql_and_params(self) -> tuple[str, list]:
        w = self.settings.weights
        extra = [
            self.bq.ScalarQueryParameter("w_stars", "FLOAT64", w.stars),
            self.bq.ScalarQueryParameter("w_forks", "FLOAT64", w.forks),
            self.bq.ScalarQueryParameter("candidates", "INT64", self.settings.bq_candidates),
        ]
        return render(CANDIDATE_DAILY_SQL, self.start.year), self.params + extra

    def dry_run(self, sql: str, params: list) -> int:
        job = self.client.query(
            sql, job_config=self._config(params, dry_run=True, use_query_cache=False)
        )
        return int(job.total_bytes_processed)

    def check_costs(self) -> dict[str, int]:
        cand_sql, cand_params = self.candidate_sql_and_params()
        costs = {
            "candidate_daily": self.dry_run(cand_sql, cand_params),
            "hourly": self.dry_run(render(HOURLY_SQL, self.start.year), self.params),
        }
        for name, b in costs.items():
            log.info("dry run %-15s would scan %.2f GB", name, b / 1e9)
            if b > self.cap:
                raise RuntimeError(f"{name} would scan {b / 1e9:.1f} GB > BQ_MAX_GB")
        return costs

    def table_exists(self) -> bool:
        from google.api_core.exceptions import NotFound

        try:
            self.client.get_table(self.table)
        except NotFound:
            return False
        return True

    def build_candidate_table(self) -> None:
        self.client.create_dataset(
            f"{self.settings.gcp_project}.{self.settings.bq_dataset}", exists_ok=True
        )
        sql, params = self.candidate_sql_and_params()
        job = self.client.query(
            sql,
            job_config=self._config(
                params,
                destination=self.table,
                write_disposition="WRITE_TRUNCATE",
                clustering_fields=["day"],
            ),
        )
        job.result()
        log.info(
            "built %s: %s rows, %.2f GB billed",
            self.table,
            job.result().total_rows,
            (job.total_bytes_billed or 0) / 1e9,
        )

    def hourly_counts(self) -> dict[datetime, tuple[int, int]]:
        """Per-hour event counts, cached as JSON next to the daily files (re-runs are free)."""
        cache = self.settings.data_dir / f"bq_hours_{self.start:%Y%m%d}_{self.end:%Y%m%d}.json"
        if cache.is_file():
            doc = json.loads(cache.read_text(encoding="utf-8"))
        else:
            rows = self.client.query(
                render(HOURLY_SQL, self.start.year), job_config=self._config()
            ).result()
            doc = {r["hour"].isoformat(): [r["events"], r["kept"]] for r in rows}
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(doc), encoding="utf-8")
        return {datetime.fromisoformat(k).astimezone(UTC): (v[0], v[1]) for k, v in doc.items()}

    def read_month(self, start: date, end: date) -> Iterator[dict[str, Any]]:
        params = [
            self.bq.ScalarQueryParameter("start", "DATE", start),
            self.bq.ScalarQueryParameter("end", "DATE", end),
        ]
        job = self.client.query(
            READ_MONTH_SQL.format(table=self.table), job_config=self._config(params)
        )
        return (dict(r.items()) for r in job.result(page_size=100_000))


def run(start: date, end: date, settings: Settings, *, dry_run: bool = False, force: bool = False):
    ingest = BigQueryIngest(settings, start, end)
    ingest.check_costs()
    if dry_run:
        return
    if force or not ingest.table_exists():
        ingest.build_candidate_table()
    else:
        log.info("reusing %s (use --force to rebuild)", ingest.table)
    hourly = ingest.hourly_counts()
    for m_start, m_end in month_ranges(start, end):
        days = [
            d
            for d in (m_start + timedelta(n) for n in range((m_end - m_start).days + 1))
            if force or not dailyfile.day_is_complete(settings.data_dir, d)
        ]
        if not days:
            log.info("%s..%s already written, skipping", m_start, m_end)
            continue
        by_day = group_rows_by_day(ingest.read_month(m_start, m_end))
        for d in days:
            hours = hours_for_day(d, hourly)
            counter = by_day.get(d, ActivityCounter())
            m = dailyfile.write_day(settings.data_dir, d, counter, hours, 0, source="bigquery")
            missing = sum(h.status == "missing" for h in hours)
            log.info("%s: %d repos%s", d, m.repos, f", {missing} hours missing" if missing else "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, default=DATASET_START)
    p.add_argument("--end", type=date.fromisoformat, default=DATASET_END)
    p.add_argument("--dry-run", action="store_true", help="only print bytes each query would scan")
    p.add_argument("--force", action="store_true", help="rebuild the table and rewrite all days")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    run(args.start, args.end, Settings.from_env(), dry_run=args.dry_run, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())

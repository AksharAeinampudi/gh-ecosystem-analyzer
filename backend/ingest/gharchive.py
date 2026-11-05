"""Ingest GH Archive hourly files into repo_daily_activity.

Usage (from backend/):
    python -m ingest.gharchive --start 2026-10-01 [--end 2026-10-03] [--force] [--dry-run]

Each UTC day is downloaded hour by hour (streamed, never stored), aggregated in memory, then
written atomically. Days whose 24 hours are all 'done' or 'missing' are skipped unless --force.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import sys
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import BinaryIO

from app.db import connect
from app.settings import Settings
from ingest import store
from ingest.events import ActivityCounter, HourResult, count_lines
from ingest.store import HourStatus, day_bounds

log = logging.getLogger(__name__)

# GH Archive publishes each hour with some delay. A 404 for an hour this recent is treated as
# "not yet published" (failed, retried next run) rather than permanently missing.
PUBLISH_GRACE = timedelta(hours=6)
USER_AGENT = "gh-ecosystem-analyzer/0.1"


class HourNotFound(Exception):
    """GH Archive has no file for this hour (HTTP 404)."""


class HourFailed(Exception):
    """The hour could not be fetched or decoded after all retries."""


def hour_url(base_url: str, hour: datetime) -> str:
    # GH Archive hour is not zero-padded: 2026-10-01-0.json.gz ... 2026-10-01-23.json.gz
    return f"{base_url}/{hour:%Y-%m-%d}-{hour.hour}.json.gz"


def open_url(url: str, timeout: float) -> BinaryIO:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise HourNotFound(url) from exc
        raise


def iter_gzip_lines(raw: BinaryIO) -> Iterator[bytes]:
    """Stream-decompress a (possibly multi-member) gzip body line by line."""
    with gzip.GzipFile(fileobj=raw) as gz:
        yield from gz


Opener = Callable[[str, float], BinaryIO]


def fetch_hour(
    hour: datetime,
    settings: Settings,
    opener: Opener = open_url,
    sleep: Callable[[float], None] = time.sleep,
) -> HourResult:
    """Download and aggregate one hour. Retries transient errors; a partial read is discarded."""
    url = hour_url(settings.gharchive_base_url, hour)
    last_error: Exception | None = None
    for attempt in range(1, settings.fetch_retries + 1):
        try:
            with opener(url, settings.http_timeout_s) as raw:
                return count_lines(iter_gzip_lines(raw))
        except HourNotFound:
            raise
        except (OSError, EOFError, zlib.error) as exc:  # URLError and timeouts are OSErrors
            last_error = exc
            log.warning("hour %s attempt %d failed: %s", url, attempt, exc)
            if attempt < settings.fetch_retries:
                sleep(2**attempt)
    raise HourFailed(url) from last_error


@dataclass
class DayResult:
    day: date
    counter: ActivityCounter = field(default_factory=ActivityCounter)
    hours: list[HourStatus] = field(default_factory=list)
    bad_lines: int = 0

    @property
    def complete(self) -> bool:
        return all(h.status in ("done", "missing") for h in self.hours)


def ingest_day(
    day: date,
    fetch: Callable[[datetime], HourResult],
    now: datetime,
) -> DayResult:
    """Fetch all 24 hours of `day` and merge them. Never raises for a single bad hour."""
    result = DayResult(day)
    start, _ = day_bounds(day)
    for h in range(24):
        hour = start + timedelta(hours=h)
        try:
            hr = fetch(hour)
        except HourNotFound:
            recent = now - (hour + timedelta(hours=1)) < PUBLISH_GRACE
            status = "failed" if recent else "missing"
            log.warning("hour %s not found -> %s", hour.isoformat(), status)
            result.hours.append(HourStatus(hour, status))
            continue
        except HourFailed as exc:
            log.error("hour %s failed: %s", hour.isoformat(), exc.__cause__)
            result.hours.append(HourStatus(hour, "failed"))
            continue
        result.counter.merge(hr.counter)
        result.bad_lines += hr.bad_lines
        result.hours.append(HourStatus(hour, "done", hr.events_seen, hr.events_kept))
        log.info(
            "hour %s: %d events, %d kept, %d bad",
            hour.strftime("%Y-%m-%d %H:00"),
            hr.events_seen,
            hr.events_kept,
            hr.bad_lines,
        )
    return result


def daterange(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run(
    start: date,
    end: date,
    settings: Settings,
    *,
    force: bool = False,
    dry_run: bool = False,
    fetch: Callable[[datetime], HourResult] | None = None,
    now: datetime | None = None,
) -> list[DayResult]:
    """Ingest every day in [start, end]. With dry_run, nothing touches the database."""
    fetch = fetch or (lambda hour: fetch_hour(hour, settings))
    now = now or datetime.now(UTC)
    conn = None if dry_run else connect(settings.database_url)
    results: list[DayResult] = []
    try:
        for day in daterange(start, end):
            if conn is not None and not force and store.day_is_complete(conn, day):
                log.info("%s already complete, skipping (use --force to redo)", day)
                continue
            t0 = time.monotonic()
            res = ingest_day(day, fetch, now)
            rows = res.counter.rows(settings.min_daily_stars, settings.min_daily_forks)
            if conn is not None:
                store.write_day(conn, day, rows, res.counter.names, res.hours)
            log.info(
                "%s: %d repos seen, %d rows %s, %d/24 hours done, %.0fs%s",
                day,
                len(res.counter.counts),
                len(rows),
                "counted (dry run)" if dry_run else "written",
                sum(h.status == "done" for h in res.hours),
                time.monotonic() - t0,
                "" if res.complete else "  [INCOMPLETE: re-run to retry failed hours]",
            )
            results.append(res)
    finally:
        if conn is not None:
            conn.close()
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, required=True, help="first UTC day")
    p.add_argument("--end", type=date.fromisoformat, help="last UTC day (default: --start)")
    p.add_argument("--force", action="store_true", help="re-ingest days already complete")
    p.add_argument("--dry-run", action="store_true", help="download and count, no DB writes")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    args.end = args.end or args.start
    if args.end < args.start:
        p.error("--end is before --start")
    if args.end >= datetime.now(UTC).date():
        p.error("--end must be before today (UTC); today's data is still being published")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    results = run(args.start, args.end, Settings.from_env(), force=args.force, dry_run=args.dry_run)
    return 0 if all(r.complete for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

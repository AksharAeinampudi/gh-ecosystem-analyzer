"""Ingest GH Archive hourly files into daily aggregate files (see ingest.dailyfile).

Usage (from backend/):
    python -m ingest.gharchive [--start 2025-01-01] [--end 2025-09-30] [--workers 3] [--force]

Each UTC day is downloaded hour by hour (streamed, never stored), aggregated in memory, then
written to DATA_DIR. Days whose 24 hours are all 'done' or 'missing' are skipped unless --force,
so an interrupted backfill resumes where it stopped. Loading into Postgres is ingest.load.
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
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import BinaryIO

from app.settings import DATASET_END, DATASET_START, Settings
from ingest import dailyfile
from ingest.dailyfile import HourStatus, day_bounds
from ingest.events import ActivityCounter, HourResult, count_lines

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


@dataclass(frozen=True)
class DaySummary:
    day: date
    status: str  # 'skipped' (already complete) | 'complete' | 'incomplete'
    repos: int = 0
    seconds: float = 0.0


def process_day(
    day: date,
    settings: Settings,
    *,
    force: bool = False,
    fetch: Callable[[datetime], HourResult] | None = None,
    now: datetime | None = None,
) -> DaySummary:
    """Ingest one day into its daily file, unless it is already complete."""
    if not force and dailyfile.day_is_complete(settings.data_dir, day):
        log.info("%s already complete, skipping (use --force to redo)", day)
        return DaySummary(day, "skipped")
    fetch = fetch or partial(fetch_hour, settings=settings)
    t0 = time.monotonic()
    res = ingest_day(day, fetch, now or datetime.now(UTC))
    manifest = dailyfile.write_day(settings.data_dir, day, res.counter, res.hours, res.bad_lines)
    seconds = time.monotonic() - t0
    log.info(
        "%s: %d repos, %d/24 hours done, %.0fs%s",
        day,
        manifest.repos,
        sum(h.status == "done" for h in res.hours),
        seconds,
        "" if manifest.complete else "  [INCOMPLETE: re-run to retry failed hours]",
    )
    return DaySummary(
        day, "complete" if manifest.complete else "incomplete", manifest.repos, seconds
    )


def _init_worker(level: int) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def run(
    start: date,
    end: date,
    settings: Settings,
    *,
    workers: int = 1,
    force: bool = False,
    fetch: Callable[[datetime], HourResult] | None = None,
    now: datetime | None = None,
) -> list[DaySummary]:
    """Ingest every day in [start, end], `workers` days at a time."""
    days = list(daterange(start, end))
    if workers <= 1:
        return [process_day(d, settings, force=force, fetch=fetch, now=now) for d in days]
    if fetch is not None or now is not None:
        raise ValueError("custom fetch/now are only supported with workers=1")
    level = logging.getLogger().level
    with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(level,)) as pool:
        return list(pool.map(partial(process_day, settings=settings, force=force), days))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, default=DATASET_START)
    p.add_argument("--end", type=date.fromisoformat, default=DATASET_END)
    p.add_argument("--workers", type=int, default=1, help="days processed in parallel")
    p.add_argument("--force", action="store_true", help="re-ingest days already complete")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    if args.end < args.start:
        p.error("--end is before --start")
    if args.end >= datetime.now(UTC).date():
        p.error("--end must be before today (UTC); today's data is still being published")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    summaries = run(
        args.start, args.end, Settings.from_env(), workers=args.workers, force=args.force
    )
    incomplete = [s.day.isoformat() for s in summaries if s.status == "incomplete"]
    log.info(
        "%d days: %d ingested, %d skipped, %d incomplete %s",
        len(summaries),
        sum(s.status == "complete" for s in summaries),
        sum(s.status == "skipped" for s in summaries),
        len(incomplete),
        incomplete or "",
    )
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())

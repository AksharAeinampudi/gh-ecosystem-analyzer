"""Daily aggregate files: the hand-off between ingest (download) and load (Postgres).

For each UTC day ingest writes two files to the data dir:

  YYYY-MM-DD.csv.gz   one row per repo with any counted event that day, sorted by repo_id:
                      repo_id, stars, forks, pushes, pr_events, issue_events, full_name
                      (full_name is empty unless the repo had a star or fork that day)
  YYYY-MM-DD.json     manifest: per-hour status and counts. Written last, so it doubles as
                      the "this day's data file is finished" marker.

These are per-repo daily counts, never raw events. Both files are written atomically
(temp file + rename), so a crash never leaves a half-written day that looks finished.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import NamedTuple

from ingest.events import ActivityCounter

COLUMNS = ("repo_id", "stars", "forks", "pushes", "pr_events", "issue_events", "full_name")


class DayRow(NamedTuple):
    repo_id: int
    stars: int
    forks: int
    pushes: int
    pr_events: int
    issue_events: int
    full_name: str | None


@dataclass(frozen=True)
class HourStatus:
    hour_ts: datetime
    status: str  # 'done' | 'failed' | 'missing'
    events_seen: int | None = None
    events_kept: int | None = None


@dataclass(frozen=True)
class DayManifest:
    day: date
    hours: tuple[HourStatus, ...]
    repos: int
    bad_lines: int

    @property
    def complete(self) -> bool:
        return len(self.hours) == 24 and all(h.status in ("done", "missing") for h in self.hours)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + timedelta(days=1)


def data_path(data_dir: Path, day: date) -> Path:
    return data_dir / f"{day.isoformat()}.csv.gz"


def manifest_path(data_dir: Path, day: date) -> Path:
    return data_dir / f"{day.isoformat()}.json"


def _replace_atomically(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def write_day(
    data_dir: Path,
    day: date,
    counter: ActivityCounter,
    hours: list[HourStatus],
    bad_lines: int,
    source: str = "gharchive",
) -> DayManifest:
    """Write the data file, then the manifest. Overwrites any earlier attempt at the day.

    `source` is recorded in the manifest: 'gharchive' files hold every repo with activity;
    'bigquery' files hold only the candidate repos chosen in BigQuery (ingest.bigquery).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz,
        io.TextIOWrapper(gz, encoding="utf-8", newline="") as text,
    ):
        w = csv.writer(text, lineterminator="\n")
        w.writerow(COLUMNS)
        for repo_id in sorted(counter.counts):
            c = counter.counts[repo_id]
            w.writerow((repo_id, *c, counter.names.get(repo_id, "")))
    _replace_atomically(data_path(data_dir, day), buf.getvalue())

    manifest = DayManifest(day, tuple(hours), len(counter.counts), bad_lines)
    doc = {
        "day": day.isoformat(),
        "source": source,
        "repos": manifest.repos,
        "bad_lines": bad_lines,
        "hours": [{**asdict(h), "hour_ts": h.hour_ts.isoformat()} for h in hours],
    }
    _replace_atomically(manifest_path(data_dir, day), json.dumps(doc, indent=1).encode())
    return manifest


def read_manifest(data_dir: Path, day: date) -> DayManifest | None:
    path = manifest_path(data_dir, day)
    if not path.is_file() or not data_path(data_dir, day).is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    hours = tuple(
        HourStatus(
            datetime.fromisoformat(h["hour_ts"]), h["status"], h["events_seen"], h["events_kept"]
        )
        for h in doc["hours"]
    )
    return DayManifest(date.fromisoformat(doc["day"]), hours, doc["repos"], doc["bad_lines"])


def day_is_complete(data_dir: Path, day: date) -> bool:
    m = read_manifest(data_dir, day)
    return m is not None and m.complete


def iter_day_rows(data_dir: Path, day: date) -> Iterator[DayRow]:
    with gzip.open(data_path(data_dir, day), "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if tuple(header) != COLUMNS:
            raise ValueError(f"unexpected columns in {data_path(data_dir, day)}: {header}")
        for r in reader:
            yield DayRow(
                int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), int(r[5]), r[6] or None
            )

"""Measure GH Archive coverage over time (SPEC §8a / §9). Writes CSVs to data/coverage/.

Usage (from backend/):
    python -m scripts.coverage_scan --start 2025-01-06 --end 2026-11-03

Two passes:
  1. hours.csv: fully downloads and parses one sample hour per day (weekly before --daily-from,
     daily after), recording event counts by kind, bot share of pushes, and payload shape.
  2. heads.csv: HEAD request for every hour from --daily-from on; records 404s and file sizes.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.settings import REPO_ROOT
from ingest.events import Kind, classify

BASE = "https://data.gharchive.org"
UA = {"User-Agent": "gh-ecosystem-analyzer/0.1 (coverage scan)"}
OUT = REPO_ROOT / "data" / "coverage"

HOUR_FIELDS = [
    "hour", "status", "gz_bytes", "events", "stars", "forks", "pushes", "pr_events",
    "issue_events", "repos", "actors", "push_bot_share", "push_has_commits", "push_has_size",
    "pr_actions", "bad_lines",
]  # fmt: skip


def url_for(hour: datetime) -> str:
    return f"{BASE}/{hour:%Y-%m-%d}-{hour.hour}.json.gz"


def scan_hour(hour: datetime) -> dict:
    row: dict = {"hour": hour.isoformat()}
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url_for(hour), headers=UA), timeout=120
        )
    except urllib.error.HTTPError as exc:
        row["status"] = "missing" if exc.code == 404 else f"http_{exc.code}"
        return row
    kinds: Counter = Counter()
    pr_actions: Counter = Counter()
    repos, actors = set(), set()
    events = bad = push_bots = push_commits = push_size = 0
    with resp, gzip.GzipFile(fileobj=resp) as gz:
        row["gz_bytes"] = int(resp.headers.get("Content-Length") or 0)
        for line in gz:
            try:
                e = json.loads(line)
            except ValueError:
                bad += 1
                continue
            events += 1
            repos.add((e.get("repo") or {}).get("id"))
            login = (e.get("actor") or {}).get("login") or ""
            actors.add(login)
            if e.get("type") == "PullRequestEvent":
                pr_actions[(e.get("payload") or {}).get("action")] += 1
            k = classify(e)
            if k is None:
                continue
            kinds[k] += 1
            if k is Kind.PUSH:
                p = e.get("payload") or {}
                push_bots += login.endswith("[bot]")
                push_commits += "commits" in p
                push_size += "size" in p
    pushes = kinds[Kind.PUSH]
    row.update(
        status="ok",
        events=events,
        stars=kinds[Kind.STAR],
        forks=kinds[Kind.FORK],
        pushes=pushes,
        pr_events=kinds[Kind.PR_EVENT],
        issue_events=kinds[Kind.ISSUE_EVENT],
        repos=len(repos),
        actors=len(actors),
        push_bot_share=round(push_bots / pushes, 3) if pushes else "",
        push_has_commits=round(push_commits / pushes, 3) if pushes else "",
        push_has_size=round(push_size / pushes, 3) if pushes else "",
        pr_actions=";".join(f"{a}={n}" for a, n in pr_actions.most_common()),
        bad_lines=bad,
    )
    return row


def head_hour(hour: datetime) -> dict:
    req = urllib.request.Request(url_for(hour), headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"hour": hour.isoformat(), "status": "ok",
                    "gz_bytes": int(r.headers.get("Content-Length") or 0)}  # fmt: skip
    except urllib.error.HTTPError as exc:
        return {"hour": hour.isoformat(), "status": "missing" if exc.code == 404 else exc.code}
    except OSError as exc:
        return {"hour": hour.isoformat(), "status": f"error:{exc.__class__.__name__}"}


def sample_hours(start: date, end: date, daily_from: date, hour_of_day: int) -> list[datetime]:
    out, d = [], start
    while d <= end:
        if d >= daily_from or (d - start).days % 7 == 0:
            out.append(datetime(d.year, d.month, d.day, hour_of_day, tzinfo=UTC))
        d += timedelta(days=1)
    return out


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["hour"]))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 6))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 3))
    p.add_argument("--daily-from", type=date.fromisoformat, default=date(2026, 6, 1))
    p.add_argument("--hour", type=int, default=16, help="UTC hour sampled each day")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()

    heads_hours = []
    h = datetime(args.daily_from.year, args.daily_from.month, args.daily_from.day, tzinfo=UTC)
    while h.date() <= args.end:
        heads_hours.append(h)
        h += timedelta(hours=1)
    with ThreadPoolExecutor(16) as pool:
        heads = list(pool.map(head_hour, heads_hours))
    write_csv(OUT / "heads.csv", heads, ["hour", "status", "gz_bytes"])
    print(
        f"heads: {len(heads)} hours, {sum(r['status'] != 'ok' for r in heads)} not ok", flush=True
    )

    hours = sample_hours(args.start, args.end, args.daily_from, args.hour)
    rows = []
    with ProcessPoolExecutor(args.workers) as pool:
        for i, row in enumerate(pool.map(scan_hour, hours), 1):
            rows.append(row)
            if i % 10 == 0 or i == len(hours):
                print(
                    f"scanned {i}/{len(hours)}  last={row['hour']} {row.get('events')}", flush=True
                )
    write_csv(OUT / "hours.csv", rows, HOUR_FIELDS)


if __name__ == "__main__":
    main()

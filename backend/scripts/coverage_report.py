"""Summarize data/coverage/*.csv from coverage_scan.py as Markdown tables (printed to stdout).

Usage (from backend/):  python -m scripts.coverage_report
"""

from __future__ import annotations

import csv
from collections import defaultdict
from statistics import median

from scripts.coverage_scan import OUT

METRICS = ["events", "stars", "forks", "pushes", "prs_opened", "issues_opened", "repos"]


def load(name: str) -> list[dict]:
    with (OUT / name).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(v: str) -> float | None:
    return float(v) if v not in ("", None) else None


def monthly(rows: list[dict]) -> None:
    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_month[r["hour"][:7]].append(r)
    cols = ["month", "samples", "missing"] + [f"med {m}" for m in METRICS] + ["bot push share"]
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for month, rs in sorted(by_month.items()):
        ok = [r for r in rs if r["status"] == "ok"]
        cells = [month, str(len(rs)), str(len(rs) - len(ok))]
        for m in METRICS + ["push_bot_share"]:
            vals = [x for r in ok if (x := num(r[m])) is not None]
            cells.append(f"{median(vals):,.2f}" if m == "push_bot_share" and vals
                         else f"{median(vals):,.0f}" if vals else "-")  # fmt: skip
        print("| " + " | ".join(cells) + " |")


def daily_tail(rows: list[dict], since: str) -> None:
    print(f"\n### Daily samples since {since}\n")
    print("| hour | status | events | stars | forks | pushes | prs | issues | has_commits |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r["hour"] >= since:
            print(f"| {r['hour'][:13]} | {r['status']} | {r['events']} | {r['stars']} | "
                  f"{r['forks']} | {r['pushes']} | {r['prs_opened']} | {r['issues_opened']} | "
                  f"{r['push_has_commits']} |")  # fmt: skip


def heads_summary(heads: list[dict]) -> None:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in heads:
        by_day[r["hour"][:10]].append(r)
    print("\n### Hourly files per day (HEAD scan)\n")
    print("| day | hours present | total MB |")
    print("|---|---|---|")
    for day, rs in sorted(by_day.items()):
        ok = [r for r in rs if r["status"] == "ok"]
        mb = sum(int(r["gz_bytes"] or 0) for r in ok) / 1e6
        print(f"| {day} | {len(ok)}/24 | {mb:,.0f} |")


def main() -> None:
    rows = load("hours.csv")
    print("### Monthly medians of sampled hours (16:00 UTC)\n")
    monthly(rows)
    daily_tail(rows, "2026-06")
    heads_summary(load("heads.csv"))


if __name__ == "__main__":
    main()

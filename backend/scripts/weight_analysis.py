"""Check the momentum score against real data (SPEC §4, "Things to check"). Prints Markdown.

Usage (from backend/):  python -m scripts.weight_analysis

Everything is computed straight from repo_daily_activity, not from the rollups, so each variant
can use its own push cap and weights. With the default settings it doubles as an independent
check of app.momentum.

  A. Push cap: does the ranking change between caps of 10, 25, 50, 100, none, and no pushes?
  B. Weights: does the ranking change when each weight moves by ±50%?
  C. June 2025: do language shares stay continuous across the GH Archive volume drop?
  D. Concentration: how often is a top gainer mostly one repo?
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from statistics import median

import psycopg

from app.db import connect
from app.momentum import window_bounds
from app.settings import Settings, Weights

NO_CAP = 10**9

_SCORES = """
WITH per_repo AS (
    SELECT r.language AS name, a.repo_id,
           coalesce(sum(a.stars * %(ws)s + a.forks * %(wf)s + LEAST(a.pushes, %(cap)s) * %(wp)s)
                    FILTER (WHERE a.day >= %(cur_start)s), 0) AS cur,
           coalesce(sum(a.stars * %(ws)s + a.forks * %(wf)s + LEAST(a.pushes, %(cap)s) * %(wp)s)
                    FILTER (WHERE a.day <= %(prev_end)s), 0) AS prev,
           bool_or(a.day >= %(cur_start)s) AS active
    FROM repo_daily_activity a JOIN repos r USING (repo_id)
    WHERE a.day BETWEEN %(prev_start)s AND %(as_of)s
    GROUP BY r.language, a.repo_id
),
tot AS (SELECT sum(cur) AS t_cur, sum(prev) AS t_prev FROM per_repo),
lang AS (
    SELECT name, sum(cur) AS a_cur, sum(prev) AS a_prev,
           count(*) FILTER (WHERE active) AS n, max(cur) AS top
    FROM per_repo WHERE name IS NOT NULL GROUP BY name
)
SELECT name,
       (a_cur / t_cur) / (GREATEST(a_prev, %(floor)s) / t_prev) - 1 AS momentum,
       a_cur / t_cur AS share,
       n,
       top / NULLIF(a_cur, 0) AS top_share
FROM lang, tot
WHERE n >= %(min_repos)s
"""

_MONTHLY_SHARE = """
WITH m AS (
    SELECT date_trunc('month', a.day)::date AS month, r.language AS name,
           sum(a.stars * %(ws)s + a.forks * %(wf)s + LEAST(a.pushes, %(cap)s) * %(wp)s) AS act
    FROM repo_daily_activity a JOIN repos r USING (repo_id)
    GROUP BY 1, 2
)
SELECT month, name, act / sum(act) OVER (PARTITION BY month) AS share
FROM m
"""


@dataclass(frozen=True)
class Score:
    momentum: float
    share: float
    repos: int
    top_share: float


def scores(
    conn: psycopg.Connection, s: Settings, window: int, as_of: date, cap: int | None = None
) -> dict[str, Score]:
    w = window_bounds(as_of, window)
    params = {
        "ws": s.weights.stars,
        "wf": s.weights.forks,
        "wp": s.weights.pushes,
        "cap": s.push_cap if cap is None else cap,
        "floor": s.momentum_floor,
        "min_repos": s.min_repos,
        "prev_start": w.prev_start,
        "prev_end": w.prev_end,
        "cur_start": w.cur_start,
        "as_of": w.as_of,
    }
    return {
        r[0]: Score(float(r[1]), float(r[2]), r[3], float(r[4] or 0))
        for r in conn.execute(_SCORES, params)
    }


def ranks(values: dict[str, float]) -> dict[str, float]:
    """1 = highest. Ties share the average rank."""
    ordered = sorted(values, key=lambda k: -values[k])
    out: dict[str, float] = {}
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and values[ordered[j + 1]] == values[ordered[i]]:
            j += 1
        for k in range(i, j + 1):
            out[ordered[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(a: dict[str, float], b: dict[str, float]) -> float:
    """Spearman rank correlation over the names both have."""
    common = sorted(set(a) & set(b))
    if len(common) < 3:
        return float("nan")
    ra, rb = ranks({k: a[k] for k in common}), ranks({k: b[k] for k in common})
    ma, mb = sum(ra.values()) / len(common), sum(rb.values()) / len(common)
    cov = sum((ra[k] - ma) * (rb[k] - mb) for k in common)
    va = sum((ra[k] - ma) ** 2 for k in common)
    vb = sum((rb[k] - mb) ** 2 for k in common)
    return cov / (va * vb) ** 0.5 if va and vb else float("nan")


def top(values: dict[str, float], n: int = 10) -> list[str]:
    return sorted(values, key=lambda k: -values[k])[:n]


def compare(label: str, base: dict[str, Score], other: dict[str, Score]) -> str:
    b = {k: v.momentum for k, v in base.items()}
    o = {k: v.momentum for k, v in other.items()}
    overlap = len(set(top(b)) & set(top(o)))
    return f"| {label} | {spearman(b, o):.3f} | {overlap}/10 | {', '.join(top(o, 5))} |"


def section_cap(conn: psycopg.Connection, s: Settings, as_of: date) -> None:
    print("## A. Push cap\n")
    for window in (30, 90):
        base = scores(conn, s, window, as_of)
        print(f"W = {window}, as of {as_of}. Baseline: cap {s.push_cap}.\n")
        print("| Variant | Spearman vs baseline | Top-10 overlap | Top 5 gainers |")
        print("|---|---|---|---|")
        for cap in (10, 25, 50, 100, NO_CAP):
            label = "no cap" if cap == NO_CAP else f"cap {cap}"
            print(compare(label, base, scores(conn, s, window, as_of, cap)))
        no_push = replace(s, weights=replace(s.weights, pushes=0.0))
        print(compare("no push term", base, scores(conn, no_push, window, as_of)))
        print()


def section_weights(conn: psycopg.Connection, s: Settings, as_of: date) -> None:
    print("## B. Weights ±50%\n")
    base = scores(conn, s, 30, as_of)
    print(f"W = 30, as of {as_of}. Baseline: stars {s.weights.stars}, forks {s.weights.forks}, "
          f"pushes {s.weights.pushes}.\n")  # fmt: skip
    print("| Variant | Spearman vs baseline | Top-10 overlap | Top 5 gainers |")
    print("|---|---|---|---|")
    for field in ("stars", "forks", "pushes"):
        for factor in (0.5, 1.5):
            w: Weights = replace(s.weights, **{field: getattr(s.weights, field) * factor})
            label = f"{field} ×{factor}"
            print(compare(label, base, scores(conn, replace(s, weights=w), 30, as_of)))
    print()


def section_june(conn: psycopg.Connection, s: Settings) -> None:
    print("## C. Share continuity across June 2025\n")
    params = {"ws": s.weights.stars, "wf": s.weights.forks, "wp": s.weights.pushes,
              "cap": s.push_cap}  # fmt: skip
    shares: dict[str, dict[date, float]] = {}
    for month, name, share in conn.execute(_MONTHLY_SHARE, params):
        if name is not None:
            shares.setdefault(name, {})[month] = float(share)
    months = sorted({m for v in shares.values() for m in v})
    biggest = sorted(shares, key=lambda k: -sum(shares[k].values()))[:12]
    print("Monthly share of activity (%), and the May→June change next to the typical "
          "month-to-month change (median of the other months, absolute).\n")  # fmt: skip
    print("| Language | " + " | ".join(f"{m:%b}" for m in months) + " | May→Jun | Typical |")
    print("|---|" + "---|" * (len(months) + 2))
    ratios = []
    for name in biggest:
        row = [shares[name].get(m, 0.0) for m in months]
        changes = {months[i + 1]: row[i + 1] / row[i] - 1 for i in range(len(row) - 1) if row[i]}
        june = changes.get(date(2025, 6, 1), float("nan"))
        typical = median(abs(v) for m, v in changes.items() if m != date(2025, 6, 1))
        ratios.append(abs(june) / typical if typical else float("nan"))
        cells = " | ".join(f"{100 * v:.1f}" for v in row)
        print(f"| {name} | {cells} | {100 * june:+.1f}% | {100 * typical:.1f}% |")
    print(f"\nMedian of |May→Jun| / typical over these languages: {median(ratios):.2f} "
          "(about 1 means June looks like any other month).\n")  # fmt: skip


def section_concentration(conn: psycopg.Connection, s: Settings, latest: date) -> None:
    print("## D. Concentration: is a top gainer mostly one repo?\n")
    print("W = 30. `top repo share` is the largest repo's part of the language's current-window "
          "activity.\n")  # fmt: skip
    print("| As of | Top-10 gainers with top repo share > 50% | > 30% | Top 3 gainers "
          "(momentum, top repo share) |")  # fmt: skip
    print("|---|---|---|---|")
    over50_all = over30_all = total = 0
    as_of = latest
    while as_of - timedelta(days=59) >= date(2025, 1, 1):
        sc = scores(conn, s, 30, as_of)
        names = top({k: v.momentum for k, v in sc.items()})
        over50 = sum(sc[n].top_share > 0.5 for n in names)
        over30 = sum(sc[n].top_share > 0.3 for n in names)
        over50_all += over50
        over30_all += over30
        total += len(names)
        top3 = "; ".join(f"{n} ({sc[n].momentum:+.0%}, {sc[n].top_share:.0%})" for n in names[:3])
        print(f"| {as_of} | {over50} | {over30} | {top3} |")
        as_of -= timedelta(days=30)
    print(f"\nOver all dates: {over50_all}/{total} top-10 gainers are above 50%, "
          f"{over30_all}/{total} above 30%.\n")  # fmt: skip
    sc = scores(conn, s, 30, latest)
    all_shares = sorted(v.top_share for v in sc.values())
    print(f"All {len(all_shares)} languages with ≥ {s.min_repos} repos, as of {latest}: "
          f"median top repo share {median(all_shares):.0%}, "
          f"{sum(v > 0.5 for v in all_shares)} above 50%, "
          f"{sum(v > 0.3 for v in all_shares)} above 30%.\n")  # fmt: skip


def main() -> None:
    s = Settings.from_env()
    with connect(s.database_url) as conn:
        latest = conn.execute("SELECT max(day) FROM repo_daily_activity").fetchone()[0]
        print(f"# Momentum score checks (data to {latest})\n")
        section_cap(conn, s, latest)
        section_weights(conn, s, latest)
        section_june(conn, s)
        section_concentration(conn, s, latest)


if __name__ == "__main__":
    main()

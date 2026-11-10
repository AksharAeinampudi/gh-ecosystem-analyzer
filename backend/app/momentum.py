"""Share-based momentum queries (SPEC §4 and §5): leaderboard, time series, top repos, meta.

    activity(repo, day) = stars*w_stars + forks*w_forks + min(pushes, PUSH_CAP)*w_pushes
    share_cur(x)        = A_cur(x) / T_cur
    share_prev(x)       = max(A_prev(x), FLOOR) / T_prev
    momentum(x)         = share_cur(x) / share_prev(x) - 1

The current window is the W days ending on as_of; the previous window is the W days before it.
Language and topic totals come from the rollup tables; per-repo figures (active repos, the top
repo's share, top repos) come from repo_daily_activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import psycopg
from psycopg import sql

from app.settings import Settings

Dimension = Literal["language", "topic"]
Order = Literal["desc", "asc"]
WINDOWS = (30, 60, 90)


@dataclass(frozen=True)
class Windows:
    prev_start: date
    prev_end: date
    cur_start: date
    as_of: date


@dataclass(frozen=True)
class LeaderboardRow:
    rank: int
    name: str
    momentum: float
    share_current: float
    share_previous: float
    activity_current: float
    activity_previous: float
    active_repos: int
    top_repo_share: float
    concentrated: bool


@dataclass(frozen=True)
class TimeseriesPoint:
    day: date
    stars: int
    forks: int
    pushes: int
    pr_events: int
    issue_events: int
    activity: float
    share: float


@dataclass(frozen=True)
class TopRepo:
    full_name: str
    activity_current: float
    activity_previous: float
    delta: float
    stars_total: int | None

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


@dataclass(frozen=True)
class Meta:
    earliest_day: date
    latest_day: date
    tracked_repos: int


class AsOfOutOfRange(ValueError):
    """Both windows must lie inside the loaded data."""


class UnknownName(LookupError):
    """No language or topic with this name is in the rollups."""


def window_bounds(as_of: date, window: int) -> Windows:
    """Current window = [as_of - W + 1, as_of]; previous = the W days before that."""
    if window < 1:
        raise ValueError("window must be >= 1")
    cur_start = as_of - timedelta(days=window - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=window - 1)
    return Windows(prev_start, prev_end, cur_start, as_of)


def data_range(conn: psycopg.Connection) -> tuple[date, date] | None:
    """Earliest and latest day in the rollups, or None when they are empty."""
    row = conn.execute("SELECT min(day), max(day) FROM total_daily").fetchone()
    return None if row is None or row[0] is None else (row[0], row[1])


def resolve_windows(conn: psycopg.Connection, window: int, as_of: date | None) -> Windows:
    """Default as_of to the latest day and check both windows fit inside the data."""
    rng = data_range(conn)
    if rng is None:
        raise AsOfOutOfRange("no data loaded: run ingest.load and ingest.rollup")
    earliest, latest = rng
    w = window_bounds(as_of or latest, window)
    if w.as_of > latest or w.prev_start < earliest:
        first_ok = earliest + timedelta(days=2 * window - 1)
        raise AsOfOutOfRange(
            f"as_of must be between {first_ok} and {latest} for a {window}-day window"
        )
    return w


# --- SQL building blocks -------------------------------------------------------------------
# Only identifiers and fixed SQL fragments from the mappings below are formatted into queries.
# Every value (names, dates, weights, limits) is passed as a parameter.

# Activity of a rollup row, and of one repo-day (alias `a` = repo_daily_activity).
_ROLLUP_ACTIVITY = sql.SQL(
    "stars * %(w_stars)s + forks * %(w_forks)s + pushes_capped * %(w_pushes)s"
)
_REPO_ACTIVITY = sql.SQL(
    "a.stars * %(w_stars)s + a.forks * %(w_forks)s + LEAST(a.pushes, %(push_cap)s) * %(w_pushes)s"
)


@dataclass(frozen=True)
class _Dim:
    table: sql.Identifier  # rollup table
    name_col: sql.Identifier  # its name column
    repo_names: sql.SQL  # FROM/WHERE giving (a.*, name) pairs: one row per repo-day per name
    repo_match: sql.SQL  # WHERE condition selecting repos (alias r) of %(name)s


_DIMS: dict[str, _Dim] = {
    "language": _Dim(
        sql.Identifier("language_daily"),
        sql.Identifier("language"),
        sql.SQL(
            "FROM repo_daily_activity a JOIN repos r USING (repo_id)"
            " CROSS JOIN LATERAL (SELECT r.language AS name) n"
            " WHERE r.language IS NOT NULL"
        ),
        sql.SQL("r.language = %(name)s"),
    ),
    "topic": _Dim(
        sql.Identifier("topic_daily"),
        sql.Identifier("topic"),
        sql.SQL(
            "FROM repo_daily_activity a JOIN repos r USING (repo_id)"
            " CROSS JOIN LATERAL (SELECT DISTINCT unnest(r.topics) AS name) n"
            " WHERE true"
        ),
        sql.SQL("r.topics @> ARRAY[%(name)s]::text[]"),
    ),
}

_ORDERS = {"desc": sql.SQL("DESC"), "asc": sql.SQL("ASC")}

_LEADERBOARD = sql.SQL("""
WITH win AS (
    SELECT {name_col} AS name,
           coalesce(sum({rollup_activity}) FILTER (WHERE day >= %(cur_start)s), 0) AS a_cur,
           coalesce(sum({rollup_activity}) FILTER (WHERE day <= %(prev_end)s), 0) AS a_prev
    FROM {table}
    WHERE day BETWEEN %(prev_start)s AND %(as_of)s
    GROUP BY {name_col}
),
tot AS (
    SELECT sum({rollup_activity}) FILTER (WHERE day >= %(cur_start)s) AS t_cur,
           sum({rollup_activity}) FILTER (WHERE day <= %(prev_end)s) AS t_prev
    FROM total_daily
    WHERE day BETWEEN %(prev_start)s AND %(as_of)s
),
per_repo AS (
    SELECT n.name, a.repo_id, sum({repo_activity}) AS act
    {repo_names} AND a.day BETWEEN %(cur_start)s AND %(as_of)s
    GROUP BY n.name, a.repo_id
),
active AS (
    SELECT name, count(*) AS n, max(act) AS top_act FROM per_repo GROUP BY name
),
scored AS (
    SELECT w.name, w.a_cur, w.a_prev, a.n,
           w.a_cur / tot.t_cur AS share_cur,
           GREATEST(w.a_prev, %(floor)s) / tot.t_prev AS share_prev,
           coalesce(a.top_act / NULLIF(w.a_cur, 0), 0) AS top_share
    FROM win w
    JOIN active a USING (name)
    CROSS JOIN tot
    WHERE a.n >= %(min_repos)s AND tot.t_cur > 0 AND tot.t_prev > 0
)
SELECT name, share_cur / share_prev - 1 AS momentum, share_cur, share_prev, a_cur, a_prev, n,
       top_share, top_share > %(max_top_share)s AS concentrated
FROM scored
WHERE %(include_concentrated)s OR top_share <= %(max_top_share)s
ORDER BY momentum {direction}, name
LIMIT %(limit)s
""")

_NAME_EXISTS = sql.SQL("SELECT EXISTS (SELECT 1 FROM {table} WHERE {name_col} = %(name)s)")

_TIMESERIES = sql.SQL("""
SELECT g.day::date,
       coalesce(x.stars, 0), coalesce(x.forks, 0), coalesce(x.pushes, 0),
       coalesce(x.pr_events, 0), coalesce(x.issue_events, 0),
       coalesce(x.activity, 0) AS activity,
       CASE WHEN t.activity > 0 THEN coalesce(x.activity, 0) / t.activity ELSE 0 END AS share
FROM generate_series(%(prev_start)s::date, %(as_of)s::date, interval '1 day') AS g(day)
LEFT JOIN (
    SELECT day, stars, forks, pushes, pr_events, issue_events, {rollup_activity} AS activity
    FROM {table} WHERE {name_col} = %(name)s
) x ON x.day = g.day::date
LEFT JOIN (
    SELECT day, {rollup_activity} AS activity FROM total_daily
) t ON t.day = g.day::date
ORDER BY g.day
""")

_TOP_REPOS = sql.SQL("""
WITH per_repo AS (
    SELECT a.repo_id,
           coalesce(sum({repo_activity}) FILTER (WHERE a.day >= %(cur_start)s), 0) AS cur,
           coalesce(sum({repo_activity}) FILTER (WHERE a.day <= %(prev_end)s), 0) AS prev
    FROM repo_daily_activity a JOIN repos r USING (repo_id)
    WHERE a.day BETWEEN %(prev_start)s AND %(as_of)s AND {repo_match}
    GROUP BY a.repo_id
)
SELECT r.full_name, p.cur, p.prev, p.cur - p.prev AS delta, r.stars_total
FROM per_repo p JOIN repos r USING (repo_id)
ORDER BY delta {direction}, r.full_name
LIMIT %(limit)s
""")

_TRACKED_REPOS = """
SELECT count(*) FROM repos r
WHERE EXISTS (SELECT 1 FROM repo_daily_activity a WHERE a.repo_id = r.repo_id)
"""


def _params(settings: Settings, w: Windows, **extra: object) -> dict[str, object]:
    return {
        "prev_start": w.prev_start,
        "prev_end": w.prev_end,
        "cur_start": w.cur_start,
        "as_of": w.as_of,
        "w_stars": settings.weights.stars,
        "w_forks": settings.weights.forks,
        "w_pushes": settings.weights.pushes,
        "push_cap": settings.push_cap,
        **extra,
    }


def _require_name(conn: psycopg.Connection, dim: _Dim, dimension: str, name: str) -> None:
    query = _NAME_EXISTS.format(table=dim.table, name_col=dim.name_col)
    row = conn.execute(query, {"name": name}).fetchone()
    if row is None or not row[0]:
        raise UnknownName(f"unknown {dimension}: {name!r}")


def leaderboard(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    dimension: Dimension = "language",
    window: int = 30,
    as_of: date | None = None,
    order: Order = "desc",
    limit: int = 20,
    include_concentrated: bool = False,
) -> tuple[Windows, list[LeaderboardRow]]:
    """Languages or topics ranked by momentum. order='desc' gives gainers, 'asc' decliners.

    Entries where one repo is more than MAX_TOP_REPO_SHARE of current activity are left out
    unless include_concentrated is set.
    """
    dim = _DIMS[dimension]
    w = resolve_windows(conn, window, as_of)
    query = _LEADERBOARD.format(
        table=dim.table,
        name_col=dim.name_col,
        rollup_activity=_ROLLUP_ACTIVITY,
        repo_activity=_REPO_ACTIVITY,
        repo_names=dim.repo_names,
        direction=_ORDERS[order],
    )
    params = _params(
        settings,
        w,
        floor=settings.momentum_floor,
        min_repos=settings.min_repos,
        max_top_share=settings.max_top_repo_share,
        include_concentrated=include_concentrated,
        limit=limit,
    )
    rows = conn.execute(query, params).fetchall()
    return w, [
        LeaderboardRow(
            rank=i,
            name=r[0],
            momentum=float(r[1]),
            share_current=float(r[2]),
            share_previous=float(r[3]),
            activity_current=float(r[4]),
            activity_previous=float(r[5]),
            active_repos=r[6],
            top_repo_share=float(r[7]),
            concentrated=r[8],
        )
        for i, r in enumerate(rows, 1)
    ]


def timeseries(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    dimension: Dimension,
    name: str,
    window: int = 30,
    as_of: date | None = None,
) -> tuple[Windows, list[TimeseriesPoint]]:
    """One point per day across both windows (2*W days); days with no activity are zeros."""
    dim = _DIMS[dimension]
    w = resolve_windows(conn, window, as_of)
    _require_name(conn, dim, dimension, name)
    query = _TIMESERIES.format(
        table=dim.table, name_col=dim.name_col, rollup_activity=_ROLLUP_ACTIVITY
    )
    rows = conn.execute(query, _params(settings, w, name=name)).fetchall()
    return w, [
        TimeseriesPoint(r[0], r[1], r[2], r[3], r[4], r[5], float(r[6]), float(r[7])) for r in rows
    ]


def top_repos(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    dimension: Dimension,
    name: str,
    window: int = 30,
    as_of: date | None = None,
    order: Order = "desc",
    limit: int = 10,
) -> tuple[Windows, list[TopRepo]]:
    """Repos of a language or topic by change in activity between the two windows.

    order='desc' gives the biggest gains first, 'asc' the biggest drops.
    """
    dim = _DIMS[dimension]
    w = resolve_windows(conn, window, as_of)
    _require_name(conn, dim, dimension, name)
    query = _TOP_REPOS.format(
        repo_activity=_REPO_ACTIVITY, repo_match=dim.repo_match, direction=_ORDERS[order]
    )
    rows = conn.execute(query, _params(settings, w, name=name, limit=limit)).fetchall()
    return w, [TopRepo(r[0], float(r[1]), float(r[2]), float(r[3]), r[4]) for r in rows]


def meta(conn: psycopg.Connection) -> Meta | None:
    """Data range and tracked-repo count, or None when nothing is loaded."""
    rng = data_range(conn)
    if rng is None:
        return None
    row = conn.execute(_TRACKED_REPOS).fetchone()
    return Meta(rng[0], rng[1], row[0] if row else 0)

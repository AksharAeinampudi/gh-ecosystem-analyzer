"""Share-based momentum leaderboard (SPEC §4), read from the rollup tables.

    activity(day)  = stars*w_stars + forks*w_forks + pushes_capped*w_pushes
    share_cur(x)   = A_cur(x) / T_cur
    share_prev(x)  = max(A_prev(x), FLOOR) / T_prev
    momentum(x)    = share_cur(x) / share_prev(x) - 1

The current window is the W days ending on as_of; the previous window is the W days before it.
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


class AsOfOutOfRange(ValueError):
    """Both windows must lie inside the loaded data."""


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


# One query shape for both dimensions; only the rollup table, its name column and the
# active-repo subquery differ. Those come from this fixed mapping, never from user input.
_ACTIVE_LANGUAGE = sql.SQL("""
    SELECT r.language AS name, count(DISTINCT a.repo_id) AS n
    FROM repo_daily_activity a JOIN repos r USING (repo_id)
    WHERE a.day BETWEEN %(cur_start)s AND %(as_of)s AND r.language IS NOT NULL
    GROUP BY r.language
""")

_ACTIVE_TOPIC = sql.SQL("""
    SELECT t.name, count(DISTINCT a.repo_id) AS n
    FROM repo_daily_activity a
    JOIN repos r USING (repo_id)
    CROSS JOIN LATERAL (SELECT DISTINCT unnest(r.topics) AS name) t
    WHERE a.day BETWEEN %(cur_start)s AND %(as_of)s
    GROUP BY t.name
""")

_DIMENSIONS: dict[str, tuple[sql.Identifier, sql.Identifier, sql.SQL]] = {
    "language": (sql.Identifier("language_daily"), sql.Identifier("language"), _ACTIVE_LANGUAGE),
    "topic": (sql.Identifier("topic_daily"), sql.Identifier("topic"), _ACTIVE_TOPIC),
}

_ORDERS = {"desc": sql.SQL("DESC"), "asc": sql.SQL("ASC")}

_ACTIVITY = sql.SQL("stars * %(w_stars)s + forks * %(w_forks)s + pushes_capped * %(w_pushes)s")

_LEADERBOARD = sql.SQL("""
WITH win AS (
    SELECT {name_col} AS name,
           coalesce(sum({activity}) FILTER (WHERE day >= %(cur_start)s), 0) AS a_cur,
           coalesce(sum({activity}) FILTER (WHERE day <= %(prev_end)s), 0) AS a_prev
    FROM {table}
    WHERE day BETWEEN %(prev_start)s AND %(as_of)s
    GROUP BY {name_col}
),
tot AS (
    SELECT sum({activity}) FILTER (WHERE day >= %(cur_start)s) AS t_cur,
           sum({activity}) FILTER (WHERE day <= %(prev_end)s) AS t_prev
    FROM total_daily
    WHERE day BETWEEN %(prev_start)s AND %(as_of)s
),
active AS ({active}),
scored AS (
    SELECT w.name, w.a_cur, w.a_prev, a.n,
           w.a_cur / tot.t_cur AS share_cur,
           GREATEST(w.a_prev, %(floor)s) / tot.t_prev AS share_prev
    FROM win w
    JOIN active a USING (name)
    CROSS JOIN tot
    WHERE a.n >= %(min_repos)s AND tot.t_cur > 0 AND tot.t_prev > 0
)
SELECT name, share_cur / share_prev - 1 AS momentum, share_cur, share_prev, a_cur, a_prev, n
FROM scored
ORDER BY momentum {direction}, name
LIMIT %(limit)s
""")


def leaderboard(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    dimension: Dimension = "language",
    window: int = 30,
    as_of: date | None = None,
    order: Order = "desc",
    limit: int = 20,
) -> tuple[Windows, list[LeaderboardRow]]:
    """Languages or topics ranked by momentum. order='desc' gives gainers, 'asc' decliners."""
    table, name_col, active = _DIMENSIONS[dimension]
    w = resolve_windows(conn, window, as_of)
    query = _LEADERBOARD.format(
        table=table,
        name_col=name_col,
        activity=_ACTIVITY,
        active=active,
        direction=_ORDERS[order],
    )
    params = {
        "prev_start": w.prev_start,
        "prev_end": w.prev_end,
        "cur_start": w.cur_start,
        "as_of": w.as_of,
        "w_stars": settings.weights.stars,
        "w_forks": settings.weights.forks,
        "w_pushes": settings.weights.pushes,
        "floor": settings.momentum_floor,
        "min_repos": settings.min_repos,
        "limit": limit,
    }
    rows = conn.execute(query, params).fetchall()
    return w, [
        LeaderboardRow(
            i, r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), r[6]
        )
        for i, r in enumerate(rows, 1)
    ]

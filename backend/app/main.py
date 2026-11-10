"""FastAPI app (SPEC §5).

Run from backend/:  uvicorn app.main:app --reload     (docs at http://localhost:8000/docs)
"""

from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import date
from enum import IntEnum
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import momentum, schemas
from app.db import connect
from app.settings import Settings

CACHE_SIZE = 512


class Window(IntEnum):
    """Allowed comparison windows, in days."""

    D30 = 30
    D60 = 60
    D90 = 90


Dimension = Literal["language", "topic"]
Order = Literal["desc", "asc"]


class ResponseCache:
    """A small LRU for query results.

    The dataset only changes when the pipeline is re-run, so results stay valid for the life
    of the process; restart the API after a reload. The slowest query (topic leaderboard,
    90 days) takes about 3 s uncached.
    """

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self._size = size
        self._items: OrderedDict[tuple, Any] = OrderedDict()

    def get_or_compute(self, key: tuple, compute: Callable[[], Any]) -> Any:
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        value = compute()
        self._items[key] = value
        if len(self._items) > self._size:
            self._items.popitem(last=False)
        return value


def _window_info(dimension: str, window: int, w: momentum.Windows) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "window": window,
        "as_of": w.as_of,
        "current": (w.cur_start, w.as_of),
        "previous": (w.prev_start, w.prev_end),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="GitHub Ecosystem Analyzer",
        description="Language and topic momentum on GitHub, from GH Archive (2025 snapshot).",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    cache = ResponseCache()

    def get_conn() -> Iterator[psycopg.Connection]:
        try:
            conn = connect(settings.database_url, connect_timeout=5)
        except psycopg.OperationalError:
            raise HTTPException(503, "database unavailable") from None
        with conn:
            yield conn

    Conn = Annotated[psycopg.Connection, Depends(get_conn)]
    DimensionQ = Annotated[Dimension, Query(description="Rank languages or topics")]
    WindowQ = Annotated[Window, Query(description="Window length in days")]
    AsOfQ = Annotated[
        date | None,
        Query(description="Last day of the current window; defaults to the latest day"),
    ]
    NameQ = Annotated[str, Query(min_length=1, max_length=100, description="Language or topic")]

    @app.exception_handler(momentum.AsOfOutOfRange)
    def _as_of_out_of_range(_: Request, exc: momentum.AsOfOutOfRange) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(momentum.UnknownName)
    def _unknown_name(_: Request, exc: momentum.UnknownName) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/health", response_model=schemas.Health)
    def health(conn: Conn) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/v1/meta", response_model=schemas.MetaOut)
    def get_meta(conn: Conn) -> dict[str, Any]:
        def compute() -> dict[str, Any]:
            m = momentum.meta(conn)
            if m is None:
                raise HTTPException(503, "no data loaded")
            return {
                **asdict(m),
                "windows": [w.value for w in Window],
                "weights": asdict(settings.weights),
                "push_cap": settings.push_cap,
                "floor": settings.momentum_floor,
                "min_repos": settings.min_repos,
                "max_top_repo_share": settings.max_top_repo_share,
            }

        return cache.get_or_compute(("meta",), compute)

    @app.get("/api/v1/leaderboard", response_model=schemas.LeaderboardOut)
    def get_leaderboard(
        conn: Conn,
        dimension: DimensionQ = "language",
        window: WindowQ = Window.D30,
        as_of: AsOfQ = None,
        order: Annotated[Order, Query(description="desc = gainers, asc = decliners")] = "desc",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        include_concentrated: Annotated[
            bool, Query(description="Also return entries dominated by a single repo")
        ] = False,
    ) -> dict[str, Any]:
        def compute() -> dict[str, Any]:
            w, rows = momentum.leaderboard(
                conn,
                settings,
                dimension=dimension,
                window=window.value,
                as_of=as_of,
                order=order,
                limit=limit,
                include_concentrated=include_concentrated,
            )
            return {
                **_window_info(dimension, window.value, w),
                "items": [asdict(r) for r in rows],
            }

        key = ("leaderboard", dimension, window.value, as_of, order, limit, include_concentrated)
        return cache.get_or_compute(key, compute)

    @app.get("/api/v1/timeseries", response_model=schemas.TimeseriesOut)
    def get_timeseries(
        conn: Conn,
        name: NameQ,
        dimension: DimensionQ = "language",
        window: WindowQ = Window.D30,
        as_of: AsOfQ = None,
    ) -> dict[str, Any]:
        w, points = momentum.timeseries(
            conn, settings, dimension=dimension, name=name, window=window.value, as_of=as_of
        )
        return {
            **_window_info(dimension, window.value, w),
            "name": name,
            "points": [asdict(p) for p in points],
        }

    @app.get("/api/v1/top-repos", response_model=schemas.TopReposOut)
    def get_top_repos(
        conn: Conn,
        name: NameQ,
        dimension: DimensionQ = "language",
        window: WindowQ = Window.D30,
        as_of: AsOfQ = None,
        order: Annotated[Order, Query(description="desc = biggest gains, asc = drops")] = "desc",
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        w, repos = momentum.top_repos(
            conn,
            settings,
            dimension=dimension,
            name=name,
            window=window.value,
            as_of=as_of,
            order=order,
            limit=limit,
        )
        return {
            **_window_info(dimension, window.value, w),
            "name": name,
            "items": [{**asdict(r), "url": r.url} for r in repos],
        }

    return app


app = create_app()

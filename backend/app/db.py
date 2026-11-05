"""Database connection helpers."""

from __future__ import annotations

import psycopg


def connect(database_url: str, connect_timeout: int = 10) -> psycopg.Connection:
    """Open an autocommit connection; use `with conn.transaction():` for atomic units.

    An explicit timeout matters: without one, an unreachable host can hang for minutes.
    """
    return psycopg.connect(database_url, autocommit=True, connect_timeout=connect_timeout)

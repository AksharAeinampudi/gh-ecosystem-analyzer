"""Apply plain-SQL migrations from db/migrations in filename order.

Usage (from backend/):  python -m app.migrate
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import psycopg

from app.db import connect
from app.settings import REPO_ROOT, Settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

_CREATE_TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def pending_migrations(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[Path]:
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    return [p for p in sorted(directory.glob("*.sql")) if p.stem not in applied]


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply each pending migration in its own transaction. Returns applied versions."""
    conn.execute(_CREATE_TRACKING)
    applied: list[str] = []
    for path in pending_migrations(conn, directory):
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        log.info("applied migration %s", path.stem)
        applied.append(path.stem)
    return applied


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with connect(Settings.from_env().database_url) as conn:
        applied = migrate(conn)
    log.info("%d migration(s) applied", len(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())

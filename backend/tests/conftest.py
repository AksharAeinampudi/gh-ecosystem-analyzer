from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import conninfo, sql

from app.db import connect
from app.migrate import migrate
from app.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB_NAME = "gea_test"


@pytest.fixture
def sample_hour_path() -> Path:
    return FIXTURES / "sample-hour.json.gz"


@pytest.fixture
def sample_hour_lines(sample_hour_path: Path) -> list[bytes]:
    with gzip.open(sample_hour_path, "rb") as f:
        return list(f)


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Create (or recreate) the gea_test database and migrate it. Skips if Postgres is down."""
    base_url = Settings.from_env().database_url
    try:
        admin = connect(base_url, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc.__class__.__name__}); run docker compose up")
    with admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(TEST_DB_NAME)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME)))
    url = conninfo.make_conninfo(base_url, dbname=TEST_DB_NAME)
    with connect(url) as conn:
        migrate(conn)
    yield url


@pytest.fixture
def db(test_database_url: str) -> Iterator[psycopg.Connection]:
    """A connection to a migrated test database with all data tables emptied."""
    with connect(test_database_url) as conn:
        conn.execute(
            "TRUNCATE repo_daily_activity, repos, ingest_hours,"
            " language_daily, topic_daily, total_daily"
        )
        yield conn

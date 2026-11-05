"""All configuration, read from environment variables (and an optional repo-root .env)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DATABASE_URL = "postgresql://gea:gea@localhost:5432/gea"
DEFAULT_GHARCHIVE_BASE_URL = "https://data.gharchive.org"


def load_dotenv(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Missing file -> empty dict."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    github_token: str | None = field(default=None, repr=False)
    gharchive_base_url: str = DEFAULT_GHARCHIVE_BASE_URL
    # A repo-day is stored only if it has at least this many stars OR this many forks.
    min_daily_stars: int = 1
    min_daily_forks: int = 1
    http_timeout_s: float = 60.0
    fetch_retries: int = 3

    def __post_init__(self) -> None:
        # Both thresholds must be >= 1: repo names are only captured from star/fork events.
        if self.min_daily_stars < 1 or self.min_daily_forks < 1:
            raise ValueError("min_daily_stars and min_daily_forks must be >= 1")
        if self.fetch_retries < 1:
            raise ValueError("fetch_retries must be >= 1")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            env = {**load_dotenv(), **os.environ}
        return cls(
            database_url=env.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            github_token=env.get("GITHUB_TOKEN") or None,
            gharchive_base_url=env.get("GHARCHIVE_BASE_URL", DEFAULT_GHARCHIVE_BASE_URL).rstrip(
                "/"
            ),
            min_daily_stars=int(env.get("INGEST_MIN_DAILY_STARS", "1")),
            min_daily_forks=int(env.get("INGEST_MIN_DAILY_FORKS", "1")),
            http_timeout_s=float(env.get("INGEST_HTTP_TIMEOUT_S", "60")),
            fetch_retries=int(env.get("INGEST_FETCH_RETRIES", "3")),
        )

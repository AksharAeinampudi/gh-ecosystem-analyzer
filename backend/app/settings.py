"""All configuration, read from environment variables (and an optional repo-root .env)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DATABASE_URL = "postgresql://gea:gea@localhost:5432/gea"
DEFAULT_GHARCHIVE_BASE_URL = "https://data.gharchive.org"
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "daily"

# The fixed v1 dataset (SPEC §1).
DATASET_START = date(2025, 1, 1)
DATASET_END = date(2025, 9, 30)


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
class Weights:
    """activity = stars*w_stars + forks*w_forks + min(pushes, PUSH_CAP)*w_pushes (SPEC §4)."""

    stars: float = 1.0
    forks: float = 2.0
    pushes: float = 0.5


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    github_token: str | None = field(default=None, repr=False)
    gharchive_base_url: str = DEFAULT_GHARCHIVE_BASE_URL
    data_dir: Path = DEFAULT_DATA_DIR
    http_timeout_s: float = 60.0
    fetch_retries: int = 3
    weights: Weights = field(default_factory=Weights)
    # Momentum score (SPEC §4)
    push_cap: int = 25  # max pushes one repo contributes per day
    momentum_floor: float = 100.0  # minimum previous-window activity used in the ratio
    min_repos: int = 20  # leaderboard needs this many active tracked repos
    max_top_repo_share: float = 0.5  # above this, one repo dominates ("concentrated")
    # API
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    # Folder with the built dashboard (frontend/dist). When set, the API also serves it at /.
    static_dir: Path | None = None
    # Enrichment (ingest.enrich)
    github_graphql_url: str = "https://api.github.com/graphql"
    enrich_batch_size: int = 50
    # Tracked set (SPEC §3.2): top N repos by activity over the loaded range.
    tracked_repos: int = 25_000
    # Pass 1 of the load ranks by stars/forks only and keeps N * factor candidates,
    # so repos whose pushes would lift them into the top N are not cut early.
    candidate_factor: int = 4
    # BigQuery ingest (ingest.bigquery)
    gcp_project: str | None = None
    bq_dataset: str = "gea"
    bq_candidates: int = 200_000
    bq_max_gb: float = 150.0  # hard cap on bytes billed per query

    def __post_init__(self) -> None:
        if self.fetch_retries < 1:
            raise ValueError("fetch_retries must be >= 1")
        if self.tracked_repos < 1 or self.candidate_factor < 1:
            raise ValueError("tracked_repos and candidate_factor must be >= 1")
        if self.push_cap < 1:
            raise ValueError("push_cap must be >= 1")
        if not 1 <= self.enrich_batch_size <= 100:
            raise ValueError("enrich_batch_size must be between 1 and 100")

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
            data_dir=Path(env.get("DATA_DIR", str(DEFAULT_DATA_DIR))),
            http_timeout_s=float(env.get("INGEST_HTTP_TIMEOUT_S", "60")),
            fetch_retries=int(env.get("INGEST_FETCH_RETRIES", "3")),
            weights=Weights(
                stars=float(env.get("WEIGHT_STARS", "1")),
                forks=float(env.get("WEIGHT_FORKS", "2")),
                pushes=float(env.get("WEIGHT_PUSHES", "0.5")),
            ),
            push_cap=int(env.get("PUSH_CAP", "25")),
            momentum_floor=float(env.get("MOMENTUM_FLOOR", "100")),
            min_repos=int(env.get("MIN_REPOS", "20")),
            max_top_repo_share=float(env.get("MAX_TOP_REPO_SHARE", "0.5")),
            static_dir=Path(env["STATIC_DIR"]) if env.get("STATIC_DIR") else None,
            cors_origins=tuple(
                o.strip()
                for o in env.get("CORS_ORIGINS", "http://localhost:5173").split(",")
                if o.strip()
            ),
            enrich_batch_size=int(env.get("ENRICH_BATCH_SIZE", "50")),
            tracked_repos=int(env.get("TRACKED_REPOS", "25000")),
            candidate_factor=int(env.get("CANDIDATE_FACTOR", "4")),
            gcp_project=env.get("GOOGLE_CLOUD_PROJECT") or None,
            bq_dataset=env.get("BQ_DATASET", "gea"),
            bq_candidates=int(env.get("BQ_CANDIDATES", "200000")),
            bq_max_gb=float(env.get("BQ_MAX_GB", "150")),
        )

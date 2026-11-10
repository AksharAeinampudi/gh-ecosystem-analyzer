"""Fetch language, topics and star totals for tracked repos from the GitHub GraphQL API.

Usage (from backend/, with GITHUB_TOKEN set in .env):
    python -m ingest.enrich [--limit N] [--retry-errors]

GH Archive only gives a numeric repo id and a name, so repos are looked up by name, about 50
per request, and the returned databaseId is checked against ours. GraphQL follows renames; a
name that now belongs to a different repo fails the id check and is marked not_found.

Each batch is saved in its own transaction and only repos without an enrich_status are
fetched, so the run can be stopped and resumed. Run ingest.rollup afterwards.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from app.db import connect
from app.settings import Settings

log = logging.getLogger(__name__)

USER_AGENT = "gh-ecosystem-analyzer/0.1"
MAX_TOPICS = 20
RATE_RESERVE = 50  # pause until the reset when fewer points than this remain
MAX_ATTEMPTS = 4
MAX_FAILED_BATCHES = 3  # consecutive; then give up instead of hammering the API

_REPO_FIELDS = f"""
fragment RepoFields on Repository {{
  databaseId
  stargazerCount
  primaryLanguage {{ name }}
  repositoryTopics(first: {MAX_TOPICS}) {{ nodes {{ topic {{ name }} }} }}
}}
"""


@dataclass(frozen=True)
class RepoRef:
    repo_id: int
    full_name: str


@dataclass(frozen=True)
class EnrichResult:
    repo_id: int
    status: str  # 'ok' | 'not_found' | 'error'
    language: str | None = None
    topics: tuple[str, ...] = ()
    stars_total: int | None = None


@dataclass(frozen=True)
class RateLimit:
    remaining: int
    reset_at: datetime


@dataclass(frozen=True)
class EnrichSummary:
    ok: int = 0
    not_found: int = 0
    error: int = 0
    requests: int = 0
    failed_batches: int = 0


class AuthError(RuntimeError):
    """The token was rejected (HTTP 401)."""


class TransientError(RuntimeError):
    """A failure worth retrying. `wait_s` is how long the server asked us to wait, if it said."""

    def __init__(self, message: str, wait_s: float | None = None) -> None:
        super().__init__(message)
        self.wait_s = wait_s


def split_name(full_name: str) -> tuple[str, str] | None:
    owner, sep, name = full_name.partition("/")
    return (owner, name) if sep and owner and name and "/" not in name else None


def build_query(n: int) -> str:
    """A query for n repositories, aliased r0..r{n-1}; owners and names are passed as variables."""
    variables = ", ".join(f"$o{i}: String!, $n{i}: String!" for i in range(n))
    lookups = "\n".join(
        f"  r{i}: repository(owner: $o{i}, name: $n{i}) {{ ...RepoFields }}" for i in range(n)
    )
    return (
        f"query({variables}) {{\n{lookups}\n  rateLimit {{ remaining resetAt }}\n}}\n{_REPO_FIELDS}"
    )


def build_variables(names: Sequence[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, (owner, name) in enumerate(names):
        out[f"o{i}"] = owner
        out[f"n{i}"] = name
    return out


def parse_response(
    refs: Sequence[RepoRef], payload: dict[str, Any]
) -> tuple[list[EnrichResult], RateLimit | None]:
    """Turn a GraphQL response for `refs` (aliased r0..) into one result per repo."""
    data = payload.get("data") or {}
    error_types: dict[str, str] = {}
    for err in payload.get("errors") or []:
        path = err.get("path") or []
        if path:
            error_types[str(path[0])] = err.get("type", "")
    results = []
    for i, ref in enumerate(refs):
        node = data.get(f"r{i}")
        if node is None:
            status = "not_found" if error_types.get(f"r{i}") == "NOT_FOUND" else "error"
            results.append(EnrichResult(ref.repo_id, status))
        elif node.get("databaseId") != ref.repo_id:
            # The name now points at a different repository than the one GH Archive saw.
            results.append(EnrichResult(ref.repo_id, "not_found"))
        else:
            topics = [
                t["topic"]["name"] for t in (node.get("repositoryTopics") or {}).get("nodes", [])
            ]
            results.append(
                EnrichResult(
                    ref.repo_id,
                    "ok",
                    language=(node.get("primaryLanguage") or {}).get("name"),
                    topics=tuple(dict.fromkeys(topics)),
                    stars_total=node.get("stargazerCount"),
                )
            )
    rate = None
    if rl := data.get("rateLimit"):
        rate = RateLimit(
            rl["remaining"], datetime.fromisoformat(rl["resetAt"].replace("Z", "+00:00"))
        )
    return results, rate


def post_graphql(
    url: str, token: str, query: str, variables: dict[str, str], timeout: float
) -> dict[str, Any]:
    """POST one GraphQL request. The token only ever goes into the Authorization header."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError("GitHub rejected the token (HTTP 401)") from None
        if exc.code in (403, 429, 502, 503, 504):
            retry_after = exc.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise TransientError(f"HTTP {exc.code}", wait) from None
        raise
    except (TimeoutError, urllib.error.URLError) as exc:
        raise TransientError(str(exc)) from None


Post = Callable[[str, str, str, dict[str, str], float], dict[str, Any]]

_PENDING = """
SELECT repo_id, full_name FROM repos
WHERE enrich_status IS NULL OR (%(retry_errors)s AND enrich_status = 'error')
ORDER BY repo_id
LIMIT %(limit)s
"""

_SAVE_OK = """
UPDATE repos
SET language = %s, topics = %s, stars_total = %s, enrich_status = 'ok', enriched_at = now()
WHERE repo_id = %s
"""

# Keep whatever language/topics an earlier successful run stored.
_SAVE_STATUS = "UPDATE repos SET enrich_status = %s, enriched_at = now() WHERE repo_id = %s"


def pending_repos(conn: psycopg.Connection, limit: int | None, retry_errors: bool) -> list[RepoRef]:
    rows = conn.execute(_PENDING, {"retry_errors": retry_errors, "limit": limit}).fetchall()
    return [RepoRef(r[0], r[1]) for r in rows]


def save_results(conn: psycopg.Connection, results: Sequence[EnrichResult]) -> None:
    with conn.transaction(), conn.cursor() as cur:
        ok = [
            (r.language, list(r.topics), r.stars_total, r.repo_id)
            for r in results
            if r.status == "ok"
        ]
        other = [(r.status, r.repo_id) for r in results if r.status != "ok"]
        if ok:
            cur.executemany(_SAVE_OK, ok)
        if other:
            cur.executemany(_SAVE_STATUS, other)


def fetch_batch(
    refs: Sequence[RepoRef], settings: Settings, post: Post, sleep: Callable[[float], None]
) -> tuple[list[EnrichResult], RateLimit | None]:
    """Look up one batch, retrying transient failures. Malformed names never reach the API."""
    named = [(ref, split_name(ref.full_name)) for ref in refs]
    lookups = [(ref, parts) for ref, parts in named if parts is not None]
    results = [EnrichResult(ref.repo_id, "not_found") for ref, parts in named if parts is None]
    if not lookups:
        return results, None
    query = build_query(len(lookups))
    variables = build_variables([parts for _, parts in lookups])
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            payload = post(
                settings.github_graphql_url,
                settings.github_token or "",
                query,
                variables,
                settings.http_timeout_s,
            )
            break
        except TransientError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            wait = exc.wait_s if exc.wait_s is not None else 2.0**attempt
            log.warning("request failed (%s), retry %d in %.0fs", exc, attempt, wait)
            sleep(wait)
    fetched, rate = parse_response([ref for ref, _ in lookups], payload)
    return results + fetched, rate


def run(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    limit: int | None = None,
    retry_errors: bool = False,
    post: Post = post_graphql,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EnrichSummary:
    if not settings.github_token:
        raise AuthError("GITHUB_TOKEN is not set (add it to .env)")
    todo = pending_repos(conn, limit, retry_errors)
    log.info("%d repos to enrich", len(todo))
    counts = {"ok": 0, "not_found": 0, "error": 0}
    requests = failed = consecutive_failed = 0
    size = settings.enrich_batch_size
    for start in range(0, len(todo), size):
        batch = todo[start : start + size]
        try:
            results, rate = fetch_batch(batch, settings, post, sleep)
        except TransientError as exc:
            failed += 1
            consecutive_failed += 1
            log.error("batch at %d failed after %d attempts: %s", start, MAX_ATTEMPTS, exc)
            if consecutive_failed >= MAX_FAILED_BATCHES:
                log.error("stopping after %d failed batches in a row; re-run to resume", failed)
                break
            continue
        consecutive_failed = 0
        requests += 1
        save_results(conn, results)
        for r in results:
            counts[r.status] += 1
        done = start + len(batch)
        if requests % 20 == 0 or done == len(todo):
            log.info("%d/%d done (%s)", done, len(todo), counts)
        if rate is not None and rate.remaining < RATE_RESERVE and done < len(todo):
            wait = max((rate.reset_at - now()).total_seconds(), 0) + 5
            log.warning("rate limit nearly used up; waiting %.0fs for the reset", wait)
            sleep(wait)
    return EnrichSummary(counts["ok"], counts["not_found"], counts["error"], requests, failed)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--limit", type=int, help="enrich at most this many repos")
    p.add_argument("--retry-errors", action="store_true", help="also retry repos marked 'error'")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    try:
        with connect(settings.database_url) as conn:
            s = run(conn, settings, limit=args.limit, retry_errors=args.retry_errors)
    except AuthError as exc:
        log.error("%s", exc)
        return 2
    log.info(
        "enriched: %d ok, %d not found, %d errors, %d requests, %d failed batches",
        s.ok,
        s.not_found,
        s.error,
        s.requests,
        s.failed_batches,
    )
    return 1 if s.failed_batches or s.error else 0


if __name__ == "__main__":
    sys.exit(main())

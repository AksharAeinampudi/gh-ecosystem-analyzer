from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from app.settings import Settings
from ingest.enrich import (
    AuthError,
    EnrichResult,
    RepoRef,
    TransientError,
    build_query,
    build_variables,
    parse_response,
    run,
    split_name,
)

SETTINGS = Settings(github_token="test-token", enrich_batch_size=2)
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
RATE_OK = {"remaining": 4990, "resetAt": "2026-11-09T13:00:00Z"}


def node(repo_id, language="Rust", topics=("cli",), stars=10):
    return {
        "databaseId": repo_id,
        "stargazerCount": stars,
        "primaryLanguage": {"name": language} if language else None,
        "repositoryTopics": {"nodes": [{"topic": {"name": t}} for t in topics]},
    }


def test_split_name():
    assert split_name("rust-lang/rust") == ("rust-lang", "rust")
    assert split_name("no-slash") is None
    assert split_name("a/b/c") is None
    assert split_name("/x") is None


def test_build_query_uses_variables_not_inline_names():
    q = build_query(2)
    assert "query($o0: String!, $n0: String!, $o1: String!, $n1: String!)" in q
    assert "r1: repository(owner: $o1, name: $n1) { ...RepoFields }" in q
    assert "rateLimit { remaining resetAt }" in q and "fragment RepoFields on Repository" in q
    assert build_variables([("a", "b"), ("c", "d")]) == {"o0": "a", "n0": "b", "o1": "c", "n1": "d"}


def test_parse_response_covers_every_outcome():
    refs = [
        RepoRef(1, "a/ok"),
        RepoRef(2, "b/gone"),
        RepoRef(3, "c/reused"),
        RepoRef(4, "d/nolang"),
    ]
    refs.append(RepoRef(5, "e/broken"))
    payload = {
        "data": {
            "r0": node(1, "Rust", ("cli", "cli", "web"), 99),
            "r1": None,
            "r2": node(777),  # the name now belongs to a different repository
            "r3": node(4, language=None, topics=()),
            "r4": None,
            "rateLimit": RATE_OK,
        },
        "errors": [
            {"type": "NOT_FOUND", "path": ["r1"], "message": "Could not resolve"},
            {"type": "SERVICE_UNAVAILABLE", "path": ["r4"], "message": "timeout"},
        ],
    }
    results, rate = parse_response(refs, payload)
    assert results == [
        EnrichResult(1, "ok", "Rust", ("cli", "web"), 99),
        EnrichResult(2, "not_found"),
        EnrichResult(3, "not_found"),
        EnrichResult(4, "ok", None, (), 10),
        EnrichResult(5, "error"),
    ]
    assert rate is not None
    assert (rate.remaining, rate.reset_at) == (4990, datetime(2026, 11, 9, 13, 0, tzinfo=UTC))


def test_parse_response_with_no_data_marks_all_error():
    results, rate = parse_response(
        [RepoRef(1, "a/b")], {"data": None, "errors": [{"message": "x"}]}
    )
    assert results == [EnrichResult(1, "error")] and rate is None


def test_run_requires_a_token():
    with pytest.raises(AuthError):
        run(None, Settings(github_token=None))


class FakeGitHub:
    """Answers GraphQL requests from a dict of owner/name -> node (missing = NOT_FOUND)."""

    def __init__(self, repos, rate=RATE_OK, fail_first=0):
        self.repos, self.rate, self.fail_first = repos, rate, fail_first
        self.calls = []

    def __call__(self, url, token, query, variables, timeout):
        self.calls.append(variables)
        assert token == "test-token"
        if len(self.calls) <= self.fail_first:
            raise TransientError("HTTP 502")
        data, errors = {"rateLimit": self.rate}, []
        for i in range(len(variables) // 2):
            found = self.repos.get(f"{variables[f'o{i}']}/{variables[f'n{i}']}")
            data[f"r{i}"] = found
            if found is None:
                errors.append({"type": "NOT_FOUND", "path": [f"r{i}"]})
        return {"data": data, "errors": errors}


@pytest.fixture
def three_repos(db):
    db.execute(
        """INSERT INTO repos (repo_id, full_name, name_seen_day) VALUES
           (1, 'a/one', %(d)s), (2, 'b/two', %(d)s), (3, 'c/three', %(d)s)""",
        {"d": date(2025, 3, 1)},
    )
    return db


def repo_rows(db):
    return db.execute(
        "SELECT repo_id, language, topics, stars_total, enrich_status FROM repos ORDER BY repo_id"
    ).fetchall()


@pytest.mark.db
def test_run_saves_results_and_is_resumable(three_repos):
    github = FakeGitHub({"a/one": node(1, "Rust", ("cli",), 50), "c/three": node(3, None, (), 7)})
    summary = run(three_repos, SETTINGS, post=github, sleep=lambda s: None, now=lambda: NOW)
    assert (summary.ok, summary.not_found, summary.error, summary.requests) == (2, 1, 0, 2)
    assert [len(c) // 2 for c in github.calls] == [2, 1]  # batches of 2, then 1
    assert repo_rows(three_repos) == [
        (1, "Rust", ["cli"], 50, "ok"),
        (2, None, [], None, "not_found"),
        (3, None, [], 7, "ok"),
    ]
    again = run(three_repos, SETTINGS, post=github, sleep=lambda s: None, now=lambda: NOW)
    assert again.requests == 0 and len(github.calls) == 2  # nothing left to fetch


@pytest.mark.db
def test_transient_failures_are_retried(three_repos):
    github, sleeps = FakeGitHub({"a/one": node(1)}, fail_first=2), []
    summary = run(three_repos, SETTINGS, limit=1, post=github, sleep=sleeps.append, now=lambda: NOW)
    assert (summary.ok, summary.failed_batches) == (1, 0)
    assert sleeps == [2.0, 4.0]


@pytest.mark.db
def test_batch_that_keeps_failing_is_left_for_the_next_run(three_repos):
    github = FakeGitHub({}, fail_first=99)
    summary = run(three_repos, SETTINGS, post=github, sleep=lambda s: None, now=lambda: NOW)
    assert (summary.requests, summary.failed_batches) == (0, 2)
    assert {r[4] for r in repo_rows(three_repos)} == {None}


@pytest.mark.db
def test_waits_for_reset_when_rate_limit_is_low(three_repos):
    low = {"remaining": 3, "resetAt": (NOW + timedelta(seconds=120)).isoformat()}
    github, sleeps = FakeGitHub({"a/one": node(1), "b/two": node(2), "c/three": node(3)}, low), []
    run(three_repos, SETTINGS, post=github, sleep=sleeps.append, now=lambda: NOW)
    assert sleeps == [125.0]  # waits once, between the two batches, not after the last


@pytest.mark.db
def test_retry_errors_flag(three_repos):
    three_repos.execute("UPDATE repos SET enrich_status = 'error' WHERE repo_id = 1")
    three_repos.execute("UPDATE repos SET enrich_status = 'ok' WHERE repo_id IN (2, 3)")
    github = FakeGitHub({"a/one": node(1)})
    settings = replace(SETTINGS, enrich_batch_size=50)
    assert run(three_repos, settings, post=github, sleep=lambda s: None).requests == 0
    assert run(three_repos, settings, retry_errors=True, post=github, sleep=lambda s: None).ok == 1


@pytest.mark.db
def test_parallel_workers_give_the_same_result(three_repos):
    github = FakeGitHub({"a/one": node(1, "Rust"), "b/two": node(2, "Go"), "c/three": node(3, "C")})
    settings = replace(SETTINGS, enrich_batch_size=1)
    summary = run(three_repos, settings, workers=3, post=github, sleep=lambda s: None)
    assert (summary.ok, summary.requests) == (3, 3)
    assert [r[1] for r in repo_rows(three_repos)] == ["Rust", "Go", "C"]

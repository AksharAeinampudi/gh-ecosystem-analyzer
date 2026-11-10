"""API integration tests against the test database (skipped when Postgres is down)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from ingest.rollup import rebuild

pytestmark = pytest.mark.db

START = date(2025, 3, 1)
P1, P2 = START, START + timedelta(days=1)  # previous window: days 1-30
C1, C2 = START + timedelta(days=30), START + timedelta(days=59)  # current window: days 31-60
AS_OF = C2

BASE = Settings(push_cap=25, momentum_floor=1.0, min_repos=1, max_top_repo_share=1.0)


@pytest.fixture
def seeded(db, test_database_url):
    """The same four repos as test_momentum, spread over two 30-day windows.

                      previous (P1, P2)        current (C1, C2)
    repo 1 Rust       P1 10 stars  = 10        C1 30 stars            = 30
    repo 2 Rust       P2 5 forks   = 10        C2 100 pushes -> 25*.5 = 12.5
    repo 3 Python     P1 40 stars  = 40        C1 20 stars            = 20
    repo 4 (no lang)  P2 40 stars  = 40        C2 37 stars + 1 push   = 37.5
    totals            100                      100
    """
    db.execute(
        """INSERT INTO repos (repo_id, full_name, name_seen_day, language, topics, stars_total)
           VALUES (1, 'a/one', %(d)s, 'Rust', '{cli,web}', 500),
                  (2, 'b/two', %(d)s, 'Rust', '{}', NULL),
                  (3, 'c/three', %(d)s, 'Python', '{web}', 90),
                  (4, 'd/four', %(d)s, NULL, '{}', 7)""",
        {"d": START},
    )
    db.cursor().executemany(
        "INSERT INTO repo_daily_activity (repo_id, day, stars, forks, pushes) "
        "VALUES (%s, %s, %s, %s, %s)",
        [
            (1, P1, 10, 0, 0),
            (1, C1, 30, 0, 0),
            (2, P2, 0, 5, 0),
            (2, C2, 0, 0, 100),
            (3, P1, 40, 0, 0),
            (3, C1, 20, 0, 0),
            (4, P2, 40, 0, 0),
            (4, C2, 37, 0, 1),
        ],
    )
    rebuild(db, BASE.push_cap)
    return replace(BASE, database_url=test_database_url)


@pytest.fixture
def client(seeded):
    with TestClient(create_app(seeded)) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_health_503_when_database_is_down():
    down = Settings(database_url="postgresql://nobody@127.0.0.1:1/none")
    with TestClient(create_app(down)) as c:
        assert c.get("/health").status_code == 503


def test_meta(client):
    assert client.get("/api/v1/meta").json() == {
        "earliest_day": "2025-03-01",
        "latest_day": "2025-04-29",
        "windows": [30, 60, 90],
        "tracked_repos": 4,
        "weights": {"stars": 1.0, "forks": 2.0, "pushes": 0.5},
        "push_cap": 25,
        "floor": 1.0,
        "min_repos": 1,
        "max_top_repo_share": 1.0,
    }


def test_leaderboard_defaults(client):
    body = client.get("/api/v1/leaderboard").json()
    assert {k: body[k] for k in ("dimension", "window", "as_of", "current", "previous")} == {
        "dimension": "language",
        "window": 30,
        "as_of": "2025-04-29",
        "current": ["2025-03-31", "2025-04-29"],
        "previous": ["2025-03-01", "2025-03-30"],
    }
    rust, python = body["items"]
    assert rust == {
        "rank": 1,
        "name": "Rust",
        "momentum": pytest.approx(1.125),
        "share_current": pytest.approx(0.425),
        "share_previous": pytest.approx(0.2),
        "activity_current": 42.5,
        "activity_previous": 20.0,
        "active_repos": 2,
        "top_repo_share": pytest.approx(30 / 42.5),
        "concentrated": False,
    }
    assert (python["name"], python["momentum"]) == ("Python", pytest.approx(-0.5))


def test_leaderboard_params(client):
    r = client.get(
        "/api/v1/leaderboard",
        params={"dimension": "topic", "order": "asc", "limit": 1, "as_of": AS_OF.isoformat()},
    )
    assert [(i["name"], i["momentum"]) for i in r.json()["items"]] == [("web", pytest.approx(0.0))]


def test_leaderboard_hides_concentrated_by_default(seeded):
    with TestClient(create_app(replace(seeded, max_top_repo_share=0.8))) as c:
        assert [i["name"] for i in c.get("/api/v1/leaderboard").json()["items"]] == ["Rust"]
        both = c.get("/api/v1/leaderboard", params={"include_concentrated": "true"}).json()
        assert [(i["name"], i["concentrated"]) for i in both["items"]] == [
            ("Rust", False),
            ("Python", True),
        ]


@pytest.mark.parametrize(
    "params",
    [
        {"window": 45},
        {"dimension": "repo"},
        {"order": "up"},
        {"limit": 0},
        {"limit": 101},
        {"as_of": "not-a-date"},
        {"as_of": "2025-04-28"},  # previous window would start before the data
        {"as_of": "2025-04-30"},  # after the data
        {"window": 60},  # needs 120 days; only 60 are loaded
    ],
)
def test_leaderboard_rejects_bad_params(client, params):
    r = client.get("/api/v1/leaderboard", params=params)
    assert r.status_code == 422, r.text
    assert "detail" in r.json()


def test_timeseries(client):
    body = client.get("/api/v1/timeseries", params={"name": "Rust"}).json()
    assert (body["name"], body["dimension"], body["window"]) == ("Rust", "language", 30)
    points = body["points"]
    assert len(points) == 60
    assert (points[0]["day"], points[-1]["day"]) == ("2025-03-01", "2025-04-29")
    # P1: Rust 10 of a day total of 50. C2: capped pushes 12.5 of 50, raw pushes reported.
    assert (points[0]["stars"], points[0]["share"]) == (10, pytest.approx(0.2))
    assert points[-1] == {
        "day": "2025-04-29",
        "stars": 0,
        "forks": 0,
        "pushes": 100,
        "pr_events": 0,
        "issue_events": 0,
        "activity": 12.5,
        "share": pytest.approx(0.25),
    }
    assert points[5]["activity"] == 0 and points[5]["share"] == 0  # a day with no events at all


def test_top_repos(client):
    body = client.get("/api/v1/top-repos", params={"name": "web", "dimension": "topic"}).json()
    assert body["items"] == [
        {
            "full_name": "a/one",
            "activity_current": 30.0,
            "activity_previous": 10.0,
            "delta": 20.0,
            "stars_total": 500,
            "url": "https://github.com/a/one",
        },
        {
            "full_name": "c/three",
            "activity_current": 20.0,
            "activity_previous": 40.0,
            "delta": -20.0,
            "stars_total": 90,
            "url": "https://github.com/c/three",
        },
    ]
    drops = client.get(
        "/api/v1/top-repos", params={"name": "web", "dimension": "topic", "order": "asc"}
    ).json()
    assert drops["items"][0]["full_name"] == "c/three"


@pytest.mark.parametrize("path", ["/api/v1/timeseries", "/api/v1/top-repos"])
def test_unknown_or_missing_name(client, path):
    assert client.get(path, params={"name": "COBOL"}).status_code == 404
    assert client.get(path, params={"name": "Rust", "dimension": "topic"}).status_code == 404
    assert client.get(path).status_code == 422  # name is required


def test_names_with_special_characters(client, db):
    db.execute("UPDATE repos SET language = 'C++' WHERE repo_id = 3")
    rebuild(db, BASE.push_cap)
    r = client.get("/api/v1/timeseries", params={"name": "C++"})
    assert r.status_code == 200 and r.json()["name"] == "C++"


def test_leaderboard_is_cached(client, db):
    first = client.get("/api/v1/leaderboard").json()
    db.execute("TRUNCATE language_daily")
    assert client.get("/api/v1/leaderboard").json() == first  # served from the cache
    assert client.get("/api/v1/leaderboard", params={"limit": 5}).json()["items"] == []


def test_openapi_documents_every_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/health",
        "/api/v1/meta",
        "/api/v1/leaderboard",
        "/api/v1/timeseries",
        "/api/v1/top-repos",
    }

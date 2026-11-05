from datetime import date, timedelta

import pytest

from app.settings import Settings, Weights
from ingest.dailyfile import HourStatus, day_bounds, write_day
from ingest.events import ActivityCounter
from ingest.load import GapsError, complete_manifests, pick_top, run, score_stars_forks

D1 = date(2025, 3, 1)
D2 = D1 + timedelta(days=1)


def hours(day: date) -> list[HourStatus]:
    start, _ = day_bounds(day)
    return [HourStatus(start + timedelta(hours=h), "done", 10, 5) for h in range(24)]


def write(data_dir, day, counts, names):
    c = ActivityCounter()
    c.counts = {k: list(v) for k, v in counts.items()}
    c.names = dict(names)
    write_day(data_dir, day, c, hours(day), 0)


@pytest.fixture
def two_days(tmp_path):
    #            [stars, forks, pushes, prs, issues]
    write(
        tmp_path,
        D1,
        {1: [5, 0, 0, 0, 0], 2: [1, 0, 0, 0, 0], 3: [0, 0, 50, 0, 0], 4: [0, 1, 0, 0, 0]},
        {1: "a/one", 2: "b/two", 4: "d/old-name"},
    )
    write(
        tmp_path,
        D2,
        {1: [0, 0, 9, 2, 1], 2: [1, 0, 0, 0, 0], 4: [0, 1, 0, 0, 0]},
        {2: "b/two", 4: "d/new-name"},
    )
    return tmp_path


def test_score_stars_forks_ignores_push_only_rows(two_days):
    scores = score_stars_forks(two_days, [D1, D2], Weights())
    assert scores == {1: 5.0, 2: 2.0, 4: 4.0}  # repo 3 never had a star or fork


def test_pick_top_breaks_ties_by_repo_id():
    assert pick_top({1: 5.0, 2: 2.0, 4: 2.0, 9: 1.0}, 2) == {1, 2}


def test_complete_manifests_reports_gaps(two_days):
    ready, not_ready = complete_manifests(two_days, D1, D2 + timedelta(days=1))
    assert [m.day for m in ready] == [D1, D2]
    assert not_ready == [D2 + timedelta(days=1)]


def test_run_refuses_gaps_without_flag(two_days):
    settings = Settings(data_dir=two_days, tracked_repos=2)
    with pytest.raises(GapsError):
        run(D1, D2 + timedelta(days=1), settings)


@pytest.mark.db
def test_load_end_to_end(two_days, db):
    settings = Settings(data_dir=two_days, tracked_repos=2, candidate_factor=1)
    summary = run(D1, D2, settings, conn=db)

    # Candidates (top 2 by stars/forks): {1, 4}, so repo 2 is cut.
    # Weighted activity: repo 1 = 5 + 0.5*9 = 9.5, repo 4 = 2*2 = 4.
    # Repo 3 (push-only) can never be tracked.
    assert (summary.days, summary.candidates, summary.tracked) == (2, 2, 2)
    rows = db.execute(
        "SELECT repo_id, day, stars, forks, pushes, pr_events, issue_events "
        "FROM repo_daily_activity ORDER BY repo_id, day"
    ).fetchall()
    assert rows == [
        (1, D1, 5, 0, 0, 0, 0),
        (1, D2, 0, 0, 9, 2, 1),  # push/PR-only day of a tracked repo is kept (the M1 bug)
        (4, D1, 0, 1, 0, 0, 0),
        (4, D2, 0, 1, 0, 0, 0),
    ]
    assert db.execute("SELECT repo_id, full_name FROM repos ORDER BY repo_id").fetchall() == [
        (1, "a/one"),
        (4, "d/new-name"),
    ]
    assert db.execute("SELECT count(*) FROM ingest_hours").fetchone() == (48,)


@pytest.mark.db
def test_reload_is_idempotent_and_keeps_enrichment(two_days, db):
    settings = Settings(data_dir=two_days, tracked_repos=2, candidate_factor=1)
    run(D1, D2, settings, conn=db)
    db.execute("UPDATE repos SET language = 'Rust' WHERE repo_id = 1")
    run(D1, D2, settings, conn=db)
    assert db.execute("SELECT count(*) FROM repo_daily_activity").fetchone() == (4,)
    assert db.execute("SELECT language FROM repos WHERE repo_id = 1").fetchone() == ("Rust",)

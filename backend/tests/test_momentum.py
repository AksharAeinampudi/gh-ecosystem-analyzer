"""Rollup and momentum tests. Expected values are computed by hand in the comments."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.momentum import (
    AsOfOutOfRange,
    UnknownName,
    leaderboard,
    meta,
    resolve_windows,
    timeseries,
    top_repos,
    window_bounds,
)
from app.settings import Settings
from ingest.rollup import rebuild

D1 = date(2025, 3, 1)
D2, D3, D4 = (D1 + timedelta(days=n) for n in (1, 2, 3))

# Weights 1 / 2 / 0.5, push cap 25. Floor 1, min_repos 1 and no concentration filter unless a
# test says otherwise.
SETTINGS = Settings(push_cap=25, momentum_floor=1.0, min_repos=1, max_top_repo_share=1.0)


def test_window_bounds():
    w = window_bounds(date(2025, 9, 30), 30)
    assert (w.cur_start, w.as_of) == (date(2025, 9, 1), date(2025, 9, 30))
    assert (w.prev_start, w.prev_end) == (date(2025, 8, 2), date(2025, 8, 31))
    assert (w.as_of - w.cur_start).days == (w.prev_end - w.prev_start).days == 29


def test_window_bounds_rejects_zero():
    with pytest.raises(ValueError):
        window_bounds(D1, 0)


@pytest.fixture
def scored_db(db):
    """Four repos over four days; with W = 2 the windows are D1-D2 (prev) and D3-D4 (cur).

    activity per repo-day = stars + 2*forks + 0.5*min(pushes, 25)

                      prev (D1, D2)            cur (D3, D4)
    repo 1 Rust       D1 10 stars  = 10        D3 30 stars            = 30
    repo 2 Rust       D2 5 forks   = 10        D4 100 pushes -> 25*.5 = 12.5
    repo 3 Python     D1 40 stars  = 40        D3 20 stars            = 20
    repo 4 (no lang)  D2 40 stars  = 40        D4 37 stars + 1 push   = 37.5
    totals            T_prev = 100             T_cur = 100
    """
    db.execute(
        """INSERT INTO repos (repo_id, full_name, name_seen_day, language, topics) VALUES
           (1, 'a/one', %(d)s, 'Rust', '{cli,web}'),
           (2, 'b/two', %(d)s, 'Rust', '{}'),
           (3, 'c/three', %(d)s, 'Python', '{web}'),
           (4, 'd/four', %(d)s, NULL, '{}')""",
        {"d": D1},
    )
    db.cursor().executemany(
        "INSERT INTO repo_daily_activity (repo_id, day, stars, forks, pushes) "
        "VALUES (%s, %s, %s, %s, %s)",
        [
            (1, D1, 10, 0, 0),
            (1, D3, 30, 0, 0),
            (2, D2, 0, 5, 0),
            (2, D4, 0, 0, 100),
            (3, D1, 40, 0, 0),
            (3, D3, 20, 0, 0),
            (4, D2, 40, 0, 0),
            (4, D4, 37, 0, 1),
        ],
    )
    rebuild(db, SETTINGS.push_cap)
    return db


@pytest.mark.db
def test_rollup_tables(scored_db):
    rust = scored_db.execute(
        "SELECT day, stars, forks, pushes, pushes_capped, active_repos "
        "FROM language_daily WHERE language = 'Rust' ORDER BY day"
    ).fetchall()
    assert rust == [
        (D1, 10, 0, 0, 0, 1),
        (D2, 0, 5, 0, 0, 1),
        (D3, 30, 0, 0, 0, 1),
        (D4, 0, 0, 100, 25, 1),
    ]
    # Repos without a language are in the total but in no language row.
    assert scored_db.execute(
        "SELECT stars, pushes, pushes_capped, active_repos FROM total_daily WHERE day = %s", (D4,)
    ).fetchone() == (37, 101, 26, 2)
    assert scored_db.execute("SELECT count(DISTINCT language) FROM language_daily").fetchone() == (
        2,
    )
    # Repo 1 has two topics and counts toward both.
    assert scored_db.execute(
        "SELECT topic, stars FROM topic_daily WHERE day = %s ORDER BY topic", (D3,)
    ).fetchall() == [("cli", 30), ("web", 50)]


@pytest.mark.db
def test_rollup_drops_topics_with_too_few_repos(scored_db):
    rebuild(scored_db, SETTINGS.push_cap, min_topic_repos=2)  # cli has 1 repo, web has 2
    assert scored_db.execute("SELECT DISTINCT topic FROM topic_daily").fetchall() == [("web",)]


@pytest.mark.db
def test_rollup_rebuild_is_idempotent(scored_db):
    before = scored_db.execute("SELECT * FROM language_daily ORDER BY 1, 2").fetchall()
    rebuild(scored_db, SETTINGS.push_cap)
    assert scored_db.execute("SELECT * FROM language_daily ORDER BY 1, 2").fetchall() == before


@pytest.mark.db
def test_language_leaderboard(scored_db):
    w, rows = leaderboard(scored_db, SETTINGS, dimension="language", window=2)
    assert (w.prev_start, w.prev_end, w.cur_start, w.as_of) == (D1, D2, D3, D4)
    # Rust:   share_cur 42.5/100 = 0.425, share_prev 20/100 = 0.20 -> 0.425/0.20 - 1 = 1.125
    # Python: share_cur 20/100   = 0.20,  share_prev 40/100 = 0.40 -> 0.20/0.40 - 1  = -0.5
    assert [(r.rank, r.name, r.active_repos) for r in rows] == [(1, "Rust", 2), (2, "Python", 1)]
    rust, python = rows
    assert rust.momentum == pytest.approx(1.125)
    assert (rust.share_current, rust.share_previous) == pytest.approx((0.425, 0.20))
    assert (rust.activity_current, rust.activity_previous) == pytest.approx((42.5, 20.0))
    assert python.momentum == pytest.approx(-0.5)


@pytest.mark.db
def test_order_asc_gives_decliners_first(scored_db):
    _, rows = leaderboard(scored_db, SETTINGS, window=2, order="asc", limit=1)
    assert [r.name for r in rows] == ["Python"]


@pytest.mark.db
def test_min_repos_filters_small_languages(scored_db):
    _, rows = leaderboard(scored_db, replace(SETTINGS, min_repos=2), window=2)
    assert [r.name for r in rows] == ["Rust"]


@pytest.mark.db
def test_floor_limits_growth_from_a_tiny_base(scored_db):
    # Floor 50: Rust share_prev = max(20, 50)/100 = 0.5 -> 0.425/0.5 - 1 = -0.15
    #           Python share_prev = max(40, 50)/100 = 0.5 -> 0.20/0.5 - 1 = -0.6
    _, rows = leaderboard(scored_db, replace(SETTINGS, momentum_floor=50.0), window=2)
    assert {r.name: r.momentum for r in rows} == pytest.approx({"Rust": -0.15, "Python": -0.6})


@pytest.mark.db
def test_topic_leaderboard(scored_db):
    # cli (repo 1):      prev 10 -> cur 30: (30/100)/(10/100) - 1 = 2.0
    # web (repos 1, 3):  prev 50 -> cur 50: 0.0
    _, rows = leaderboard(scored_db, SETTINGS, dimension="topic", window=2)
    assert [(r.name, r.active_repos) for r in rows] == [("cli", 1), ("web", 2)]
    assert [r.momentum for r in rows] == pytest.approx([2.0, 0.0])


@pytest.mark.db
def test_as_of_must_leave_room_for_both_windows(scored_db):
    with pytest.raises(AsOfOutOfRange):
        leaderboard(scored_db, SETTINGS, window=2, as_of=D3)  # previous window would start at D0
    with pytest.raises(AsOfOutOfRange):
        leaderboard(scored_db, SETTINGS, window=2, as_of=D4 + timedelta(days=1))
    with pytest.raises(AsOfOutOfRange):
        leaderboard(scored_db, SETTINGS, window=3)


@pytest.mark.db
def test_empty_rollups_raise(db):
    with pytest.raises(AsOfOutOfRange):
        resolve_windows(db, 30, None)


@pytest.mark.db
def test_concentrated_entries_are_hidden_unless_asked(scored_db):
    # Top repo share of current activity: Rust 30/42.5 = 0.706 (repo 1), Python 20/20 = 1.0.
    half = replace(SETTINGS, max_top_repo_share=0.5)
    assert leaderboard(scored_db, half, window=2)[1] == []
    _, rows = leaderboard(scored_db, half, window=2, include_concentrated=True)
    assert [(r.name, r.concentrated) for r in rows] == [("Rust", True), ("Python", True)]
    assert [r.top_repo_share for r in rows] == pytest.approx([30 / 42.5, 1.0])
    # A threshold between the two keeps Rust and hides Python.
    _, rows = leaderboard(scored_db, replace(SETTINGS, max_top_repo_share=0.8), window=2)
    assert [(r.name, r.concentrated) for r in rows] == [("Rust", False)]


@pytest.mark.db
def test_timeseries_covers_both_windows_with_shares(scored_db):
    # Day totals: D1 10 + 40 = 50, D2 10 + 40 = 50, D3 30 + 20 = 50, D4 12.5 + 37.5 = 50.
    w, points = timeseries(scored_db, SETTINGS, dimension="language", name="Rust", window=2)
    assert (w.prev_start, w.as_of) == (D1, D4)
    assert [p.day for p in points] == [D1, D2, D3, D4]
    assert [p.activity for p in points] == pytest.approx([10, 10, 30, 12.5])
    assert [p.share for p in points] == pytest.approx([0.2, 0.2, 0.6, 0.25])
    assert (points[3].pushes, points[1].forks) == (100, 5)  # raw counts, not capped


@pytest.mark.db
def test_timeseries_fills_days_without_activity(scored_db):
    _, points = timeseries(scored_db, SETTINGS, dimension="language", name="Python", window=2)
    assert [(p.day, p.stars, p.share) for p in points] == [
        (D1, 40, pytest.approx(0.8)),
        (D2, 0, 0),
        (D3, 20, pytest.approx(0.4)),
        (D4, 0, 0),
    ]


@pytest.mark.db
def test_unknown_name_raises(scored_db):
    with pytest.raises(UnknownName):
        timeseries(scored_db, SETTINGS, dimension="language", name="COBOL", window=2)
    with pytest.raises(UnknownName):
        top_repos(scored_db, SETTINGS, dimension="topic", name="Rust", window=2)


@pytest.mark.db
def test_top_repos_by_change(scored_db):
    # Rust: repo 1 goes 10 -> 30 (+20), repo 2 goes 10 -> 12.5 (+2.5).
    _, repos = top_repos(scored_db, SETTINGS, dimension="language", name="Rust", window=2)
    assert [(r.full_name, r.activity_previous, r.activity_current, r.delta) for r in repos] == [
        ("a/one", 10.0, 30.0, 20.0),
        ("b/two", 10.0, 12.5, 2.5),
    ]
    assert repos[0].url == "https://github.com/a/one"


@pytest.mark.db
def test_top_repos_for_a_topic_and_biggest_drops(scored_db):
    # web: repo 1 +20, repo 3 goes 40 -> 20 (-20).
    _, drops = top_repos(
        scored_db, SETTINGS, dimension="topic", name="web", window=2, order="asc", limit=1
    )
    assert [(r.full_name, r.delta) for r in drops] == [("c/three", -20.0)]


@pytest.mark.db
def test_meta(scored_db, db):
    m = meta(scored_db)
    assert m is not None
    assert (m.earliest_day, m.latest_day, m.tracked_repos) == (D1, D4, 4)
    db.execute("TRUNCATE total_daily")
    assert meta(db) is None

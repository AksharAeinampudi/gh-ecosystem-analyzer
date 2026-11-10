"""Rollup and momentum tests. Expected values are computed by hand in the comments."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.momentum import AsOfOutOfRange, leaderboard, resolve_windows, window_bounds
from app.settings import Settings
from ingest.rollup import rebuild

D1 = date(2025, 3, 1)
D2, D3, D4 = (D1 + timedelta(days=n) for n in (1, 2, 3))

# Weights 1 / 2 / 0.5, push cap 25. Floor 1 and min_repos 1 unless a test says otherwise.
SETTINGS = Settings(push_cap=25, momentum_floor=1.0, min_repos=1)


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

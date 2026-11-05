"""DB integration tests. Skipped automatically when the docker-compose Postgres is down."""

from datetime import UTC, date, datetime, timedelta

import pytest

from ingest.events import RepoDayRow
from ingest.store import HourStatus, day_bounds, day_is_complete, write_day

pytestmark = pytest.mark.db

DAY = date(2026, 10, 1)


def all_hours(day: date, status: str = "done") -> list[HourStatus]:
    start, _ = day_bounds(day)
    return [HourStatus(start + timedelta(hours=h), status, 10, 5) for h in range(24)]


def activity(db):
    return db.execute(
        "SELECT repo_id, day, stars, forks, pushes, prs_opened, issues_opened "
        "FROM repo_daily_activity ORDER BY repo_id, day"
    ).fetchall()


def test_write_day_and_rerun_is_idempotent(db):
    rows = [RepoDayRow(1, 2, 1, 1, 0, 0), RepoDayRow(3, 1, 1, 0, 1, 1)]
    names = {1: "alice/fast-lib", 3: "carol/tool"}
    for _ in range(2):
        assert write_day(db, DAY, rows, names, all_hours(DAY)) == 2
    assert activity(db) == [(1, DAY, 2, 1, 1, 0, 0), (3, DAY, 1, 1, 0, 1, 1)]
    assert day_is_complete(db, DAY)


def test_rewrite_replaces_rows_for_that_day_only(db):
    other = DAY + timedelta(days=1)
    write_day(db, DAY, [RepoDayRow(1, 1, 0, 0, 0, 0)], {1: "a/b"}, [])
    write_day(db, other, [RepoDayRow(1, 5, 0, 0, 0, 0)], {1: "a/b"}, [])
    write_day(db, DAY, [RepoDayRow(2, 1, 0, 0, 0, 0)], {2: "c/d"}, [])
    assert activity(db) == [(1, other, 5, 0, 0, 0, 0), (2, DAY, 1, 0, 0, 0, 0)]


def test_repo_name_only_moves_forward_in_time(db):
    row = [RepoDayRow(1, 1, 0, 0, 0, 0)]
    write_day(db, DAY, row, {1: "new/name"}, [])
    write_day(db, DAY - timedelta(days=30), row, {1: "old/name"}, [])  # backfill an older day
    assert db.execute("SELECT full_name FROM repos").fetchone() == ("new/name",)
    write_day(db, DAY + timedelta(days=1), row, {1: "newer/name"}, [])
    assert db.execute("SELECT full_name FROM repos").fetchone() == ("newer/name",)


def test_day_incomplete_with_failed_hour(db):
    hours = all_hours(DAY)
    hours[5] = HourStatus(hours[5].hour_ts, "failed")
    write_day(db, DAY, [], {}, hours)
    assert not day_is_complete(db, DAY)
    hours[5] = HourStatus(hours[5].hour_ts, "missing")
    write_day(db, DAY, [], {}, hours)
    assert day_is_complete(db, DAY)


def test_hours_are_utc(db):
    write_day(db, DAY, [], {}, all_hours(DAY))
    first = db.execute("SELECT min(hour_ts) FROM ingest_hours").fetchone()[0]
    assert first == datetime(2026, 10, 1, tzinfo=UTC)

import gzip
from datetime import date, timedelta

from ingest.dailyfile import (
    COLUMNS,
    DayRow,
    HourStatus,
    data_path,
    day_bounds,
    day_is_complete,
    iter_day_rows,
    manifest_path,
    read_manifest,
    write_day,
)
from ingest.events import ActivityCounter

DAY = date(2025, 3, 12)


def all_hours(status: str = "done") -> list[HourStatus]:
    start, _ = day_bounds(DAY)
    return [HourStatus(start + timedelta(hours=h), status, 100, 50) for h in range(24)]


def counter() -> ActivityCounter:
    c = ActivityCounter()
    c.counts = {30: [0, 0, 7, 1, 0], 10: [2, 1, 1, 0, 0]}
    c.names = {10: "alice/fast-lib"}
    return c


def test_roundtrip_sorted_with_optional_names(tmp_path):
    write_day(tmp_path, DAY, counter(), all_hours(), bad_lines=3)
    assert list(iter_day_rows(tmp_path, DAY)) == [
        DayRow(10, 2, 1, 1, 0, 0, "alice/fast-lib"),
        DayRow(30, 0, 0, 7, 1, 0, None),  # push-only repo: kept, but no name
    ]
    with gzip.open(data_path(tmp_path, DAY), "rt", encoding="utf-8") as f:
        assert f.readline().strip() == ",".join(COLUMNS)


def test_manifest_roundtrip(tmp_path):
    write_day(tmp_path, DAY, counter(), all_hours(), bad_lines=3)
    m = read_manifest(tmp_path, DAY)
    assert m is not None
    assert (m.day, m.repos, m.bad_lines, len(m.hours)) == (DAY, 2, 3, 24)
    assert m.hours[0] == all_hours()[0]
    assert m.complete and day_is_complete(tmp_path, DAY)


def test_failed_hour_makes_day_incomplete(tmp_path):
    hours = all_hours()
    hours[5] = HourStatus(hours[5].hour_ts, "failed")
    write_day(tmp_path, DAY, counter(), hours, 0)
    assert not day_is_complete(tmp_path, DAY)
    hours[5] = HourStatus(hours[5].hour_ts, "missing")
    write_day(tmp_path, DAY, counter(), hours, 0)
    assert day_is_complete(tmp_path, DAY)


def test_data_file_without_manifest_is_not_complete(tmp_path):
    write_day(tmp_path, DAY, counter(), all_hours(), 0)
    manifest_path(tmp_path, DAY).unlink()  # e.g. crash between the two writes
    assert read_manifest(tmp_path, DAY) is None
    assert not day_is_complete(tmp_path, DAY)


def test_no_temp_files_left_behind(tmp_path):
    write_day(tmp_path, DAY, counter(), all_hours(), 0)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2025-03-12.csv.gz", "2025-03-12.json"]

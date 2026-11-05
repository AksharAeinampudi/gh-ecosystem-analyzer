from datetime import UTC, date, datetime

from ingest.bigquery import (
    CANDIDATE_DAILY_SQL,
    HOURLY_SQL,
    group_rows_by_day,
    hours_for_day,
    month_ranges,
    render,
)

DAY = date(2025, 3, 12)


def test_render_fills_year_and_types_without_payload():
    sql = render(CANDIDATE_DAILY_SQL, 2025)
    assert "`githubarchive.day.2025*`" in sql
    assert "'PullRequestEvent'" in sql and "{" not in sql
    assert "payload" not in sql  # reading payload costs ~3.6 TB instead of ~66 GB
    assert "payload" not in render(HOURLY_SQL, 2025)


def test_hours_for_day_marks_hours_without_events_missing():
    hourly = {datetime(2025, 3, 12, h, tzinfo=UTC): (1000 + h, 800) for h in range(24) if h != 5}
    hours = hours_for_day(DAY, hourly)
    assert len(hours) == 24
    assert hours[5].status == "missing" and hours[5].events_seen is None
    assert hours[6].status == "done" and hours[6].events_seen == 1006


def test_group_rows_by_day():
    rows = [
        {"day": DAY, "repo_id": 1, "stars": 2, "forks": 1, "pushes": 0, "pr_events": 0,
         "issue_events": 0, "full_name": "a/b"},
        {"day": DAY, "repo_id": 2, "stars": 0, "forks": 0, "pushes": 9, "pr_events": 1,
         "issue_events": 0, "full_name": None},
        {"day": date(2025, 3, 13), "repo_id": 1, "stars": 1, "forks": 0, "pushes": 0,
         "pr_events": 0, "issue_events": 0, "full_name": "a/b"},
    ]  # fmt: skip
    by_day = group_rows_by_day(rows)
    assert by_day[DAY].counts == {1: [2, 1, 0, 0, 0], 2: [0, 0, 9, 1, 0]}
    assert by_day[DAY].names == {1: "a/b"}
    assert by_day[date(2025, 3, 13)].counts == {1: [1, 0, 0, 0, 0]}


def test_month_ranges():
    assert month_ranges(date(2025, 1, 15), date(2025, 3, 2)) == [
        (date(2025, 1, 15), date(2025, 1, 31)),
        (date(2025, 2, 1), date(2025, 2, 28)),
        (date(2025, 3, 1), date(2025, 3, 2)),
    ]
    assert month_ranges(date(2025, 12, 1), date(2025, 12, 31)) == [
        (date(2025, 12, 1), date(2025, 12, 31))
    ]

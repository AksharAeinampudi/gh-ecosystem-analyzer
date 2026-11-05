import io
from datetime import UTC, date, datetime, timedelta

import pytest

from app.settings import DATASET_END, DATASET_START, Settings
from ingest import dailyfile
from ingest.events import ActivityCounter, HourResult
from ingest.gharchive import (
    HourFailed,
    HourNotFound,
    fetch_hour,
    hour_url,
    ingest_day,
    parse_args,
    run,
)

SETTINGS = Settings(gharchive_base_url="https://example.test", fetch_retries=3)
DAY = date(2026, 10, 1)
LONG_AFTER = datetime(2026, 10, 5, tzinfo=UTC)


def test_hour_url_not_zero_padded():
    assert (
        hour_url("https://x", datetime(2026, 1, 2, 0, tzinfo=UTC))
        == "https://x/2026-01-02-0.json.gz"
    )
    assert (
        hour_url("https://x", datetime(2026, 1, 2, 23, tzinfo=UTC))
        == "https://x/2026-01-02-23.json.gz"
    )


def test_fetch_hour_streams_gzip(sample_hour_path):
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return io.BytesIO(sample_hour_path.read_bytes())

    result = fetch_hour(datetime(2026, 10, 1, 5, tzinfo=UTC), SETTINGS, opener=opener)
    assert calls == ["https://example.test/2026-10-01-5.json.gz"]
    assert (result.events_seen, result.events_kept) == (14, 12)


def test_fetch_hour_retries_then_succeeds(sample_hour_path):
    attempts, sleeps = [], []

    def opener(url, timeout):
        attempts.append(url)
        if len(attempts) == 1:
            return io.BytesIO(sample_hour_path.read_bytes()[:40])  # truncated gzip -> EOFError
        return io.BytesIO(sample_hour_path.read_bytes())

    result = fetch_hour(datetime(2026, 10, 1, tzinfo=UTC), SETTINGS, opener, sleeps.append)
    assert len(attempts) == 2 and sleeps == [2]
    # The partial first attempt must not leak into the counts.
    assert result.events_kept == 12


def test_fetch_hour_gives_up(sample_hour_path):
    def opener(url, timeout):
        raise TimeoutError("slow")

    with pytest.raises(HourFailed):
        fetch_hour(datetime(2026, 10, 1, tzinfo=UTC), SETTINGS, opener, lambda s: None)


def test_fetch_hour_404_not_retried():
    attempts = []

    def opener(url, timeout):
        attempts.append(url)
        raise HourNotFound(url)

    with pytest.raises(HourNotFound):
        fetch_hour(datetime(2026, 10, 1, tzinfo=UTC), SETTINGS, opener, lambda s: None)
    assert len(attempts) == 1


def _hour_with_star(repo_id: int, name: str) -> HourResult:
    c = ActivityCounter()
    c.counts[repo_id] = [1, 0, 0, 0, 0]
    c.names[repo_id] = name
    return HourResult(c, events_seen=1, events_kept=1, bad_lines=0)


def test_ingest_day_merges_all_hours_and_records_statuses():
    def fetch(hour):
        if hour.hour == 3:
            raise HourNotFound("gap")
        if hour.hour == 4:
            raise HourFailed("boom")
        return _hour_with_star(1, "a/b")

    res = ingest_day(DAY, fetch, now=LONG_AFTER)
    statuses = [h.status for h in res.hours]
    assert len(statuses) == 24
    assert statuses[3] == "missing" and statuses[4] == "failed"
    assert statuses.count("done") == 22
    assert res.counter.counts[1][0] == 22
    assert not res.complete


def test_recent_404_is_failed_not_missing():
    def fetch(hour):
        raise HourNotFound("not yet published")

    just_after = datetime.combine(DAY, datetime.min.time(), tzinfo=UTC) + timedelta(hours=25)
    res = ingest_day(DAY, fetch, now=just_after)
    assert res.hours[0].status == "missing"  # 00:00 hour ended 24h before `now`
    assert res.hours[23].status == "failed"  # 23:00 hour ended 1h before `now`


def _settings(tmp_path):
    return Settings(gharchive_base_url="https://example.test", data_dir=tmp_path)


def test_run_writes_daily_files_and_skips_complete_days(tmp_path):
    calls = []

    def fetch(hour):
        calls.append(hour)
        return _hour_with_star(1, "a/b")

    settings = _settings(tmp_path)
    first = run(DAY, DAY + timedelta(days=1), settings, fetch=fetch, now=LONG_AFTER)
    assert [(s.day, s.status, s.repos) for s in first] == [
        (DAY, "complete", 1),
        (DAY + timedelta(days=1), "complete", 1),
    ]
    assert len(calls) == 48
    assert dailyfile.day_is_complete(tmp_path, DAY)

    second = run(DAY, DAY + timedelta(days=1), settings, fetch=fetch, now=LONG_AFTER)
    assert [s.status for s in second] == ["skipped", "skipped"]
    assert len(calls) == 48  # nothing downloaded again

    run(DAY, DAY, settings, fetch=fetch, now=LONG_AFTER, force=True)
    assert len(calls) == 72


def test_incomplete_day_is_retried(tmp_path):
    settings = _settings(tmp_path)

    def flaky(hour):
        if hour.hour == 7:
            raise HourFailed("boom")
        return _hour_with_star(1, "a/b")

    assert run(DAY, DAY, settings, fetch=flaky, now=LONG_AFTER)[0].status == "incomplete"
    assert not dailyfile.day_is_complete(tmp_path, DAY)
    healthy = run(DAY, DAY, settings, fetch=lambda h: _hour_with_star(1, "a/b"), now=LONG_AFTER)
    assert healthy[0].status == "complete"
    rows = list(dailyfile.iter_day_rows(tmp_path, DAY))
    assert rows[0].stars == 24  # the retry replaced the day, it did not add to it


def test_parse_args_rejects_today():
    today = datetime.now(UTC).date().isoformat()
    with pytest.raises(SystemExit):
        parse_args(["--start", today, "--end", today])


def test_parse_args_defaults_to_dataset():
    args = parse_args([])
    assert (args.start, args.end) == (DATASET_START, DATASET_END)

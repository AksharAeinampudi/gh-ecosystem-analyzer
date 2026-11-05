import json

import pytest

from ingest.events import (
    ActivityCounter,
    BadLine,
    Kind,
    ParsedEvent,
    RepoDayRow,
    classify,
    count_lines,
    parse_line,
)


@pytest.mark.parametrize(
    ("etype", "action", "expected"),
    [
        ("WatchEvent", "started", Kind.STAR),
        ("ForkEvent", None, Kind.FORK),
        ("PushEvent", None, Kind.PUSH),
        ("PullRequestEvent", "opened", Kind.PR_OPENED),
        ("PullRequestEvent", "closed", None),
        ("PullRequestEvent", "reopened", None),
        ("IssuesEvent", "opened", Kind.ISSUE_OPENED),
        ("IssuesEvent", "closed", None),
        ("CreateEvent", None, None),
        ("IssueCommentEvent", "created", None),
    ],
)
def test_classify(etype, action, expected):
    payload = {"action": action} if action else {}
    assert classify({"type": etype, "payload": payload}) == expected


def test_classify_tolerates_missing_payload():
    assert classify({"type": "WatchEvent"}) is None
    assert classify({"type": "PushEvent", "payload": None}) is Kind.PUSH


def _line(**event) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode()


def test_parse_line_counted_event():
    line = _line(type="WatchEvent", repo={"id": 7, "name": "a/b"}, payload={"action": "started"})
    assert parse_line(line) == ParsedEvent(Kind.STAR, 7, "a/b")


def test_parse_line_skips_uncounted_types_without_parsing():
    # Not valid JSON, but filtered out before json.loads, so no error.
    assert parse_line(b'{"type":"GollumEvent", nonsense') is None


def test_parse_line_bad_json_raises():
    with pytest.raises(BadLine):
        parse_line(b'{"type":"PushEvent","repo":{"id":')


def test_parse_line_missing_repo_raises():
    with pytest.raises(BadLine):
        parse_line(_line(type="PushEvent", payload={}))


def test_counter_merge_sums_and_keeps_latest_name():
    a, b = ActivityCounter(), ActivityCounter()
    a.add(ParsedEvent(Kind.STAR, 1, "old/name"))
    a.add(ParsedEvent(Kind.PUSH, 1, "old/name"))
    b.add(ParsedEvent(Kind.STAR, 1, "new/name"))
    b.add(ParsedEvent(Kind.FORK, 2, "x/y"))
    a.merge(b)
    assert a.counts == {1: [2, 0, 1, 0, 0], 2: [0, 1, 0, 0, 0]}
    assert a.names == {1: "new/name", 2: "x/y"}


def test_merge_does_not_alias_other_counter():
    a, b = ActivityCounter(), ActivityCounter()
    b.add(ParsedEvent(Kind.STAR, 1, "x/y"))
    a.merge(b)
    a.add(ParsedEvent(Kind.STAR, 1, "x/y"))
    assert b.counts[1][Kind.STAR] == 1


def test_names_not_recorded_for_push_only_repos():
    c = ActivityCounter()
    c.add(ParsedEvent(Kind.PUSH, 5, "bot/repo"))
    c.add(ParsedEvent(Kind.PR_OPENED, 5, "bot/repo"))
    assert c.names == {}


def test_rows_threshold():
    c = ActivityCounter()
    c.counts = {1: [1, 0, 0, 0, 0], 2: [0, 1, 0, 0, 0], 3: [0, 0, 9, 9, 9], 4: [1, 0, 5, 0, 0]}
    assert sorted(c.rows(min_stars=1, min_forks=1)) == [
        RepoDayRow(1, 1, 0, 0, 0, 0),
        RepoDayRow(2, 0, 1, 0, 0, 0),
        RepoDayRow(4, 1, 0, 5, 0, 0),
    ]
    assert [r.repo_id for r in c.rows(min_stars=2, min_forks=1)] == [2]


def test_count_lines_on_fixture(sample_hour_lines):
    result = count_lines(sample_hour_lines)
    assert (result.events_seen, result.events_kept, result.bad_lines) == (14, 10, 1)
    assert sorted(result.counter.rows(1, 1)) == [
        RepoDayRow(1, 2, 1, 1, 0, 0),
        RepoDayRow(3, 1, 1, 0, 1, 1),
    ]
    assert result.counter.counts[2] == [0, 0, 2, 0, 0]
    assert result.counter.names == {1: "alice/fast-lib", 3: "carol/tool-v2"}

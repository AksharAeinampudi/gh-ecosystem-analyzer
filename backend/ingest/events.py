"""Pure parsing and aggregation of GH Archive events. No I/O here (see SPEC §2)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, NamedTuple


class Kind(IntEnum):
    """Counted event kinds. Values double as indexes into a repo's count list."""

    STAR = 0
    FORK = 1
    PUSH = 2
    PR_OPENED = 3
    ISSUE_OPENED = 4


N_KINDS = len(Kind)

# Cheap byte-level prefilter: GH Archive lines are compact JSON, so an event we care about
# always contains one of these. False positives are fine (full parse decides).
_TYPE_MARKERS: tuple[bytes, ...] = tuple(
    f'"type":"{t}"'.encode()
    for t in ("WatchEvent", "ForkEvent", "PushEvent", "PullRequestEvent", "IssuesEvent")
)


class ParsedEvent(NamedTuple):
    kind: Kind
    repo_id: int
    repo_name: str


class RepoDayRow(NamedTuple):
    """One row of repo_daily_activity (column order matches the COPY in ingest.store)."""

    repo_id: int
    stars: int
    forks: int
    pushes: int
    prs_opened: int
    issues_opened: int


def classify(event: dict[str, Any]) -> Kind | None:
    """Map a decoded event to the Kind it counts as, or None if it is not counted."""
    etype = event.get("type")
    if etype == "PushEvent":
        return Kind.PUSH
    if etype == "ForkEvent":
        return Kind.FORK
    action = (event.get("payload") or {}).get("action")
    if etype == "WatchEvent":
        return Kind.STAR if action == "started" else None
    if etype == "PullRequestEvent":
        return Kind.PR_OPENED if action == "opened" else None
    if etype == "IssuesEvent":
        return Kind.ISSUE_OPENED if action == "opened" else None
    return None


class BadLine(ValueError):
    """A line that passed the prefilter but is not a usable event."""


def parse_line(line: bytes) -> ParsedEvent | None:
    """Parse one GH Archive line. Returns None for event types we skip.

    Raises BadLine for malformed JSON or events missing repo id/name.
    """
    if not any(marker in line for marker in _TYPE_MARKERS):
        return None
    try:
        event = json.loads(line)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise BadLine(str(exc)) from exc
    if not isinstance(event, dict):
        raise BadLine("event is not an object")
    kind = classify(event)
    if kind is None:
        return None
    repo = event.get("repo") or {}
    repo_id, repo_name = repo.get("id"), repo.get("name")
    if not isinstance(repo_id, int) or not isinstance(repo_name, str):
        raise BadLine("missing repo id or name")
    return ParsedEvent(kind, repo_id, repo_name)


@dataclass
class ActivityCounter:
    """Per-repo counts for a period (an hour or a day).

    Names are only recorded from star/fork events: every stored repo-day needs at least one
    of those (Settings enforces thresholds >= 1), and skipping push-only repos saves memory.
    Later `add`/`merge` calls overwrite names, so merging hours in order keeps the latest name.
    """

    counts: dict[int, list[int]] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)

    def add(self, event: ParsedEvent) -> None:
        c = self.counts.get(event.repo_id)
        if c is None:
            c = self.counts[event.repo_id] = [0] * N_KINDS
        c[event.kind] += 1
        if event.kind <= Kind.FORK:
            self.names[event.repo_id] = event.repo_name

    def merge(self, other: ActivityCounter) -> None:
        for repo_id, theirs in other.counts.items():
            mine = self.counts.get(repo_id)
            if mine is None:
                self.counts[repo_id] = list(theirs)
            else:
                for i in range(N_KINDS):
                    mine[i] += theirs[i]
        self.names.update(other.names)

    def rows(self, min_stars: int, min_forks: int) -> list[RepoDayRow]:
        """Repo rows meeting the daily threshold (stars >= min_stars OR forks >= min_forks)."""
        return [
            RepoDayRow(repo_id, *c)
            for repo_id, c in self.counts.items()
            if c[Kind.STAR] >= min_stars or c[Kind.FORK] >= min_forks
        ]


@dataclass
class HourResult:
    counter: ActivityCounter
    events_seen: int
    events_kept: int
    bad_lines: int


def count_lines(lines: Iterable[bytes]) -> HourResult:
    """Aggregate an iterable of raw GH Archive lines into one HourResult."""
    counter = ActivityCounter()
    seen = kept = bad = 0
    for line in lines:
        if not line.strip():
            continue
        seen += 1
        try:
            parsed = parse_line(line)
        except BadLine:
            bad += 1
            continue
        if parsed is not None:
            counter.add(parsed)
            kept += 1
    return HourResult(counter, seen, kept, bad)

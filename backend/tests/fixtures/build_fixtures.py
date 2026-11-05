"""Regenerate sample-hour.json.gz. Run from backend/: python tests/fixtures/build_fixtures.py

Expected aggregates (asserted in tests):
  repo 1 alice/fast-lib : stars 2, forks 1, pushes 1
  repo 2 bob/pushy      : pushes 2 (push-only, so no name recorded)
  repo 3 carol/tool-v2  : stars 1, forks 1, PR events 2, issue events 2 (latest name wins)
  14 non-empty lines, 12 counted events, 1 malformed line
"""

import gzip
import json
from pathlib import Path

HERE = Path(__file__).parent


def ev(etype: str, repo_id: int, name: str, payload: dict | None = None) -> str:
    event = {
        "id": "1",
        "type": etype,
        "actor": {"id": 9, "login": "someone"},
        "repo": {"id": repo_id, "name": name, "url": f"https://api.github.com/repos/{name}"},
        "payload": payload or {},
        "public": True,
        "created_at": "2026-10-01T00:00:00Z",
    }
    return json.dumps(event, separators=(",", ":"))


LINES = [
    ev("WatchEvent", 1, "alice/fast-lib", {"action": "started"}),
    ev("WatchEvent", 1, "alice/fast-lib", {"action": "started"}),
    ev("ForkEvent", 1, "alice/fast-lib", {"forkee": {"id": 100}}),
    ev("PushEvent", 1, "alice/fast-lib", {"push_id": 1}),
    ev("PushEvent", 2, "bob/pushy", {"push_id": 2}),
    ev("PushEvent", 2, "bob/pushy", {"push_id": 3}),
    ev("PullRequestEvent", 3, "carol/tool", {"action": "opened"}),
    ev("PullRequestEvent", 3, "carol/tool", {"action": "closed"}),
    ev("IssuesEvent", 3, "carol/tool", {"action": "opened"}),
    ev("IssuesEvent", 3, "carol/tool", {"action": "closed"}),
    ev("WatchEvent", 3, "carol/tool", {"action": "started"}),
    ev("ForkEvent", 3, "carol/tool-v2", {"forkee": {"id": 101}}),
    ev("CreateEvent", 1, "alice/fast-lib", {"ref_type": "branch"}),
    '{"id":"2","type":"PushEvent","repo":{"id":',  # truncated / malformed
]


def main() -> None:
    with gzip.open(HERE / "sample-hour.json.gz", "wt", encoding="utf-8", newline="\n") as f:
        for line in LINES:
            f.write(line + "\n")


if __name__ == "__main__":
    main()

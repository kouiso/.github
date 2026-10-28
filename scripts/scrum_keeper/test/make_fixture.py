"""test/fixture/*.json を作り直す。--fixture で本物と同じ形の応答を読ませる時の見本。
使い方: python3 scripts/scrum_keeper/test/make_fixture.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from helpers import raw_project  # noqa: E402

ITEMS = [
    {"n": 1, "week": "2026-10-26", "prio": "P0", "est": 8, "status": "In Progress"},
    {"n": 2, "week": "2026-10-26", "prio": "P1", "est": 4},
    {"n": 3, "week": "2026-10-26", "state": "CLOSED", "status": "Done", "closed": "2026-10-29T03:00:00Z", "est": 3},
    {"n": 4, "week": "2026-11-02", "prio": "P1", "est": 6, "wait": "W1", "updated": "2026-10-20T00:00:00Z"},
    {"n": 5, "week": "2026-11-09", "prio": "P2", "est": 2},
    {"n": 6, "prio": "P1", "est": 5},
    {"n": 7, "kind": "PullRequest"},
    {"n": 8, "kind": "DraftIssue"},
]

if __name__ == "__main__":
    out = HERE / "fixture"
    out.mkdir(exist_ok=True)
    (out / "board-o-1.json").write_text(json.dumps({"owner": "o", "number": 1, "project": raw_project(ITEMS)},
                                                    ensure_ascii=False, indent=1) + "\n")
    repos = {"o/r": {"yml": "owner: o\nproject_number: 1\n", "issues": [[6, "o/r|6", "Issue"], [9, "o/r|9", "Issue"]]}}
    (out / "repos.json").write_text(json.dumps({"repos": repos}, ensure_ascii=False, indent=1) + "\n")

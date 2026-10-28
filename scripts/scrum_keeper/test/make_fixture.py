"""test/fixture/*.json を作り直す。--fixture で本物と同じ形の応答を読ませる時の見本。
使い方: python3 scripts/scrum_keeper/test/make_fixture.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from helpers import linear_fixture, raw_linear_issue, raw_project  # noqa: E402

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

    # Linear（RIT）の見本。今の Cycle の件は全部 待ち: ラベル付きで workable=0 → おかわり候補が出る。
    # RIT-44 は Estimate 無し・RIT-45 は Priority 無しで、それぞれ拾われる。
    cyc = "cyc-3"
    cycle_items = [
        raw_linear_issue(43, priority=1, estimate=3, state_type="started", labels=["待ち:局長"], cycle=cyc),
        raw_linear_issue(44, priority=2, state_type="unstarted", labels=["待ち:実機"], cycle=cyc),
        raw_linear_issue(45, priority=0, estimate=2, state_type="unstarted", labels=["待ち:局長"], cycle=cyc),
        raw_linear_issue(46, priority=3, estimate=5, state_type="completed", cycle=cyc),
        raw_linear_issue(47, priority=3, estimate=1, state_type="canceled", cycle=cyc),
    ]
    backlog = [
        raw_linear_issue(49, priority=1, state_type="unstarted"),               # おかわり 1 番目（Urgent）
        raw_linear_issue(48, priority=2, state_type="backlog", due="2026-11-10"),
        raw_linear_issue(54, priority=3, state_type="unstarted"),
        raw_linear_issue(50, priority=0, state_type="backlog"),                 # 優先度無しは最後
        raw_linear_issue(51, priority=2, state_type="backlog", labels=["待ち:局長"]),  # 待ちは引かん
        raw_linear_issue(52, priority=3, state_type="triage"),                  # 検討中は引かん
        raw_linear_issue(53, priority=4, state_type="canceled"),
    ]
    lin = linear_fixture(cycle_items, cycle_items + backlog)
    (out / "linear-rit.json").write_text(json.dumps(lin, ensure_ascii=False, indent=1) + "\n")

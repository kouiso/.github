"""試験の部品。本物の gh の応答と同じ形の dict を作り、本物の読み取り口（parse_board）に通す。"""
import datetime as dt
import json

from scrum_keeper.github_client import FakeClient, parse_board
from scrum_keeper.linear_client import REQUIRED_FIELDS, RIT_TEAM_ID, FakeLinearClient
from scrum_keeper.linear_sweep import main as linear_main
from scrum_keeper.scrum_keeper import main

D = dt.date
TITLE = "SECRET-TITLE-XYZ"  # 題名は一度も出てはいかん。出たら試験が落ちる
DESC = "scrum-board: v1 scrum-keeper:capacity=176pt count_from=2026-10-26"
CUR = "2026-11-02"  # 月曜
NOW_MON = "2026-11-02T00:05+09:00"
NOW_WED = "2026-11-04T12:00+09:00"


def monday_range(first="2026-10-19", n=11):
    d0 = D.fromisoformat(first)
    return [(d0 + dt.timedelta(days=7 * k)).isoformat() for k in range(n)]  # 10-19 から 12-28


def iter_obj(start):
    return {"id": f"it-{start}", "title": f"Sprint {start}", "startDate": start, "duration": 7}


def _opts(*names):
    return [{"id": f"opt-{n}", "name": n} for n in names]


LEGACY_FIELDS = [  # 盤から消える項目。在っても無くても keeper は読みも書きもせん
    {"id": "F_wait", "name": "Wait", "dataType": "SINGLE_SELECT", "options": _opts("W1", "W2", "W3")},
    {"id": "F_carry", "name": "Carryover", "dataType": "NUMBER"},
    {"id": "F_size", "name": "Size", "dataType": "SINGLE_SELECT", "options": _opts("XS", "S", "M")},
    {"id": "F_start", "name": "Start date", "dataType": "DATE"},
    {"id": "F_end", "name": "End date", "dataType": "DATE"},
]


def raw_project(items, weeks=None, description=DESC, title="Board", completed_before=CUR, legacy=False,
                est_field="Story Point", iter_titles=None):
    weeks = weeks if weeks is not None else monday_range()
    objs = [dict(iter_obj(w), title=(iter_titles or {}).get(w, f"Sprint {w}")) for w in weeks]
    done = [o for o in objs if o["startDate"] < completed_before]
    active = [o for o in objs if o["startDate"] >= completed_before]
    fields = [
        {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT", "options": _opts("Backlog", "In progress", "In review", "Done")},
        {"id": "F_prio", "name": "Priority", "dataType": "SINGLE_SELECT", "options": _opts("P0", "P1", "P2", "P3")},
        {"id": "F_est", "name": est_field, "dataType": "NUMBER"},
        {"id": "F_iter", "name": "Iteration", "dataType": "ITERATION",
         "configuration": {"duration": 7, "iterations": active, "completedIterations": done}},
    ] + (LEGACY_FIELDS if legacy else [])
    return {"id": "PVT_x", "title": title, "shortDescription": description,
            "fields": {"nodes": fields}, "items": {"pageInfo": {"hasNextPage": False}, "nodes": [raw_item(i) for i in items]}}


def raw_item(i):
    """i: n, repo, state, status, week, prio, est, est_field, updated, closed, parent, children, kind, legacy

    legacy: 盤から消える項目（Wait・Carryover・Start date・End date）の値。keeper が読まんことを試すため
    """
    repo = i.get("repo", "o/r")
    n = i["n"]
    kind = i.get("kind", "Issue")
    fv = []
    f = lambda name: {"field": {"name": name}}
    if i.get("status"):
        fv.append({"name": i["status"], **f("Status")})
    if i.get("prio"):
        fv.append({"name": i["prio"], **f("Priority")})
    if i.get("week"):
        fv.append({"iterationId": f"it-{i['week']}", **f("Iteration")})
    if i.get("est") is not None:
        fv.append({"number": i["est"], **f(i.get("est_field", "Story Point"))})
    if i.get("legacy"):
        fv += [{"name": "W1", **f("Wait")}, {"number": 3, **f("Carryover")}, {"name": "S", **f("Size")},
               {"date": "2026-10-26", **f("Start date")}, {"date": "2026-11-01", **f("End date")}]
    if kind == "DraftIssue":
        content = {"__typename": "DraftIssue", "title": TITLE}
    elif kind == "PullRequest":
        content = {"__typename": "PullRequest", "number": n, "title": TITLE}
    else:
        content = {"__typename": "Issue", "id": f"I_{repo}_{n}", "number": n, "title": TITLE,
                   "state": i.get("state", "OPEN"), "updatedAt": i.get("updated", "2026-11-01T00:00:00Z"),
                   "closedAt": i.get("closed"), "repository": {"nameWithOwner": repo},
                   "parent": ({"number": int(i["parent"].split("#")[1]), "repository": {"nameWithOwner": i["parent"].split("#")[0]}}
                              if i.get("parent") else None),
                   "subIssues": {"nodes": [{"number": int(c.split("#")[1]), "repository": {"nameWithOwner": c.split("#")[0]}}
                                           for c in i.get("children", [])]}}
    return {"id": f"PVTI_{repo}_{n}", "type": kind.upper(), "content": content, "fieldValues": {"nodes": fv}}


def make_board(items, owner="o", number=1, **kw):
    node = raw_project(items, **kw)
    return parse_board(node, owner, number, node["items"]["nodes"])


def make_client(*boards, repos=None):
    return FakeClient({b.ref: b for b in boards}, repos)


def run(tmp_path, client, *extra, board="o/1", job="all", now=NOW_MON, allow=None, verified="*"):
    if verified is not None:  # 反復の設定の書き直しは、測り終えた Project だけ（本番の仕組みと同じ）
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "state" / "extend-verified.txt").write_text(verified + "\n")
    target = ["--allow", str(allow)] if allow else (["--board", board] if board else [])  # None は extra で渡す
    args = target + [
        "--job", job, "--now", now, "--ledger-dir", str(tmp_path / "ledger"),
        "--state-dir", str(tmp_path / "state"), "--halt-file", str(tmp_path / "ledger" / "HALT"),
        "--capacity-board", board if board and not allow else "o/1", *extra]
    return main(args, client=client)


def ledger_lines(tmp_path):
    p = tmp_path / "ledger" / "K.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def item(client, ref, n, repo="o/r"):
    return next(i for i in client.boards[ref].items if i.kind == "Issue" and i.repo == repo and i.number == n)


def week(client, ref, n, repo="o/r"):
    b = client.boards[ref]
    it = b.iteration_by_id(item(client, ref, n, repo).iteration_id)
    return it.start.isoformat() if it else None


# ------------------------------------------------------------------ Linear 用の部品
STATE_NAMES = {"triage": "Triage", "backlog": "Backlog", "unstarted": "Todo", "started": "In Progress",
               "completed": "Done", "canceled": "Canceled"}


def raw_linear_issue(n, team="RIT", priority=0, estimate=None, due=None, state_type="started",
                     labels=(), cycle=None, updated="2026-11-01T00:00:00Z"):
    """Linear の Issue ノード。priority: 0 無し / 1 Urgent / 2 High / 3 Medium / 4 Low。"""
    return {"id": f"lin-{team}-{n}", "identifier": f"{team}-{n}", "number": n, "title": TITLE,
            "priority": priority, "estimate": estimate, "dueDate": due, "updatedAt": updated,
            "state": {"id": f"st-{state_type}", "name": STATE_NAMES[state_type], "type": state_type},
            "labels": {"nodes": [{"name": x} for x in labels]},
            "cycle": {"id": cycle} if cycle else None}


def linear_fixture(cycle_issues=(), team_issues=(), team="RIT", types=None, no_cycle=False):
    """--fixture と FakeLinearClient の元になる dict。types を欠かせば schema の確かめが落ちる。"""
    types = types if types is not None else {t: sorted(v) + ["createdAt"] for t, v in REQUIRED_FIELDS.items()}
    cycle = None if no_cycle else {"id": "cyc-3", "number": 3, "name": "Cycle 3",
                                   "startsAt": "2026-11-02T00:00:00Z", "endsAt": "2026-11-09T00:00:00Z",
                                   "issues": list(cycle_issues)}
    return {"linear": {"types": types, "teams": [{"id": RIT_TEAM_ID, "key": team, "name": team}],
                       "activeCycles": {team: cycle}, "issues": {team: list(team_issues)}}}


def make_linear_client(data):
    lin = data["linear"]
    return FakeLinearClient(lin.get("types"), lin.get("teams"), lin.get("activeCycles"), lin.get("issues"))


def run_linear(tmp_path, client, *extra, team="RIT", now=NOW_WED):
    args = ["--team", team, "--now", now, "--ledger-dir", str(tmp_path / "ledger"),
            "--state-dir", str(tmp_path / "state"), "--halt-file", str(tmp_path / "ledger" / "HALT"),
            *extra]
    return linear_main(args, client=client)

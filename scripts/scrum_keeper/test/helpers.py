"""試験の部品。本物の gh の応答と同じ形の dict を作り、本物の読み取り口（parse_board）に通す。"""
import datetime as dt
import json

from scrum_keeper.github_client import FakeClient, parse_board
from scrum_keeper.scrum_keeper import main

D = dt.date
TITLE = "SECRET-TITLE-XYZ"  # 題名は一度も出てはいかん。出たら試験が落ちる
DESC = "scrum-board: v1 scrum-keeper:capacity=176h count_from=2026-10-26"
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


def raw_project(items, weeks=None, description=DESC, title="Board", completed_before=CUR):
    weeks = weeks if weeks is not None else monday_range()
    done = [iter_obj(w) for w in weeks if w < completed_before]
    active = [iter_obj(w) for w in weeks if w >= completed_before]
    fields = [
        {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT", "options": _opts("Backlog", "In Progress", "In Review", "Done")},
        {"id": "F_prio", "name": "Priority", "dataType": "SINGLE_SELECT", "options": _opts("P0", "P1", "P2")},
        {"id": "F_wait", "name": "Wait", "dataType": "SINGLE_SELECT", "options": _opts("W1", "W2", "W3")},
        {"id": "F_est", "name": "Estimate", "dataType": "NUMBER"},
        {"id": "F_carry", "name": "Carryover", "dataType": "NUMBER"},
        {"id": "F_end", "name": "End date", "dataType": "DATE"},
        {"id": "F_iter", "name": "Iteration", "dataType": "ITERATION",
         "configuration": {"duration": 7, "iterations": active, "completedIterations": done}},
    ]
    return {"id": "PVT_x", "title": title, "shortDescription": description,
            "fields": {"nodes": fields}, "items": {"pageInfo": {"hasNextPage": False}, "nodes": [raw_item(i) for i in items]}}


def raw_item(i):
    """i: n, repo, state, status, week, prio, wait, est, end, carry, updated, closed, parent, children, kind"""
    repo = i.get("repo", "o/r")
    n = i["n"]
    kind = i.get("kind", "Issue")
    fv = []
    f = lambda name: {"field": {"name": name}}
    if i.get("status"):
        fv.append({"name": i["status"], **f("Status")})
    if i.get("prio"):
        fv.append({"name": i["prio"], **f("Priority")})
    if i.get("wait"):
        fv.append({"name": i["wait"], **f("Wait")})
    if i.get("week"):
        fv.append({"iterationId": f"it-{i['week']}", **f("Iteration")})
    if i.get("est") is not None:
        fv.append({"number": i["est"], **f("Estimate")})
    if i.get("carry") is not None:
        fv.append({"number": i["carry"], **f("Carryover")})
    if i.get("end"):
        fv.append({"date": i["end"], **f("End date")})
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


def run(tmp_path, client, *extra, board="o/1", job="all", now=NOW_MON, allow=None):
    args = (["--allow", str(allow)] if allow else ["--board", board]) + [
        "--job", job, "--now", now, "--ledger-dir", str(tmp_path / "ledger"),
        "--state-dir", str(tmp_path / "state"), "--halt-file", str(tmp_path / "ledger" / "HALT"),
        "--capacity-board", board if not allow else "o/1", *extra]
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

import datetime as dt
import json
import plistlib
from pathlib import Path

import pytest
from graphql import parse

from scripts.exec_plan import exec_plan_from_boards as ep


MONDAY = dt.date(2026, 10, 26)
REPO = "example/tasks"


def project(field_name="Iteration"):
    return {
        "id": "project", "title": "Test board",
        "fields": {"nodes": [{
            "id": "iteration-field", "name": field_name,
            "configuration": {
                "completedIterations": [{"id": "old", "title": "Sprint 1",
                                          "startDate": "2026-10-19", "duration": 7}],
                "iterations": [{"id": "current", "title": "Sprint 2",
                                "startDate": "2026-10-26", "duration": 7},
                               {"id": "next", "title": "Sprint 3",
                                "startDate": "2026-11-02", "duration": 7}],
            },
        }, {"id": "worker", "name": "やる人"}]},
        "prs": {"totalCount": 3}, "drafts": {"totalCount": 2},
    }


def item(number=1, iteration="current", body=""):
    return {
        "id": f"item-{number}", "type": "ISSUE",
        "content": {"__typename": "Issue", "number": number,
                    "title": f"Task {number}", "state": "OPEN", "body": body,
                    "repository": {"nameWithOwner": REPO}},
        "fieldValues": {"nodes": [
            {"iterationId": iteration, "field": {"name": "Iteration"}},
            {"name": "Backlog", "field": {"name": "Status"}},
        ]},
    }


class Shell(ep.GhShell):
    def __init__(self, node=None, pages=None):
        super().__init__()
        self.node = project() if node is None else node
        self.pages = [item()] if pages is None else pages
        self.queries = []

    def check_auth(self):
        return True

    def probe_budget(self):
        pass

    def rest_yml(self, repo):
        return "ok", "worker_default: code\n"

    def dep_state(self, repo, number):
        return repo, number, "CLOSED" if number == 9 else "OPEN"

    def _gql(self, query, **variables):
        self.queries.append((query, variables))
        if query == ep.BOARD_META_Q:
            node = self.node
        else:
            offset = 1 if variables["after"] else 0
            nodes = self.pages[offset:offset + 1]
            query_filter = variables["q"]
            if query_filter:
                cfg = self.node["fields"]["nodes"][0]["configuration"]
                iterations = cfg["completedIterations"] + cfg["iterations"]
                target = next(it["id"] for it in iterations
                              if json.dumps(it["title"]) in query_filter)
                nodes = [nd for nd in nodes
                         if nd["fieldValues"]["nodes"][0]["iterationId"] == target]
            node = {"items": {"nodes": nodes, "pageInfo": {
                "hasNextPage": offset == 0 and len(self.pages) > 1,
                "endCursor": "page-2"}}}
        return {"data": {"repositoryOwner": {"projectV2": node}}}


@pytest.mark.parametrize("query", [ep.BOARD_META_Q, ep.BOARD_ITEMS_Q])
def test_queries_are_valid_graphql(query):
    parse(query)


@pytest.mark.parametrize("monday,title", [
    (MONDAY, "Sprint 2"),
    (dt.date(2026, 10, 19), "Sprint 1"),
    (dt.date(2026, 11, 2), "Sprint 3"),
])
def test_filter_uses_resolved_week_not_server_clock(monday, title):
    shell = Shell()
    shell.read_board("example", 1, monday)
    assert shell.queries[1][1]["q"] == f'iteration:"{title}"'


def test_filter_uses_custom_field_name():
    shell = Shell(project("Sprint"))
    shell.read_board("example", 1, MONDAY)
    assert shell.queries[1][1]["q"] == 'sprint:"Sprint 2"'


@pytest.mark.parametrize("field_name,monday", [
    ("Team Sprint", MONDAY),
    ("Iteration", dt.date(2027, 1, 4)),
])
def test_unsafe_or_missing_filter_falls_back_to_unfiltered_read(field_name, monday):
    shell = Shell(project(field_name))
    shell.read_board("example", 1, monday)
    assert shell.queries[1][1]["q"] is None


def test_metadata_is_fetched_once_and_items_are_paginated():
    shell = Shell(pages=[item(1), item(2)])
    board = shell.read_board("example", 1, MONDAY)
    assert [w.number for w in board.items] == [1, 2]
    assert [q for q, _ in shell.queries].count(ep.BOARD_META_Q) == 1
    assert shell.queries[2][1]["after"] == "page-2"


def test_issue_body_remains_in_query_and_dependency_parser():
    assert "createdAt body repository" in ep.BOARD_ITEMS_Q
    work = ep.parse_item_node("example/1", item(body="Depends on #9"))
    assert work.deps == [f"{REPO}#9"]


@pytest.mark.parametrize("iteration,now,sprint,target", [
    ("current", "2026-10-28T00:00:00Z", "2026-10-26", "current"),
    ("next", "2026-10-28T00:00:00Z", "2026-11-02", "next"),
    ("2026-10-19", "2026-11-02T00:00:00Z", "2026-10-19", "old"),
])
def test_main_preserves_rows_dependencies_and_board_wide_counts(
        monkeypatch, tmp_path, iteration, now, sprint, target):
    shell = Shell(pages=[item(iteration=target, body="Depends on #9, #10")])
    monkeypatch.setattr(ep, "GhShell", lambda: shell)
    rc = ep.main(["--board", "example/1", "--out", str(tmp_path),
                  "--halt-file", str(tmp_path / "HALT"),
                  "--iteration", iteration, "--now", now])
    assert rc == 0
    plan = json.loads((tmp_path / f"{sprint}.json").read_text())
    assert len(plan["rows"]) == 1
    assert plan["rows"][0]["deps_open"] == [f"{REPO}#10"]
    assert plan["boards"][0]["counts"]["pr"] == 3
    assert plan["boards"][0]["counts"]["draft"] == 2
    assert (tmp_path / f"{sprint}.md").is_file()


def test_budget_failure_preserves_existing_plan(monkeypatch, tmp_path):
    shell = Shell()
    shell.graphql_cost = 571
    monkeypatch.setattr(ep, "GhShell", lambda: shell)
    previous = tmp_path / "2026-10-26.json"
    previous.write_text("previous plan")
    rc = ep.main(["--board", "example/1", "--out", str(tmp_path),
                  "--halt-file", str(tmp_path / "HALT"),
                  "--now", "2026-10-28T00:00:00Z"])
    assert rc == 8
    assert previous.read_text() == "previous plan"


@pytest.mark.parametrize("payload,kind", [
    ({"state": "open", "is_pr": False}, "Issue"),
    ({"state": "open", "is_pr": True}, "PullRequest"),
])
def test_rest_dependency_lookup_preserves_resource_type(monkeypatch, payload, kind):
    shell = ep.GhShell()
    monkeypatch.setattr(ep.subprocess, "run", lambda *args, **kwargs:
                        ep.subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    work = ep.dep_stub(f"{REPO}#10", *shell.dep_state(REPO, 10))
    assert work.kind == kind
    assert work.state == "OPEN"


def test_unavailable_dependency_remains_conservatively_open(monkeypatch):
    shell = ep.GhShell()
    monkeypatch.setattr(ep.subprocess, "run", lambda *args, **kwargs:
                        ep.subprocess.CompletedProcess([], 1, "", "not found"))
    work = ep.dep_stub(f"{REPO}#10", *shell.dep_state(REPO, 10))
    assert work.kind == "Issue"
    assert work.state == "OPEN"


def test_filtered_out_pull_request_is_not_an_issue_dependency(monkeypatch, tmp_path):
    shell = Shell(pages=[item(body="Depends on #10")])
    monkeypatch.setattr(shell, "dep_state", lambda repo, n: (repo, n, "OPEN", "PullRequest"))
    monkeypatch.setattr(ep, "GhShell", lambda: shell)
    assert ep.main(["--board", "example/1", "--out", str(tmp_path),
                    "--halt-file", str(tmp_path / "HALT"),
                    "--now", "2026-10-28T00:00:00Z"]) == 0
    row = json.loads((tmp_path / "2026-10-26.json").read_text())["rows"][0]
    assert row["deps_open"] == []
    assert "deps-not-in-sprint" not in row["marks"]


def test_fixture_retains_legacy_counts(tmp_path):
    node = project()
    del node["prs"], node["drafts"]
    node["items"] = {"nodes": [item(), {"content": {"__typename": "PullRequest"}},
                                {"content": {"__typename": "DraftIssue"}}]}
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "board.json").write_text(json.dumps({
        "owner": "example", "number": 1, "project": node,
        "repos": {REPO: {"yml": "worker_default: code\n"}},
    }))
    out = tmp_path / "out"
    assert ep.main(["--board", "example/1", "--out", str(out),
                    "--fixture", str(fixture), "--halt-file", str(tmp_path / "HALT"),
                    "--now", "2026-10-28T00:00:00Z"]) == 0
    counts = json.loads((out / "2026-10-26.json").read_text())["boards"][0]["counts"]
    assert counts["pr"] == counts["draft"] == 1


def test_schedule_matches_rate_budget():
    plist = Path(ep.__file__).parent / "launchd" / "exec-plan.plist"
    with plist.open("rb") as stream:
        interval = plistlib.load(stream)["StartInterval"]
    assert interval * ep.RATE_PER_HOUR == 3600
    assert ep.RATE_BASE + ep.RATE_PER_HOUR * 540 <= ep.RATE_BUDGET == 2500


def test_missing_board_fails_without_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "GhShell", lambda: Shell(node={}))
    assert ep.main(["--board", "example/1", "--out", str(tmp_path),
                    "--halt-file", str(tmp_path / "HALT")]) == 4
    assert not list(tmp_path.glob("*.json"))

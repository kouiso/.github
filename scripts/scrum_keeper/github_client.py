"""gh api graphql の薄い包み。読みと書きを 1 つの面にまとめ、試験では FakeClient に差し替える。

項目の名前で引く。id は固定で持たん。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .model import Board, Item, Iteration, parse_ts


class GhError(Exception):
    pass


def gh_path() -> str:
    env = os.environ.get("SCRUM_KEEPER_GH")
    if env:
        return env
    return "/opt/homebrew/bin/gh" if Path("/opt/homebrew/bin/gh").exists() else (shutil.which("gh") or "gh")


def gh_auth_ok() -> bool:
    p = subprocess.run([gh_path(), "auth", "status"], capture_output=True, text=True)
    return p.returncode == 0


def _lit(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# ------------------------------------------------------------------ 読み取りの形
BOARD_Q = """query($login:String!,$n:Int!,$after:String){repositoryOwner(login:$login){... on ProjectV2Owner{projectV2(number:$n){
 id title shortDescription
 fields(first:50){nodes{
  ... on ProjectV2FieldCommon{id name dataType}
  ... on ProjectV2SingleSelectField{options{id name}}
  ... on ProjectV2IterationField{configuration{duration
    iterations{id title startDate duration} completedIterations{id title startDate duration}}}}}
 items(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{id type
  content{__typename
   ... on Issue{id number state updatedAt closedAt repository{nameWithOwner}
     parent{number repository{nameWithOwner}} subIssues(first:50){nodes{number repository{nameWithOwner}}}}
   ... on PullRequest{number}}
  fieldValues(first:25){nodes{
   ... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldIterationValue{iterationId field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2FieldCommon{name}}}}}}}}}}}"""

ITEM_FIELDS_Q = """query($id:ID!){node(id:$id){... on ProjectV2Item{fieldValues(first:25){nodes{
  ... on ProjectV2ItemFieldIterationValue{iterationId field{... on ProjectV2FieldCommon{name}}}
  ... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}}}}}}}"""

STATUS_UPDATES_Q = """query($id:ID!){node(id:$id){... on ProjectV2{statusUpdates(first:30,orderBy:{field:CREATED_AT,direction:DESC}){nodes{body}}}}}"""

REPOS_Q = """query($login:String!,$after:String){repositoryOwner(login:$login){repositories(first:100,after:$after,isArchived:false){
 pageInfo{hasNextPage endCursor} nodes{nameWithOwner object(expression:"HEAD:.github/scrum-board.yml"){... on Blob{text}}}}}}"""

ISSUES_Q = """query($o:String!,$r:String!,$after:String){repository(owner:$o,name:$r){issues(states:OPEN,first:100,after:$after){
 pageInfo{hasNextPage endCursor} nodes{id number}}}}"""


def _key(repo: dict, number: int) -> str:
    return f'{repo["nameWithOwner"]}#{number}'


def parse_board(node: dict, owner: str, number: int, item_nodes: list[dict]) -> Board:
    """projectV2 のノード（項目の頁は item_nodes に足した物）から Board を作る。"""
    board = Board(owner=owner, number=number, project_id=node["id"], title=node.get("title") or "",
                  description=node.get("shortDescription") or "")
    for f in node["fields"]["nodes"]:
        if not f or "name" not in f:
            continue
        board.field_ids[f["name"]] = f["id"]
        if "options" in f:
            board.status_options[f["name"]] = {o["name"]: o["id"] for o in f["options"]}
        if "configuration" in f and f["configuration"]:
            cfg = f["configuration"]
            board.iteration_duration = cfg.get("duration") or 7
            seen = set()
            for it in (cfg.get("completedIterations") or []) + (cfg.get("iterations") or []):
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                board.iterations.append(Iteration(it["id"], it["title"], dt.date.fromisoformat(it["startDate"]),
                                                  it.get("duration") or 7))
    board.iterations.sort(key=lambda i: i.start)
    if board.iterations:
        board.iteration_start = board.iterations[0].start  # 設定の startDate は読めんので最初の週で代用
    for nd in item_nodes:
        c = nd.get("content") or {}
        kind = c.get("__typename") or "DraftIssue"
        it = Item(item_id=nd["id"], kind=kind)
        if kind == "Issue":
            it.repo = c["repository"]["nameWithOwner"]
            it.number = c["number"]
            it.node_id = c.get("id") or ""
            it.state = c.get("state") or ""
            it.updated_at = parse_ts(c.get("updatedAt"))
            it.closed_at = parse_ts(c.get("closedAt"))
            if c.get("parent"):
                it.parent = _key(c["parent"]["repository"], c["parent"]["number"])
            it.children = [_key(s["repository"], s["number"]) for s in ((c.get("subIssues") or {}).get("nodes") or [])]
        elif kind == "PullRequest":
            it.number = c.get("number") or 0
        for fv in (nd.get("fieldValues") or {}).get("nodes") or []:
            fname = ((fv or {}).get("field") or {}).get("name")
            if not fname:
                continue
            if "iterationId" in fv:
                it.iteration_id = fv["iterationId"]
            elif "name" in fv:
                {"Status": lambda v: setattr(it, "status", v), "Priority": lambda v: setattr(it, "priority", v),
                 "Wait": lambda v: setattr(it, "wait", v)}.get(fname, lambda v: None)(fv["name"])
            elif "number" in fv:
                if fname == "Estimate":
                    it.estimate = fv["number"]
                elif fname == "Carryover":
                    it.carryover = fv["number"]
            elif "date" in fv and fname == "End date":
                it.end_date = dt.date.fromisoformat(fv["date"])
        board.items.append(it)
    return board


def parse_yml_target(text: str) -> tuple[str, int] | None:
    """.github/scrum-board.yml の owner と project_number だけを読む（PyYAML に頼らん）。"""
    o = re.search(r"^owner:\s*['\"]?([\w.-]+)['\"]?\s*$", text or "", re.M)
    n = re.search(r"^project_number:\s*['\"]?(\d+)['\"]?\s*$", text or "", re.M)
    if not (o and n):
        return None
    return o.group(1), int(n.group(1))


class GhClient:
    """本物。gh の認証をそのまま使う。"""

    def __init__(self):
        self.calls = 0

    def _gql(self, query: str, **variables) -> dict:
        args = [gh_path(), "api", "graphql", "-f", "query=" + query]
        for k, v in variables.items():
            if v is None:
                continue
            args += ["-F" if isinstance(v, (int, bool)) else "-f", f"{k}={v}"]
        self.calls += 1
        p = subprocess.run(args, capture_output=True, text=True)
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise GhError(f"gh exit {p.returncode}: unparseable output")
        if d.get("errors") and not d.get("data"):
            raise GhError("graphql error: " + str(d["errors"][0].get("type") or d["errors"][0].get("message", ""))[:120])
        if p.returncode != 0:
            raise GhError(f"gh exit {p.returncode}")
        return d

    # ---- 読み ----
    def read_board(self, owner: str, number: int) -> Board:
        item_nodes: list[dict] = []
        after = None
        node = None
        while True:
            d = self._gql(BOARD_Q, login=owner, n=number, after=after)
            owner_node = d["data"]["repositoryOwner"]
            if not owner_node or not owner_node.get("projectV2"):
                raise GhError(f"board {owner}/{number} not found")
            proj = owner_node["projectV2"]
            node = node or proj
            page = proj["items"]
            item_nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return parse_board(node, owner, number, item_nodes)

    def read_board_head(self, owner: str, number: int) -> tuple[str, str]:
        d = self._gql("query($login:String!,$n:Int!){repositoryOwner(login:$login){... on ProjectV2Owner{"
                      "projectV2(number:$n){title shortDescription}}}}", login=owner, n=number)
        node = (d["data"]["repositoryOwner"] or {}).get("projectV2")
        if not node:
            raise GhError(f"board {owner}/{number} not found")
        return node["title"] or "", node["shortDescription"] or ""

    def read_item_fields(self, board: Board, item: Item) -> tuple[str | None, int]:
        d = self._gql(ITEM_FIELDS_Q, id=item.item_id)
        it_id, carry = None, 0
        for fv in d["data"]["node"]["fieldValues"]["nodes"]:
            fname = ((fv or {}).get("field") or {}).get("name")
            if fname == "Iteration" and "iterationId" in fv:
                it_id = fv["iterationId"]
            elif fname == "Carryover" and "number" in fv:
                carry = int(fv["number"] or 0)
        return it_id, carry

    def list_status_updates(self, board: Board) -> list[str]:
        d = self._gql(STATUS_UPDATES_Q, id=board.project_id)
        return [n["body"] or "" for n in d["data"]["node"]["statusUpdates"]["nodes"]]

    def list_mapped_repos(self, board: Board) -> list[str]:
        out, after = [], None
        while True:
            d = self._gql(REPOS_Q, login=board.owner, after=after)
            page = d["data"]["repositoryOwner"]["repositories"]
            for r in page["nodes"]:
                text = (r.get("object") or {}).get("text") or ""
                tgt = parse_yml_target(text)
                if tgt and tgt[0].lower() == board.owner.lower() and tgt[1] == board.number:
                    out.append(r["nameWithOwner"])
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]

    def list_open_issues(self, repo: str) -> list[tuple[int, str]]:
        o, r = repo.split("/")
        out, after = [], None
        while True:
            d = self._gql(ISSUES_Q, o=o, r=r, after=after)
            page = d["data"]["repository"]["issues"]
            out += [(n["number"], n["id"]) for n in page["nodes"]]
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]

    # ---- 書き ----
    def set_item_fields(self, board: Board, item: Item, iteration_id: str | None, carryover: int | None) -> None:
        parts = []
        if iteration_id:
            parts.append(self._set_value("a", board, item, "Iteration", f"{{iterationId:{_lit(iteration_id)}}}"))
        if carryover is not None:
            parts.append(self._set_value("b", board, item, "Carryover", f"{{number:{carryover}}}"))
        self._gql("mutation{" + " ".join(parts) + "}")

    @staticmethod
    def _set_value(alias: str, board: Board, item: Item, fname: str, value: str) -> str:
        fid = board.field_ids.get(fname)
        if not fid:
            raise GhError(f"field {fname} missing on {board.ref}")
        return (f'{alias}:updateProjectV2ItemFieldValue(input:{{projectId:{_lit(board.project_id)},'
                f'itemId:{_lit(item.item_id)},fieldId:{_lit(fid)},value:{value}}}){{projectV2Item{{id}}}}')

    def update_iteration_config(self, board: Board, iterations: list[Iteration]) -> None:
        fid = board.field_ids.get("Iteration")
        if not fid or not board.iteration_start:
            raise GhError("iteration field missing")
        rows = []
        for it in sorted(iterations, key=lambda i: i.start):
            idpart = f"id:{_lit(it.id)}," if it.id and not it.id.startswith("new:") else ""
            rows.append(f"{{{idpart}title:{_lit(it.title)},startDate:{_lit(it.start.isoformat())},duration:{it.duration}}}")
        self._gql(f'mutation{{updateProjectV2Field(input:{{fieldId:{_lit(fid)},iterationConfiguration:{{'
                  f'startDate:{_lit(board.iteration_start.isoformat())},duration:{board.iteration_duration},'
                  f'iterations:[{",".join(rows)}]}}}}){{projectV2Field{{... on ProjectV2IterationField{{id}}}}}}}}')

    def add_item_backlog(self, board: Board, content_node_id: str) -> str:
        d = self._gql(f'mutation{{addProjectV2ItemById(input:{{projectId:{_lit(board.project_id)},'
                      f'contentId:{_lit(content_node_id)}}}){{item{{id}}}}}}')
        item_id = d["data"]["addProjectV2ItemById"]["item"]["id"]
        opt = (board.status_options.get("Status") or {}).get("Backlog")
        sid = board.field_ids.get("Status")
        if opt and sid:
            self._gql(f'mutation{{updateProjectV2ItemFieldValue(input:{{projectId:{_lit(board.project_id)},'
                      f'itemId:{_lit(item_id)},fieldId:{_lit(sid)},value:{{singleSelectOptionId:{_lit(opt)}}}}}){{projectV2Item{{id}}}}}}')
        return item_id

    def set_description(self, board: Board, text: str) -> None:
        self._gql(f'mutation{{updateProjectV2(input:{{projectId:{_lit(board.project_id)},'
                  f'shortDescription:{_lit(text)}}}){{projectV2{{id}}}}}}')

    def create_status_update(self, board: Board, body: str, start: dt.date, target: dt.date, status: str) -> None:
        self._gql(f'mutation{{createProjectV2StatusUpdate(input:{{projectId:{_lit(board.project_id)},'
                  f'body:{_lit(body)},startDate:{_lit(start.isoformat())},targetDate:{_lit(target.isoformat())},'
                  f'status:{status}}}){{statusUpdate{{id}}}}}}')


class FakeClient:
    """試験と --fixture 用。Board を丸ごとメモリに持ち、書き込みは Board を直接書き換えて記録する。"""

    def __init__(self, boards: dict[str, Board], repos: dict[str, dict] | None = None):
        self.boards = boards  # "owner/n" -> Board
        self.repos = repos or {}  # repo -> {"yml": str, "issues": [(n, node_id, kind)]}
        self.status_updates: dict[str, list[str]] = {}
        self.writes: list[tuple] = []
        self.reads = 0
        self.before_read_item = None  # 競合の再現用フック: f(board, item)
        self.fail_writes = False
        self.auth_ok = True

    @classmethod
    def from_fixture_dir(cls, directory: Path) -> "FakeClient":
        boards, repos = {}, {}
        for p in sorted(Path(directory).glob("*.json")):
            d = json.loads(p.read_text())
            if "project" in d:
                owner, num = d["owner"], d["number"]
                boards[f"{owner}/{num}"] = parse_board(d["project"], owner, num, d["project"]["items"]["nodes"])
            if "repos" in d:
                repos.update(d["repos"])
        return cls(boards, repos)

    def read_board(self, owner: str, number: int) -> Board:
        self.reads += 1
        b = self.boards.get(f"{owner}/{number}")
        if b is None:
            raise GhError(f"board {owner}/{number} not found")
        return b.copy()

    def read_board_head(self, owner: str, number: int) -> tuple[str, str]:
        b = self.boards.get(f"{owner}/{number}")
        if b is None:
            raise GhError(f"board {owner}/{number} not found")
        return b.title, b.description

    def _live(self, board: Board) -> Board:
        return self.boards[board.ref]

    def _live_item(self, board: Board, item: Item) -> Item:
        for it in self._live(board).items:
            if it.item_id == item.item_id:
                return it
        raise GhError("item not found")

    def read_item_fields(self, board: Board, item: Item):
        live = self._live_item(board, item)
        if self.before_read_item:
            self.before_read_item(self._live(board), live)
        return live.iteration_id, live.carry

    def list_status_updates(self, board: Board) -> list[str]:
        return list(self.status_updates.get(board.ref, []))

    def list_mapped_repos(self, board: Board) -> list[str]:
        out = []
        for repo, info in self.repos.items():
            tgt = parse_yml_target(info.get("yml", ""))
            if tgt and tgt[0].lower() == board.owner.lower() and tgt[1] == board.number:
                out.append(repo)
        return out

    def list_open_issues(self, repo: str):
        return [(n, nid) for n, nid, kind in self.repos[repo]["issues"] if kind == "Issue"]

    def _check(self):
        if self.fail_writes:
            raise GhError("injected failure")

    def set_item_fields(self, board, item, iteration_id, carryover):
        self._check()
        live = self._live_item(board, item)
        if iteration_id:
            live.iteration_id = iteration_id
        if carryover is not None:
            live.carryover = carryover
        self.writes.append(("item", board.ref, item.key, iteration_id, carryover))

    def update_iteration_config(self, board, iterations):
        self._check()
        live = self._live(board)
        old = {i.id: i for i in live.iterations}
        new = []
        for it in sorted(iterations, key=lambda i: i.start):
            if it.id and not it.id.startswith("new:"):
                new.append(Iteration(it.id, it.title, it.start, it.duration))
            else:
                new.append(Iteration(f"it-{it.start.isoformat()}", it.title, it.start, it.duration))
        live.iterations = new
        self.writes.append(("iterations", board.ref, [i.start.isoformat() for i in new if i.id not in old]))

    def add_item_backlog(self, board, content_node_id):
        self._check()
        live = self._live(board)
        repo, number = content_node_id.split("|")
        it = Item(item_id=f"PVTI_{repo}_{number}", kind="Issue", repo=repo, number=int(number), node_id=content_node_id,
                  state="OPEN", status="Backlog")
        live.items.append(it)
        self.writes.append(("add", board.ref, it.key))
        return it.item_id

    def set_description(self, board, text):
        self._check()
        self._live(board).description = text
        self.writes.append(("description", board.ref, text))

    def create_status_update(self, board, body, start, target, status):
        self._check()
        self.status_updates.setdefault(board.ref, []).insert(0, body)
        self.writes.append(("status_update", board.ref, body.splitlines()[0]))

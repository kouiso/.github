#!/usr/bin/env python3
"""GitHub Project（と Linear）を読んで Claude の実行プランを作る（plan/05 付録 2A–2C）。

使い方:
  本番: python3 exec_plan_from_boards.py --iteration current \\
          --allow ~/.local/state/scrum-keeper/allow.txt \\
          --linear-team RIT --linear-key-ref 'op://RITMO/Linear API key - scrum-keeper/credential' \\
          --out ~/.claude/plans/exec/
  試験: --board <owner>/<番号> --out <dir> [--now <ISO8601>] [--fixture <dir>]
  1 枚: --html <path> --session-id <id>
  月曜の書き込み: --fill-empty --write-via <gh_write.sh> --pre <dir>

終了コード: 0 作れた / 1 HALT / 2 引数 / 3 GitHub 不可 / 4 Project の 1 個でも不可 /
           6 Linear だけ不可 / 7 --fill-empty 途中停止 / 8 量不足

秘密は入れん。Linear の鍵は実行の時だけ `op read` で読み、表示も保存もせん。
本番の dir に書く物は全部 exec-plan.lock を取ってから（呼び出し側が既に持っとる時は
EXEC_PLAN_LOCK_HELD=1 を立てる。立てんと同じ鍵を取り直して止まる）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9), "JST")
SCHEMA = "exec-plan/v1"
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
DEVICE_WAIT = "実機"
WRITE_DOERS = {"Devin", "Sonnet", DEVICE_WAIT, "設計"}
HALT_DEFAULT = Path.home() / ".claude/plans/zero-open-20261025/ledger/HALT"
LINEAR_API = "https://api.linear.app/graphql"
LINEAR_DEFAULT_REPO = "kouiso/character-chat"
LINEAR_PRIORITY = {1: "P0", 2: "P1", 3: "P2", 4: "P3"}

# 量の決まり（plan/05 付録 2A）。残りが床未満なら rc 8、出来た回も 1360 + 4×cost <= 2500。
RATE_FLOOR = 1500
RATE_BASE = 1360
RATE_PER_HOUR = 4
# 実盤 1 枚で cost 700 程度（hm 盤は約 725）。15 分周期の 4 回/時で 2900 点/時。
# GitHub の上限 5000 点/時から床 1500 を引いた残量に合わせるため 5000 に置く。
RATE_BUDGET = 5000
REST_MAX = 500

# 項目の名前の既定。repo の .github/scrum-board.yml の fields: で上書きできる（付録 2A）。
FIELD_DEFAULTS = {
    "status": "Status", "priority": "Priority", "size": "Size", "estimate": "Estimate",
    "iteration": "Iteration", "start": "Start date", "end": "End date",
    "wait": "Wait", "carryover": "Carryover", "assignee": "やる人",
}

DEP_MARKERS = ("Blocked by", "Depends on", "先に")
CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
REF_URL_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
REF_FULL_RE = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)")
REF_NUM_RE = re.compile(r"(?<![\w/#])#(\d+)")
LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s")
RATE_LIMIT_RE = re.compile(r"rate limit|secondary rate|\b429\b|abuse|RATE_LIMITED", re.I)


# ------------------------------------------------------------------ 例外（終了コードに対応）
class GhError(Exception):
    """rc 3: GitHub が読めん。"""


class GhRateLimit(GhError):
    """rc 8: 上限の返事。"""


class ProjectMiss(GhError):
    """rc 4: その Project だけ読めん（見つからん・権限が無い）。"""


class LinearError(Exception):
    """rc 6: Linear だけ読めん。"""


class FillAbort(Exception):
    """rc 7: --fill-empty の途中停止。"""


# ------------------------------------------------------------------ 小さな部品
def to_jst(t: dt.datetime) -> dt.datetime:
    if t.tzinfo is None:
        raise ValueError("naive datetime is not allowed")
    return t.astimezone(JST)


def parse_now(text: str | None) -> dt.datetime:
    if not text:
        return dt.datetime.now(JST)
    return to_jst(dt.datetime.fromisoformat(text.replace("Z", "+00:00")))


def parse_ts(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def gh_path() -> str:
    env = os.environ.get("EXEC_PLAN_GH")
    if env:
        return env
    return "/opt/homebrew/bin/gh" if Path("/opt/homebrew/bin/gh").exists() else (shutil.which("gh") or "gh")


def op_path() -> str | None:
    env = os.environ.get("EXEC_PLAN_OP")
    if env:
        return env
    if Path("/opt/homebrew/bin/op").exists():
        return "/opt/homebrew/bin/op"
    return shutil.which("op")


# ------------------------------------------------------------------ 読み取りの形
BOARD_Q = """query($login:String!,$n:Int!,$after:String){repositoryOwner(login:$login){... on ProjectV2Owner{projectV2(number:$n){
 id title
 fields(first:50){nodes{
  ... on ProjectV2FieldCommon{id name dataType}
  ... on ProjectV2SingleSelectField{options{id name}}
  ... on ProjectV2IterationField{configuration{duration
    iterations{id title startDate duration} completedIterations{id title startDate duration}}}}}
 items(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{id type
  content{__typename
   ... on Issue{id number title url state createdAt updatedAt closedAt repository{nameWithOwner}
     body
     parent{number repository{nameWithOwner}}
     subIssues(first:50){nodes{number repository{nameWithOwner}}}
     blockedBy(first:20){nodes{number state repository{nameWithOwner}}}
     closedByPullRequestsReferences(first:10,userLinkedOnly:false){nodes{number state repository{nameWithOwner}}}}
   ... on PullRequest{number repository{nameWithOwner}}
   ... on DraftIssue{title}}
  fieldValues(first:25){nodes{
   ... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldIterationValue{iterationId field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}}
   ... on ProjectV2ItemFieldUserValue{users(first:3){nodes{login}} field{... on ProjectV2FieldCommon{name}}}}
 }}}}}}
 rateLimit{cost remaining}}"""

RATE_Q = "query{rateLimit{remaining}}"

# Linear（付録 2B）。項目名は未試験なので __type で合わせてから読む。
LINEAR_TEAM_Q = "query($k:String!){teams(filter:{key:{eq:$k}}){nodes{id key name}}}"
LINEAR_CYCLE_Q = "query($id:String!){team(id:$id){cycles(filter:{isActive:true},first:3){nodes{id name number}}}}"
LINEAR_TYPE_Q = "query($n:String!){__type(name:$n){fields{name}}}"
LINEAR_ISSUES_Q = """query($id:String!,$after:String){cycle(id:$id){
 issues(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{
  id identifier title url priority estimate createdAt
  state{name type} labels{nodes{name}} %OPTIONAL%}}}}"""


@dataclass
class Work:
    """プランの行の元。GitHub の課題か Linear の課題。題名は写し（ローカルのファイル）にだけ出す。"""

    board: str = ""           # "owner/番号"（Linear は "linear:<team>"）
    key: str = ""             # "owner/repo#n"（Linear は identifier）
    repo: str = ""
    number: int = 0
    item_id: str = ""         # Project 上の項目 id（--fill-empty が使う）
    node_id: str = ""         # 課題その物の node id
    kind: str = "Issue"       # Issue / PullRequest / DraftIssue
    title: str = ""
    url: str = ""
    state: str = "OPEN"
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    parent: str | None = None
    children: list = field(default_factory=list)
    status: str | None = None
    priority: str | None = None
    size: str | None = None
    estimate: float | None = None
    end_date: dt.date | None = None
    wait: str | None = None
    carry: int = 0
    worker: str | None = None          # やる人 に入っとる値
    worker_field: str = "やる人"        # その行で使う項目名
    worker_field_id: str = ""
    iteration_id: str | None = None
    deps: list = field(default_factory=list)               # 先の物の key
    linked_prs: list = field(default_factory=list)
    source: str = "github"            # github / linear
    # プラン組み立ての作業欄
    part: str = ""                    # work / device / wait / pr / draft / dup / ""（Sprint 外）
    marks: set = field(default_factory=set)
    deps_open: list = field(default_factory=list)
    moved_for_deps: bool = False
    note: str = ""
    doer: str = ""
    doer_source: str = ""
    rule: str = ""
    fill_target: bool = False
    order: int = 0
    fv: tuple = ()

    @property
    def is_open_issue(self) -> bool:
        return self.kind == "Issue" and self.state == "OPEN"

    @property
    def is_done_open(self) -> bool:
        """Done やのに開いたまま（R0 の相手）。"""
        return self.is_open_issue and self.status == "Done"


@dataclass
class Board:
    owner: str
    number: int
    project_id: str
    title: str = ""
    field_ids: dict = field(default_factory=dict)          # 項目名 -> id
    field_options: dict = field(default_factory=dict)      # 項目名 -> {選択肢名: id}
    iteration_starts: dict = field(default_factory=dict)   # iteration id -> start date
    items: list = field(default_factory=list)              # Work

    @property
    def ref(self) -> str:
        return f"{self.owner}/{self.number}"

    def current_iteration(self, monday: dt.date) -> str | None:
        """日本時間の今日が入る週の Iteration id（GitHub の今週には頼らん）。"""
        for iid, start in self.iteration_starts.items():
            if start == monday:
                return iid
        return None


def _fv_maps(nd: dict) -> tuple:
    """fieldValues を形ごとの写像に分ける。項目名で引けるよう中身を残す。"""
    sel, num, dat, iters, users = {}, {}, {}, {}, {}
    for fv in (nd.get("fieldValues") or {}).get("nodes") or []:
        fname = ((fv or {}).get("field") or {}).get("name")
        if not fname:
            continue
        if "iterationId" in fv:
            iters[fname] = fv["iterationId"]
        elif "name" in fv:
            sel[fname] = fv["name"]
        elif "number" in fv:
            num[fname] = fv["number"]
        elif "date" in fv:
            dat[fname] = fv["date"]
        elif "users" in fv:
            users[fname] = [u.get("login", "") for u in (fv.get("users") or {}).get("nodes") or []]
    return sel, num, dat, iters, users


def strip_code(body: str) -> str:
    """本文からコードの囲みとインラインコードを除く（先の物はコードの中は読まん）。"""
    out, fenced = [], False
    for line in (body or "").splitlines():
        if CODE_FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return INLINE_CODE_RE.sub(" ", "\n".join(out))


def refs_in_text(text: str, own_repo: str) -> list:
    """字の中から課題への参照を全部拾う。"""
    keys = [f"{o}/{r}#{n}" for o, r, n in REF_URL_RE.findall(text)]
    keys += [f"{repo}#{n}" for repo, n in REF_FULL_RE.findall(text)]
    keys += [f"{own_repo}#{n}" for n in REF_NUM_RE.findall(text)]
    return keys


def body_refs(body: str, own_repo: str) -> list:
    """本文の `Blocked by`/`Depends on`/`先に` の後の参照。後に続く箇条書きも読む。"""
    keys, eat = [], False
    for line in strip_code(body).splitlines():
        hit = next((m for m in DEP_MARKERS if m.lower() in line.lower()), None)
        if hit:
            keys += refs_in_text(line[line.lower().index(hit.lower()) + len(hit):], own_repo)
            eat = True
            continue
        if eat and (LIST_LINE_RE.match(line) or REF_URL_RE.search(line)
                    or REF_FULL_RE.match(line.strip()) or REF_NUM_RE.match(line.strip())):
            keys += refs_in_text(line, own_repo)
            continue
        eat = False
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def parse_item_node(ref: str, nd: dict) -> Work:
    c = nd.get("content") or {}
    w = Work(board=ref, item_id=nd.get("id", ""), kind=c.get("__typename") or "DraftIssue")
    if w.kind == "PullRequest":
        repo = (c.get("repository") or {}).get("nameWithOwner", "")
        w.repo, w.number = repo, c.get("number") or 0
        w.key = f"{repo}#{w.number}"
        return w
    if w.kind != "Issue":
        return w
    w.repo = (c.get("repository") or {}).get("nameWithOwner", "")
    w.number = c.get("number") or 0
    w.key = f"{w.repo}#{w.number}"
    w.node_id = c.get("id") or ""
    w.title = c.get("title") or ""
    w.url = c.get("url") or (f"https://github.com/{w.repo}/issues/{w.number}" if w.repo else "")
    w.state = c.get("state") or ""
    w.created_at = parse_ts(c.get("createdAt"))
    w.updated_at = parse_ts(c.get("updatedAt"))
    if c.get("parent"):
        w.parent = f"{c['parent']['repository']['nameWithOwner']}#{c['parent']['number']}"
    w.children = [f"{s['repository']['nameWithOwner']}#{s['number']}"
                  for s in (c.get("subIssues") or {}).get("nodes") or []]
    deps = [f"{s['repository']['nameWithOwner']}#{s['number']}"
            for s in (c.get("blockedBy") or {}).get("nodes") or [] if s.get("state") != "CLOSED"]
    deps += body_refs(c.get("body") or "", w.repo)
    seen = set()
    w.deps = [k for k in deps if k != w.key and not (k in seen or seen.add(k))]
    w.linked_prs = [f"{p['repository']['nameWithOwner']}#{p['number']}"
                    for p in (c.get("closedByPullRequestsReferences") or {}).get("nodes") or []]
    w.fv = _fv_maps(nd)
    return w


def resolve_fields(w: Work, fmap: dict) -> None:
    """repo の yml で決めた項目名で fieldValues をほぐす。yml が無い所は既定の名前。"""
    sel, num, dat, iters, users = w.fv

    def name(role: str) -> str:
        cand = fmap.get(role) or FIELD_DEFAULTS[role]
        if cand in sel or cand in num or cand in dat or cand in iters or cand in users:
            return cand
        return FIELD_DEFAULTS[role]

    w.status = sel.get(name("status"))
    w.priority = sel.get(name("priority"))
    w.size = sel.get(name("size"))
    w.wait = sel.get(name("wait"))
    w.worker = sel.get(name("assignee")) or (", ".join(users.get(name("assignee"), [])) or None)
    w.estimate = num.get(name("estimate"))
    w.carry = int(num.get(name("carryover")) or 0)
    w.iteration_id = next(iter(iters.values()), None)  # Iteration の項目は 1 つだけ
    d = dat.get(name("end"))
    w.end_date = dt.date.fromisoformat(d) if d else None
    w.worker_field = name("assignee")


def parse_board(node: dict, owner: str, number: int, item_nodes: list) -> Board:
    b = Board(owner=owner, number=number, project_id=node["id"], title=node.get("title") or "")
    for f in (node.get("fields") or {}).get("nodes") or []:
        if not f or "name" not in f:
            continue
        b.field_ids[f["name"]] = f["id"]
        if f.get("options"):
            b.field_options[f["name"]] = {o["name"]: o["id"] for o in f["options"]}
        cfg = f.get("configuration") or {}
        for it in (cfg.get("completedIterations") or []) + (cfg.get("iterations") or []):
            try:
                b.iteration_starts[it["id"]] = dt.date.fromisoformat(it["startDate"])
            except (KeyError, ValueError):
                continue
    b.items = [parse_item_node(b.ref, nd) for nd in item_nodes]
    return b


# ------------------------------------------------------------------ gh と REST
class GhShell:
    """`gh api` の薄い包み。使った量（graphql_cost / rest_calls / remaining）を数える。"""

    def __init__(self):
        self.calls = 0
        self.graphql_cost = 0
        self.remaining: int | None = None
        self.rest_calls = 0
        self.yml_cache: dict = {}   # repo -> ("ok", text) / ("404", None)

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
            d = None
        errors = (d or {}).get("errors") if isinstance(d, dict) else None
        if p.returncode != 0 or errors:
            blob = p.stderr + " " + " ".join(f"{e.get('type', '')} {e.get('message', '')}" for e in (errors or [])
                                            if isinstance(e, dict))
            if p.returncode != 0 and d is None:
                blob += " " + p.stdout
            elif isinstance(d, dict) and not errors:
                blob += " " + str(d.get("message", ""))
            if RATE_LIMIT_RE.search(blob):
                raise GhRateLimit("rate limited")
            if errors and any((e.get("type") == "NOT_FOUND" or "Could not resolve" in (e.get("message") or ""))
                              for e in errors if isinstance(e, dict)):
                raise ProjectMiss(str(errors[0].get("message", ""))[:120])
            if d is None:
                raise GhError(f"gh exit {p.returncode}: unparseable output")
            raise GhError("graphql error: " + str(errors[0].get("type") or errors[0].get("message", ""))[:120])
        if d is None:
            raise GhError(f"gh exit {p.returncode}: unparseable output")
        rl = (d.get("data") or {}).get("rateLimit") or {}
        if "cost" in rl:
            self.graphql_cost += rl["cost"] or 0
        if rl.get("remaining") is not None:
            self.remaining = rl["remaining"] if self.remaining is None else min(self.remaining, rl["remaining"])
        return d

    def check_auth(self) -> bool:
        try:
            return subprocess.run([gh_path(), "auth", "status"], capture_output=True, text=True).returncode == 0
        except OSError:
            return False

    def probe_budget(self) -> None:
        """量を見る 1 回。残りが床未満なら量不足で止める。"""
        d = self._gql(RATE_Q)
        self.remaining = (d.get("data") or {}).get("rateLimit", {}).get("remaining")
        if self.remaining is not None and self.remaining < RATE_FLOOR:
            raise GhRateLimit(f"remaining {self.remaining} < {RATE_FLOOR}")

    def read_board(self, owner: str, number: int) -> Board:
        item_nodes, after, node = [], None, None
        while True:
            d = self._gql(BOARD_Q, login=owner, n=number, after=after)
            proj = ((d.get("data") or {}).get("repositoryOwner") or {}).get("projectV2")
            if not proj:
                raise ProjectMiss(f"board {owner}/{number} not found")
            node = node or proj
            item_nodes += proj["items"]["nodes"]
            if not proj["items"]["pageInfo"]["hasNextPage"]:
                break
            after = proj["items"]["pageInfo"]["endCursor"]
        return parse_board(node, owner, number, item_nodes)

    def rest_yml(self, repo: str) -> tuple:
        """repo の .github/scrum-board.yml。返り値は ("ok", 本文) / ("404", None)。"""
        if repo in self.yml_cache:
            return self.yml_cache[repo]
        self.rest_calls += 1
        p = subprocess.run([gh_path(), "api", f"repos/{repo}/contents/.github/scrum-board.yml",
                            "-H", "Accept: application/vnd.github.raw"],
                           capture_output=True, text=True)
        if p.returncode == 0:
            self.yml_cache[repo] = ("ok", p.stdout)
        elif "404" in p.stderr or "Not Found" in p.stderr:
            self.yml_cache[repo] = ("404", None)
        else:
            raise GhError(f"rest yml {repo}: {p.stderr.strip()[:120]}")
        return self.yml_cache[repo]


# ------------------------------------------------------------------ yml（PyYAML に頼らん）
def parse_repo_yml(text: str) -> dict:
    """.github/scrum-board.yml から worker_default と fields: の写像だけ取る。"""
    out = {}
    m = re.search(r"^worker_default:\s*['\"]?(\w+)['\"]?\s*$", text or "", re.M)
    if m:
        out["worker_default"] = m.group(1)
    fm = re.search(r"^fields:\n((?:[ \t]+\S.*\n?)+)", text or "", re.M)
    if fm:
        for line in fm.group(1).splitlines():
            kv = re.match(r"^[ \t]+(\w+):\s*['\"]?([^'\"]+?)['\"]?\s*$", line)
            if kv:
                out.setdefault("fields", {})[kv.group(1)] = kv.group(2).strip()
    return out


# ------------------------------------------------------------------ Linear（読むだけ。鍵は op read だけが触る）
class LinearClient:
    def __init__(self, team: str, key_ref: str, log):
        self.team, self.key_ref, self.log = team, key_ref, log
        self.api = os.environ.get("EXEC_PLAN_LINEAR_URL", LINEAR_API)
        self._key = None

    def key(self) -> str:
        if self._key is None:
            op = op_path()
            if not op:
                raise LinearError("op not found")
            try:
                p = subprocess.run([op, "read", self.key_ref], capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise LinearError(f"op read failed: {type(e).__name__}")
            if p.returncode != 0 or not p.stdout.strip():
                raise LinearError("op read returned empty")
            self._key = p.stdout.strip()
        return self._key

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.api, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": self.key()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
        except (OSError, ValueError) as e:
            raise LinearError(f"linear api: {type(e).__name__}")
        if not isinstance(d, dict) or d.get("errors"):
            msg = d.get("errors") if isinstance(d, dict) else None
            raise LinearError("graphql: " + str((msg or [{}])[0].get("message", "bad response"))[:120])
        return d.get("data") or {}

    def _fields(self, type_name: str) -> set:
        """付録 2B: 項目の名前は未試験なので、読む前に __type で合わせる。"""
        d = self._gql(LINEAR_TYPE_Q, {"n": type_name})
        return {f["name"] for f in ((d.get("__type") or {}).get("fields") or [])}

    def read(self) -> dict:
        """今の Cycle の終わってへん issues。返り値: {"cycle", "issues", "wait_labels"}。"""
        teams = self._gql(LINEAR_TEAM_Q, {"k": self.team}).get("teams", {}).get("nodes", [])
        if not teams:
            raise LinearError(f"team {self.team} not found")
        team_id = teams[0]["id"]
        known = self._fields("Issue")
        optional = " ".join(s for s, need in
                            (("children{nodes{id}}", "children"),
                             ("attachments(first:10){nodes{url}}", "attachments"))
                            if need in known)
        cycles = (self._gql(LINEAR_CYCLE_Q, {"id": team_id})
                  .get("team", {}).get("cycles", {}).get("nodes", []))
        if not cycles:
            return {"cycle": None, "issues": [], "wait_labels": {}}
        cyc = cycles[0]
        nodes, after = [], None
        while True:
            d = self._gql(LINEAR_ISSUES_Q.replace("%OPTIONAL%", optional),
                          {"id": cyc["id"], "after": after})
            page = (d.get("cycle") or {}).get("issues") or {}
            nodes += page.get("nodes") or []
            if not (page.get("pageInfo") or {}).get("hasNextPage"):
                break
            after = page["pageInfo"]["endCursor"]
        issues, wait_labels = [], {}
        for n in nodes:
            st = (n.get("state") or {}).get("type") or ""
            if st in ("completed", "canceled"):
                continue
            waits = [lb["name"] for lb in (n.get("labels") or {}).get("nodes") or []
                     if (lb.get("name") or "").startswith("待ち:")]
            for lb in waits:
                wait_labels[lb] = wait_labels.get(lb, 0) + 1
            prs = [u for u in (a.get("url") or "" for a in (n.get("attachments") or {}).get("nodes") or [])
                   if re.search(r"github\.com/[\w.-]+/[\w.-]+/pull/\d+", u)]
            issues.append({"identifier": n.get("identifier", ""), "title": n.get("title") or "",
                           "url": n.get("url") or "", "priority": n.get("priority") or 0,
                           "estimate": n.get("estimate"), "createdAt": n.get("createdAt"),
                           "children": len((n.get("children") or {}).get("nodes") or []),
                           "wait": waits[0] if waits else None, "linked_prs": prs})
        return {"cycle": {"id": cyc["id"], "name": cyc.get("name") or "",
                          "number": cyc.get("number")}, "issues": issues, "wait_labels": wait_labels}


# ------------------------------------------------------------------ 写像・決まり
def read_allow(path: str) -> list:
    """1 行 `owner/番号`。`_` で始まる行は読まん。"""
    refs = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("_"):
            refs.append(line)
    return refs


def split_ref(ref: str) -> tuple:
    owner, _, num = ref.partition("/")
    if not owner or not num.isdigit():
        raise ValueError(f"bad board ref: {ref}")
    return owner, int(num)


def order_key(w: Work):
    """付録 2A: Priority -> End date（空は最後）-> createdAt -> repo -> 番号。"""
    pr = PRIORITY_RANK.get(w.priority or "", 3)
    end = w.end_date.toordinal() if w.end_date else 10**9
    created = w.created_at.timestamp() if w.created_at else float("inf")
    return (pr, end, created, w.repo, w.number)


def decide_doer(w: Work, worker_default: str | None) -> None:
    """付録 2B R0–R7。R0・R1 は月曜に項目へ書かん（fill_targets の相手にもならん）。"""
    if w.is_done_open and w.wait != DEVICE_WAIT:
        w.doer, w.rule, w.doer_source = "閉じ忘れ: Sonnet", "R0", "rule"
        w.marks.add("close-forget")
        return
    if w.children and "[PBI]" in w.title:
        w.doer = "確かめ: Claude" if w.status == "In Review" else "まとめ"
        w.rule, w.doer_source = "R1", "rule"
        return
    est, size = w.estimate, w.size or ""
    if (est is not None and est > 16) or (est is None and size == "XL"):
        w.doer, w.rule, w.doer_source = "設計", "R2", "rule"
        return
    if w.wait == DEVICE_WAIT:
        w.doer, w.rule, w.doer_source = DEVICE_WAIT, "R3", "rule"
        return
    if w.worker:
        w.doer, w.rule, w.doer_source = w.worker, "R4", "field"
        return
    if (est is not None and 6 < est <= 16) or (est is None and size == "L"):
        w.doer, w.rule, w.doer_source = "設計", "R5", "rule"
        return
    if worker_default == "code":
        w.doer, w.rule, w.doer_source = "Devin", "R6", "yml"
        return
    w.doer, w.rule, w.doer_source = "Sonnet", "R7", "yml"


def linear_work(issue: dict) -> Work:
    """Linear の 1 件を Work に変える。全行が作業（待ち: ラベルは数だけ）。"""
    w = Work(source="linear", kind="Issue", state="OPEN")
    w.key = issue.get("identifier") or "?"
    w.title = issue.get("title", "")
    w.url = issue.get("url", "")
    m = re.match(r"[A-Za-z]+-(\d+)$", w.key)
    w.number = int(m.group(1)) if m else 0
    w.priority = LINEAR_PRIORITY.get(issue.get("priority") or 0)
    w.estimate = issue.get("estimate")
    w.created_at = parse_ts(issue.get("createdAt"))
    w.wait = issue.get("wait")
    w.children = [f"{w.key}-child-{i}" for i in range(issue.get("children") or 0)]
    w.linked_prs = list(issue.get("linked_prs") or [])
    w.fv = ({}, {}, {}, {}, {})
    return w


def order_part(items: list, pool: dict) -> list:
    """置く前に未配置の先の物を先に置く（再帰）。輪は 1 本飛ばして印を付ける。"""
    seq, state = [], {}

    def place(w: Work) -> None:
        if state.get(w.key):
            return
        state[w.key] = "visiting"
        for dkey in w.deps:
            d = pool.get(dkey)
            if d is None or d.key == w.key:
                continue
            st = state.get(d.key)
            if st == "done":
                continue
            if st == "visiting":
                w.marks.add("deps-cycle")
                d.marks.add("deps-cycle")
                continue
            if not d.note:
                d.note = f"{w.key} のため前に出した"
            d.moved_for_deps = True
            place(d)
        state[w.key] = "done"
        seq.append(w)

    for w in sorted(items, key=order_key):
        place(w)
    return seq


# ------------------------------------------------------------------ fixture（試験用の保存済み応答）
class FixtureShell(GhShell):
    """--fixture の置き場から読む。files: board 用 {"owner","number","project"} /
    repo 用 {"repos":{repo:{"yml":...}}} / Linear 用 {"linear":{...}}。量の計測はせん。"""

    def __init__(self, directory: Path):
        super().__init__()
        self.boards, self.repos, self.linear = {}, {}, {}
        for p in sorted(Path(directory).glob("*.json")):
            d = json.loads(p.read_text())
            if "project" in d:
                self.boards[f"{d['owner']}/{d['number']}"] = d["project"]
            if "repos" in d:
                self.repos.update(d["repos"])
            if "linear" in d:
                self.linear = d["linear"]

    def check_auth(self) -> bool:
        return True

    def probe_budget(self) -> None:
        return

    def read_board(self, owner: str, number: int) -> Board:
        node = self.boards.get(f"{owner}/{number}")
        if node is None:
            raise ProjectMiss(f"board {owner}/{number} not found")
        return parse_board(node, owner, number, node["items"]["nodes"])

    def rest_yml(self, repo: str):
        if repo in self.yml_cache:
            return self.yml_cache[repo]
        info = self.repos.get(repo)
        self.yml_cache[repo] = ("ok", info["yml"]) if info and "yml" in info else ("404", None)
        return self.yml_cache[repo]


# ------------------------------------------------------------------ 書き出し
def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def render_md(plan: dict) -> str:
    lines = [
        f"# 実行プラン {plan['sprint']}",
        "",
        f"作った時刻: {plan['built_at']}（{SCHEMA}）",
        "",
        "> STALE/STALE-count が無い・last-built が 26 時間以内・今日がこの Sprint の時だけ使う。",
        "",
        f"決まりで出したやる人: {plan['rule_filled']} 件（うち月曜に書く相手 {plan['fill_targets']} 件）",
        f"Linear: {plan['linear']}  GraphQL cost: {plan['graphql_cost']}  REST: {plan['rest_calls']}",
        "",
    ]
    for part, head in (("work", "## 作業プラン"), ("device", "## 実機プラン")):
        lines += [head, "", "| # | 課題 | P | 締め | やる人 | 決まり | 備考 |",
                  "|---|---|---|---|---|---|---|"]
        for r in [r for r in plan["rows"] if r["part"] == part]:
            note = " / ".join(x for x in (r.get("note", ""), " ".join(r.get("marks", []))) if x)
            title = (r.get("title") or "").replace("|", "\\|")
            lines.append(f"| {r['order']} | [{r['key']}]({r['url']}) {title} "
                         f"| {r.get('priority') or ''} | {r.get('end_date') or ''} "
                         f"| {r['doer']} | {r['rule']} | {note} |")
        lines.append("")
    lines += ["## Project ごとの数", "",
              "| Project | 作業 | 実機 | その他の待ち | PR | 下書き | 書く相手 |",
              "|---|---|---|---|---|---|---|"]
    for b in plan["boards"]:
        c = b["counts"]
        waits = " ".join(f"{k}={v}" for k, v in sorted(c.get("wait", {}).items())) or "0"
        lines.append(f"| {b['ref']} | {c.get('work', 0)} | {c.get('device', 0)} | {waits} "
                     f"| {c.get('pr', 0)} | {c.get('draft', 0)} | {b['fill_targets']} |")
    lc = plan.get("linear_counts") or {}
    if lc:
        cyc = lc.get("cycle") or {}
        waits = " ".join(f"{k}={v}" for k, v in sorted(lc.get("wait_labels", {}).items())) or "なし"
        lines.append(f"| linear:{cyc.get('name') or lc.get('team', '')} | {lc.get('issues', 0)} "
                     f"| - | {waits} | - | - | - |")
    lines += ["", "## 食い違い・印", ""]
    lines += [f"- {a}" for a in plan["anomalies"]] or ["- なし"]
    lines.append("")
    return "\n".join(lines)


def render_html(plan: dict, session_id: str) -> str:
    """月曜の 1 枚。Mermaid の流れ図（やる人ごとの列と先の物の矢印）。題の下に Session ID。"""
    node, lanes, edges = {}, {}, []
    row_keys = {r["key"] for r in plan["rows"]}
    for i, r in enumerate(plan["rows"]):
        nid = f"n{i}"
        node[r["key"]] = nid
        lanes.setdefault(r["doer"], []).append(f'    {nid}["{r["key"]}"]')
        for d in r.get("deps_open", []):
            if d in row_keys:
                edges.append((d, r["key"]))
    mer = ["flowchart LR"]
    for j, (doer, ids) in enumerate(lanes.items()):
        mer.append(f'  subgraph g{j}["{doer}"]')
        mer += ids
        mer.append("  end")
    for a, b in edges:
        mer.append(f"  {node[a]} --> {node[b]}")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>実行プラン {plan['sprint']}</title>
<script type="module">import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';</script>
</head><body>
<h1>実行プラン {plan['sprint']}</h1>
<p>Session ID: {session_id} / 作った時刻: {plan['built_at']}</p>
<pre class="mermaid">
{chr(10).join(mer)}
</pre>
</body></html>
"""


# ------------------------------------------------------------------ --fill-empty（付録 2C）
def fill_empty(plan: dict, boards: list, args, log) -> None:
    """やる人 が空の行へ決まりの値を書く。写しが無い所には書かん。確かめ: 作り直した
    fill_targets = 前 - written。"""
    repo_root = Path(__file__).resolve().parents[2]
    if not (repo_root / "scripts/q/item-clear.graphql").exists():
        raise FillAbort("scripts/q/item-clear.graphql が無い（2C の決まりで動かさん）")
    snapshot_tool = repo_root / "scripts/board_snapshot.py"
    stamp = to_jst(parse_now(args.now)).strftime("%Y%m%d")
    pre_dir = Path(args.pre)
    pre_dir.mkdir(parents=True, exist_ok=True)
    tsv_rows = []
    for b in boards:
        targets = [w for w in plan["_works"] if w.board == b.ref and w.fill_target]
        if not targets:
            continue
        snap_path = pre_dir / f"{b.owner}-{b.number}-{stamp}.json"
        if snapshot_tool.exists():
            p = subprocess.run([sys.executable, str(snapshot_tool), "--project", b.project_id,
                                "--out", str(snap_path)], capture_output=True, text=True)
            if p.returncode != 0 or not snap_path.exists():
                log(f"fill: snapshot failed board={b.ref} — 写しの無い所には書かん")
                continue
        elif not snap_path.exists():
            log(f"fill: snapshot tool も写しも無い board={b.ref} — skip")
            continue
        # 写しで値がある件は飛ばす
        preset = set()
        try:
            snap = json.loads(snap_path.read_text())
            for it in snap.get("items", snap.get("nodes", [])):
                nodes = (it.get("fieldValues") or {}).get("nodes", [])
                vals = {((v or {}).get("field") or {}).get("name"): v.get("name") for v in nodes}
                if any(vals.get(t.worker_field) for t in targets):
                    iid = it.get("id")
                    if iid:
                        preset.add(iid)
        except (ValueError, OSError):
            pass
        for w in targets:
            field_id = b.field_ids.get(w.worker_field)
            oid = (b.field_options.get(w.worker_field) or {}).get(w.doer)
            if w.item_id in preset:
                tsv_rows.append((b.ref, w.key, "skip"))
            elif not field_id or not oid:
                tsv_rows.append((b.ref, w.key, "fail:no-option"))
            else:
                mutation = {"query": "mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
                                     "input:{projectId:$p,itemId:$i,fieldId:$f,"
                                     "value:{singleSelectOptionId:$o}}){projectV2Item{id}}}",
                            "variables": {"p": b.project_id, "i": w.item_id, "f": field_id, "o": oid}}
                pre = json.dumps({"item": w.item_id, "field": field_id, "value": None},
                                 ensure_ascii=False)
                p = subprocess.run([args.write_via, "--wave", "M1", "--step", "M2-fill",
                                    "--target", w.key, "--class", "field", "--prio", "norm",
                                    "--pre", pre, "--", gh_path(), "api", "graphql", "--input", "-"],
                                   input=json.dumps(mutation), capture_output=True, text=True)
                tsv_rows.append((b.ref, w.key, "written" if p.returncode == 0 else "fail"))
            if Path(args.halt_file).exists():
                raise FillAbort("HALT during fill-empty")
    stamp_file = Path(args.out) / f"fill-{stamp}.tsv"
    atomic_write(stamp_file, "".join(f"{b}\t{k}\t{r}\n" for b, k, r in tsv_rows))
    written = sum(1 for _, _, r in tsv_rows if r == "written")
    log(f"fill-empty: written={written} rows={len(tsv_rows)}")


# ------------------------------------------------------------------ main
def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--board", help="<owner>/<番号>。この 1 つだけ読む（試験用）")
    g.add_argument("--allow", help="<owner>/<番号> を 1 行ずつ書いた許可リスト。_ で始まる行は読まん")
    ap.add_argument("--iteration", default="current", help="current / next / <日付>（その週の月曜）")
    ap.add_argument("--out", required=True, help="写しの置き場（<Sprint の月曜>.json/.md と board-ids.tsv）")
    ap.add_argument("--linear-team", help="Linear の team key（例 RIT）。無ければ Linear は読まん")
    ap.add_argument("--linear-key-ref", help="op:// の参照。--linear-team と一緒に使う")
    ap.add_argument("--now", help="ISO8601。週と日付は常に Asia/Tokyo")
    ap.add_argument("--fixture", help="GitHub/Linear の代わりに読む保存済みの応答の置き場（試験用）")
    ap.add_argument("--html", help="月曜の 1 枚の書き出し先（Mermaid）")
    ap.add_argument("--session-id", default="", help="1 枚の題の下に出す Session ID")
    ap.add_argument("--fill-empty", action="store_true", help="やる人 が空の行へ決まりの値を書く（月曜）")
    ap.add_argument("--write-via", help="--fill-empty が書き込みに使う gh_write.sh のパス")
    ap.add_argument("--pre", help="--fill-empty の写しの置き場")
    ap.add_argument("--halt-file", default=str(HALT_DEFAULT), help="非常停止の印のファイル")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log = lambda msg: print(msg, file=sys.stderr)
    if Path(args.halt_file).exists():
        print("HALT")
        return 1
    if args.fill_empty and not (args.write_via and args.pre):
        log("--fill-empty には --write-via と --pre が要る")
        return 2
    if args.fill_empty and not Path(args.write_via).exists():
        log(f"--write-via が見つからん: {args.write_via}")
        return 2
    if args.linear_team and not args.linear_key_ref:
        log("--linear-team には --linear-key-ref が要る")
        return 2
    try:
        now = parse_now(args.now)
    except ValueError:
        log("bad --now")
        return 2
    if args.iteration == "current":
        monday = monday_of(to_jst(now).date())
    elif args.iteration == "next":
        monday = monday_of(to_jst(now).date()) + dt.timedelta(days=7)
    else:
        try:
            monday = monday_of(dt.date.fromisoformat(args.iteration))
        except ValueError:
            log("bad --iteration")
            return 2

    shell = FixtureShell(Path(args.fixture)) if args.fixture else GhShell()
    if not shell.check_auth():
        log("gh auth failed")
        return 3

    try:
        refs = [args.board] if args.board else read_allow(args.allow)
        refs = [f"{o}/{n}" for o, n in (split_ref(r) for r in refs)]
    except (ValueError, OSError) as e:
        log(str(e))
        return 2

    # ---- 量の床を先に見る（残り 1,500 未満なら何も残さず rc 8）----
    try:
        shell.probe_budget()
    except GhRateLimit as e:
        log(f"budget: {e}")
        return 8
    except GhError as e:
        log(f"github unreachable: {e}")
        return 3

    # ---- 盤を全部読む。1 つでも読めん所があれば前のプランを残して rc 4 ----
    boards, misses = [], []
    for ref in refs:
        try:
            b = shell.read_board(*split_ref(ref))
            if args.allow and b.title.startswith("_"):
                continue
            boards.append(b)
        except ProjectMiss as e:
            log(f"project miss: {e}")
            misses.append(ref)
        except GhRateLimit as e:
            log(f"rate limited: {e}")
            return 8
        except GhError as e:
            log(f"github failure on {ref}: {e}")
            return 3
    if misses:
        return 4

    # ---- repo ごとの .github/scrum-board.yml（worker_default と項目名）----
    repo_cfg, anomalies = {}, []
    repos = sorted({w.repo for b in boards for w in b.items if w.kind == "Issue" and w.repo}
                   | ({LINEAR_DEFAULT_REPO} if args.linear_team else set()))
    try:
        for repo in repos:
            st, text = shell.rest_yml(repo)
            if st == "404":
                anomalies.append(f"no-scrum-board-yml:{repo}")
                repo_cfg[repo] = {"ok": False}
                continue
            cfg = parse_repo_yml(text)
            cfg["ok"] = "worker_default" in cfg
            if not cfg["ok"]:
                anomalies.append(f"bad-scrum-board-yml:{repo}")
            repo_cfg[repo] = cfg
    except GhError as e:
        log(str(e))
        return 3

    for b in boards:
        for w in b.items:
            if w.kind == "Issue":
                cfg = repo_cfg.get(w.repo) or {}
                resolve_fields(w, cfg.get("fields") or {})
                w.worker_field_id = b.field_ids.get(w.worker_field, "")

    # ---- Linear（失敗しても盤の分は作る。終わりに rc 6）----
    linear_status, linear_counts, linear_items = "skipped", {}, []
    if args.linear_team:
        try:
            linear = (shell.linear if isinstance(shell, FixtureShell)
                      else LinearClient(args.linear_team, args.linear_key_ref, log).read())
            linear_status = "read"
            linear_counts = {"team": args.linear_team, "cycle": linear.get("cycle"),
                             "issues": len(linear.get("issues", [])),
                             "wait_labels": linear.get("wait_labels", {})}
            linear_items = linear.get("issues", [])
            if linear.get("cycle") is None:
                anomalies.append("linear:no-active-cycle")
        except LinearError as e:
            linear_status = "unread"
            linear_counts = {"team": args.linear_team, "cycle": None, "issues": 0, "wait_labels": {}}
            anomalies.append(f"linear:{e}")
            log(f"linear unread: {e}")
        # つながった PR の repo の yml も読む（付録 2B。盤に載っとらん repo があり得る）
        try:
            for issue in linear_items:
                for u in issue.get("linked_prs") or []:
                    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/\d+", u)
                    if not m or m.group(1) in repo_cfg:
                        continue
                    st, text = shell.rest_yml(m.group(1))
                    if st == "404":
                        anomalies.append(f"no-scrum-board-yml:{m.group(1)}")
                        repo_cfg[m.group(1)] = {"ok": False}
                    else:
                        cfg = parse_repo_yml(text)
                        cfg["ok"] = "worker_default" in cfg
                        if not cfg["ok"]:
                            anomalies.append(f"bad-scrum-board-yml:{m.group(1)}")
                        repo_cfg[m.group(1)] = cfg
        except GhError as e:
            log(str(e))
            return 3

    # ---- 行の仕分け（待ち無し=作業 ／ 実機待ち=実機 ／ 局長・外部待ち=数だけ）----
    for b in boards:
        cur_id = b.current_iteration(monday)
        if cur_id is None:
            anomalies.append(f"no-current-iteration:{b.ref}")
            continue
        names = {FIELD_DEFAULTS["assignee"]}
        for cfg in repo_cfg.values():
            if cfg.get("fields", {}).get("assignee"):
                names.add(cfg["fields"]["assignee"])
        if not any(n in b.field_ids for n in names):
            anomalies.append(f"no-worker-field:{b.ref}")
        for w in b.items:
            if w.kind == "PullRequest":
                w.part = "pr"
            elif w.kind == "DraftIssue":
                w.part = "draft"
            elif w.state != "OPEN" or w.iteration_id != cur_id:
                w.part = ""
            elif w.wait == DEVICE_WAIT:
                w.part = "device"
            elif w.wait and not w.is_done_open:
                w.part = "wait"
            else:
                w.part = "work"

    # 同じ課題が 2 つの盤で今週に居る時は先に当たった方だけ
    seen_keys = set()
    for b in boards:
        for w in b.items:
            if w.part in ("work", "device"):
                if w.key in seen_keys:
                    w.part = "dup"
                    anomalies.append(f"dup-on-boards:{w.key}")
                else:
                    seen_keys.add(w.key)

    # 先の物の仕分け（閉じた物・PR・親子は拾う段で除いてある）
    sprint_open = {w.key: w for b in boards for w in b.items
                   if w.part in ("work", "device", "wait")}
    pools = {p: {w.key: w for b in boards for w in b.items if w.part == p}
             for p in ("work", "device")}
    # 閉じた物は数えん。盤に載っとる物は状態で、載っとらん物は開いとると見なす。
    all_items = {w.key: w for b in boards for w in b.items}
    for w in [w for b in boards for w in b.items if w.part in ("work", "device")]:
        for dkey in w.deps:
            target = all_items.get(dkey)
            if target is not None and (target.state == "CLOSED" or target.kind != "Issue"):
                continue
            if dkey in pools[w.part]:
                pass                                    # 同じプランの中 — 並べ替えの相手
            elif dkey in sprint_open:
                w.marks.add("deps-other-part")          # 別のプランか待ちの先の物
            else:
                w.marks.add("deps-not-in-sprint")       # Sprint の外（並べ替えん）
            w.deps_open.append(dkey)

    for b in boards:
        for w in b.items:
            if w.part in ("work", "device") and w.source == "github":
                decide_doer(w, (repo_cfg.get(w.repo) or {}).get("worker_default"))

    for issue in linear_items:
        w = linear_work(issue)
        w.board = f"linear:{args.linear_team}"
        w.part = "work"
        pr_repo = next((m.group(1) for m in
                        (re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/\d+", u)
                         for u in w.linked_prs) if m), None)
        cfg = repo_cfg.get(pr_repo) or repo_cfg.get(LINEAR_DEFAULT_REPO) or {}
        if not cfg.get("ok"):
            a = f"linear-default-repo-yml:{pr_repo or LINEAR_DEFAULT_REPO}"
            if a not in anomalies:
                anomalies.append(a)
        decide_doer(w, cfg.get("worker_default"))
        w.repo = pr_repo or ""
        pools["work"][w.key] = w

    # 並べる（作業と実機で別々に、Project をまたいで 1 つの順）
    rows, order = [], 0
    for part in ("work", "device"):
        pool = pools[part]
        for w in order_part([w for w in pool.values()], pool):
            order += 1
            w.order = order
            rows.append({
                "order": order, "part": part, "board": w.board, "repo": w.repo, "n": w.number,
                "key": w.key, "url": w.url, "title": w.title, "priority": w.priority,
                "end_date": w.end_date.isoformat() if w.end_date else None,
                "estimate": w.estimate, "wait": w.wait, "carryover": w.carry,
                "doer": w.doer, "doer_source": w.doer_source, "rule": w.rule,
                "deps_open": w.deps_open, "marks": sorted(w.marks),
                "moved_for_deps": w.moved_for_deps, "linked_prs": w.linked_prs,
                "note": w.note, "source": w.source,
            })

    # 決まりで出した行と、月曜に書く相手（付録 2C の fill_targets）
    plan_works = [w for w in pools["work"].values()] + [w for w in pools["device"].values()]
    for w in plan_works:
        w.fill_target = bool(
            w.source == "github" and w.doer in WRITE_DOERS and not w.worker
            and w.worker_field_id
            and not (w.rule == "R7" and not (repo_cfg.get(w.repo) or {}).get("ok")))
    rule_filled = sum(1 for w in plan_works if w.rule not in ("", "R4"))

    board_counts = {}
    for b in boards:
        c = {"work": 0, "device": 0, "wait": {}, "pr": 0, "draft": 0}
        for w in b.items:
            if w.part == "work":
                c["work"] += 1
            elif w.part == "device":
                c["device"] += 1
            elif w.part == "wait":
                c["wait"][w.wait] = c["wait"].get(w.wait, 0) + 1
            elif w.part == "pr":
                c["pr"] += 1
            elif w.part == "draft":
                c["draft"] += 1
        c["fill_targets"] = sum(1 for w in plan_works if w.board == b.ref and w.fill_target)
        board_counts[b.ref] = c
    fill_targets = sum(c["fill_targets"] for c in board_counts.values())

    # ---- 量の決まり（付録 2A）。外れたら何も書かず rc 8 ----
    if not args.fixture and (RATE_BASE + RATE_PER_HOUR * shell.graphql_cost > RATE_BUDGET
                             or shell.rest_calls > REST_MAX):
        log(f"budget exceeded: cost={shell.graphql_cost} rest={shell.rest_calls}")
        return 8

    plan = {
        "schema": SCHEMA, "sprint": monday.isoformat(), "built_at": to_jst(now).isoformat(timespec="seconds"),
        "linear": linear_status, "graphql_cost": shell.graphql_cost, "rest_calls": shell.rest_calls,
        "rate_remaining": shell.remaining,
        "boards": [{"ref": b.ref, "id": b.project_id, "counts": board_counts[b.ref],
                    "fill_targets": board_counts[b.ref]["fill_targets"]} for b in boards],
        "linear_counts": linear_counts,
        "rows": rows, "rule_filled": rule_filled, "fill_targets": fill_targets,
        "anomalies": anomalies,
        "_works": plan_works,
    }

    # ---- 書き出し。本番の dir に書く物は全部 exec-plan.lock を取ってから ----
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    if not os.environ.get("EXEC_PLAN_LOCK_HELD"):
        lock_fd = os.open(out / "exec-plan.lock", os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        atomic_write(out / f"{monday.isoformat()}.json",
                     json.dumps({k: v for k, v in plan.items() if not k.startswith("_")},
                                ensure_ascii=False, indent=1) + "\n")
        atomic_write(out / f"{monday.isoformat()}.md", render_md(plan))
        atomic_write(out / "board-ids.tsv",
                     "".join(f"{b.ref}\t{b.project_id}\n" for b in boards))
        if args.html:
            atomic_write(Path(args.html), render_html(plan, args.session_id))
    finally:
        if lock_fd is not None:
            os.close(lock_fd)

    # ---- 月曜の書き込み（plan を書いてから。止まったら rc 7）----
    if args.fill_empty:
        try:
            fill_empty(plan, boards, args, log)
        except (FillAbort, GhError, OSError) as e:
            log(f"fill-empty aborted: {e}")
            return 7

    log(f"built sprint={monday.isoformat()} rows={len(rows)} boards={len(boards)} "
        f"cost={shell.graphql_cost} rest={shell.rest_calls} linear={linear_status}")
    return 6 if linear_status == "unread" else 0


if __name__ == "__main__":
    sys.exit(main())

"""Linear を読むだけの薄い包み。書き込みの面は持たない（Linear の課題は絶対に書き換えん）。

鍵は 1Password の参照（`op://...`）を実行の時だけ `op read` で取る。値はファイルにもログにも
残さんし、例外の文言にも載せん。`op` が無い・参照が無い・Linear に届かん時は LinearUnavailable を
投げ、呼び出し側が rc 6 でこの回を諦める。

フィールド名は未試験（plan/05 付録 2B）なので、読む前に `__type` で schema を確かめる。
食い違いがあれば LinearUnavailable。試験と --fixture では FakeLinearClient に差し替える。

確かめた形（2026-10 Linear API docs。優先度 1=Urgent→P0, 2=High→P1, 3=Medium→P2, 4=Low, 0=無し）:
  team(id:String!){id key name activeCycle{id number name startsAt endsAt issues(first:250){...}}}
  team(id:String!){issues(first:250,after:){nodes{... cycle{id}}} teams(first:100){nodes{id key name}}
  Issue.state.type: triage / backlog / unstarted / started / completed / canceled
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .model import parse_ts

API_URL = "https://api.linear.app/graphql"
DEFAULT_KEY_REF = "op://RITMO/Linear API key - scrum-keeper/credential"
DEFAULT_TEAM = "RIT"
RIT_TEAM_ID = "27ad1b97-2c57-442a-9612-e538a50a22cb"
PAGE_SIZE = 250
OP_TIMEOUT = 15
HTTP_TIMEOUT = 30
DONE_TYPES = {"completed", "canceled"}
TRIAGE_TYPE = "triage"  # 起票を検討中の物。進める候補には出さん
WAIT_LABEL_PREFIX = "待ち:"
PRIORITY_MAP = {1: "P0", 2: "P1", 3: "P2"}  # Urgent→P0 / High→P1 / Medium→P2（付録 2B）
PRIORITY_ORDER = {1: 0, 2: 1, 3: 2, 4: 3}  # 並べ用。0（無し）は最後
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class LinearError(Exception):
    """読み取りの誤り（応答の形、team が無いなど）。"""


class LinearUnavailable(LinearError):
    """Linear に届かん（op・鍵・通信・schema の食い違い）。この回は諦めて rc 6。"""


# ------------------------------------------------------------------ 型
@dataclass
class LinearIssue:
    id: str
    identifier: str  # "RIT-43"。台帳と CSV に出すのはこれだけ（題名は持たない）
    number: int = 0
    priority: int = 0  # 0 無し / 1 Urgent / 2 High / 3 Medium / 4 Low
    estimate: float | None = None
    due_date: dt.date | None = None
    updated_at: dt.datetime | None = None
    state_name: str = ""
    state_type: str = ""
    labels: list[str] = field(default_factory=list)
    cycle_id: str | None = None

    @property
    def is_unfinished(self) -> bool:
        return self.state_type not in DONE_TYPES

    @property
    def is_wait(self) -> bool:
        return any(x.startswith(WAIT_LABEL_PREFIX) for x in self.labels)

    @property
    def prio(self) -> str | None:
        """GitHub 側の並べに合わせた字。Low と無しは None。"""
        return PRIORITY_MAP.get(self.priority)


@dataclass
class LinearCycle:
    id: str
    number: int = 0
    name: str = ""
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None
    issues: list[LinearIssue] = field(default_factory=list)


def order_key(i: LinearIssue):
    """優先度 → 締め → 番号。ordering.py の order_key の Linear 版。"""
    pr = PRIORITY_ORDER.get(i.priority, 4)
    due = i.due_date.toordinal() if i.due_date else 10**9
    return (pr, due, i.number)


# ------------------------------------------------------------------ 鍵（op read。値は絶対に出さん・残さん）
def op_path() -> str | None:
    env = os.environ.get("SCRUM_KEEPER_OP")
    if env:
        return env
    return shutil.which("op")


def op_read(ref: str) -> str:
    """op:// 参照をその場で読む。失敗の文言に中身（エラーの本文）を載せん。"""
    op = op_path()
    if not op:
        raise LinearUnavailable("op CLI not found")
    try:
        p = subprocess.run([op, "read", ref], capture_output=True, text=True, timeout=OP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        raise LinearUnavailable("op read failed")
    if p.returncode != 0:
        raise LinearUnavailable("op read failed")  # stderr は漏らさん
    key = p.stdout.strip()
    if not key:
        raise LinearUnavailable("op read returned empty")
    return key


# ------------------------------------------------------------------ 読み取りの形
ISSUE_FIELDS = ("id identifier number priority estimate dueDate updatedAt "
                "state{id name type} labels{nodes{name}} cycle{id}")

TEAM_Q = "query($id:String!){team(id:$id){id key name}}"

TEAMS_Q = "query{teams(first:100){nodes{id key name}}}"

CYCLE_Q = ("query($id:String!,$after:String){team(id:$id){activeCycle{id number name startsAt endsAt "
           f"issues(first:{PAGE_SIZE},after:$after){{pageInfo{{hasNextPage endCursor}} nodes{{{ISSUE_FIELDS}}}}}}}}}")

ISSUES_Q = (f"query($id:String!,$after:String){{team(id:$id){{issues(first:{PAGE_SIZE},after:$after)"
            f"{{pageInfo{{hasNextPage endCursor}} nodes{{{ISSUE_FIELDS}}}}}}}}}")

SCHEMA_Q = ('query{Team:__type(name:"Team"){fields{name}} Cycle:__type(name:"Cycle"){fields{name}} '
            'Issue:__type(name:"Issue"){fields{name}} WorkflowState:__type(name:"WorkflowState"){fields{name}}}')

# この番が読む名前。無ければ schema が違う → LinearUnavailable
REQUIRED_FIELDS = {
    "Team": {"id", "key", "name", "activeCycle", "issues"},
    "Cycle": {"id", "number", "name", "startsAt", "endsAt", "issues"},
    "Issue": {"id", "identifier", "number", "priority", "estimate", "dueDate", "updatedAt",
              "state", "cycle", "labels"},
    "WorkflowState": {"id", "name", "type"},
}


def schema_missing(types: dict) -> list[str]:
    """types = {"Team": {"field",...}, ...} に対して、要る名前で無い物の一覧。"""
    missing = []
    for tname, need in REQUIRED_FIELDS.items():
        have = set(types.get(tname) or ())
        missing += [f"{tname}.{n}" for n in sorted(need - have)]
    return missing


def parse_issue_node(nd: dict) -> LinearIssue:
    """Linear の Issue ノードから LinearIssue。title は読まん（持たないので漏れようがない）。"""
    st = nd.get("state") or {}
    due = nd.get("dueDate")
    return LinearIssue(
        id=nd.get("id") or "",
        identifier=nd.get("identifier") or "",
        number=int(nd.get("number") or 0),
        priority=int(nd.get("priority") or 0),
        estimate=nd.get("estimate"),
        due_date=dt.date.fromisoformat(due) if due else None,
        updated_at=parse_ts(nd.get("updatedAt")),
        state_name=st.get("name") or "",
        state_type=st.get("type") or "",
        labels=[x.get("name") or "" for x in ((nd.get("labels") or {}).get("nodes") or [])],
        cycle_id=((nd.get("cycle") or {}).get("id")),
    )


def parse_cycle_node(node: dict, issues: list[LinearIssue]) -> LinearCycle:
    return LinearCycle(id=node.get("id") or "", number=int(node.get("number") or 0),
                       name=node.get("name") or "",
                       starts_at=parse_ts(node.get("startsAt")), ends_at=parse_ts(node.get("endsAt")),
                       issues=issues)


class LinearClient:
    """本物。Authorization に鍵をそのまま付ける（個人キーは Bearer を付けん）。"""

    def __init__(self, key: str, endpoint: str = API_URL):
        self._key = key  # メモリの中だけ。repr もログも出さん
        self.endpoint = endpoint
        self.calls = 0
        self._schema_ok = False

    def __repr__(self):  # うっかり repr されても鍵が出んように
        return f"LinearClient(endpoint={self.endpoint!r}, calls={self.calls})"

    def _gql(self, query: str, **variables) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(self.endpoint, data=body, method="POST",
                                     headers={"Authorization": self._key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise LinearUnavailable("linear auth rejected")
            raise LinearUnavailable(f"linear http {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LinearUnavailable("linear unreachable")
        try:
            d = json.loads(payload)
        except ValueError:
            raise LinearError("unparseable response")
        errors = d.get("errors") if isinstance(d, dict) else None
        if errors and not d.get("data"):
            raise LinearError("graphql error: " + str(errors[0].get("message", ""))[:120])
        return d

    # ---- schema ----
    def check_schema(self) -> None:
        """読む前に 1 回。フィールド名が違えば LinearUnavailable。"""
        if self._schema_ok:
            return
        d = self._gql(SCHEMA_Q)
        types = {name: {f.get("name") for f in (t or {}).get("fields") or []}
                 for name, t in (d.get("data") or {}).items()}
        missing = schema_missing(types)
        if missing:
            raise LinearUnavailable("schema drift: " + ", ".join(missing))
        self._schema_ok = True

    # ---- 読み ----
    def viewer(self) -> dict:
        d = self._gql("query{viewer{id name}}")
        return d["data"]["viewer"]

    def resolve_team(self, ref: str) -> dict:
        """キー（RIT）でも UUID でも受ける。返すのは {id, key, name}。"""
        if UUID_RE.match(ref):
            d = self._gql(TEAM_Q, id=ref)
            team = (d["data"] or {}).get("team")
        else:
            d = self._gql(TEAMS_Q)
            nodes = ((d["data"] or {}).get("teams") or {}).get("nodes") or []
            team = next((t for t in nodes if (t.get("key") or "").upper() == ref.upper()), None)
        if not team:
            raise LinearError(f"team {ref} not found")
        return team

    def read_active_cycle(self, team_id: str) -> LinearCycle | None:
        """今の Cycle とその課題（全頁）。Cycle が無ければ None。"""
        nodes: list[dict] = []
        after = None
        cycle_node = None
        while True:
            d = self._gql(CYCLE_Q, id=team_id, after=after)
            ac = ((d["data"] or {}).get("team") or {}).get("activeCycle")
            if ac is None:
                return None
            cycle_node = cycle_node or ac
            page = ac["issues"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return parse_cycle_node(cycle_node, [parse_issue_node(n) for n in nodes])

    def read_team_issues(self, team_id: str) -> list[LinearIssue]:
        """チームの課題ぜんぶ（全頁）。Cycle 未所属の拾いは呼び出し側。"""
        out: list[LinearIssue] = []
        after = None
        while True:
            d = self._gql(ISSUES_Q, id=team_id, after=after)
            team = (d["data"] or {}).get("team")
            if team is None:
                raise LinearError(f"team {team_id} not found")
            page = team["issues"]
            out += [parse_issue_node(n) for n in page["nodes"]]
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]


class FakeLinearClient:
    """試験と --fixture 用。本物と同じ読み取り面だけ。書き込みの面はそもそも無い。"""

    def __init__(self, types: dict | None = None, teams: list[dict] | None = None,
                 active_cycles: dict | None = None, issues: dict | None = None):
        self.types = types or {}
        self.teams = teams or []
        self.active_cycles = active_cycles or {}  # team key -> cycle node
        self.issues = issues or {}  # team key -> [issue node]
        self.calls = 0
        self.schema_checked = False

    @classmethod
    def from_fixture_dir(cls, directory: Path) -> "FakeLinearClient":
        types, teams, cycles, issues = {}, [], {}, {}
        for p in sorted(Path(directory).glob("*.json")):
            d = json.loads(p.read_text())
            lin = d.get("linear")
            if not lin:
                continue
            types.update(lin.get("types") or {})
            teams += lin.get("teams") or []
            cycles.update(lin.get("activeCycles") or {})
            for key, nodes in (lin.get("issues") or {}).items():
                issues.setdefault(key, []).extend(nodes)
        return cls(types, teams, cycles, issues)

    # ---- 本物と同じ面 ----
    def check_schema(self) -> None:
        self.calls += 1
        missing = schema_missing(self.types)
        if missing:
            raise LinearUnavailable("schema drift: " + ", ".join(missing))
        self.schema_checked = True

    def viewer(self) -> dict:
        self.calls += 1
        return {"id": "U_fake", "name": "scrum-keeper"}

    def resolve_team(self, ref: str) -> dict:
        self.calls += 1
        if UUID_RE.match(ref):
            team = next((t for t in self.teams if t.get("id") == ref), None)
        else:
            team = next((t for t in self.teams if (t.get("key") or "").upper() == ref.upper()), None)
        if not team:
            raise LinearError(f"team {ref} not found")
        return team

    def read_active_cycle(self, team_id: str) -> LinearCycle | None:
        self.calls += 1
        team = next((t for t in self.teams if t.get("id") == team_id), None)
        node = self.active_cycles.get((team or {}).get("key") or "")
        if not node:
            return None
        return parse_cycle_node(node, [parse_issue_node(n) for n in node.get("issues") or []])

    def read_team_issues(self, team_id: str) -> list[LinearIssue]:
        self.calls += 1
        team = next((t for t in self.teams if t.get("id") == team_id), None)
        if team is None:
            raise LinearError(f"team {team_id} not found")
        return [parse_issue_node(n) for n in self.issues.get(team.get("key") or "", [])]

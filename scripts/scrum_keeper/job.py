"""週の自動管理の各仕事。計画（読むだけ）と実行（ガード付きの書き込み）を分ける。

どの仕事も二度走らせて安全に作る: 動かした物は元の週にもう居らんので、同じ回の再実行は何もせん。
ログと CSV には題名を出さん。出すのは repo#番号 と件数だけ。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import sys
from collections import Counter
from dataclasses import dataclass, field

from .github_client import GhError
from .ledger import Halted, Ledger, LockBusy, write_halt
from .model import (Board, Item, Iteration, ITER_TITLE, CAPACITY_RE, JST, days_to_sunday_inclusive, to_jst,
                    week_start)
from .ordering import overflow, sort_in_progress_first, sort_plain

STALL_HOURS = 48
WEEKS_AHEAD = 8
CAP_MIN, CAP_MAX = 70.0, 282.0
DEFAULT_CAPACITY = 176.0
MAX_CONSECUTIVE_FAILURES = 3
CSV_COLUMNS = ["board", "job", "action", "repo", "number", "from_week", "to_week", "carryover_from", "carryover_to",
               "reason"]


@dataclass
class Move:
    item: Item
    to_id: str
    carry_to: int | None  # None は Carryover を書き換えん
    reason: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Ctx:
    now: object  # JST の datetime
    boards: list[Board]
    capacity: float = DEFAULT_CAPACITY
    count_from: dt.date | None = None
    capacity_ref: str = ""

    @property
    def monday(self) -> dt.date:
        return week_start(self.now)


# ------------------------------------------------------------------ 実行役
class Runner:
    def __init__(self, client, ledger: Ledger, dry_run: bool, max_writes: int, halt_file=None, log=None):
        self.client = client
        self.ledger = ledger
        self.dry_run = dry_run
        self.max_writes = max_writes
        self.halt_file = halt_file
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.rows: list[dict] = []
        self.counts: Counter = Counter()
        self.writes = 0
        self.consecutive_failures = 0
        self.failed = False

    # ---- 共通 ----
    def row(self, board: Board, job: str, action: str, item: Item | None = None, from_week="", to_week="",
            carry_from="", carry_to="", reason=""):
        self.rows.append({"board": board.ref, "job": job, "action": action, "repo": item.repo if item else "",
                          "number": item.number if item else "", "from_week": from_week, "to_week": to_week,
                          "carryover_from": carry_from, "carryover_to": carry_to, "reason": reason})

    def csv_text(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(self.rows)
        return buf.getvalue()

    def _guard(self):
        if self.writes >= self.max_writes:
            raise LockBusy("per-tick write cap reached")
        self.ledger.check_budget()

    def _fail(self):
        self.failed = True
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            write_halt(self.halt_file, "3 consecutive gh failures")
            raise Halted("3 consecutive gh failures")

    def _write(self, job: str, target: str, board: Board, pre: dict, call, extra: dict | None = None) -> bool:
        """intent → 書き込み → done。ロックは書き込み 1 回ごと。"""
        self._guard()
        with self.ledger.write_lock():
            self.ledger.intent(job, target, board.ref, pre)
            try:
                call()
            except GhError:
                self.ledger.done(job, target, board.ref, False, extra)
                self.writes += 1
                self._fail()
                return False
            self.ledger.done(job, target, board.ref, True, extra)
        self.writes += 1
        self.consecutive_failures = 0
        return True

    # ---- 課題の移動 ----
    def execute_moves(self, board: Board, job: str, moves: list[Move]) -> int:
        done = 0
        for mv in sorted(moves, key=lambda m: (m.item.repo, m.item.number)):
            item = mv.item
            from_it = board.iteration_by_id(item.iteration_id)
            to_it = board.iteration_by_id(mv.to_id)
            carry_from = item.carry
            carry_to = mv.carry_to if mv.carry_to is not None else carry_from
            if not self.dry_run:
                snapshot = (item.iteration_id, item.carry)
                try:
                    live = self.client.read_item_fields(board, item)
                except GhError:
                    self._fail()
                    continue
                if tuple(live) != snapshot:
                    self.log(f"race board={board.ref} job={job}")
                    self.counts[(board.ref, job, "race")] += 1
                    continue
                ok = self._write(job, item.key, board, {"Iteration": item.iteration_id, "Carryover": carry_from},
                                 lambda: self.client.set_item_fields(board, item, mv.to_id, mv.carry_to), mv.extra)
                if not ok:
                    continue
            self.row(board, job, "move", item, from_it.start.isoformat() if from_it else "",
                     to_it.start.isoformat() if to_it else mv.to_id.removeprefix("new:"), carry_from, carry_to, mv.reason)
            item.iteration_id = mv.to_id
            if mv.carry_to is not None:
                item.carryover = mv.carry_to
            self.counts[(board.ref, job, "moved")] += 1
            done += 1
        return done


# ------------------------------------------------------------------ 共通の計画部品
def carry_to(item: Item, source_start: dt.date, count_from: dt.date | None) -> int | None:
    """Carryover に 1 を足す値。count_from より前の週から動かす時は足さん（None）。"""
    if count_from is not None and source_start >= count_from:
        return item.carry + 1
    return None


def open_work(board: Board, week: dt.date) -> list[Item]:
    it = board.iteration_by_start(week)
    if not it:
        return []
    return [i for i in board.items if i.is_open_issue and not i.is_done and i.iteration_id == it.id]


def apply_pbi_rule(board: Board, plan: dict[str, Move]) -> dict[str, Move]:
    """親（PBI）の Iteration は、子の一番遅い週。+1 はせん。動かした子か親が居る時だけ直す。"""
    issues = {i.key: i for i in board.items if i.kind == "Issue"}

    def eff(item: Item):
        return plan[item.key].to_id if item.key in plan else item.iteration_id

    for pbi in board.items:
        if pbi.kind != "Issue" or not pbi.children or pbi.state != "OPEN" or pbi.is_done:
            continue
        kids = [issues[k] for k in pbi.children if k in issues]
        if not kids or not (pbi.key in plan or any(k.key in plan for k in kids)):
            continue
        starts = [(board.iteration_by_id(eff(k)).start, eff(k)) for k in kids
                  if eff(k) and board.iteration_by_id(eff(k))]
        if not starts:
            continue
        latest = max(starts)[1]
        if latest == eff(pbi):
            continue
        plan[pbi.key] = Move(pbi, latest, plan[pbi.key].carry_to if pbi.key in plan else None, "latest-child-week")
    return plan


def family(board: Board, item: Item) -> list[Item]:
    issues = {i.key: i for i in board.items if i.kind == "Issue"}
    kids = [issues[k] for k in item.children if k in issues and issues[k].is_open_issue and not issues[k].is_done]
    return [item] + kids


def find_mismatches(board: Board, week: dt.date) -> list[tuple[Item, str]]:
    it = board.iteration_by_start(week)
    if not it:
        return []
    out = []
    for i in board.items:
        if i.kind != "Issue" or i.iteration_id != it.id:
            continue
        if i.state == "CLOSED" and not i.is_done:
            out.append((i, "closed-not-done"))
        elif i.state == "OPEN" and i.is_done:
            out.append((i, "open-done"))
    return out


# ------------------------------------------------------------------ extend
def job_extend(rn: Runner, board: Board, ctx: Ctx) -> list[dt.date]:
    need = [ctx.monday + dt.timedelta(days=7 * k) for k in range(WEEKS_AHEAD)]
    missing = [d for d in need if not board.iteration_by_start(d)]
    if not missing:
        return []
    added = [Iteration("new:" + d.isoformat(), ITER_TITLE.format(d.isoformat()), d, board.iteration_duration)
             for d in missing]
    full = sorted(board.iterations + added, key=lambda i: i.start)
    if not rn.dry_run:
        before = rn.client.read_board(board.owner, board.number)
        counts_before = Counter(i.iteration_id for i in before.items if i.iteration_id)
        ok = rn._write("extend", board.ref, board, {"iterations": [i.id for i in board.iterations]},
                       lambda: rn.client.update_iteration_config(board, full))
        if not ok:
            return []
        after = rn.client.read_board(board.owner, board.number)
        counts_after = Counter(i.iteration_id for i in after.items if i.iteration_id)
        for iid, n in counts_before.items():
            if counts_after.get(iid, 0) < n:
                write_halt(rn.halt_file, "extend lowered an iteration item count")
                raise Halted("extend lowered an iteration item count")
        board.iterations = after.iterations
    else:
        board.iterations = full
    for d in missing:
        rn.row(board, "extend", "add-iteration", from_week="", to_week=d.isoformat())
    rn.counts[(board.ref, "extend", "added")] += len(missing)
    return missing


def job_fill_new_week(rn: Runner, board: Board, ctx: Ctx, week: dt.date) -> None:
    """新しい 8 週目を、週に入っとらん物の上から埋める（待ちの付いた物は入れん）。"""
    target = board.iteration_by_start(week)
    if not target:
        return
    cands = sort_plain([i for i in board.items if i.is_open_issue and not i.is_done and not i.wait
                        and not i.iteration_id])
    plan: dict[str, Move] = {}
    total = 0.0
    for c in cands:
        if c.key in plan:
            continue
        group = [g for g in family(board, c) if not g.iteration_id and g.key not in plan]
        est = sum(g.est for g in group)
        if plan and total + est > ctx.capacity:
            break
        total += est
        for g in group:
            plan[g.key] = Move(g, target.id, None, "fill-new-week")
    rn.execute_moves(board, "extend", list(apply_pbi_rule(board, plan).values()))


# ------------------------------------------------------------------ carryover
def job_carryover(rn: Runner, board: Board, ctx: Ctx) -> None:
    prev = ctx.monday - dt.timedelta(days=7)
    src, dst = board.iteration_by_start(prev), board.iteration_by_start(ctx.monday)
    if not src or not dst:
        return
    plan: dict[str, Move] = {}
    for i in board.items:
        if i.kind != "Issue" or i.iteration_id != src.id:
            continue
        if i.state == "OPEN" and not i.is_done:
            plan[i.key] = Move(i, dst.id, carry_to(i, prev, ctx.count_from), "previous-week-open")
    for i, why in find_mismatches(board, prev):
        rn.row(board, "carryover", "mismatch", i, prev.isoformat(), "", reason=why)
        rn.counts[(board.ref, "carryover", "mismatch")] += 1
    rn.execute_moves(board, "carryover", list(apply_pbi_rule(board, plan).values()))


# ------------------------------------------------------------------ stalled
def job_stalled_a(rn: Runner, board: Board, ctx: Ctx) -> None:
    cur, nxt = board.iteration_by_start(ctx.monday), board.iteration_by_start(ctx.monday + dt.timedelta(days=7))
    if not cur or not nxt:
        return
    limit = dt.timedelta(hours=STALL_HOURS)
    plan: dict[str, Move] = {}
    for i in board.items:
        if not (i.is_open_issue and i.iteration_id == cur.id and i.wait and i.updated_at):
            continue
        if ctx.now - to_jst(i.updated_at) > limit:
            plan[i.key] = Move(i, nxt.id, carry_to(i, ctx.monday, ctx.count_from), "wait-48h", {"wait": i.wait})
    rn.execute_moves(board, "stalled-a", list(apply_pbi_rule(board, plan).values()))


def job_stalled_b(rn: Runner, board: Board, ctx: Ctx) -> None:
    cur, nxt = board.iteration_by_start(ctx.monday), board.iteration_by_start(ctx.monday + dt.timedelta(days=7))
    if not cur or not nxt:
        return
    ordered = sort_in_progress_first(open_work(board, ctx.monday))
    limit = days_to_sunday_inclusive(ctx.now) * (ctx.capacity / 7)
    plan = {i.key: Move(i, nxt.id, carry_to(i, ctx.monday, ctx.count_from), "over-capacity")
            for i in overflow(ordered, limit)}
    rn.execute_moves(board, "stalled-b", list(apply_pbi_rule(board, plan).values()))


# ------------------------------------------------------------------ okawari
def job_okawari(rn: Runner, board: Board, ctx: Ctx) -> None:
    cur, nxt = board.iteration_by_start(ctx.monday), board.iteration_by_start(ctx.monday + dt.timedelta(days=7))
    if not cur or not nxt:
        return
    if any(not i.wait for i in open_work(board, ctx.monday)):
        return  # まだ今週にやる物がある
    open_est = sum(i.est for b in ctx.boards for i in open_work(b, ctx.monday))
    limit = days_to_sunday_inclusive(ctx.now) * (ctx.capacity / 7)
    budget = limit - open_est
    cands = sort_plain([i for i in board.items if i.is_open_issue and not i.is_done and not i.wait
                        and i.iteration_id == nxt.id])
    plan: dict[str, Move] = {}
    total = 0.0
    for c in cands:
        if c.key in plan:
            continue
        group = [g for g in family(board, c) if g.key not in plan and not (g.wait and g is not c)]
        est = sum(g.est for g in group)
        if est > limit:
            continue  # 引いても見込み無し (b) がすぐ来週へ戻す。引くたびに Carryover が増えるので最初から引かん
        if plan and total + est > budget:
            break
        total += est
        for g in group:
            plan[g.key] = Move(g, cur.id, None, "okawari")
    rn.execute_moves(board, "okawari", list(apply_pbi_rule(board, plan).values()))


# ------------------------------------------------------------------ sync
def job_sync(rn: Runner, board: Board, ctx: Ctx) -> None:
    repos = rn.client.list_mapped_repos(board)
    if not repos:
        return
    on_board = {i.key for i in board.items if i.kind == "Issue"}
    for repo in sorted(repos):
        for number, node_id in rn.client.list_open_issues(repo):
            key = f"{repo}#{number}"
            if key in on_board:
                continue
            item = Item(item_id="", kind="Issue", repo=repo, number=number, node_id=node_id, state="OPEN",
                        status="Backlog")
            if not rn.dry_run:
                holder = {}

                def call():
                    holder["id"] = rn.client.add_item_backlog(board, node_id)

                if not rn._write("sync", key, board, {}, call):
                    continue
                item.item_id = holder["id"]
            board.items.append(item)
            on_board.add(key)
            rn.row(board, "sync", "add-to-board", item, reason="status=Backlog")
            rn.counts[(board.ref, "sync", "added")] += 1


# ------------------------------------------------------------------ capacity
def closed_estimate_last_week(boards: list[Board], monday: dt.date) -> float:
    start = dt.datetime.combine(monday - dt.timedelta(days=7), dt.time(0), tzinfo=JST)
    end = dt.datetime.combine(monday, dt.time(0), tzinfo=JST)
    total = 0.0
    for b in boards:
        for i in b.items:
            if i.kind == "Issue" and i.state == "CLOSED" and i.closed_at and start <= to_jst(i.closed_at) < end:
                total += i.est
    return total


def job_capacity(rn: Runner, ctx: Ctx) -> None:
    if ctx.count_from is None or ctx.monday < ctx.count_from:
        return  # count_from より前の週は置き換えん
    new = min(CAP_MAX, max(CAP_MIN, closed_estimate_last_week(ctx.boards, ctx.monday)))
    if new == ctx.capacity:
        return
    target = next((b for b in ctx.boards if b.ref == ctx.capacity_ref), None)
    if target is None or not CAPACITY_RE.search(target.description):
        rn.log("capacity: board for the token is not a target, read-only")
        return  # 目印を置いた GitHub Project が今回の相手でなければ、読むだけで書かん
    token = f"scrum-keeper:capacity={new:g}h count_from={ctx.count_from.isoformat()}"
    text = CAPACITY_RE.sub(token, target.description)
    if not rn.dry_run:
        if not rn._write("capacity", target.ref, target, {"description": target.description},
                         lambda: rn.client.set_description(target, text)):
            return
    target.description = text
    rn.row(target, "capacity", "set-capacity", reason=f"{ctx.capacity:g}->{new:g}")
    rn.counts[(target.ref, "capacity", "set")] += 1
    ctx.capacity = new


# ------------------------------------------------------------------ report
def report_body(rn: Runner, board: Board, ctx: Ctx) -> tuple[str, int]:
    monday = ctx.monday
    prev = monday - dt.timedelta(days=7)
    tz = ctx.now.tzinfo
    since = dt.datetime.combine(prev, dt.time(0), tzinfo=tz)
    until = ctx.now + dt.timedelta(seconds=1)
    rec = lambda job: rn.ledger.records(job, board.ref, since, until)
    closed = [i for i in board.items if i.kind == "Issue" and i.state == "CLOSED" and i.closed_at
              and since <= to_jst(i.closed_at) < dt.datetime.combine(monday, dt.time(0), tzinfo=tz)]
    waits = Counter(r.get("wait", "") for r in rec("stalled-a"))
    cur = board.iteration_by_start(monday)
    empty_est = sum(1 for i in board.items if i.is_open_issue and cur and i.iteration_id == cur.id
                    and i.estimate is None)
    resplit = sorted(i.key for i in board.items if i.is_open_issue and i.carry >= 2)
    mism = find_mismatches(board, prev)
    lines = [f"scrum-keeper:week={monday.isoformat()}",
             f"closed={len(closed)} carried={len(rec('carryover'))} "
             f"stalled={len(rec('stalled-a')) + len(rec('stalled-b'))} okawari={len(rec('okawari'))}",
             "stalled-wait: " + (" ".join(f"{k}={v}" for k, v in sorted(waits.items())) or "none"),
             f"resplit-candidates(carryover>=2): {len(resplit)}" + (" " + ", ".join(resplit[:20]) if resplit else ""),
             f"mismatch={len(mism)} empty-estimate={empty_est}"]
    return "\n".join(lines), len(mism)


def job_report(rn: Runner, board: Board, ctx: Ctx) -> None:
    body, mismatches = report_body(rn, board, ctx)
    header = body.splitlines()[0]
    if any(b.splitlines()[:1] == [header] for b in rn.client.list_status_updates(board)):
        return  # 同じ週の報告はもう出した
    if not rn.dry_run:
        status = "AT_RISK" if mismatches else "ON_TRACK"
        if not rn._write("report", board.ref, board, {},
                         lambda: rn.client.create_status_update(board, body, ctx.monday,
                                                                ctx.monday + dt.timedelta(days=6), status)):
            return
    rn.row(board, "report", "status-update", reason=header)
    rn.counts[(board.ref, "report", "posted")] += 1

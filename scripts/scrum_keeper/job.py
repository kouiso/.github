"""週の自動管理の各仕事。計画（読むだけ）と実行（ガード付きの書き込み）を分ける。

どの仕事も二度走らせて安全に作る: 動かした物は元の週にもう居らんので、同じ回の再実行は何もせん。
ログと CSV には題名を出さん。出すのは repo#番号 と件数だけ。

1 週の量は全部の Project で 1 つ。stalled-b・okawari・extend の埋めは同じ「量」と同じ並べ順を使い、
Project をまたいで 1 回で計画する（Project ごとに量を使うと、Project の数だけ量が膨らむ）。
Carryover を足すのは carryover だけ。stalled と okawari は週の中の組み替えなので足さん。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .github_client import GhError, GhRateLimit
from .ledger import Halted, HaltPresent, Ledger, LockBusy, halt_present, write_halt
from .model import (Board, Item, Iteration, ITER_TITLE, CAPACITY_RE, JST, days_to_sunday_inclusive, to_jst,
                    week_start)
from .ordering import EPS, order_key, overflow

STALL_HOURS = 48
WEEKS_AHEAD = 8
CAP_MIN, CAP_MAX = 70.0, 282.0
DEFAULT_CAPACITY = 176.0
MAX_CONSECUTIVE_FAILURES = 3
WRITE_GAP = 1  # 書き込みの前に空ける秒数（gh_write.sh と同じ）
BACKOFF = (60, 120, 240)  # 上限の返事の後に待つ秒数（gh_write.sh と同じ）
RATE_LIMIT_HALT_AT = 5  # 上限の返事がこの数続いたら HALT（plan/11 自動停止 3）
CSV_COLUMNS = ["board", "job", "action", "repo", "number", "from_week", "to_week", "carryover_from", "carryover_to",
               "reason"]


def sleep_fn(seconds: float) -> None:
    """試験で差し替える。"""
    time.sleep(seconds)


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

    @property
    def next_monday(self) -> dt.date:
        return self.monday + dt.timedelta(days=7)


def limit_today(ctx: Ctx) -> float:
    """今日を含めて日曜までに入る量。stalled-b が使う。"""
    return days_to_sunday_inclusive(ctx.now) * (ctx.capacity / 7)


def limit_okawari(ctx: Ctx) -> float:
    """おかわりが引ける量の天井。翌日の stalled-b の量（日曜は今日の量）。

    今日の量まで引くと、明日の小さい量で stalled-b が押し戻す。それを毎日繰り返すので、最初から明日の量に合わせる。
    """
    return max(days_to_sunday_inclusive(ctx.now) - 1, 1) * (ctx.capacity / 7)


def week_cap(ctx: Ctx, week: dt.date) -> float:
    return limit_okawari(ctx) if week == ctx.monday else ctx.capacity


def boards_with_weeks(ctx: Ctx) -> list[Board]:
    return [b for b in ctx.boards if b.iteration_by_start(ctx.monday) and b.iteration_by_start(ctx.next_monday)]


# ------------------------------------------------------------------ 実行役
class Runner:
    def __init__(self, client, ledger: Ledger, dry_run: bool, max_writes: int, halt_file=None, log=None,
                 extend_verified: set[str] | None = None, streak_file: Path | None = None):
        self.client = client
        self.ledger = ledger
        self.dry_run = dry_run
        self.max_writes = max_writes
        self.halt_file = halt_file
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.extend_verified = extend_verified or set()
        self.streak_file = Path(streak_file) if streak_file else None
        self._streak_mem = 0
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
        # 緊急停止は書き込みの前に毎回見る。入口で 1 回見るだけだと、回の途中で置かれた HALT が効かん
        if halt_present(self.halt_file):
            raise HaltPresent("HALT present")
        if self.writes >= self.max_writes:
            raise LockBusy("per-tick write cap reached")
        self.ledger.check_budget()

    def _fail(self):
        self.failed = True
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            write_halt(self.halt_file, "3 consecutive gh failures")
            raise Halted("3 consecutive gh failures")

    # ---- 上限の返事（連続の数は回をまたいで数える）----
    def _streak(self) -> int:
        if self.streak_file and self.streak_file.exists():
            try:
                return int(self.streak_file.read_text().strip() or 0)
            except (ValueError, OSError):
                return 0
        return self._streak_mem

    def _set_streak(self, n: int) -> None:
        self._streak_mem = n
        if self.streak_file:
            if n == 0 and not self.streak_file.exists():
                return
            self.streak_file.parent.mkdir(parents=True, exist_ok=True)
            self.streak_file.write_text(f"{n}\n")

    def _note_rate_limit(self) -> None:
        n = self._streak() + 1
        self._set_streak(n)
        if n >= RATE_LIMIT_HALT_AT:
            write_halt(self.halt_file, f"{n} consecutive rate-limit replies")
            raise Halted("rate limit replies in a row")

    def _ok(self) -> None:
        self.consecutive_failures = 0
        if self._streak():
            self._set_streak(0)

    def _write(self, job: str, target: str, board: Board, pre: dict, call, extra: dict | None = None) -> bool:
        """intent → 書き込み → done。ロックは書き込み 1 回ごと。

        上限の返事は 60/120/240 秒待って 3 回まで再試行する（待つ間はロックを放す）。それでも駄目なら HALT は書かず、
        この回を止めて次の回に回す。上限の返事が 5 回続いた時だけ HALT。
        """
        attempt = 0
        while True:
            sleep_fn(WRITE_GAP)
            self._guard()
            limited = False
            with self.ledger.write_lock():
                if attempt == 0:
                    self.ledger.intent(job, target, board.ref, pre)
                try:
                    call()
                except GhRateLimit:
                    limited = True
                except GhError:
                    self.ledger.done(job, target, board.ref, False, extra)
                    self.writes += 1
                    self._fail()
                    return False
                else:
                    self.ledger.done(job, target, board.ref, True, extra)
            if not limited:
                self.writes += 1
                self._ok()
                return True
            self._note_rate_limit()
            if attempt >= len(BACKOFF):
                self.ledger.done(job, target, board.ref, False, {**(extra or {}), "rate_limited": True})
                self.writes += 1
                self.failed = True
                raise LockBusy("rate limited, retry next tick")
            sleep_fn(BACKOFF[attempt])
            attempt += 1

    # ---- 課題の移動 ----
    def execute_moves(self, board: Board, job: str, moves: list[Move]) -> int:
        done = 0
        for mv in sorted(moves, key=lambda m: (m.item.repo, m.item.number)):
            item = mv.item
            carry_from = item.carry
            carry_to = mv.carry_to if mv.carry_to is not None else carry_from
            if mv.to_id == item.iteration_id and carry_to == carry_from:
                continue  # 今の値と同じ書き込みはせん（毎回同じ値を書き続けて上限を食うため）
            from_it = board.iteration_by_id(item.iteration_id)
            to_it = board.iteration_by_id(mv.to_id)
            start, end = date_changes(board, item, to_it) if mv.to_id != item.iteration_id else (None, None)
            if not self.dry_run:
                snapshot = (item.iteration_id, item.carry)
                try:
                    live = self.client.read_item_fields(board, item)
                except GhRateLimit:
                    self._note_rate_limit()
                    raise LockBusy("rate limited, retry next tick")
                except GhError:
                    self._fail()
                    continue
                if tuple(live) != snapshot:
                    self.log(f"race board={board.ref} job={job}")
                    self.counts[(board.ref, job, "race")] += 1
                    continue
                ok = self._write(job, item.key, board, {"Iteration": item.iteration_id, "Carryover": carry_from,
                                                        "Start date": str(item.start_date or ""),
                                                        "End date": str(item.end_date or "")},
                                 lambda: self.client.set_item_fields(board, item, mv.to_id, mv.carry_to, start, end),
                                 mv.extra)
                if not ok:
                    continue
            self.row(board, job, "move", item, from_it.start.isoformat() if from_it else "",
                     to_it.start.isoformat() if to_it else mv.to_id.removeprefix("new:"), carry_from, carry_to, mv.reason)
            item.iteration_id = mv.to_id
            if mv.carry_to is not None:
                item.carryover = mv.carry_to
            if start is not None:
                item.start_date = start
            if end is not None:
                item.end_date = end
            self.counts[(board.ref, job, "moved")] += 1
            done += 1
        return done


def date_changes(board: Board, item: Item, to_it: Iteration | None) -> tuple[dt.date | None, dt.date | None]:
    """週を動かす時に Start/End date も同じ書き込みで直す（board_place.py と同じ組）。

    Start＝その週の月曜、End＝その週の日曜。End が動かす先の週の中に既にあれば（期日を言われた物）残す。
    その Project に項目が無ければ書かん。
    """
    if to_it is None:
        return None, None
    start = to_it.start if "Start date" in board.field_ids and item.start_date != to_it.start else None
    keep = item.end_date is not None and to_it.start <= item.end_date <= to_it.end
    end = to_it.end if "End date" in board.field_ids and not keep else None
    return start, end


# ------------------------------------------------------------------ 共通の計画部品
def carry_to(item: Item, source_start: dt.date, count_from: dt.date | None) -> int | None:
    """Carryover に 1 を足す値。count_from より前の週から動かす時は足さん（None）。"""
    if count_from is not None and source_start >= count_from:
        return item.carry + 1
    return None


def open_work(board: Board, week: dt.date) -> list[Item]:
    """その週に居る open で Done でない課題（Wait も含む）。"""
    it = board.iteration_by_start(week)
    if not it:
        return []
    return [i for i in board.items if i.is_open_issue and not i.is_done and i.iteration_id == it.id]


def workable(board: Board, week: dt.date) -> list[Item]:
    """その週で今やれる物（Wait の付いた物は除く）。量の数えも、おかわりの「空」の判定もこれ 1 つ。"""
    return [i for i in open_work(board, week) if not i.wait]


def ordered_entries(entries: list[tuple[Board, Item]], in_progress_first: bool = False) -> list[tuple[Board, Item]]:
    """Project をまたいで 1 つの順に並べる（Priority → End date → repo → 番号）。"""
    key = (lambda e: (0 if e[1].status == "In Progress" else 1,) + order_key(e[1])) if in_progress_first \
        else (lambda e: order_key(e[1]))
    return sorted(entries, key=key)


def apply_pbi_rule(board: Board, plan: dict[str, Move]) -> dict[str, Move]:
    """親（PBI）の Iteration は、子の一番遅い週。+1 はせん。動かした子か親が居る時だけ直す。"""
    issues = {i.key: i for i in board.items if i.kind == "Issue"}

    def eff(item: Item):
        return plan[item.key].to_id if item.key in plan else item.iteration_id

    for pbi in board.items:
        if pbi.kind != "Issue" or not pbi.children or pbi.state != "OPEN" or pbi.is_done:
            continue
        # 閉じた子は「終わる週」を決めん。open の子が居らん親は計画のまま（Done 以外は繰り越す）
        kids = [issues[k] for k in pbi.children if k in issues and issues[k].is_open_issue and not issues[k].is_done]
        if not kids or not (pbi.key in plan or any(k.key in plan for k in kids)):
            continue
        starts = [(board.iteration_by_id(eff(k)).start, eff(k)) for k in kids
                  if eff(k) and board.iteration_by_id(eff(k))]
        if not starts:
            continue
        latest = max(starts)[1]
        if latest == eff(pbi):
            continue
        carry = plan[pbi.key].carry_to if pbi.key in plan else None
        if latest == pbi.iteration_id and carry is None:
            plan.pop(pbi.key, None)  # 子の一番遅い週は親が今居る週。動かす理由が無い
            continue
        plan[pbi.key] = Move(pbi, latest, carry, "latest-child-week")
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
        if not ({board.ref, "*"} & rn.extend_verified):
            # 反復の設定を丸ごと書き直す呼び出しが id を保つかは、試しの写しで測るまで分からん（保たんと全件の週が消える）
            rn.log(f"extend blocked board={board.ref}: not listed in extend-verified.txt")
            rn.counts[(board.ref, "extend", "blocked")] += 1
            rn.failed = True
            return []
        before = rn.client.read_board(board.owner, board.number)
        counts_before = Counter(i.iteration_id for i in before.items if i.iteration_id)
        ok = rn._write("extend", board.ref, board,
                       {"iterations": [i.id for i in board.iterations], "counts": dict(sorted(counts_before.items()))},
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


def job_fill_new_week(rn: Runner, ctx: Ctx, week: dt.date, filled: list[Board]) -> None:
    """新しくできた 8 週目を埋める。週に入っとらん物を 1 つの順（全 Project）で、量に空きがある一番早い週へ置く。

    置き場所が 8 週目になった物だけ書く。もっと早い週に空きがある物は触らず「週なし」のまま見える所に残す
    （P0 を 7 週先に隠さんため。早い週へ置くのは board_place.py の仕事）。早い週に置く予定の物は、その週の量に数える。
    """
    weeks = [ctx.monday + dt.timedelta(days=7 * k) for k in range(WEEKS_AHEAD)]
    load = {w: sum(i.est for b in ctx.boards for i in workable(b, w)) for w in weeks}
    entries = [(b, i) for b in ctx.boards for i in b.items
               if i.is_open_issue and not i.is_done and not i.wait and not i.iteration_id]
    plans: dict[str, dict[str, Move]] = {}
    placed: set[tuple[str, str]] = set()
    for b, c in ordered_entries(entries):
        if (b.ref, c.key) in placed:
            continue
        group = [g for g in family(b, c) if not g.iteration_id and (b.ref, g.key) not in placed]
        est = sum(g.est for g in group)
        slot = next((w for w in weeks if load[w] + est <= week_cap(ctx, w) + EPS), None)
        if slot is None:
            continue
        load[slot] += est
        for g in group:
            placed.add((b.ref, g.key))
        target = b.iteration_by_start(week) if slot == week and b in filled else None
        if target:
            for g in group:
                plans.setdefault(b.ref, {})[g.key] = Move(g, target.id, None, "fill-new-week")
    for b in ctx.boards:
        if b.ref in plans:
            rn.execute_moves(b, "extend", list(apply_pbi_rule(b, plans[b.ref]).values()))


# ------------------------------------------------------------------ carryover
def job_carryover(rn: Runner, board: Board, ctx: Ctx) -> None:
    """終わった週に残る open で Done でない物を今週へ。どの終わった週からでも（Mac が止まっとった週も拾う）。"""
    dst = board.iteration_by_start(ctx.monday)
    if not dst:
        return
    ended = {it.id: it for it in board.iterations if it.end < ctx.monday}
    plan: dict[str, Move] = {}
    for i in board.items:
        if i.kind != "Issue" or i.iteration_id not in ended:
            continue
        if i.state == "OPEN" and not i.is_done:
            plan[i.key] = Move(i, dst.id, carry_to(i, ended[i.iteration_id].start, ctx.count_from), "ended-week-open")
    prev = ctx.monday - dt.timedelta(days=7)
    for i, why in find_mismatches(board, prev):
        rn.row(board, "carryover", "mismatch", i, prev.isoformat(), "", reason=why)
        rn.counts[(board.ref, "carryover", "mismatch")] += 1
    rn.execute_moves(board, "carryover", list(apply_pbi_rule(board, plan).values()))


# ------------------------------------------------------------------ stalled
def job_stalled_a(rn: Runner, board: Board, ctx: Ctx) -> None:
    cur, nxt = board.iteration_by_start(ctx.monday), board.iteration_by_start(ctx.next_monday)
    if not cur or not nxt:
        return
    limit = dt.timedelta(hours=STALL_HOURS)
    plan: dict[str, Move] = {}
    for i in board.items:
        if not (i.is_open_issue and i.iteration_id == cur.id and i.wait and i.updated_at):
            continue
        if ctx.now - to_jst(i.updated_at) > limit:
            plan[i.key] = Move(i, nxt.id, None, "wait-48h", {"wait": i.wait})
    rn.execute_moves(board, "stalled-a", list(apply_pbi_rule(board, plan).values()))


def job_stalled_b(rn: Runner, ctx: Ctx) -> None:
    """日曜までに入りきらん分を来週へ。量は全 Project で 1 つなので、全部を 1 つの順に並べて 1 回で切る。"""
    movable = {b.ref for b in boards_with_weeks(ctx)}
    entries = ordered_entries([(b, i) for b in ctx.boards for i in workable(b, ctx.monday)], in_progress_first=True)
    over = {id(i) for i in overflow([i for _, i in entries], limit_today(ctx))}
    plans: dict[str, dict[str, Move]] = {}
    for b, i in entries:
        if id(i) in over and b.ref in movable:
            nxt = b.iteration_by_start(ctx.next_monday)
            plans.setdefault(b.ref, {})[i.key] = Move(i, nxt.id, None, "over-capacity")
    for b in ctx.boards:
        if b.ref in plans:
            rn.execute_moves(b, "stalled-b", list(apply_pbi_rule(b, plans[b.ref]).values()))


# ------------------------------------------------------------------ okawari
def job_okawari(rn: Runner, ctx: Ctx) -> None:
    """今週にやる物が無くなった Project へ、来週の上から。引ける量は全 Project で 1 つ。

    引く量は stalled-b が翌日に押し戻さん量までにする（同じ量を両方が見る）。そうでないと引いては戻すが回り続ける。
    """
    cur_w, nxt_w = ctx.monday, ctx.next_monday
    eligible = [b for b in boards_with_weeks(ctx) if not workable(b, cur_w)]
    if not eligible:
        return
    limit = limit_okawari(ctx)
    budget = limit - sum(i.est for b in ctx.boards for i in workable(b, cur_w))
    entries = [(b, i) for b in eligible for i in b.items
               if i.is_open_issue and not i.is_done and not i.wait
               and i.iteration_id == b.iteration_by_start(nxt_w).id]
    plans: dict[str, dict[str, Move]] = {}
    total = 0.0
    for b, c in ordered_entries(entries):
        plan = plans.setdefault(b.ref, {})
        if c.key in plan:
            continue
        issues = {i.key: i for i in b.items if i.kind == "Issue"}
        kids = [issues[k] for k in c.children if k in issues and issues[k].is_open_issue and not issues[k].is_done]
        # 待ちの子が居る親は動かさん（親の週は子の一番遅い週で、待ちの子は来週に残るため）。待ちの子も引かん
        members = [k for k in kids if not k.wait] if any(k.wait for k in kids) else [c] + kids
        cur = b.iteration_by_start(cur_w)
        group = [g for g in members if g.key not in plan and g.iteration_id != cur.id]
        if not group:
            continue
        est = sum(g.est for g in group)
        if est > limit + EPS:
            continue  # 1 件で量を超える物は引いても stalled-b が戻す。最初から引かん
        if total + est > budget + EPS:
            break
        total += est
        for g in group:
            plan[g.key] = Move(g, cur.id, None, "okawari")
    for b in ctx.boards:
        if plans.get(b.ref):
            rn.execute_moves(b, "okawari", list(apply_pbi_rule(b, plans[b.ref]).values()))


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
    ended_ids = {it.id for it in board.iterations if it.end < monday}
    stranded = sum(1 for i in board.items if i.is_open_issue and not i.is_done and i.iteration_id in ended_ids)
    lines = [f"scrum-keeper:week={monday.isoformat()}",
             f"closed={len(closed)} carried={len(rec('carryover'))} "
             f"stalled={len(rec('stalled-a')) + len(rec('stalled-b'))} okawari={len(rec('okawari'))}",
             "stalled-wait: " + (" ".join(f"{k}={v}" for k, v in sorted(waits.items())) or "none"),
             f"resplit-candidates(carryover>=2): {len(resplit)}" + (" " + ", ".join(resplit[:20]) if resplit else ""),
             f"mismatch={len(mism)} stranded={stranded} empty-estimate={empty_est}"]
    return "\n".join(lines), len(mism) + stranded


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

#!/usr/bin/env python3
"""GitHub Project の週の管理を 15 分ごとに回す入口（launchd から呼ぶ）。

使い方:
  scrum_keeper.py --board <owner>/<n> --job all
  scrum_keeper.py --allow <file> --job all --halt-file <path> --ledger-dir <dir>
  scrum_keeper.py --board <owner>/<n> --job carryover --dry-run      # 動かす予定を CSV で出すだけ
終了コード: 0 正常（HALT・二重起動も 0）/ 1 gh の失敗 / 2 引数 / 4 上限かロック待ちで中断 / 5 gh の認証切れ / 6 HALT を書いて中断
"""
from __future__ import annotations

import sys
from pathlib import Path

if not __package__:  # python3 scripts/scrum_keeper/scrum_keeper.py で直接呼ばれた時
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scrum_keeper"

import argparse
import datetime as dt
import fcntl
import os
from .github_client import FakeClient, GhClient, GhError, gh_auth_ok
from .job import (Ctx, DEFAULT_CAPACITY, Runner, WEEKS_AHEAD, job_capacity, job_carryover, job_extend,
                  job_fill_new_week, job_okawari, job_report, job_stalled_a, job_stalled_b, job_sync)
from .ledger import Halted, HaltPresent, Ledger, LockBusy, halt_present
from .model import BOARD_MARKER, Board, parse_capacity, parse_now, to_jst, week_start

JOB_CHOICES = ["all", "extend", "carryover", "stalled", "okawari", "sync", "report", "capacity"]
STATE_DIR_DEFAULT = Path.home() / ".local/state/scrum-keeper"


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--board", help="<owner>/<number>。許可リストとマーカーを無視してこの 1 つだけ")
    g.add_argument("--allow", help="<owner>/<number> を 1 行ずつ書いた許可リスト")
    ap.add_argument("--job", choices=JOB_CHOICES, default="all")
    ap.add_argument("--now", help="ISO8601。週の計算は常に Asia/Tokyo")
    ap.add_argument("--dry-run", action="store_true", help="何も書かず、動かす予定を CSV で標準出力へ")
    ap.add_argument("--halt-file")
    ap.add_argument("--ledger-dir")
    ap.add_argument("--capacity-board", help="1 週の量の目印を置いた <owner>/<number>。省くと最初の相手")
    ap.add_argument("--max-writes", type=int, default=200)
    ap.add_argument("--fixture", help="GitHub の代わりに読む保存済みの応答の置き場（試験用）")
    ap.add_argument("--state-dir", default=str(STATE_DIR_DEFAULT), help="last-run とロックの置き場")
    return ap.parse_args(argv)


def split_ref(ref: str) -> tuple[str, int]:
    owner, _, num = ref.partition("/")
    if not owner or not num.isdigit():
        raise ValueError(f"bad board ref: {ref}")
    return owner, int(num)


def read_allow(path: str) -> list[str]:
    refs = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            refs.append(line)
    return refs


def select_boards(client, args) -> list[Board]:
    if args.board:
        return [client.read_board(*split_ref(args.board))]
    boards = []
    for ref in read_allow(args.allow):
        owner, num = split_ref(ref)
        title, desc = client.read_board_head(owner, num)
        if title.startswith("_") or BOARD_MARKER not in desc:
            continue
        boards.append(client.read_board(owner, num))
    return boards


def read_last_run(state_dir: Path) -> dt.datetime | None:
    try:
        return to_jst(dt.datetime.fromisoformat((state_dir / "last-run").read_text().strip()))
    except (OSError, ValueError):
        return None


def read_extend_verified(state_dir: Path) -> set[str]:
    """反復の設定の書き直しが id を保つと、試しの写しで測り終えた Project（1 行 1 つ。`*` は全部）。"""
    try:
        lines = (state_dir / "extend-verified.txt").read_text().splitlines()
    except OSError:
        return set()
    return {ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()}


def due_jobs(job: str, now: dt.datetime, last_run: dt.datetime | None = None) -> list[str]:
    """時刻の窓は使わん。Mac が窓の間ずっと寝とっても、起きた最初の回で取り戻すため。

    どの仕事も二度走らせて安全。週に 1 回・日に 1 回の物は、前の成功した回（last-run）より後の最初の回で走る。
    """
    if job != "all":
        return {"stalled": ["stalled-a", "stalled-b"]}.get(job, [job])
    new_week = last_run is None or week_start(last_run) < week_start(now)
    new_day = last_run is None or to_jst(last_run).date() < now.date()
    jobs = ["extend"]
    if new_week:
        jobs.append("capacity")
    jobs += ["carryover", "stalled-a"]
    if new_day:
        jobs.append("stalled-b")
    jobs += ["okawari", "sync"]
    if new_week:
        jobs.append("report")
    return jobs


def run_jobs(rn: Runner, ctx: Ctx, jobs: list[str]) -> None:
    per_board = {"carryover": job_carryover, "stalled-a": job_stalled_a, "sync": job_sync, "report": job_report}
    eighth = ctx.monday + dt.timedelta(days=7 * (WEEKS_AHEAD - 1))
    for job in jobs:
        if job == "capacity":
            job_capacity(rn, ctx)
        elif job == "extend":
            filled = [b for b in ctx.boards if eighth in job_extend(rn, b, ctx)]
            if filled:
                job_fill_new_week(rn, ctx, eighth, filled)
        elif job == "stalled-b":
            job_stalled_b(rn, ctx)
        elif job == "okawari":
            job_okawari(rn, ctx)
        else:
            for b in ctx.boards:
                per_board[job](rn, b, ctx)


def main(argv=None, client=None) -> int:
    args = parse_args(argv)
    log = lambda msg: print(msg, file=sys.stderr)
    halt = Path(args.halt_file) if args.halt_file else None
    if halt_present(halt):
        print("HALT")
        return 0
    if not args.dry_run and not args.ledger_dir:
        log("--ledger-dir is required unless --dry-run")
        return 2
    state_dir = Path(args.state_dir)
    if args.dry_run:  # 読むだけなので本番の回と並んでよい
        return _tick(args, client, halt, state_dir, log)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(state_dir / "lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another instance holds the lock")
        return 0
    try:
        return _tick(args, client, halt, state_dir, log)
    finally:
        os.close(lock_fd)


def _tick(args, client, halt, state_dir: Path, log) -> int:
    now = parse_now(args.now)
    if client is None:
        if args.fixture:
            client = FakeClient.from_fixture_dir(Path(args.fixture))
        else:
            if not gh_auth_ok():
                log("gh auth failed")
                return 5
            client = GhClient()
    ledger = Ledger(Path(args.ledger_dir) if args.ledger_dir else None, halt)
    rn = Runner(client, ledger, args.dry_run, args.max_writes, halt, log,
                extend_verified=read_extend_verified(state_dir), streak_file=state_dir / "rate-limit-streak")
    try:
        ledger.check_disk()
        boards = select_boards(client, args)
        cap_ref = args.capacity_board or (boards[0].ref if boards else "")
        cap_board = next((b for b in boards if b.ref == cap_ref), None)
        if cap_board:
            cap_desc = cap_board.description
        else:
            cap_desc = client.read_board_head(*split_ref(cap_ref))[1] if cap_ref else ""
        cap = parse_capacity(cap_desc)
        ctx = Ctx(now=now, boards=boards, capacity=cap[0] if cap else DEFAULT_CAPACITY,
                  count_from=cap[1] if cap else None, capacity_ref=cap_ref)
        jobs = due_jobs(args.job, now, read_last_run(state_dir))
        log(f"tick boards={len(boards)} jobs={','.join(jobs)}")
        run_jobs(rn, ctx, jobs)
    except HaltPresent:
        print("HALT")
        return 0
    except Halted:
        log("HALT written, stopped")
        return 6
    except LockBusy as e:
        log(f"stopped: {e}")
        return 4
    except GhError as e:
        log(f"gh failure: {e}")
        return 1
    for key, n in sorted(rn.counts.items()):
        log(f"count board={key[0]} job={key[1]} {key[2]}={n}")
    if rn.failed:
        return 1
    if args.dry_run:
        sys.stdout.write(rn.csv_text())
        return 0
    if args.job == "all":  # 手で 1 つだけ走らせた回は、週・日に 1 回の仕事の目印にせん
        (state_dir / "last-run").write_text(now.isoformat(timespec="seconds") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

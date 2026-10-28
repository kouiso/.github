#!/usr/bin/env python3
"""Linear（チーム RIT）の今の Cycle を読んで、欠けを台帳に足す入口（launchd から呼ぶ）。

Linear には何も書かん。繰り越しは Linear 自身（Cycles）がやるので、この番の仕事は:
- 今の Cycle の終わってへん課題で、Estimate 無し・Priority 無しを拾って台帳に 1 行（見込み無しの Linear 版）
- 今の Cycle にやれる物が無い時、次に進めるべき課題を優先度の順に出す（Linear 版おかわり。提案だけ。
  実際に Cycle に入れるのは Linear の画面か、次の Cycle への自動繰り越し）

使い方:
  linear_sweep.py --ledger-dir <dir> [--team RIT] [--key-ref 'op://...']
  linear_sweep.py --fixture <dir> --dry-run    # op にも Linear にも触らず、計画を CSV で標準出力へ
終了コード: 0 正常（HALT・二重起動も 0）/ 1 Linear の応答の誤り / 2 引数 / 4 ロック待ちで中断 /
6 Linear に届かん（op・鍵・通信・schema）か HALT を書いて中断
"""
from __future__ import annotations

import sys
from pathlib import Path

if not __package__:  # python3 scripts/scrum_keeper/linear_sweep.py で直接呼ばれた時
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scrum_keeper"

import argparse
import csv
import datetime as dt
import fcntl
import io
import os

from .job import CSV_COLUMNS
from .ledger import Halted, HaltPresent, Ledger, LockBusy, halt_present
from .linear_client import (DEFAULT_KEY_REF, DEFAULT_TEAM, TRIAGE_TYPE, FakeLinearClient,
                            LinearClient, LinearCycle, LinearError, LinearUnavailable, op_read, order_key)
from .model import JST, parse_now

JOB = "linear"
STATE_DIR_DEFAULT = Path.home() / ".local/state/scrum-keeper"


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team", default=DEFAULT_TEAM, help="チームの key（RIT）か UUID。既定 %(default)s")
    ap.add_argument("--key-ref", default=DEFAULT_KEY_REF,
                    help="op:// の参照。実行の時だけ op read で読む（既定: %(default)s）")
    ap.add_argument("--now", help="ISO8601。Cycle の残り日数の計算は常に Asia/Tokyo")
    ap.add_argument("--dry-run", action="store_true", help="台帳にも書かず、拾った物を CSV で標準出力へ")
    ap.add_argument("--fixture", help="Linear の代わりに読む保存済みの応答の置き場（試験用）")
    ap.add_argument("--halt-file")
    ap.add_argument("--ledger-dir")
    ap.add_argument("--state-dir", default=str(STATE_DIR_DEFAULT), help="ロックの置き場（scrum_keeper と同じ）")
    ap.add_argument("--max-suggest", type=int, default=20, help="おかわり候補を出す件数の上限（既定 %(default)s）")
    return ap.parse_args(argv)


# ------------------------------------------------------------------ 計画（読んだ結果を並べるだけ）
def build_sweep(team_key: str, cycle: LinearCycle | None, issues: list, now: dt.datetime,
                max_suggest: int) -> dict:
    """今の Cycle の欠けと、進める候補。どの課題も動かさん（Linear は読むだけ）。"""
    unfinished = [i for i in cycle.issues if i.is_unfinished] if cycle else []
    workable = [i for i in unfinished if not i.is_wait]
    waits = len(unfinished) - len(workable)
    no_estimate = sorted(i.identifier for i in unfinished if i.estimate is None)
    no_priority = sorted(i.identifier for i in unfinished if i.priority == 0)
    pool = sorted((i for i in issues if i.cycle_id is None and i.is_unfinished
                   and not i.is_wait and i.state_type != TRIAGE_TYPE), key=order_key)
    if workable:
        suggest, suggest_reason = [], ""
    else:
        suggest = [i.identifier for i in pool[:max_suggest]]
        suggest_reason = "cycle-empty" if cycle else "no-active-cycle"
    days_left = ""
    if cycle and cycle.ends_at:
        days_left = (cycle.ends_at.astimezone(JST).date() - now.date()).days
    return {"team": team_key, "cycle": cycle, "days_left": days_left, "unfinished": unfinished,
            "workable": workable, "waits": waits, "no_estimate": no_estimate, "no_priority": no_priority,
            "suggest": suggest, "suggest_reason": suggest_reason}


def sweep_rows(sweep: dict) -> list[dict]:
    """dry-run の CSV 行。列は scrum_keeper の CSV_COLUMNS と同じ。題名は出さん。"""
    board = f"linear/{sweep['team']}"
    cycle_name = sweep["cycle"].name if sweep["cycle"] else ""
    rows = []
    for ident in sweep["no_estimate"]:
        rows.append({"board": board, "job": JOB, "action": "no-estimate", "repo": "linear",
                     "number": ident, "reason": cycle_name})
    for ident in sweep["no_priority"]:
        rows.append({"board": board, "job": JOB, "action": "no-priority", "repo": "linear",
                     "number": ident, "reason": cycle_name})
    for ident in sweep["suggest"]:
        rows.append({"board": board, "job": JOB, "action": "okawari-suggest", "repo": "linear",
                     "number": ident, "reason": sweep["suggest_reason"]})
    rows.append({"board": board, "job": JOB, "action": "cycle-count", "repo": "linear", "number": "",
                 "reason": (f"cycle={cycle_name or 'none'} unfinished={len(sweep['unfinished'])} "
                            f"workable={len(sweep['workable'])} waits={sweep['waits']} "
                            f"no_estimate={len(sweep['no_estimate'])} no_priority={len(sweep['no_priority'])} "
                            f"days_left={sweep['days_left']}")})
    return rows


def csv_text(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in CSV_COLUMNS})
    return buf.getvalue()


def ledger_extra(sweep: dict) -> dict:
    """台帳の done 行に足す分。identifier の一覧だけ（題名は絶対に入れん）。"""
    return {"cycle": sweep["cycle"].name if sweep["cycle"] else "",
            "cycle_number": sweep["cycle"].number if sweep["cycle"] else "",
            "days_left": sweep["days_left"],
            "unfinished": len(sweep["unfinished"]), "workable": len(sweep["workable"]),
            "waits": sweep["waits"],
            "no_estimate": sweep["no_estimate"], "no_priority": sweep["no_priority"],
            "suggest": sweep["suggest"], "suggest_reason": sweep["suggest_reason"]}


# ------------------------------------------------------------------ 入口
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
        return _tick(args, client, halt, log)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(state_dir / "lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another instance holds the lock")
        return 0
    try:
        return _tick(args, client, halt, log)
    finally:
        os.close(lock_fd)


def _tick(args, client, halt, log) -> int:
    now = parse_now(args.now)
    if client is None:
        if args.fixture:
            client = FakeLinearClient.from_fixture_dir(Path(args.fixture))
        else:
            try:
                client = LinearClient(op_read(args.key_ref))
            except LinearUnavailable as e:
                log(f"linear unavailable: {e}")
                return 6
    try:
        client.check_schema()  # 名前は未試験: 読む前に __type で確かめる
        team = client.resolve_team(args.team)
        cycle = client.read_active_cycle(team["id"])
        issues = client.read_team_issues(team["id"])
    except LinearUnavailable as e:
        log(f"linear unavailable: {e}")
        return 6
    except LinearError as e:
        log(f"linear read failed: {e}")
        return 1
    sweep = build_sweep(team["key"], cycle, issues, now, args.max_suggest)
    rows = sweep_rows(sweep)
    log(f"sweep team={team['key']} cycle={rows[-1]['reason']}")
    if args.dry_run:
        sys.stdout.write(csv_text(rows))
        return 0
    ledger = Ledger(Path(args.ledger_dir), halt)
    try:
        ledger.check_disk()
        target = f"linear/{team['key']}"
        with ledger.write_lock():  # scrum_keeper と同じ共有の鍵で 1 行足す
            ledger.done(JOB, target, target, True, ledger_extra(sweep))
    except HaltPresent:
        print("HALT")
        return 0
    except Halted:
        log("HALT written, stopped")
        return 6
    except LockBusy as e:
        log(f"stopped: {e}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())

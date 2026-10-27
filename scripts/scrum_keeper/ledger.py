"""非常停止の印、書き込みの上限、共有の書き込みロック、書く前後の記録。

記録の行の形は gh_write.sh と同じ:
{"ts","wave","step","target","class","prio","pre","phase"} を書く前に fsync、後に done を足す。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

WAVE = "K"
DEFAULT_HOURLY_CAP = 300
NORM_SHARE = 0.6
MIN_FREE_BYTES = 2 * 1024**3
LOCK_WAIT_SECONDS = 30
STALE_LOCK_SECONDS = 600


class LockBusy(Exception):
    """共有の書き込みロックが 30 秒待っても取れん。次の回に回す。"""


class Halted(Exception):
    """HALT を書いた。この回は止める。"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def halt_present(halt_file: Path | None) -> bool:
    return bool(halt_file) and Path(halt_file).exists()


def write_halt(halt_file: Path | None, reason: str) -> None:
    if not halt_file:
        return
    p = Path(halt_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"{stamp} scrum-keeper: {reason}\n")


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


class Ledger:
    def __init__(self, directory: Path | None, halt_file: Path | None = None,
                 min_free_bytes: int = MIN_FREE_BYTES, now=utc_now):
        self.dir = Path(directory) if directory else None
        self.halt_file = Path(halt_file) if halt_file else None
        self.min_free_bytes = min_free_bytes
        self._now = now
        self.writes = 0  # この回で終わった書き込みの数

    # ---- 上限 ----
    def hourly_cap(self) -> int:
        limit = DEFAULT_HOURLY_CAP
        if self.dir and (self.dir / "cap.json").exists():
            try:
                limit = int(json.loads((self.dir / "cap.json").read_text()).get("content_per_hour", limit))
            except (ValueError, OSError):
                limit = DEFAULT_HOURLY_CAP
        return int(limit * NORM_SHARE)

    def done_in_last_hour(self) -> int:
        if not self.dir or not self.dir.exists():
            return 0
        cut = self._now() - dt.timedelta(hours=1)
        n = 0
        for p in self.dir.glob("*.jsonl"):
            for line in p.read_text(errors="replace").splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("phase") != "done":
                    continue
                try:
                    t = dt.datetime.fromisoformat(str(d.get("ts", "")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.timezone.utc)
                if t >= cut:
                    n += 1
        return n

    def check_disk(self) -> None:
        probe = self.dir if self.dir and self.dir.exists() else Path("/")
        if free_bytes(probe) < self.min_free_bytes:
            write_halt(self.halt_file, "free disk below 2 GiB")
            raise Halted("disk floor")

    def check_budget(self) -> None:
        """書く前に呼ぶ。ディスクが足りん時は HALT を書いて止める。上限なら止める（HALT は書かん）。"""
        self.check_disk()
        if self.done_in_last_hour() >= self.hourly_cap():
            raise LockBusy("hourly write cap reached")

    # ---- 共有ロック ----
    @contextmanager
    def write_lock(self):
        if not self.dir:
            yield
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        lock = self.dir / "write.lock"
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                os.mkdir(lock)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                        os.rmdir(lock)  # 落ちた持ち主の残り
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise LockBusy("write.lock busy")
                time.sleep(0.2)
        try:
            yield
        finally:
            try:
                os.rmdir(lock)
            except OSError:
                pass

    # ---- 記録 ----
    def _append(self, record: dict) -> None:
        assert self.dir is not None
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{WAVE}.jsonl"
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            write_halt(self.halt_file, "ledger write failed")
            raise Halted("ledger write failed")

    def intent(self, job: str, target: str, board: str, pre: dict) -> None:
        if not self.dir:
            return
        self._append({
            "ts": self._now().strftime("%Y-%m-%dT%H:%M:%SZ"), "wave": WAVE, "step": f"keeper-{job}",
            "target": target, "board": board, "class": "field", "prio": "norm",
            "pre": json.dumps(pre, ensure_ascii=False, sort_keys=True), "phase": "intent",
        })

    def done(self, job: str, target: str, board: str, ok: bool, extra: dict | None = None) -> None:
        self.writes += 1
        if not self.dir:
            return
        self._append({
            "ts": self._now().strftime("%Y-%m-%dT%H:%M:%SZ"), "wave": WAVE, "step": f"keeper-{job}",
            "target": target, "board": board, "class": "field", "prio": "norm",
            "phase": "done", "result": "ok" if ok else "fail", **(extra or {}),
        })

    def records(self, job: str, board: str, since: dt.datetime, until: dt.datetime) -> list[dict]:
        """週の報告用。成功した done の行を返す。"""
        path = self.dir / f"{WAVE}.jsonl" if self.dir else None
        if not path or not path.exists():
            return []
        out = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("phase") != "done" or d.get("result") != "ok":
                continue
            if d.get("step") != f"keeper-{job}" or d.get("board") != board:
                continue
            try:
                t = dt.datetime.fromisoformat(d["ts"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if since <= t < until:
                out.append(d)
        return out

#!/bin/bash
# launchd の入口（30 分ごと）。~/.local/bin/exec-plan-run.sh に置く。
# 公開 repo に置く雛形なので、持ち主ごとの値（__HOME__・__LEDGER_DIR__・__HALT_FILE__・__LINEAR_KEY_REF__）は入れる時に差し替える。
# 決まりは plan/05 付録 2D: TZ=Asia/Tokyo で判断し、force-once か EXEC_PLAN_MANUAL=1 は時刻の決まりを飛ばす。
set -u
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export TZ=Asia/Tokyo

HOME_DIR="${HOME:-__HOME__}"
STATE="${EXEC_PLAN_STATE:-$HOME_DIR/.local/state/scrum-keeper}"
WORKTREE="${EXEC_PLAN_WORKTREE:-$HOME_DIR/.local/share/scrum-keeper/wt}"
EXEC_DIR="${EXEC_PLAN_DIR:-$HOME_DIR/.claude/plans/exec}"
LEDGER_DIR="${EXEC_PLAN_LEDGER_DIR:-__LEDGER_DIR__}"
HALT_FILE="${EXEC_PLAN_HALT_FILE:-__HALT_FILE__}"
LINEAR_TEAM="${EXEC_PLAN_LINEAR_TEAM:-RIT}"
LINEAR_KEY_REF="${EXEC_PLAN_LINEAR_KEY_REF:-__LINEAR_KEY_REF__}"
ALLOW="${EXEC_PLAN_ALLOW:-$STATE/allow.txt}"
PYTHON="${EXEC_PLAN_PYTHON:-/opt/homebrew/bin/python3}"
GIT=/usr/bin/git
FLOCK="${EXEC_PLAN_FLOCK:-/opt/homebrew/bin/flock}"
LOG="$EXEC_DIR/exec-plan-out.log"
LOCK="$EXEC_DIR/exec-plan.lock"
ALERT="$LEDGER_DIR/ALERT"

case "$LEDGER_DIR$HALT_FILE$LINEAR_KEY_REF" in
  *__LEDGER_DIR__*|*__HALT_FILE__*|*__LINEAR_KEY_REF__*)
    echo "exec-plan-run: placeholders not replaced" >&2; exit 2 ;;
esac

mkdir -p "$EXEC_DIR"
now_epoch="${EXEC_PLAN_NOW:-$(date +%s)}"
now_iso="$(date -r "$now_epoch" '+%Y-%m-%dT%H:%M:%S%z' | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/')"
today="$(date -r "$now_epoch" +%Y-%m-%d)"
cutoff="$(date -j -f '%Y-%m-%d %H:%M:%S' "$today 08:31:00" +%s)"
now_hm="$(date -r "$now_epoch" '+%Y-%m-%dT%H:%M:%S')"

log() { printf '%s %s\n' "$now_hm" "$1" >> "$LOG"; }

# 決めた commit 以外のコードは動かさん（scrum-keeper と同じ pinned-sha の決まり）
pinned="$(cat "$STATE/pinned-sha" 2>/dev/null || true)"
head="$("$GIT" -C "$WORKTREE" rev-parse HEAD 2>/dev/null || true)"
if [ -z "$pinned" ] || [ "$pinned" != "$head" ]; then
  log "run rc=9 force=0 manual=0 reason=worktree-mismatch"
  echo "exec-plan-run: worktree HEAD does not match pinned-sha" >&2
  exit 9
fi
dirty="$("$GIT" -C "$WORKTREE" status --porcelain --untracked-files=all 2>/dev/null; echo "rc=$?")"
if [ "$dirty" != "rc=0" ]; then
  log "run rc=9 force=0 manual=0 reason=worktree-dirty"
  echo "exec-plan-run: worktree is not clean" >&2
  exit 9
fi
ignored="$("$GIT" -C "$WORKTREE" ls-files --others --ignored --exclude-standard 2>/dev/null | grep -v '/__pycache__/' || true)"
if [ -n "$ignored" ]; then
  log "run rc=9 force=0 manual=0 reason=worktree-ignored-files"
  echo "exec-plan-run: worktree has ignored files" >&2
  exit 9
fi

manual=0; [ "${EXEC_PLAN_MANUAL:-0}" = "1" ] && manual=1
force=0
if [ -f "$EXEC_DIR/force-once" ]; then force=1; rm -f "$EXEC_DIR/force-once"; fi
[ "$manual" = "1" ] && force=1

# fail-state: 中身は連続失敗の回数、mtime は最後の失敗
fails=0; fail_mtime=0
if [ -f "$EXEC_DIR/fail-state" ]; then
  fails="$(head -1 "$EXEC_DIR/fail-state" | tr -d '[:space:]')"; fails="${fails:-0}"
  fail_mtime="$(stat -f %m "$EXEC_DIR/fail-state" 2>/dev/null || echo 0)"
fi

build=0; reason=""
if [ "$force" = 1 ]; then
  build=1
else
  m2="$(head -1 "$EXEC_DIR/m2-date" 2>/dev/null | tr -d '[:space:]')"
  if [ -n "$m2" ] && [ "$today" '<' "$m2" ]; then
    reason="before-m2"
  elif [ "$now_epoch" -lt "$cutoff" ]; then
    reason="before-831"
  elif [ "${fails:-0}" -ge 3 ]; then
    reason="fail-state"
  elif [ "$fail_mtime" -gt 0 ] && [ $((now_epoch - fail_mtime)) -lt 3600 ]; then
    reason="fail-backoff"
  else
    last_built="$(cat "$EXEC_DIR/last-built" 2>/dev/null || true)"
    lb_epoch=0
    if [ -n "$last_built" ]; then
      lb_epoch="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$last_built" +%s 2>/dev/null || echo 0)"
    fi
    if [ "$lb_epoch" -lt "$cutoff" ]; then
      build=1                                        # last-built が今日 8:31 前
    elif ls "$LEDGER_DIR"/*.jsonl >/dev/null 2>&1; then
      # 台帳に前に作った後の keeper-carryover|stalled|okawari の done があれば作る
      newest="$(grep -h '"phase": *"done"' "$LEDGER_DIR"/*.jsonl 2>/dev/null \
                | grep -E '"step": *"keeper-(carryover|stalled|okawari)[^"]*"' \
                | sed -n 's/.*"ts": *"\([^"]*\)".*/\1/p' | sort | tail -1)"
      if [ -n "$newest" ]; then
        nd_epoch="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$newest" +%s 2>/dev/null || echo 0)"
        [ "$nd_epoch" -gt "$lb_epoch" ] && build=1
      fi
      [ "$build" = 0 ] && reason="idle"
    else
      # 台帳が読めんなら 60 分ごと
      [ $((now_epoch - lb_epoch)) -ge 3600 ] && build=1
      [ "$build" = 0 ] && reason="idle"
    fi
  fi
fi

if [ "$build" = 0 ]; then
  log "skip reason=$reason force=$force manual=$manual"
  exit 0
fi

run=(env EXEC_PLAN_LOCK_HELD=1 "$PYTHON" -I "$WORKTREE/scripts/exec_plan/exec_plan_from_boards.py"
     --iteration current --allow "$ALLOW" --linear-team "$LINEAR_TEAM"
     --linear-key-ref "$LINEAR_KEY_REF" --halt-file "$HALT_FILE" --out "$EXEC_DIR" --now "$now_iso")

# exec-plan.lock: launchd は -n、手は -w 900（取れんと exit 9）。flock が無ければ鍵無しで動かす
if [ "$manual" = "1" ]; then
  rc=0
  if [ -x "$FLOCK" ]; then "$FLOCK" -w 900 -E 9 "$LOCK" "${run[@]}"; rc=$?; else "${run[@]}"; rc=$?; fi
else
  rc=0
  if [ -x "$FLOCK" ]; then "$FLOCK" -n -E 9 "$LOCK" "${run[@]}"; rc=$?; else "${run[@]}"; rc=$?; fi
fi
log "run rc=$rc force=$force manual=$manual"

case "$rc" in
  0|6)
    # last-built を進めて STALE 類と fail-state を消す。6 なら STALE-linear
    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$EXEC_DIR/last-built"
    rm -f "$EXEC_DIR/fail-state" "$EXEC_DIR/STALE" "$EXEC_DIR/STALE-linear"
    [ "$rc" = "6" ] && touch "$EXEC_DIR/STALE-linear" ;;
  8|9)
    : ;;   # 量不足と lock 取れずは状態を触らん
  *)
    n=$(( ${fails:-0} + 1 ))
    printf '%s\n' "$n" > "$EXEC_DIR/fail-state"
    touch "$EXEC_DIR/STALE"
    if [ "$n" = "1" ] || [ "$n" = "3" ]; then
      printf '%s exec-plan: rc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$rc" >> "$ALERT"
    fi ;;
esac
exit "$rc"

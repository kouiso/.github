#!/bin/bash
# launchd の入口（15 分ごと）。~/.local/bin/scrum-keeper-run.sh に置く。
# 公開 repo に置く雛形なので、持ち主ごとの値（__HOME__・__HALT_FILE__・__LEDGER_DIR__・__CAPACITY_BOARD__）は入れる時に差し替える。
# env -i HOME=$HOME /bin/bash scrum-keeper-run.sh でも動くよう、PATH はここで決める。
set -u
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export TZ=Asia/Tokyo

HOME_DIR="${HOME:-__HOME__}"
STATE="${SCRUM_KEEPER_STATE:-$HOME_DIR/.local/state/scrum-keeper}"
WORKTREE="${SCRUM_KEEPER_WORKTREE:-$HOME_DIR/.local/share/scrum-keeper/wt}"
HALT_FILE="${SCRUM_KEEPER_HALT_FILE:-__HALT_FILE__}"
LEDGER_DIR="${SCRUM_KEEPER_LEDGER_DIR:-__LEDGER_DIR__}"
CAPACITY_BOARD="${SCRUM_KEEPER_CAPACITY_BOARD:-__CAPACITY_BOARD__}"
PYTHON="${SCRUM_KEEPER_PYTHON:-/opt/homebrew/bin/python3}"
GIT=/usr/bin/git

case "$HALT_FILE$LEDGER_DIR$CAPACITY_BOARD" in
  *__HALT_FILE__*|*__LEDGER_DIR__*|*__CAPACITY_BOARD__*) echo "scrum-keeper-run: placeholders not replaced" >&2; exit 2 ;;
esac

# 決めた commit 以外のコードは動かさん（worktree が書き換わっても止まる）
pinned="$(cat "$STATE/pinned-sha" 2>/dev/null || true)"
head="$("$GIT" -C "$WORKTREE" rev-parse HEAD 2>/dev/null || true)"
if [ -z "$pinned" ] || [ "$pinned" != "$head" ]; then
  echo "scrum-keeper-run: worktree HEAD does not match pinned-sha" >&2
  exit 9
fi

# 引数が無ければ許可リスト。M1 の練習では --board <owner>/<n> を渡す
if [ "$#" -eq 0 ]; then
  set -- --allow "$STATE/allow.txt"
fi

run=("$PYTHON" "$WORKTREE/scripts/scrum_keeper/scrum_keeper.py" "$@" --job all
     --halt-file "$HALT_FILE" --ledger-dir "$LEDGER_DIR" --state-dir "$STATE"
     --capacity-board "$CAPACITY_BOARD")

# 二重起動は flock で弾く（取れん時は何もせず 0）。python 側にも同じ鍵がある
if [ -x /opt/homebrew/bin/flock ]; then
  exec /opt/homebrew/bin/flock -n -E 0 "$STATE/run.flock" "${run[@]}"
fi
exec "${run[@]}"

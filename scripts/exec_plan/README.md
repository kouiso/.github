# exec_plan

GitHub Project（と Linear の今の Cycle）を読んで、Claude の実行プランを毎朝作る小さな Python プログラムです。
scrum-keeper が週の整理をした後の盤を**読むだけ**で、月曜の `--fill-empty` を除いて GitHub には何も書きません。
鍵は手元の `gh` の認証をそのまま使い、Linear の鍵は実行の時だけ `op read` で読みます（表示も保存もしません）。

## 何をするか

- `--allow` の 1 行 `owner/番号`（`_` で始まる行は読まない）の全部の Project から、`fields` と `items(first:100)` を
  全ページ読みます（課題の本文・`blockedBy`・親子・`fieldValues` まで）。今の Sprint は日本時間の日付で決めます。
- 今の Sprint の開いとる課題を 3 つに分けます: 待ち無し＝作業一覧 ／ Wait=実機＝実機一覧 ／ 局長・外部待ち＝数だけ。
  PR・下書きは数だけ、閉じた物は数えません。
- 先の物（`blockedBy` と本文の `Blocked by`/`Depends on`/`先に` の後の参照。コードの中は読まない）は、
  待つ行のすぐ前に置いて `note` に `<待つ行> のため前に出した` と書きます。輪は 1 本飛ばして `deps-cycle`、
  別のプランか待ちの先の物は `deps-other-part`、Sprint の外は `deps-not-in-sprint`。
- やる人は付録 2B の決まり R0–R7 で出します: R0 Done なのに開いたまま→閉じ忘れ / R1 `[PBI]`→確かめ・まとめ /
  R2 Estimate>16→設計 / R3 Wait=実機→実機 / R4 `やる人` の値 / R5 6<Estimate<=16→設計 /
  R6 `worker_default: code`→Devin / R7 他→Sonnet。
- `~/.claude/plans/exec/<Sprint の月曜>.json`（`exec-plan/v1`）と `.md` と `board-ids.tsv` を、
  一時ファイルから名前を変えて書きます。書く物は全部 `exec-plan.lock` を取ってから
  （呼び出し側が持っとる時は `EXEC_PLAN_LOCK_HELD=1`）。
- Linear（`--linear-team`）は今の Cycle の終わってへん issues を読み、全行を作業に足します。
  `待ち:` ラベルは数だけ。読めん時は盤の分だけ作って `linear: "unread"`、終了コード 6。

## 使い方

```
python3 scripts/exec_plan/exec_plan_from_boards.py (--board <owner>/<番号> | --allow <ファイル>)
    [--iteration current|next|<日付>] --out <dir> [--linear-team RIT --linear-key-ref 'op://…']
    [--now <ISO8601>] [--fixture <dir>] [--html <path> --session-id <id>]
    [--fill-empty --write-via <gh_write.sh> --pre <dir>] [--halt-file <path>]
```

本番（`launchd/exec-plan-run.sh` が打つ）:

```
python3 "$WT/scripts/exec_plan/exec_plan_from_boards.py" --iteration current \
    --allow ~/.local/state/scrum-keeper/allow.txt --linear-team RIT \
    --linear-key-ref 'op://…/credential' --out ~/.claude/plans/exec/
```

終了コード: 0 作れた / 1 HALT / 2 引数 / 3 GitHub 不可 / 4 Project の 1 個でも不可 /
6 Linear だけ不可 / 7 `--fill-empty` 途中停止 / 8 量不足。3, 4, 8 の時は前のプランが残ります。

量の決まり: 開始前に `rateLimit.remaining` が 1,500 未満なら rc 8。作り終えて
`1,360 + 2 × graphql_cost > 2,500` か `rest_calls > 500` なら rc 8（2 = 30 分ごとに起きるので
1 時間に作れる最大の回数）。

## 出力

- `<out>/<月曜>.json` — `exec-plan/v1`: `sprint`, `built_at`, `linear`, `graphql_cost`, `rest_calls`,
  `boards[]`（ref・id・counts・fill_targets、長さ N）, `linear_counts`, `rows[]`（order, part, repo, n, url,
  title, priority, end_date, doer, doer_source, rule, deps_open, marks, moved_for_deps, linked_prs, note 他）,
  `rule_filled`, `fill_targets`, `anomalies[]`。
- `<out>/<月曜>.md` — 題・作った時刻・使いどきの注意・作業と実機の表・Project ごとの数・食い違い。
- `<out>/board-ids.tsv` — `owner/番号<TAB>id`。
- `--fill-empty` は `fill-<YYYYMMDD>.tsv` に `written` を残します。確かめ: 作り直した `fill_targets` = 前 - `written`。

## repo の種類と項目名

各 repo の `.github/scrum-board.yml` を REST で 1 回ずつ読み、`worker_default`（R6/R7）と `fields:` の項目名
（既定は Status/Priority/Estimate/Iteration/Start date/End date/Wait/Carryover/やる人）を取ります。
404 は `no-scrum-board-yml`、`worker_default` が無いのは `bad-scrum-board-yml`（どっちも R7 の行は
`fill_targets` に入らず月曜に書かん）。他の失敗は rc 3。

## 守っていること

- HALT の印（既定 `~/.claude/plans/zero-open-20261025/ledger/HALT`）があれば何もせず 1 で終わる。
- 量不足（rc 8）と盤の読み損ない（rc 3/4）では何も書かない＝前のプランが残る。
- `--fill-empty` は `scripts/q/item-clear.graphql` が repo にある時だけ動く（2C の決まり）。
  写しの無い Project には書かない。書くのは決まりで出した値（Devin/Sonnet/実機/設計）で `やる人` が空の行だけ。
- Linear の鍵は `op read` の stdout をメモリに持つだけ。ログ・写し・TSV に出さない。

## launchd（付録 2D）

`launchd/exec-plan-run.sh` を `~/.local/bin/exec-plan-run.sh` に、`launchd/exec-plan.plist` を
`~/Library/LaunchAgents/jp.ritmo.exec-plan.plist` に写して、プレースホルダ（`__HOME__`・`__LEDGER_DIR__`・
`__HALT_FILE__`・`__LINEAR_KEY_REF__`・`__LABEL__`）を差し替えます。`StartInterval` 1800、`RunAtLoad`。

- 30 分ごとに起きて日本時間で決める: `force-once` か `EXEC_PLAN_MANUAL=1` は時刻の決まりを飛ばす ／
  今日 < `m2-date` か 8:31 前は作らん ／ `fail-state` が 3 回か最後の失敗から 60 分未満は作らん ／
  `last-built` が今日 8:31 前なら作る ／ 台帳に前に作った後の `keeper-carryover|stalled|okawari` の done が
  あれば作る（台帳が読めんなら 60 分ごと）。
- `exec-plan.lock` は launchd は `flock -n`、手は `flock -w 900`（取れんと exit 9）。
- rc 0/6 は `last-built` を進めて `STALE` 類と `fail-state` を消し、6 なら `STALE-linear`。
  他は `fail-state` +1 と `STALE`、`$LEDGER_DIR/ALERT` は 1 回目と 3 回目だけ。`STALE-count` は触らん。
- 手で作り直す: `EXEC_PLAN_MANUAL=1 /bin/bash ~/.local/bin/exec-plan-run.sh; tail -2 ~/.claude/plans/exec/exec-plan-out.log`
  → 最後の行 `run rc=0 force=1 manual=1`。

## 試験用の環境変数

`EXEC_PLAN_GH`（gh のパス）, `EXEC_PLAN_OP`（op のパス）, `EXEC_PLAN_LINEAR_URL`（Linear API の差し替え）,
`EXEC_PLAN_LOCK_HELD`（呼び出し側が鍵を持っとる印）, `EXEC_PLAN_NOW`（run.sh の時計差し替え、epoch 秒）。

`--fixture <dir>` は GitHub/Linear の代わりに保存済みの応答を読みます（量の計測はしません）。
置き場の `*.json`: 盤は `{"owner","number","project":{…生の projectV2 ノード…}}`、repo の yml は
`{"repos":{"o/r":{"yml":"…"}}}`、Linear は `{"linear":{"cycle":…,"issues":[…],"wait_labels":{…}}}`。

## 分かっている限界

- Linear の項目名は `__type` で合わせてから読むが、実物では未試験（Cycle が無い時は `linear:no-active-cycle`）。
- 本文の参照の向き先が開いとるかは、盤に載っとる物だけ分かる。載っとらん物は開いとると見なして `deps_open` に入れ、
  `deps-not-in-sprint` を付ける。
- 同じ課題が複数の Project の今週に居る時は、先に当たった盤の行だけが残る（`dup-on-boards`）。
- `Estimate` が無い行の設計判定は `Size`（XL→R2、L→R5）で代用する。

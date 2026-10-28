# scrum_keeper

GitHub Project の週の管理を、Claude も AI も使わずに 15 分ごとに回す小さな Python プログラムです。
Mac の launchd から動かし、鍵は手元の `gh` の認証をそのまま使います。秘密は repo にも GitHub にも置きません。

## 何をするか

| 仕事 | いつ（JST） | 内容 |
| --- | --- | --- |
| extend | 毎回 | Iteration を今週から 8 本に保つ。既存の id は全部残して送り直し、前後で件数が減ったら HALT。新しい 8 週目には、週に入っとらん物を全 Project 1 つの順で並べ、量に空きがある一番早い週が 8 週目になった物だけ入れる（早い週に空きがある物は触らず「週なし」のまま残す） |
| capacity | 週の最初の回 | 前の週に閉じた Estimate の合計（空は 1.5）を 70〜282 時間に収め、説明文の目印だけ置き換える |
| carryover | 毎回 | 終わった週に残る open で Done でない課題を、どの週からでも今週へ。Carryover を足すのはここだけ。閉じたのに Done でない物は動かさず食い違いとして出す |
| stalled (a) | 毎回 | 今週の Wait 付きで、課題の更新が 48 時間より前の物を来週へ（Carryover は足さない） |
| stalled (b) | 日の最初の回 | 日曜までに入らない分を来週へ。全 Project の今週の物を 1 つの順（In Progress、Priority、End date、repo、番号）に並べて 1 回で切る（Carryover は足さない） |
| okawari | 毎回 | 今週にやれる物が 0 になった Project へ、来週の上から引く。PBI は open の SBI と一緒に |
| sync | 毎回 | `.github/scrum-board.yml` がこの Project を指す repo の open の課題を Backlog で足す |
| report | 週の最初の回 | Status update を 1 週 1 本。先頭行 `scrum-keeper:week=<月曜>` で二重投稿を防ぐ。終わった週に残る物（stranded）も数える |

- 「週の最初の回」「日の最初の回」は時刻の窓ではなく、成功した最後の回（`last-run`）より後の最初の回です。
  Mac が月曜の夜中に寝ていても、起きた最初の回で取り戻します。`--job all` だけが `last-run` を更新します。
- 1 週の量は全 Project で 1 つです。stalled (b) も okawari も extend の埋めも、Project をまたいで同じ量・同じ並べ順で
  1 回で計画します。Wait の付いた物は量に数えません（stalled (b) と okawari が同じ数え方をします）。
- おかわりが引ける量は「翌日の stalled (b) が押し戻さない量」（翌日の日数ぶん）までです。今日の量まで引くと、
  毎晩押し戻されて引いては戻すが回るためです。1 件で量を超える物、待ちの子が居る親は引きません。
- 動かす時は Iteration と同じ書き込みで Start date（その週の月曜）と End date（その週の日曜。期日が動かす先の週に
  既にあれば残す）も直します。その Project に項目が無ければ書きません。今と同じ値の書き込みはしません。
- 週は月曜始まりの 7 日で、計算は常に Asia/Tokyo。ホストの時計は見ません。
- PR は全部飛ばします。下書き（DraftIssue）は数えるだけです。
- Carryover は、動かす元の週が `count_from` 以降の時だけ 1 足します。目印は説明文の
  `scrum-keeper:capacity=<時間>h count_from=<日付>` です。
- どの仕事も「動かした物は元の週にもう居ない」ので、同じ回を二度走らせても何も起きません。

## 使い方

```
python3 scripts/scrum_keeper/scrum_keeper.py (--board <owner>/<番号> | --allow <ファイル>) [--job all|extend|carryover|stalled|okawari|sync|report|capacity]
    [--now <ISO8601>] [--dry-run] [--halt-file <パス>] [--ledger-dir <ディレクトリ>]
    [--capacity-board <owner>/<番号>] [--max-writes 200] [--fixture <ディレクトリ>] [--state-dir <ディレクトリ>]
```

- `--dry-run` は何も書かず、動かす予定を CSV で標準出力に出します。台帳にも書きません。
- `--job` を名指しした時は、その仕事を必ず走らせます。`all` だけが `last-run` で週・日の仕事を決めます。
- `--allow` は `owner/番号` を 1 行ずつ。説明文に `scrum-board: v1` がある Project だけが相手です。
  題名が `_` で始まる物は飛ばします。`--board` を明示した時だけ、その 1 つに必ず動きます。
- 1 週の量の目印を書き換えるのは、`--capacity-board` がこの回の相手に含まれる時だけです。含まれない時は読むだけです。
- 題名はログにも CSV にも出しません。出すのは `repo#番号` と件数です。

終了コード: 0 正常（HALT の印があった時、二重起動だった時も 0）/ 1 gh の失敗 / 2 引数の誤り /
4 書き込みの上限かロック待ちで中断 / 5 gh の認証切れ / 6 HALT を書いて中断 / 9 入口の固定 commit 違い（`launchd/` の入口が返す）。
`last-run` は、`--job all` が書き込みも含めて最後まで成功した時だけ更新します。

## Linear（チーム RIT）の読み取り: `linear_sweep.py`

Linear には**何も書きません**。繰り越しは Linear 自身（Cycles）がやるので、この入口の仕事は読みと記録だけです
（S17: Linear 版のおかわりと見込み無し）。

- 今の Cycle（`team.activeCycle`）の終わっていない課題で、Estimate 無し・Priority 無しを拾い、台帳に 1 行足します。
- 今の Cycle にやれる物（`待ち:` ラベル無し）が 0 の時だけ、Cycle 未所属の課題を優先度の順（Urgent→P0、High→P1、
  Medium→P2。`待ち:` と triage は除く）に「進める候補」として出します — Linear 版おかわり。提案だけで、
  Cycle に入れる操作はしません。
- 鍵は `--key-ref`（既定 `op://RITMO/Linear API key - scrum-keeper/credential`）を実行の時だけ `op read` で読み、
  値はファイルにもログにも残しません。`op` が無い・参照が無い・Linear に届かない・schema の名が違う時は
  終了コード 6 でその回を諦めます（GitHub 側の回は別プロセスなので影響しません）。
- フィールド名は未試験なので、読む前に `__type` で `Team`/`Cycle`/`Issue`/`WorkflowState` の要る名前を確かめます。
- 台帳は同じ `<ledger>/K.jsonl` に `step=keeper-linear`・`target=linear/<key>` の done を 1 行。identifier
  （`RIT-43` など）の一覧と件数だけを持ち、題名は出しません。

```
python3 scripts/scrum_keeper/linear_sweep.py [--team RIT] [--key-ref 'op://...'] \
    [--now <ISO8601>] [--dry-run] [--fixture <ディレクトリ>] [--halt-file <パス>] \
    --ledger-dir <ディレクトリ> [--state-dir <ディレクトリ>] [--max-suggest 20]
```

- `--dry-run` は台帳にも書かず、拾った物を CSV（`scrum_keeper.py` と同じ列）で標準出力に出します。
- `--fixture` は `linear` キーを持つ JSON を置いたディレクトリ（見本 `test/fixture/linear-rit.json`。
  `python3 scripts/scrum_keeper/test/make_fixture.py` で作り直せます）。`op` も Linear も触りません。

終了コード: 0 正常（HALT・二重起動も 0）/ 1 Linear の応答の誤り / 2 引数 / 4 台帳の鍵待ちで中断 /
6 Linear に届かん・HALT を書いて中断。

## 守っていること

- HALT の印があれば何も書かず 0 で終わる。回の途中で置かれた HALT も、次の書き込みの前に見て止まる。
  3 回続けて gh が失敗した時、上限の返事が 5 回続いた時、空きが 2 GiB を切った時、台帳に書けなかった時、
  extend で件数が減った時は、自分で HALT を書いて止まる。
- 書き込みは 1 本ずつ 1 秒空ける。上限の返事は 60/120/240 秒待って 3 回まで再試行し（待つ間は書き込みの鍵を放す）、
  それでも駄目ならこの回を止めて次の回に任せる（HALT にはしない）。返事の連続数は `<state>/rate-limit-streak` に持ち、
  通った書き込みで 0 に戻る。
- extend は `<state>/extend-verified.txt` に Project が書いてある時だけ本番で書く（`*` は全部）。反復の設定を丸ごと
  書き直す呼び出しが id を保つことを、練習用の写しで測り終えてから 1 行足す。書いてなければ extend は止まり、
  その回は終了コード 1 になる（`--dry-run` は読むだけなので止まらない）。
- 書き込みの前に、その課題の Iteration と Carryover を読み直し、読んだ時と違えば `race` として飛ばす。
- 書き込みは 1 回ごとに `<台帳>/write.lock`（`mkdir`）を最大 30 秒待って取る。取れなければその回を止めて次の回に任せる。
- 台帳 `<台帳>/K.jsonl` には書く前に `intent`（前の値つき、fsync）、後に `done` を 1 行ずつ足す。
  1 時間の書き込み数は `cap.json` の `content_per_hour`（既定 300）の 6 割で止める。1 回の上限は `--max-writes`。
- 二重起動は `<state>/lock` の flock で弾く。
- 入口 `launchd/scrum-keeper-run.sh` は、worktree の HEAD が `pinned-sha` と同じで、未コミットの書き換えも未追跡のファイルも
  （`__pycache__` 以外の `.gitignore` で隠れた物も）無い時だけ動かす。違えば終了コード 9。python は `-I`（環境変数と
  実行ファイルの隣のディレクトリを読まない）で起動する。

## 入れ方

1. 決めた commit の worktree を作る（例 `~/.local/share/scrum-keeper/wt`）。その SHA を `~/.local/state/scrum-keeper/pinned-sha` に書く。
2. `launchd/scrum-keeper-run.sh` を `~/.local/bin/` に置き、`__HOME__ __HALT_FILE__ __LEDGER_DIR__ __CAPACITY_BOARD__` を差し替える。
   `launchd/scrum-keeper.plist` の `__LABEL__ __HOME__` も差し替えて `~/Library/LaunchAgents/` に置く。
3. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<ラベル>.plist`
4. 最初は `--board` で練習用の Project を 1 つだけ渡して数回回し、`last-run` が更新され続けることを確かめてから `allow.txt` に広げる。

戻し方: `launchctl bootout gui/$(id -u)/<ラベル>` してから、この変更を revert する。
動かした課題は台帳の `pre`（前の値）から元に戻せる。

## 試験

```
python3 -m pytest scripts/scrum_keeper/test -q
```

GitHub には触りません。本物の `gh api graphql` の応答と同じ形の dict を `FakeClient` に読ませます。
`python3 scripts/scrum_keeper/test/make_fixture.py` で `test/fixture/` の見本を作り直せます（`--fixture` で読める）。

## 分かっている限界

- Mac が丸一日止まっていても、次の回で extend と carryover は走ります（時刻の窓が無いため）。止まっている間に
  Iteration が足りなくなった分は、起きた回の extend が足します。
- carryover は終わった週のどこからでも拾うので、運用の前に board_place.py で週へ並べ終えていない Project に初めて
  走らせると、終わった週に残る物が一度に今週へ動きます（`--dry-run` の CSV で件数を見てから）。
- 週の量は前の週に閉じた分 1 週ぶんだけで置き換えます（平滑化なし。70〜282 時間に収めるだけ）。休みの週は量が下限まで下がります。
- 同じ課題が複数の Project に載っている時は、量の計算で Project の数だけ数えます。
- 動かす先の週にあらかじめ Start/End date が入っていない物でも、Start＝月曜・End＝日曜を書きます（`board_place.py` と同じ組）。
- `createProjectV2StatusUpdate` と `updateProjectV2Field` は schema には在りますが、実際の権限と、書き直しで id が保たれるかは
  練習用 Project で書いて測ってから本番に向けてください（上の extend-verified.txt）。

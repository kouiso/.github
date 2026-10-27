# scrum_keeper

GitHub Project の週の管理を、Claude も AI も使わずに 15 分ごとに回す小さな Python プログラムです。
Mac の launchd から動かし、鍵は手元の `gh` の認証をそのまま使います。秘密は repo にも GitHub にも置きません。

## 何をするか

| 仕事 | いつ（JST） | 内容 |
| --- | --- | --- |
| extend | 月曜 0:00〜2:59 | Iteration を今週から 8 本に保つ。既存の id は全部残して送り直し、前後で件数が減ったら HALT |
| capacity | 月曜 0:00〜2:59 | 前の週に閉じた Estimate の合計（空は 1.5）を 70〜282 時間に収め、説明文の目印だけ置き換える |
| carryover | 月曜 0:00〜2:59 | 前の週の open で Done でない課題を今週へ。閉じたのに Done でない物は動かさず食い違いとして出す |
| stalled (a) | 毎回 | 今週の Wait 付きで、課題の更新が 48 時間より前の物を来週へ |
| stalled (b) | 毎日 0:00〜2:59 | 日曜までに入らない分を来週へ（In Progress、Priority、End date、番号の順に Estimate を足す） |
| okawari | 毎回 | 今週にやれる物が 0 になったら、来週の上から今週へ引く。PBI は open の SBI と一緒に |
| sync | 毎回 | `.github/scrum-board.yml` がこの Project を指す repo の open の課題を Backlog で足す |
| report | 月曜 0:00〜2:59 | Status update を 1 週 1 本。先頭行 `scrum-keeper:week=<月曜>` で二重投稿を防ぐ |

- おかわりは、1 週に入りきらない大きい物（見込み無し (b) がすぐ戻してしまう物）を引きません。
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
- `--job` を名指しした時は、時間帯の縛りを無視してその仕事を走らせます。`all` だけが時間帯で決めます。
- `--allow` は `owner/番号` を 1 行ずつ。説明文に `scrum-board: v1` がある Project だけが相手です。
  題名が `_` で始まる物は飛ばします。`--board` を明示した時だけ、その 1 つに必ず動きます。
- 1 週の量の目印を書き換えるのは、`--capacity-board` がこの回の相手に含まれる時だけです。含まれない時は読むだけです。
- 題名はログにも CSV にも出しません。出すのは `repo#番号` と件数です。

終了コード: 0 正常（HALT の印があった時、二重起動だった時も 0）/ 1 gh の失敗 / 2 引数の誤り /
4 書き込みの上限かロック待ちで中断 / 5 gh の認証切れ / 6 HALT を書いて中断 / 9 入口の固定 commit 違い（`launchd/` の入口が返す）。
`last-run` は、書き込みも含めて回が最後まで成功した時だけ更新します。

## 守っていること

- HALT の印があれば何も書かず 0 で終わる。3 回続けて gh が失敗した時、空きが 2 GiB を切った時、台帳に書けなかった時、
  extend で件数が減った時は、自分で HALT を書いて止まる。
- 書き込みの前に、その課題の Iteration と Carryover を読み直し、読んだ時と違えば `race` として飛ばす。
- 書き込みは 1 回ごとに `<台帳>/write.lock`（`mkdir`）を最大 30 秒待って取る。取れなければその回を止めて次の回に任せる。
- 台帳 `<台帳>/K.jsonl` には書く前に `intent`（前の値つき、fsync）、後に `done` を 1 行ずつ足す。
  1 時間の書き込み数は `cap.json` の `content_per_hour`（既定 300）の 6 割で止める。1 回の上限は `--max-writes`。
- 二重起動は `<state>/lock` の flock で弾く。

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

- 月曜 0:00〜2:59 の間 Mac が止まっていると、その週の extend と carryover は走りません。`last-run` が古ければ手で
  `--job extend` `--job carryover` を回してください（どちらも二度走らせて安全です）。
- `createProjectV2StatusUpdate` と `updateProjectV2Field` は schema には在りますが、実際の権限は練習用 Project で書いて確かめてから本番に向けてください。

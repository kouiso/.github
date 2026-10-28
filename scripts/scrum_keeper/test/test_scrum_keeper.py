"""scrum-keeper の試験 17 本。GitHub には触らず、本物の応答と同じ形の dict を FakeClient に読ませる。"""
import datetime as dt
import re

from helpers import (CUR, DESC, NOW_MON, NOW_WED, TITLE, item, ledger_lines, make_board, make_client, monday_range,
                     run, week)
from scrum_keeper.github_client import FakeClient
from scrum_keeper.model import CAPACITY_RE


def moved(client):
    return [w for w in client.writes if w[0] == "item"]


# 1. 今週に open・Wait 空・Done でない物が 0 なら、おかわりが動く
def test_01_okawari_fires_when_current_week_has_no_workable_item(tmp_path):
    board = make_board([
        {"n": 1, "week": CUR, "state": "CLOSED", "status": "Done"},
        {"n": 2, "week": CUR, "status": "Done"},                 # open だが Done
        {"n": 3, "week": CUR, "wait": "W1"},                     # 待ち
        {"n": 10, "week": "2026-11-09", "prio": "P0", "est": 8},
        {"n": 11, "week": "2026-11-09", "prio": "P1", "est": 8},
    ])
    c = make_client(board)
    assert run(tmp_path, c, job="okawari", now=NOW_WED) == 0
    assert week(c, "o/1", 10) == CUR and week(c, "o/1", 11) == CUR
    assert item(c, "o/1", 10).carry == 0  # 前倒しは繰り越しやない


# 2. Wait の物は「空」の判定を邪魔せんし、引いてもこん。今週に仕事があれば動かん
def test_02_wait_items_do_not_block_and_are_never_pulled(tmp_path):
    board = make_board([
        {"n": 3, "week": CUR, "wait": "W1"},
        {"n": 12, "week": "2026-11-09", "prio": "P0", "wait": "W2", "est": 1},
        {"n": 10, "week": "2026-11-09", "prio": "P2", "est": 1},
    ])
    c = make_client(board)
    run(tmp_path, c, job="okawari", now=NOW_WED)
    assert week(c, "o/1", 10) == CUR
    assert week(c, "o/1", 12) == "2026-11-09"
    # 1 週に入りきらん大きい物は引かん（引いても見込み無し (b) が戻して Carryover だけ増えるため）
    huge = make_client(make_board([{"n": 3, "week": CUR, "wait": "W1"},
                                   {"n": 13, "week": "2026-11-09", "prio": "P0", "est": 500},
                                   {"n": 14, "week": "2026-11-09", "prio": "P2", "est": 1}]))
    run(tmp_path / "h", huge, job="okawari", now=NOW_WED)
    assert week(huge, "o/1", 13) == "2026-11-09" and week(huge, "o/1", 14) == CUR
    busy = make_board([{"n": 4, "week": CUR}, {"n": 10, "week": "2026-11-09", "est": 1}])
    c2 = make_client(busy)
    run(tmp_path / "b", c2, job="okawari", now=NOW_WED)
    assert moved(c2) == []


# 3. PBI は open の SBI と一緒に動き、PBI の Iteration は子の一番遅い週
def test_03_pbi_moves_with_children_and_follows_latest_child_week(tmp_path):
    kids = ["o/r#21", "o/r#22", "o/r#23"]
    board = make_board([
        {"n": 20, "week": "2026-11-09", "children": kids, "est": 1},
        {"n": 21, "week": "2026-11-09", "parent": "o/r#20", "est": 1},
        {"n": 22, "week": "2026-11-23", "parent": "o/r#20", "est": 1},
        {"n": 23, "week": "2026-10-26", "parent": "o/r#20", "state": "CLOSED", "status": "Done"},
    ])
    c = make_client(board)
    run(tmp_path, c, job="okawari", now=NOW_WED)
    assert {week(c, "o/1", n) for n in (20, 21, 22)} == {CUR}
    # 繰り越し側: 親は 1 度だけ +1、その後は子の一番遅い週へ（+1 しない）
    board2 = make_board([
        {"n": 30, "week": "2026-10-26", "children": ["o/r#31", "o/r#32"]},
        {"n": 31, "week": "2026-10-26", "parent": "o/r#30"},
        {"n": 32, "week": "2026-11-16", "parent": "o/r#30"},
    ])
    c2 = make_client(board2)
    run(tmp_path / "b", c2, job="carryover")
    assert week(c2, "o/1", 31) == CUR and item(c2, "o/1", 31).carry == 1
    assert week(c2, "o/1", 30) == "2026-11-16" and item(c2, "o/1", 30).carry == 1


# 4. 閉じとるのに Done やない物は繰り越さず、食い違いとして出す
def test_04_closed_not_done_is_reported_not_carried(tmp_path, capsys):
    items = [
        {"n": 40, "week": "2026-10-26", "state": "CLOSED", "status": "In Review"},
        {"n": 41, "week": "2026-10-26", "status": "Done"},
        {"n": 42, "week": "2026-10-26"},
    ]
    c = make_client(make_board(items))
    run(tmp_path / "dry", make_client(make_board(items)), "--dry-run", job="carryover")
    out = capsys.readouterr().out
    assert "closed-not-done" in out and "open-done" in out
    run(tmp_path, c, job="carryover")
    assert week(c, "o/1", 42) == CUR
    assert week(c, "o/1", 40) == "2026-10-26" and week(c, "o/1", 41) == "2026-10-26"


# 5. 同じ回を 2 度走らせても二重に動かず、読み直しで変わっとれば race として飛ばす
def test_05_double_run_is_noop_and_cas_race_is_skipped(tmp_path, capsys):
    c = make_client(make_board([{"n": 50, "week": "2026-10-26"}, {"n": 51, "week": "2026-10-26"}]))
    run(tmp_path, c, job="carryover")
    first = len(moved(c))
    run(tmp_path, c, job="carryover")
    assert first == 2 and len(moved(c)) == 2
    assert item(c, "o/1", 50).carry == 1
    lines = ledger_lines(tmp_path)
    assert [x["phase"] for x in lines] == ["intent", "done"] * 2
    assert {"ts", "wave", "step", "target", "class", "prio", "pre", "phase"} <= set(lines[0])
    assert lines[0]["wave"] == "K" and lines[0]["step"] == "keeper-carryover" and lines[0]["class"] == "field"
    # race: 読み直しの時に別の誰かが 52 を動かしとった
    c2 = make_client(make_board([{"n": 52, "week": "2026-10-26"}, {"n": 53, "week": "2026-10-26"}]))

    def other_person_moves_it(live_board, live_item):
        if live_item.number == 52:
            live_item.iteration_id = "it-2026-11-09"

    c2.before_read_item = other_person_moves_it
    capsys.readouterr()
    run(tmp_path / "r", c2, job="carryover")
    assert "race" in capsys.readouterr().err
    assert week(c2, "o/1", 52) == "2026-11-09" and item(c2, "o/1", 52).carry == 0
    assert week(c2, "o/1", 53) == CUR


# 6. 週の境は JST。ホストの時計（PDT）は見ん。週・日に 1 回の仕事は時刻の窓でなく last-run で決まる
def test_06_jst_boundary_ignores_host_clock_and_weekly_jobs_follow_last_run(tmp_path, capsys):
    base = [{"n": 60, "week": "2026-10-26"}]
    pdt_same_instant = "2026-11-01T08:05-07:00"  # = 2026-11-02 00:05 JST
    for now, weekly in ((NOW_MON, True), (pdt_same_instant, True), ("2026-11-01T23:59+09:00", False)):
        capsys.readouterr()
        c = make_client(make_board(base))
        d = tmp_path / now.replace(":", "")
        (d / "state").mkdir(parents=True)
        (d / "state" / "last-run").write_text("2026-10-30T12:00:00+09:00\n")  # 前の週の金曜に成功した回
        run(d, c, now=now)
        jobs = re.search(r"jobs=(\S+)", capsys.readouterr().err).group(1).split(",")
        assert ("report" in jobs) is weekly and ("capacity" in jobs) is weekly
        assert "extend" in jobs and "carryover" in jobs and "okawari" in jobs  # 毎回の仕事は窓なしで動く
    # Mac が月曜の 00:00〜02:59 をまるごと寝とっても、起きた最初の回（07:00）で週の仕事が走る
    capsys.readouterr()
    woke = make_client(make_board(base))
    (tmp_path / "w" / "state").mkdir(parents=True)
    (tmp_path / "w" / "state" / "last-run").write_text("2026-11-01T23:50:00+09:00\n")
    run(tmp_path / "w", woke, now="2026-11-02T07:00+09:00")
    assert "report" in re.search(r"jobs=(\S+)", capsys.readouterr().err).group(1).split(",")
    assert week(woke, "o/1", 60) == CUR  # 繰り越しも窓なしで走った
    # 同じ日の 2 回目は週・日の仕事を繰り返さん（成功した回の last-run が目印）
    run(tmp_path / "w", woke, now="2026-11-02T07:15+09:00")
    assert "report" not in re.search(r"jobs=(\S+)", capsys.readouterr().err).group(1).split(",")


# 7. 見込み無し (a): 48 時間 1 分で動き、47 時間 59 分では残る
def test_07_stalled_a_threshold(tmp_path):
    c = make_client(make_board([
        {"n": 70, "week": CUR, "wait": "W1", "updated": "2026-11-02T02:59:00Z"},   # 48h01m 前
        {"n": 71, "week": CUR, "wait": "W1", "updated": "2026-11-02T03:01:00Z"},   # 47h59m 前
        {"n": 72, "week": CUR, "updated": "2026-10-01T00:00:00Z"},                 # Wait 無しは対象外
    ]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert week(c, "o/1", 70) == "2026-11-09" and item(c, "o/1", 70).carry == 0  # 繰り越しやない週内の組み替え
    assert week(c, "o/1", 71) == CUR and week(c, "o/1", 72) == CUR
    done = [x for x in ledger_lines(tmp_path) if x["phase"] == "done"]
    assert done[0]["wait"] == "W1"  # 週の報告が Wait の値ごとに数えるため


def cap_board(items, cap="70", count_from="2026-10-26", **kw):
    return make_board(items, description=f"scrum-board: v1 scrum-keeper:capacity={cap}h count_from={count_from}", **kw)


# 8. 見込み無し (b): 合計がちょうど上限なら残る
def test_08_stalled_b_exactly_at_limit_stays(tmp_path):
    # 水曜は日曜まで 5 日。70 時間 ÷ 7 × 5 = 50
    c = make_client(cap_board([{"n": 80, "week": CUR, "prio": "P0", "est": 25}, {"n": 81, "week": CUR, "prio": "P1", "est": 25}]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert moved(c) == []


# 9. 見込み無し (b): またいだ件から下は全部動く。小さい件を詰め直さん。In Progress が先頭
def test_09_stalled_b_crossing_item_and_all_below_move(tmp_path):
    c = make_client(cap_board([
        {"n": 90, "week": CUR, "prio": "P2", "est": 20, "status": "In Progress"},
        {"n": 91, "week": CUR, "prio": "P0", "est": 20, "end": "2026-11-05"},
        {"n": 92, "week": CUR, "prio": "P0", "est": 20, "end": "2026-11-06"},   # 累計 60 > 50 ここから下
        {"n": 93, "week": CUR, "prio": "P1", "est": 5},
        {"n": 94, "week": CUR, "prio": "P2", "est": 5},
    ]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert week(c, "o/1", 90) == CUR and week(c, "o/1", 91) == CUR
    assert [week(c, "o/1", n) for n in (92, 93, 94)] == ["2026-11-09"] * 3  # 93・94 は詰め直せば入るが動く
    assert item(c, "o/1", 92).carry == 0  # Carryover を足すのは carryover だけ


# 10. 見込み無し (b): 1 番上だけで超えたら全部。上限 0 以下も全部
def test_10_stalled_b_top_item_over_limit_and_zero_limit(tmp_path):
    c = make_client(cap_board([{"n": 100, "week": CUR, "prio": "P0", "est": 60}, {"n": 101, "week": CUR, "est": 1}]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert week(c, "o/1", 100) == "2026-11-09" and week(c, "o/1", 101) == "2026-11-09"
    c0 = make_client(cap_board([{"n": 102, "week": CUR, "est": 1}], cap="0"))
    run(tmp_path / "z", c0, job="stalled", now=NOW_WED)
    assert week(c0, "o/1", 102) == "2026-11-09"


# 11. count_from より前の週からは Carryover を足さず、後は足す。2 以上は分け直し候補
def test_11_count_from_gate_and_resplit_candidates(tmp_path):
    before = make_board([{"n": 110, "week": "2026-10-26", "carry": 1}], description=DESC.replace("2026-10-26", "2026-11-09"))
    c = make_client(before)
    run(tmp_path, c, job="carryover")
    assert week(c, "o/1", 110) == CUR and item(c, "o/1", 110).carry == 1  # 足さん
    after = make_board([{"n": 111, "week": "2026-11-09", "carry": 1}, {"n": 112, "week": "2026-11-16", "carry": 2}],
                       description=DESC.replace("2026-10-26", "2026-11-09"), completed_before="2026-11-16")
    c2 = make_client(after)
    now = "2026-11-16T00:05+09:00"
    run(tmp_path / "b", c2, job="carryover", now=now)
    assert item(c2, "o/1", 111).carry == 2 and week(c2, "o/1", 111) == "2026-11-16"
    run(tmp_path / "b", c2, job="report", now=now)
    body = c2.status_updates["o/1"][0]
    assert "resplit-candidates(carryover>=2): 2 o/r#111, o/r#112" in body


# 12. extend は先 8 週を保ち、既存の id を残す。件数が減ったら HALT。8 週目は「量に空きがある一番早い週が 8 週目」の物だけ埋める
def test_12_extend_keeps_eight_weeks_preserves_ids_and_halts_on_loss(tmp_path):
    weeks = monday_range(n=9)  # 10-19 〜 12-14（今週から数えて 7 本）
    fillers = [{"n": 130 + k, "week": w, "est": 176} for k, w in enumerate(
        ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30", "2026-12-07", "2026-12-14"])]
    items = fillers + [{"n": 120, "prio": "P0", "est": 10}, {"n": 121, "prio": "P1", "est": 10}, {"n": 122, "wait": "W1"}]
    c = make_client(make_board(items, weeks=weeks))
    old_ids = {i.id for i in c.boards["o/1"].iterations}
    assert run(tmp_path, c, job="extend") == 0
    its = c.boards["o/1"].iterations
    assert old_ids <= {i.id for i in its}
    assert len([i for i in its if i.start >= dt.date.fromisoformat(CUR)]) == 8
    assert week(c, "o/1", 120) == "2026-12-21" and week(c, "o/1", 121) == "2026-12-21"
    assert week(c, "o/1", 122) is None and week(c, "o/1", 130) == CUR
    # 早い週に空きがあるなら、8 週目へ隠さず週なしのまま残す（P0 を 7 週先に置かん）
    roomy = make_client(make_board([{"n": 140, "prio": "P0", "est": 10}], weeks=weeks))
    assert run(tmp_path / "roomy", roomy, job="extend") == 0
    assert week(roomy, "o/1", 140) is None
    # 反復の設定を書き直して id が保たれるかを測り終えた Project だけ、本番で書く
    blocked = make_client(make_board(items, weeks=weeks))
    assert run(tmp_path / "blocked", blocked, job="extend", verified=None) == 1
    assert not [w for w in blocked.writes if w[0] == "iterations"]
    assert run(tmp_path / "blocked2", blocked, "--dry-run", job="extend", verified=None) == 0

    class Lossy(FakeClient):
        def update_iteration_config(self, board, iterations):
            super().update_iteration_config(board, iterations)
            for i in self.boards[board.ref].items:
                if i.iteration_id == "it-2026-11-09":
                    i.iteration_id = None  # 書き直しで件数が減った

    bad = Lossy({"o/1": make_board(items, weeks=weeks)})
    assert run(tmp_path / "bad", bad, job="extend") == 6
    assert (tmp_path / "bad" / "ledger" / "HALT").exists()


# 13. 相手は allow.txt ∩ scrum-board: v1。_ で始まる題の物は触らん
def test_13_targets_are_allow_list_intersect_marker(tmp_path):
    rows = [{"n": 130, "week": "2026-10-26"}]
    b1 = make_board(rows, number=1)
    b2 = make_board(rows, number=2, description="no marker")
    b3 = make_board(rows, number=3, title="_template")
    c = make_client(b1, b2, b3)
    allow = tmp_path / "allow.txt"
    allow.write_text("o/1\no/2\no/3  # ひな形\n\n")
    assert run(tmp_path, c, job="carryover", allow=allow) == 0
    assert {w[1] for w in moved(c)} == {"o/1"}
    # --board を明示した時だけ、ひな形にも触る
    c2 = make_client(make_board(rows, number=3, title="_template"))
    run(tmp_path / "x", c2, job="carryover", board="o/3")
    assert {w[1] for w in moved(c2)} == {"o/3"}


# 14. HALT なら何もせず 0。gh が落ちたら非 0 で last-run はそのまま
def test_14_halt_and_gh_failure_leave_last_run_alone(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "last-run").write_text("OLD\n")
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "HALT").write_text("stop\n")
    c = make_client(make_board([{"n": 140, "week": "2026-10-26"}]))
    assert run(tmp_path, c, job="carryover") == 0
    assert c.writes == [] and (state / "last-run").read_text() == "OLD\n"
    assert "HALT" in capsys.readouterr().out
    (tmp_path / "ledger" / "HALT").unlink()
    c.fail_writes = True
    assert run(tmp_path, c, job="carryover") == 1
    assert (state / "last-run").read_text() == "OLD\n"
    many = make_client(make_board([{"n": 141 + k, "week": "2026-10-26"} for k in range(4)]))
    many.fail_writes = True
    assert run(tmp_path, many, job="carryover") == 6          # 3 回続けて失敗 → HALT
    assert (tmp_path / "ledger" / "HALT").exists()
    (tmp_path / "ledger" / "HALT").unlink()
    ok = make_client(make_board([{"n": 150, "week": "2026-10-26"}]))
    assert run(tmp_path, ok, job="carryover") == 0
    assert (state / "last-run").read_text() == "OLD\n"  # 1 つの仕事だけを手で走らせた回は目印にならん
    ok = make_client(make_board([{"n": 150, "week": "2026-10-26"}]))
    assert run(tmp_path, ok, job="all") == 0
    assert (state / "last-run").read_text().strip() == "2026-11-02T00:05:00+09:00"


# 15. capacity: 前の週に閉じた分（空は 1.5）を 70〜282 に収めて置き換え。count_from より前は置き換えん
def test_15_capacity_rewrite_clamp_and_count_from(tmp_path):
    closed = [{"n": 151, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 40},
              {"n": 152, "state": "CLOSED", "closed": "2026-10-30T01:00:00Z"},                # 空 = 1.5
              {"n": 153, "state": "CLOSED", "closed": "2026-11-01T15:30:00Z", "est": 99},     # 11-02 00:30 JST は今週
              {"n": 154, "state": "CLOSED", "closed": "2026-10-25T14:00:00Z", "est": 99}]     # 10-25 23:00 JST は先々週
    c = make_client(make_board(closed))
    run(tmp_path, c, job="capacity")
    assert c.boards["o/1"].description == "scrum-board: v1 scrum-keeper:capacity=70h count_from=2026-10-26"  # 41.5 → 70
    big = make_client(make_board([{"n": 155, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 500}]))
    run(tmp_path / "big", big, job="capacity")
    assert "capacity=282h" in big.boards["o/1"].description
    early = make_client(make_board(closed, description=DESC.replace("2026-10-26", "2026-11-09")))
    run(tmp_path / "early", early, job="capacity")
    assert early.writes == []
    same = make_client(make_board([{"n": 156, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 176}]))
    run(tmp_path / "same", same, job="capacity")
    assert same.writes == []
    board_place_pattern = r"scrum-keeper:capacity=(\d+(?:\.\d+)?)h count_from=(\d{4}-\d{2}-\d{2})"  # board_place.py と同じ
    assert CAPACITY_RE.pattern == board_place_pattern
    m = re.compile(board_place_pattern).search(c.boards["o/1"].description)
    assert m.groups() == ("70", "2026-10-26")


# 16. 週の報告は 1 つの Project に 1 週 1 本。題名はどこにも出ん
def test_16_report_once_per_week_and_no_titles(tmp_path, capsys):
    c = make_client(make_board([{"n": 160, "week": CUR}, {"n": 161, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z"}]))
    run(tmp_path, c, job="report")
    run(tmp_path, c, job="report")
    assert len(c.status_updates["o/1"]) == 1
    assert c.status_updates["o/1"][0].splitlines()[0] == "scrum-keeper:week=2026-11-02"
    assert "closed=1" in c.status_updates["o/1"][0]
    run(tmp_path, c, job="report", now="2026-11-09T00:05+09:00")
    assert len(c.status_updates["o/1"]) == 2
    run(tmp_path / "dry", make_client(make_board([{"n": 162, "week": "2026-10-26"}])), "--dry-run", job="carryover")
    seen = capsys.readouterr()
    blob = seen.out + seen.err + "".join(c.status_updates["o/1"]) + "".join(
        p.read_text() for p in (tmp_path / "ledger").glob("*.jsonl"))
    assert TITLE not in blob


# 17. sync: 載っとらん open の課題を Backlog で足す。PR と下書きは無視。対応する yml が無ければ何もせん
def test_17_sync_adds_unboarded_issues_only(tmp_path):
    board = make_board([{"n": 902, "repo": "o/r1"}, {"n": 1, "kind": "DraftIssue"}, {"n": 5, "kind": "PullRequest"}])
    repos = {
        "o/r1": {"yml": "owner: o\nproject_number: 1\n",
                 "issues": [(900, "o/r1|900", "Issue"), (901, "o/r1|901", "PullRequest"), (902, "o/r1|902", "Issue")]},
        "o/r2": {"yml": "owner: o\nproject_number: 9\n", "issues": [(910, "o/r2|910", "Issue")]},
    }
    c = make_client(board, repos=repos)
    assert run(tmp_path, c, job="sync") == 0
    added = [w for w in c.writes if w[0] == "add"]
    assert [w[2] for w in added] == ["o/r1#900"]
    assert item(c, "o/1", 900, "o/r1").status == "Backlog"
    run(tmp_path, c, job="sync")
    assert len([w for w in c.writes if w[0] == "add"]) == 1  # 2 回目は足さん
    lonely = make_client(make_board([{"n": 1}], number=5), repos={"o/r2": repos["o/r2"]})
    run(tmp_path / "l", lonely, job="sync", board="o/5")
    assert lonely.writes == []


# ------------------------------------------------------------------ 敵対レビューで出た穴の回帰試験
FUTURE_COUNT = DESC.replace("2026-10-26", "2026-12-28")  # count_from を先にして、量の置き換えを試験の邪魔にせん


def ticks(tmp_path, client, times, **kw):
    for t in times:
        assert run(tmp_path, client, now=t, **kw) == 0


def item_writes(client):
    return [w for w in client.writes if w[0] == "item"]


# 18. おかわりと見込み無し (b) は同じ量を見る。何回回しても引いては戻すが起きず、Carryover も増えん
def test_18_okawari_and_stalled_b_do_not_ping_pong(tmp_path):
    # 今週は待ちの件だけ（60h）、来週に 40h。量 70h。月曜 00:00〜02:45 の 12 回
    c = make_client(make_board(
        [{"n": 1, "week": CUR, "prio": "P0", "est": 60, "wait": "W1"}, {"n": 2, "week": "2026-11-09", "prio": "P1", "est": 40}],
        description=FUTURE_COUNT.replace("176h", "70h")))
    ticks(tmp_path, c, [f"2026-11-02T{h:02d}:{m:02d}+09:00" for h in (0, 1, 2) for m in (0, 15, 30, 45)])
    assert week(c, "o/1", 2) == CUR and item(c, "o/1", 2).carry == 0
    assert len(item_writes(c)) == 1  # 引いたのは 1 回だけ。毎回の書き込みは無い
    # 待ちの件が 90h、来週に 30h。量 176h。木曜の 00:00〜02:45
    c2 = make_client(make_board(
        [{"n": 1, "week": CUR, "prio": "P0", "est": 90, "wait": "W1", "updated": "2026-11-04T10:00:00Z"},
         {"n": 10, "week": "2026-11-09", "prio": "P1", "est": 30}], description=FUTURE_COUNT))
    ticks(tmp_path / "b", c2, [f"2026-11-05T{h:02d}:{m:02d}+09:00" for h in (0, 1, 2) for m in (0, 15, 30, 45)])
    assert item(c2, "o/1", 10).carry == 0 and len(item_writes(c2)) <= 1
    # 水曜の昼に空の今週へ 15 件(10h)の来週から引き、翌日の夜中の見込み無し (b) が押し戻さん
    c3 = make_client(make_board([{"n": 200 + k, "week": "2026-11-09", "prio": "P1", "est": 10} for k in range(15)],
                                description=FUTURE_COUNT))
    ticks(tmp_path / "c", c3, ["2026-11-04T12:00+09:00"])
    pulled = len(item_writes(c3))
    assert 0 < pulled < 15
    ticks(tmp_path / "c", c3, ["2026-11-05T00:30+09:00", "2026-11-05T01:00+09:00"])
    assert len(item_writes(c3)) == pulled and all(item(c3, "o/1", 200 + k).carry == 0 for k in range(15))
    # 木曜に 20h を 5 件、翌金曜 00:20 に押し戻されて Carryover が付く、は起きん
    c4 = make_client(make_board([{"n": 300 + k, "week": "2026-11-09", "prio": "P1", "est": 20} for k in range(5)],
                                description=FUTURE_COUNT))
    ticks(tmp_path / "d", c4, ["2026-11-05T12:00+09:00", "2026-11-06T00:20+09:00"])
    assert all(item(c4, "o/1", 300 + k).carry == 0 for k in range(5))
    assert not [w for w in item_writes(c4) if w[3] == "it-2026-11-09"]  # 来週へ戻す書き込みは無い


# 19. 1 週の量は全部の Project で 1 つ。Project ごとに量を使わず、Project をまたいで優先度の順に切る
def test_19_capacity_is_shared_across_boards(tmp_path):
    p2 = [{"n": 400 + k, "week": CUR, "prio": "P2", "est": 10} for k in range(17)]
    p0 = [{"n": 500 + k, "week": CUR, "prio": "P0", "est": 10} for k in range(17)]
    b1 = make_board(p2, number=1, description=FUTURE_COUNT)
    b2 = make_board(p0, number=2, description=FUTURE_COUNT)
    c = make_client(b1, b2)
    allow = tmp_path / "allow.txt"
    allow.write_text("o/1\no/2\n")
    ticks(tmp_path, c, ["2026-11-03T00:30+09:00"], job="stalled", allow=allow)  # 火曜: 量 150.86h
    in_cur = lambda ref: [i for i in c.boards[ref].items if i.iteration_id == f"it-{CUR}"]
    total = sum(i.est for r in ("o/1", "o/2") for i in in_cur(r))
    assert total <= 6 * 176 / 7 + 1e-9
    assert len(in_cur("o/1")) == 0 and len(in_cur("o/2")) == 15  # P0 の方が先に残る
    # 二度目は何も動かさん
    n = len(item_writes(c))
    ticks(tmp_path, c, ["2026-11-03T00:45+09:00"], job="stalled", allow=allow)
    assert len(item_writes(c)) == n


# 20. 終わった週のどこからでも繰り越す。月曜の窓を Mac が寝とっても、次の回で拾い、取りこぼしは報告に出る
def test_20_carryover_from_any_ended_week_without_monday_window(tmp_path):
    desc = DESC.replace("2026-10-26", "2026-10-12")
    c = make_client(make_board([{"n": 600, "week": "2026-10-19", "prio": "P1"}, {"n": 601, "week": "2026-10-26"}],
                               description=desc))
    ticks(tmp_path, c, ["2026-11-02T07:00+09:00"], job="carryover")
    assert week(c, "o/1", 600) == CUR and week(c, "o/1", 601) == CUR
    assert item(c, "o/1", 600).carry == 1 and item(c, "o/1", 601).carry == 1  # 動かした 1 回につき 1 だけ
    # 次の月曜まで放っておかれた物は、報告の stranded に出る
    stuck = make_client(make_board([{"n": 602, "week": "2026-10-19"}], description=desc))
    ticks(tmp_path / "s", stuck, ["2026-11-02T07:00+09:00"], job="report")
    assert "stranded=1" in stuck.status_updates["o/1"][0]


# 21. Carryover を足すのは carryover だけ。同じ月曜の回で stalled が重ねて足さん
def test_21_carryover_incremented_once_per_week(tmp_path):
    desc = DESC.replace("2026-10-26", "2026-10-12")
    wait = {"n": 700, "week": "2026-10-26", "wait": "W1", "updated": "2026-10-28T00:00:00Z"}
    many = [{"n": 710 + k, "week": "2026-10-26", "prio": "P1", "est": 10} for k in range(20)]
    c = make_client(make_board([wait] + many, description=desc))
    ticks(tmp_path, c, [NOW_MON])
    assert week(c, "o/1", 700) == "2026-11-09" and item(c, "o/1", 700).carry == 1
    assert {item(c, "o/1", 710 + k).carry for k in range(20)} == {1}  # 量を超えて来週へ戻った物も 1 のまま
    assert "resplit-candidates(carryover>=2): 0" in c.status_updates["o/1"][0]


# 22. 上限の返事: 1 秒空け、60/120/240 秒で再試行。HALT は 5 回続いた時だけ
def test_22_rate_limit_backs_off_and_halts_only_after_five(tmp_path, no_real_sleep):
    items = [{"n": 800, "week": "2026-10-26"}]
    c = make_client(make_board(items))
    c.rate_limit_writes = 2
    assert run(tmp_path, c, job="carryover") == 0
    assert week(c, "o/1", 800) == CUR
    assert no_real_sleep == [1, 60, 1, 120, 1]
    assert not (tmp_path / "ledger" / "HALT").exists()
    # 3 回の再試行でも通らん: この回を止める（4）。HALT は書かず、連続の数は回をまたいで 4
    no_real_sleep.clear()
    c2 = make_client(make_board(items))
    c2.rate_limit_writes = 99
    assert run(tmp_path / "x", c2, job="carryover") == 4
    assert no_real_sleep == [1, 60, 1, 120, 1, 240, 1]
    assert not (tmp_path / "x" / "ledger" / "HALT").exists()
    assert (tmp_path / "x" / "state" / "rate-limit-streak").read_text().strip() == "4"
    # 次の回の最初の返事で 5 回続き → HALT
    assert run(tmp_path / "x", c2, job="carryover") == 6
    assert (tmp_path / "x" / "ledger" / "HALT").exists()
    # 通った書き込みで連続の数は 0 に戻る
    c3 = make_client(make_board(items))
    c3.rate_limit_writes = 1
    assert run(tmp_path / "y", c3, job="carryover") == 0
    assert (tmp_path / "y" / "state" / "rate-limit-streak").read_text().strip() == "0"


# 23. 同じ値の書き込みはせん。待ちの子が居る親は動かさず、毎回同じ書き込みを繰り返さん
def test_23_no_repeated_identical_writes(tmp_path):
    c = make_client(make_board([
        {"n": 3, "week": CUR, "wait": "W1"},
        {"n": 20, "week": "2026-11-09", "children": ["o/r#21"], "est": 1},
        {"n": 21, "week": "2026-11-09", "parent": "o/r#20", "wait": "W2", "est": 1},
    ], description=FUTURE_COUNT))
    for k in range(8):
        run(tmp_path, c, job="okawari", now=f"2026-11-04T1{k}:00+09:00")
    assert item_writes(c) == []
    assert [x for x in ledger_lines(tmp_path) if x["phase"] == "done"] == []


# 24. 回の途中で置かれた HALT は、次の書き込みから効く
def test_24_halt_placed_mid_tick_stops_further_writes(tmp_path):
    c = make_client(make_board([{"n": 900 + k, "week": "2026-10-26"} for k in range(20)]))
    halt = tmp_path / "ledger" / "HALT"

    def owner_presses_stop(live_board, live_item):
        if live_item.number == 902:
            halt.parent.mkdir(parents=True, exist_ok=True)
            halt.write_text("by=owner stop\n")

    c.before_read_item = owner_presses_stop
    assert run(tmp_path, c, job="all") == 0
    assert len(item_writes(c)) <= 3
    assert not (tmp_path / "state" / "last-run").exists()


# 25. 週を動かす時は Start/End date も同じ書き込みで直す。期日が動かす先の週の中にあれば残す
def test_25_move_rewrites_start_and_end_date(tmp_path):
    desc = DESC.replace("2026-10-26", "2026-10-12")
    c = make_client(make_board([
        {"n": 1000, "week": "2026-10-26", "start": "2026-10-26", "end": "2026-11-01"},
        {"n": 1001, "week": "2026-10-26", "start": "2026-10-26", "end": "2026-11-05"},   # 局長が言った期日
        {"n": 1002, "week": "2026-10-26"},
    ], description=desc))
    run(tmp_path, c, job="carryover")
    a, b, d = (item(c, "o/1", n) for n in (1000, 1001, 1002))
    assert (a.start_date, a.end_date) == (dt.date(2026, 11, 2), dt.date(2026, 11, 8))
    assert (b.start_date, b.end_date) == (dt.date(2026, 11, 2), dt.date(2026, 11, 5))
    assert (d.start_date, d.end_date) == (dt.date(2026, 11, 2), dt.date(2026, 11, 8))
    # 週を動かさん書き込み（待ちの親など）は日付にも触らん
    run(tmp_path, c, job="carryover")
    assert len(item_writes(c)) == 3


# 26. 起動の入口は、決めた commit と寸分違わん木だけを動かす
def test_26_launcher_refuses_anything_but_the_pinned_clean_tree(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    runner = Path(__file__).resolve().parents[1] / "launchd" / "scrum-keeper-run.sh"
    wt, state = tmp_path / "wt", tmp_path / "state"
    (wt / "scripts" / "scrum_keeper").mkdir(parents=True)
    state.mkdir()
    (wt / "scripts" / "scrum_keeper" / "scrum_keeper.py").write_text("import sys\nsys.exit(0)\n")
    (wt / ".gitignore").write_text("__pycache__/\n*.local\n")
    git = lambda *a: subprocess.run(["/usr/bin/git", "-C", str(wt), "-c", "user.email=t@example.com", "-c", "user.name=t",
                                     *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "x")
    (state / "pinned-sha").write_text(git("rev-parse", "HEAD") + "\n")
    env = {"HOME": str(tmp_path), "SCRUM_KEEPER_STATE": str(state), "SCRUM_KEEPER_WORKTREE": str(wt),
           "SCRUM_KEEPER_HALT_FILE": str(tmp_path / "HALT"), "SCRUM_KEEPER_LEDGER_DIR": str(tmp_path / "l"),
           "SCRUM_KEEPER_CAPACITY_BOARD": "o/1", "SCRUM_KEEPER_PYTHON": sys.executable, "PATH": os.environ["PATH"]}
    go = lambda: subprocess.run(["/bin/bash", str(runner), "--board", "o/1"], env=env, capture_output=True, text=True).returncode
    assert go() == 0
    (wt / "scripts" / "scrum_keeper" / "__pycache__").mkdir()
    (wt / "scripts" / "scrum_keeper" / "__pycache__" / "x.pyc").write_text("")
    assert go() == 0                                   # __pycache__ だけは許す
    (wt / "scripts" / "scrum_keeper" / "scrum_keeper.py").write_text("import sys\nsys.exit(42)\n")
    assert go() == 9                                   # 未コミットの書き換え
    git("checkout", "--", ".")
    (wt / "scripts" / "scrum_keeper" / "json.py").write_text("")
    assert go() == 9                                   # 足された未追跡のファイル
    (wt / "scripts" / "scrum_keeper" / "json.py").unlink()
    (wt / "scripts" / "scrum_keeper" / "evil.local").write_text("")
    assert go() == 9                                   # .gitignore で隠れとるファイルも
    (wt / "scripts" / "scrum_keeper" / "evil.local").unlink()
    assert go() == 0
    (state / "pinned-sha").write_text("0" * 40 + "\n")
    assert go() == 9                                   # commit が違う


# 27. 計画に「今の週と同じ値」が混ざっても、書き込みにならん（上限と記録を食わん）
def test_27_same_value_move_is_not_written(tmp_path):
    from scrum_keeper.job import Move, Runner
    from scrum_keeper.ledger import Ledger
    c = make_client(make_board([{"n": 20, "week": "2026-11-09", "carry": 1}]))
    board = c.read_board("o", 1)
    pbi = board.items[0]
    rn = Runner(c, Ledger(tmp_path / "l", None), False, 50)
    assert rn.execute_moves(board, "okawari", [Move(pbi, "it-2026-11-09", None), Move(pbi, "it-2026-11-09", 1)]) == 0
    assert c.writes == [] and not (tmp_path / "l" / "K.jsonl").exists()

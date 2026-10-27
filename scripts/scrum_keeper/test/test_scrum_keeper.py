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


# 6. 週の境は JST。ホストの時計（PDT）は見ん
def test_06_jst_boundary_ignores_host_clock(tmp_path, capsys):
    base = [{"n": 60, "week": "2026-10-26"}]
    pdt_same_instant = "2026-11-01T08:05-07:00"  # = 2026-11-02 00:05 JST
    for now, expect_monday in ((NOW_MON, True), (pdt_same_instant, True), ("2026-11-01T23:59+09:00", False)):
        capsys.readouterr()
        c = make_client(make_board(base))
        run(tmp_path / now.replace(":", ""), c, now=now)
        jobs = re.search(r"jobs=(\S+)", capsys.readouterr().err).group(1).split(",")
        assert ("carryover" in jobs) is expect_monday
        assert ("extend" in jobs) is expect_monday
        assert ("okawari" in jobs)  # 毎回の仕事は日曜の夜でも動く


# 7. 見込み無し (a): 48 時間 1 分で動き、47 時間 59 分では残る
def test_07_stalled_a_threshold(tmp_path):
    c = make_client(make_board([
        {"n": 70, "week": CUR, "wait": "W1", "updated": "2026-11-02T02:59:00Z"},   # 48h01m 前
        {"n": 71, "week": CUR, "wait": "W1", "updated": "2026-11-02T03:01:00Z"},   # 47h59m 前
        {"n": 72, "week": CUR, "updated": "2026-10-01T00:00:00Z"},                 # Wait 無しは対象外
    ]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert week(c, "o/1", 70) == "2026-11-09" and item(c, "o/1", 70).carry == 1
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
    assert item(c, "o/1", 92).carry == 1


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


# 12. extend は先 8 週を保ち、既存の id を残す。件数が減ったら HALT
def test_12_extend_keeps_eight_weeks_preserves_ids_and_halts_on_loss(tmp_path):
    weeks = monday_range(n=9)  # 10-19 〜 12-14（今週から数えて 7 本）
    items = [{"n": 120, "prio": "P0", "est": 10}, {"n": 121, "prio": "P1", "est": 10}, {"n": 122, "wait": "W1"},
             {"n": 123, "week": "2026-11-09"}]
    c = make_client(make_board(items, weeks=weeks))
    old_ids = {i.id for i in c.boards["o/1"].iterations}
    assert run(tmp_path, c, job="extend") == 0
    its = c.boards["o/1"].iterations
    assert old_ids <= {i.id for i in its}
    assert len([i for i in its if i.start >= dt.date.fromisoformat(CUR)]) == 8
    assert week(c, "o/1", 120) == "2026-12-21" and week(c, "o/1", 121) == "2026-12-21"
    assert week(c, "o/1", 122) is None and week(c, "o/1", 123) == "2026-11-09"

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

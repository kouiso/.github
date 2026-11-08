"""scrum-keeper の試験。GitHub には触らず、本物の応答と同じ形の dict を FakeClient に読ませる。"""
import datetime as dt
import json
import re

from helpers import (CUR, DESC, NOW_MON, NOW_WED, TITLE, item, ledger_lines, make_board, make_client, monday_range,
                     raw_project, run, week)
from scrum_keeper.github_client import FakeClient, GhClient, GhError
from scrum_keeper.model import CAPACITY_RE, parse_capacity


def moved(client):
    return [w for w in client.writes if w[0] == "item"]


# 1. 今週に open・Done でない物が 0 なら、おかわりが動く
def test_01_okawari_fires_when_current_week_has_no_workable_item(tmp_path):
    board = make_board([
        {"n": 1, "week": CUR, "state": "CLOSED", "status": "Done"},
        {"n": 2, "week": CUR, "status": "Done"},                 # open だが Done
        {"n": 10, "week": "2026-11-09", "prio": "P0", "est": 8},
        {"n": 11, "week": "2026-11-09", "prio": "P1", "est": 8},
    ])
    c = make_client(board)
    assert run(tmp_path, c, job="okawari", now=NOW_WED) == 0
    assert week(c, "o/1", 10) == CUR and week(c, "o/1", 11) == CUR


# 2. 今週に仕事があれば引かん。1 週に入りきらん大きい物も引かん
def test_02_okawari_skips_busy_week_and_oversized_item(tmp_path):
    # 1 週に入りきらん大きい物は引かん（引いても見込み無し (b) が戻すため）
    huge = make_client(make_board([{"n": 13, "week": "2026-11-09", "prio": "P0", "est": 500},
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
    # 繰り越し側: 親は子の一番遅い週へ
    board2 = make_board([
        {"n": 30, "week": "2026-10-26", "children": ["o/r#31", "o/r#32"]},
        {"n": 31, "week": "2026-10-26", "parent": "o/r#30"},
        {"n": 32, "week": "2026-11-16", "parent": "o/r#30"},
    ])
    c2 = make_client(board2)
    run(tmp_path / "b", c2, job="carryover")
    assert week(c2, "o/1", 31) == CUR
    assert week(c2, "o/1", 30) == "2026-11-16"


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
    assert week(c2, "o/1", 52) == "2026-11-09"
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


# 7. 盤から消える項目（Wait・Carryover・Size・Start date・End date）は、在っても無くても読みも書きもせん
def test_07_legacy_fields_are_never_read_or_written(tmp_path, capsys):
    rows = [{"n": 70, "week": "2026-10-26", "legacy": True},           # 前は Wait 付きで飛ばしとった物も動く
            {"n": 71, "week": CUR, "est": 4, "legacy": True},
            {"n": 72, "week": "2026-11-09", "prio": "P0", "est": 2, "legacy": True}]
    for legacy in (True, False):
        c = make_client(make_board(rows, legacy=legacy))
        d = tmp_path / str(legacy)
        assert run(d, c, now=NOW_WED) == 0
        assert week(c, "o/1", 70) == CUR
        assert {len(w) for w in moved(c)} == {4}  # ("item", board, key, iteration) だけ
        pre = [json.loads(x["pre"]) for x in ledger_lines(d) if x["phase"] == "intent" and x["step"] == "keeper-carryover"]
        assert pre == [{"Iteration": "it-2026-10-26"}]
    # 本物の書き込みの文も Iteration の 1 本だけ
    sent = []

    class Spy(GhClient):
        def _gql(self, query, **variables):
            sent.append(query)
            return {"data": {}}

    b = make_board(rows, legacy=True)
    Spy().set_item_fields(b, b.items[0], "it-2026-11-02")
    assert len(sent) == 1 and sent[0].count("updateProjectV2ItemFieldValue") == 1 and "F_iter" in sent[0]
    for gone in ("F_wait", "F_carry", "F_size", "F_start", "F_end"):
        assert gone not in sent[0]


def cap_board(items, cap="70", count_from="2026-10-26", **kw):
    return make_board(items, description=f"scrum-board: v1 scrum-keeper:capacity={cap}pt count_from={count_from}", **kw)


# 8. 見込み無し (b): 合計がちょうど上限なら残る
def test_08_stalled_b_exactly_at_limit_stays(tmp_path):
    # 水曜は日曜まで 5 日。70 点 ÷ 7 × 5 = 50
    c = make_client(cap_board([{"n": 80, "week": CUR, "prio": "P0", "est": 25}, {"n": 81, "week": CUR, "prio": "P1", "est": 25}]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert moved(c) == []


# 9. 見込み無し (b): またいだ件から盤の下は全部動く。小さい件を詰め直さん。In Progress が先頭。Priority は見ん
def test_09_stalled_b_crossing_item_and_all_below_move(tmp_path):
    c = make_client(cap_board([
        {"n": 90, "week": CUR, "prio": "P2", "est": 20, "status": "In Progress"},
        {"n": 91, "week": CUR, "prio": "P0", "est": 20},
        {"n": 92, "week": CUR, "prio": "P0", "est": 20},   # 累計 60 > 50 ここから下
        {"n": 93, "week": CUR, "prio": "P1", "est": 5},
        {"n": 94, "week": CUR, "prio": "P0", "est": 5},   # P0 でも盤の一番下なので押し出す
    ]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert week(c, "o/1", 90) == CUR and week(c, "o/1", 91) == CUR
    assert [week(c, "o/1", n) for n in (92, 93, 94)] == ["2026-11-09"] * 3  # 93・94 は詰め直せば入るが動く


# 10. 見込み無し (b): 1 番上だけで超えたら全部。上限 0 以下も全部
def test_10_stalled_b_top_item_over_limit_and_zero_limit(tmp_path):
    c = make_client(cap_board([{"n": 100, "week": CUR, "prio": "P0", "est": 60}, {"n": 101, "week": CUR, "est": 1}]))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert week(c, "o/1", 100) == "2026-11-09" and week(c, "o/1", 101) == "2026-11-09"
    c0 = make_client(cap_board([{"n": 102, "week": CUR, "est": 1}], cap="0"))
    run(tmp_path / "z", c0, job="stalled", now=NOW_WED)
    assert week(c0, "o/1", 102) == "2026-11-09"


# 11. 並べ順は盤の手で並べた順。Priority は見ん。量は Story Point を読み、無い盤では Estimate を読む
def test_11_board_position_order_ignores_priority_and_story_point(tmp_path):
    # 水曜は日曜まで 5 日。70 点 ÷ 7 × 5 = 50。盤の上から 4 件で 32、5 件目（P0）で 52 > 50
    rows = [{"n": 113, "week": CUR, "est": 1},
            {"n": 112, "week": CUR, "prio": "P3", "est": 1},
            {"n": 111, "week": CUR, "prio": "P2", "est": 10},
            {"n": 110, "week": CUR, "prio": "P1", "est": 20},
            {"n": 114, "week": CUR, "prio": "P0", "est": 20}]
    c = make_client(cap_board(rows))
    assert [i.position for i in c.boards["o/1"].items] == [0, 1, 2, 3, 4]
    run(tmp_path, c, job="stalled", now=NOW_WED)
    assert [week(c, "o/1", n) for n in (113, 112, 111, 110)] == [CUR] * 4
    assert week(c, "o/1", 114) == "2026-11-09"  # P0 でも盤の一番下から押し出す
    assert c.boards["o/1"].items[3].estimate == 20  # Story Point を読んだ
    # 名前の付け替え前の盤（Estimate）も同じ量で読む
    old = make_client(cap_board([dict(r, est_field="Estimate") for r in rows], est_field="Estimate"))
    run(tmp_path / "old", old, job="stalled", now=NOW_WED)
    assert week(old, "o/1", 110) == CUR and week(old, "o/1", 114) == "2026-11-09"
    # おかわりは来週の盤の上から引く。水曜に引ける量は 70 ÷ 7 × 4 = 40。上の P3（30）が入り、下の P0 は入らん
    pull = make_client(cap_board([{"n": 115, "week": "2026-11-09", "prio": "P3", "est": 30},
                                  {"n": 116, "week": "2026-11-09", "prio": "P0", "est": 30}]))
    run(tmp_path / "pull", pull, job="okawari", now=NOW_WED)
    assert week(pull, "o/1", 115) == CUR and week(pull, "o/1", 116) == "2026-11-09"
    # 本物の読み取りは盤の並び順を名指しで頼み、頁をつないだ順をそのまま position にする
    sent = []
    pages = [raw_project([{"n": 1}, {"n": 2}]), raw_project([{"n": 3}])]
    pages[0]["items"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c1"}

    class Paged(GhClient):
        def _gql(self, query, **variables):
            sent.append((query, variables.get("after")))
            return {"data": {"repositoryOwner": {"projectV2": pages[len(sent) - 1]}}}

    b = Paged().read_board("o", 1)
    assert [(i.number, i.position) for i in b.items] == [(1, 0), (2, 1), (3, 2)]
    assert [a for _, a in sent] == [None, "c1"]
    assert "orderBy:{field:POSITION,direction:ASC}" in sent[0][0]


# 12. extend は今週と 3 週先まで（4 本）を保ち、既存の id を残す。新しい週の題は「Iteration N」。件数が減ったら HALT
def test_12_extend_keeps_three_weeks_ahead_preserves_ids_and_halts_on_loss(tmp_path):
    weeks = monday_range(n=5)  # 10-19 〜 11-16（今週から数えて 3 本）
    titles = {"2026-10-19": "Iteration 5", "2026-10-26": "Sprint 2026-10-26", "2026-11-09": "Iteration 2"}
    fillers = [{"n": 130 + k, "week": w, "est": 176} for k, w in enumerate(["2026-11-02", "2026-11-09", "2026-11-16"])]
    items = fillers + [{"n": 120, "prio": "P0", "est": 10}, {"n": 121, "prio": "P1", "est": 10}]
    mk = lambda: make_board(items, weeks=weeks, iter_titles=titles)
    c = make_client(mk())
    old_ids = {i.id for i in c.boards["o/1"].iterations}
    assert run(tmp_path, c, job="extend") == 0
    its = c.boards["o/1"].iterations
    assert old_ids <= {i.id for i in its}
    ahead = [i for i in its if i.start >= dt.date.fromisoformat(CUR)]
    assert [i.start.isoformat() for i in ahead] == ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"]
    new = ahead[-1]
    assert new.title == "Iteration 6" and new.duration == 7  # 終わった週の Iteration 5 も数えて次の番号
    assert week(c, "o/1", 120) == "2026-11-23" and week(c, "o/1", 121) == "2026-11-23"
    assert week(c, "o/1", 130) == CUR
    assert run(tmp_path, c, job="extend") == 0
    assert len(c.boards["o/1"].iterations) == len(its)  # 2 回目は何も足さん
    # 「Iteration N」が 1 つも無ければ 1 から。足りん週が 2 つなら続き番号
    fresh = make_client(make_board([], weeks=monday_range(n=3)))
    assert run(tmp_path / "fresh", fresh, job="extend") == 0
    assert [i.title for i in fresh.boards["o/1"].iterations if i.start >= dt.date.fromisoformat(CUR)] == \
        ["Sprint 2026-11-02", "Iteration 1", "Iteration 2", "Iteration 3"]
    # 早い週に空きがあるなら、先の週へ隠さず週なしのまま残す（P0 を先の週に置かん）
    roomy = make_client(make_board([{"n": 140, "prio": "P0", "est": 10}], weeks=weeks))
    assert run(tmp_path / "roomy", roomy, job="extend") == 0
    assert week(roomy, "o/1", 140) is None
    # 反復の設定を書き直して id が保たれるかを測り終えた Project だけ、本番で書く
    # 載ってない時は書かんが、gh の失敗やないので回は止めず last-run も進める（日・週に 1 回の仕事を毎回走らせん）
    blocked = make_client(mk())
    assert run(tmp_path / "blocked", blocked, job="all", verified=None) == 0
    assert not [w for w in blocked.writes if w[0] == "iterations"]
    assert (tmp_path / "blocked" / "state" / "last-run").exists()
    assert run(tmp_path / "blocked2", blocked, "--dry-run", job="extend", verified=None) == 0
    # 週が 1 つも無い盤は設定の始まりの日が読めんので書かずに飛ばす。これも回の失敗にせん
    empty = make_client(make_board([], weeks=[]))
    assert run(tmp_path / "empty", empty, job="extend") == 0
    assert not [w for w in empty.writes if w[0] == "iterations"]

    class Lossy(FakeClient):
        def update_iteration_config(self, board, iterations):
            super().update_iteration_config(board, iterations)
            for i in self.boards[board.ref].items:
                if i.iteration_id == "it-2026-11-09":
                    i.iteration_id = None  # 書き直しで件数が減った

    bad = Lossy({"o/1": mk()})
    assert run(tmp_path / "bad", bad, job="extend") == 6
    assert (tmp_path / "bad" / "ledger" / "HALT").exists()

    # 書いた後の読み直しが落ちたら、件数を確かめられんので HALT（読めんまま先へ進まん）
    class BlindAfterWrite(FakeClient):
        def read_board(self, owner, number):
            if any(w[0] == "iterations" for w in self.writes):
                raise GhError("boom after write")
            return super().read_board(owner, number)

    blind = BlindAfterWrite({"o/1": mk()})
    assert run(tmp_path / "blind", blind, job="extend") == 6
    assert "extend re-read failed" in (tmp_path / "blind" / "ledger" / "HALT").read_text()
    assert not item_writes(blind)
    # 本物の書き直しの文: 既存の週は id 付き、新しい週は id 無しで 7 日
    sent = []

    class Spy(GhClient):
        def _gql(self, query, **variables):
            sent.append(query)
            return {"data": {}}

    b = mk()
    from scrum_keeper.model import Iteration
    Spy().update_iteration_config(b, b.iterations + [Iteration("new:2026-11-16", "Iteration 6", dt.date(2026, 11, 16))])
    assert all(f'id:"{i.id}"' in sent[0] for i in b.iterations)
    assert '{title:"Iteration 6",startDate:"2026-11-16",duration:7}' in sent[0]


# 12b. 新しい「Iteration N」の番号は始まりの日の順に増える。後ろの週に番号付きが在る隙間の週は足さず、ログに出す
def test_12b_gap_week_is_not_numbered_out_of_date_order(tmp_path, capsys):
    gap = monday_range(n=4) + ["2026-11-30"]  # 11-16・11-23 が抜けとる
    later_numbered = make_client(make_board([], weeks=gap, iter_titles={"2026-11-30": "Iteration 9"}))
    capsys.readouterr()
    assert run(tmp_path, later_numbered, job="extend") == 0
    err = capsys.readouterr().err
    assert "extend skipped board=o/1 week=2026-11-16" in err and "week=2026-11-23" in err
    assert not later_numbered.writes  # 既存の週の題も付け替えん
    titles = [(i.start.isoformat(), i.title) for i in later_numbered.boards["o/1"].iterations]
    assert ("2026-11-30", "Iteration 9") in titles and len(titles) == 5
    # 後ろの週の題が番号付きでなければ順は崩れんので足す
    later_plain = make_client(make_board([], weeks=gap))
    assert run(tmp_path / "plain", later_plain, job="extend") == 0
    added = {i.start.isoformat(): i.title for i in later_plain.boards["o/1"].iterations}
    assert added["2026-11-16"] == "Iteration 1" and added["2026-11-23"] == "Iteration 2"
    # 番号付きが隙間より前だけなら、続きの番号で足す
    before = make_client(make_board([], weeks=gap, iter_titles={"2026-11-09": "Iteration 4"}))
    assert run(tmp_path / "before", before, job="extend") == 0
    got = [i.title for i in before.boards["o/1"].iterations if i.start >= dt.date(2026, 11, 9)]
    assert got == ["Iteration 4", "Iteration 5", "Iteration 6", "Sprint 2026-11-30"]


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


# 15. capacity: 前の 3 週に閉じた Story Point の平均（空は 2 点、10 点より下げん）で置き換え。count_from より前は置き換えん
def test_15_capacity_rewrite_clamp_and_count_from(tmp_path):
    closed = [{"n": 151, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 40},
              {"n": 152, "state": "CLOSED", "closed": "2026-10-30T01:00:00Z"},                # 空 = 2 点
              {"n": 153, "state": "CLOSED", "closed": "2026-11-01T15:30:00Z", "est": 99},     # 11-02 00:30 JST は今週
              {"n": 154, "state": "CLOSED", "closed": "2026-10-18T14:00:00Z", "est": 9},      # 10-18 23:00 JST は 3 週前
              {"n": 157, "state": "CLOSED", "closed": "2026-10-11T14:00:00Z", "est": 99}]     # 10-11 23:00 JST は 4 週前
    c = make_client(make_board(closed))
    run(tmp_path, c, job="capacity")
    # (40 + 2 + 9) ÷ 3 = 17 点。前の週 1 週ぶんの時間の数え方なら 42 → 70h になる
    assert c.boards["o/1"].description == "scrum-board: v1 scrum-keeper:capacity=17pt count_from=2026-10-26"
    low = make_client(make_board([{"n": 158, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 1}]))
    run(tmp_path / "low", low, job="capacity")
    assert "capacity=10pt" in low.boards["o/1"].description  # 0 点には潰さん
    big = make_client(make_board([{"n": 155, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 500}]))
    run(tmp_path / "big", big, job="capacity")
    assert "capacity=166pt" in big.boards["o/1"].description  # 上限は置かん。500 ÷ 3 の切り捨て
    early = make_client(make_board(closed, description=DESC.replace("2026-10-26", "2026-11-09")))
    run(tmp_path / "early", early, job="capacity")
    assert early.writes == []
    desc20 = "scrum-board: v1 scrum-keeper:capacity=20pt count_from=2026-10-26"
    same = make_client(make_board([{"n": 156, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 60}],
                                  description=desc20))
    run(tmp_path / "same", same, job="capacity")
    assert same.writes == []
    # 前の決まりの時間の目印は、同じ数でも点の目印に書き換える。時間の値は量として使わん
    hours = make_client(make_board([{"n": 159, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 60}],
                                   description=desc20.replace("20pt", "20h")))
    run(tmp_path / "hours", hours, job="capacity")
    assert hours.boards["o/1"].description == desc20
    assert parse_capacity(desc20.replace("20pt", "176h")) == (None, dt.date(2026, 10, 26))
    assert parse_capacity(desc20) == (20.0, dt.date(2026, 10, 26))
    m = CAPACITY_RE.search(c.boards["o/1"].description)
    assert m.groups() == ("17", "pt", "2026-10-26")


# 15b. 時間の古い目印の盤は、月曜に置き換えるまでの回も前の 3 週の実績の点を量にする。時間の数は量に使わん
def test_15b_hours_token_uses_velocity_points_until_rewritten(tmp_path):
    closed = [{"n": 160 + k, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 35} for k in range(3)]
    cur = [{"n": 170 + k, "week": CUR, "est": 10} for k in range(6)]
    c = make_client(make_board(closed + cur, description=DESC.replace("176pt", "176h")))
    run(tmp_path, c, job="stalled", now=NOW_WED)
    # 量は 105 ÷ 3 = 35 点。水曜は 35 ÷ 7 × 5 = 25 点で、上の 2 件（20 点）だけ残る。176 を量に使うと 6 件とも残る
    assert [week(c, "o/1", 170 + k) for k in range(6)] == [CUR] * 2 + ["2026-11-09"] * 4


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


# 18. おかわりと見込み無し (b) は同じ量を見る。何回回しても引いては戻すが起きん
def test_18_okawari_and_stalled_b_do_not_ping_pong(tmp_path):
    # 今週は空、来週に 40 点。量 70 点。月曜 00:00〜02:45 の 12 回
    c = make_client(make_board(
        [{"n": 1, "week": CUR, "status": "Done"}, {"n": 2, "week": "2026-11-09", "prio": "P1", "est": 40}],
        description=FUTURE_COUNT.replace("176pt", "70pt")))
    ticks(tmp_path, c, [f"2026-11-02T{h:02d}:{m:02d}+09:00" for h in (0, 1, 2) for m in (0, 15, 30, 45)])
    assert week(c, "o/1", 2) == CUR
    assert len(item_writes(c)) == 1  # 引いたのは 1 回だけ。毎回の書き込みは無い
    # 水曜の昼に空の今週へ 15 件(10 点)の来週から引き、翌日の夜中の見込み無し (b) が押し戻さん
    c3 = make_client(make_board([{"n": 200 + k, "week": "2026-11-09", "prio": "P1", "est": 10} for k in range(15)],
                                description=FUTURE_COUNT))
    ticks(tmp_path / "c", c3, ["2026-11-04T12:00+09:00"])
    pulled = len(item_writes(c3))
    assert 0 < pulled < 15
    ticks(tmp_path / "c", c3, ["2026-11-05T00:30+09:00", "2026-11-05T01:00+09:00"])
    assert len(item_writes(c3)) == pulled
    # 木曜に 20 点を 5 件、翌金曜 00:20 に押し戻される、は起きん
    c4 = make_client(make_board([{"n": 300 + k, "week": "2026-11-09", "prio": "P1", "est": 20} for k in range(5)],
                                description=FUTURE_COUNT))
    ticks(tmp_path / "d", c4, ["2026-11-05T12:00+09:00", "2026-11-06T00:20+09:00"])
    assert not [w for w in item_writes(c4) if w[3] == "it-2026-11-09"]  # 来週へ戻す書き込みは無い


# 19. 1 週の量は全部の Project で 1 つ。Project ごとに量を使わず、各 Project の盤の上から交互に並べて下から切る
def test_19_capacity_is_shared_across_boards(tmp_path):
    p2 = [{"n": 400 + k, "week": CUR, "prio": "P2", "est": 10} for k in range(17)]
    p0 = [{"n": 500 + k, "week": CUR, "prio": "P0", "est": 10} for k in range(17)]
    b1 = make_board(p2, number=1, description=FUTURE_COUNT)
    b2 = make_board(p0, number=2, description=FUTURE_COUNT)
    c = make_client(b1, b2)
    allow = tmp_path / "allow.txt"
    allow.write_text("o/1\no/2\n")
    ticks(tmp_path, c, ["2026-11-03T00:30+09:00"], job="stalled", allow=allow)  # 火曜: 量 150.86 点
    in_cur = lambda ref: [i for i in c.boards[ref].items if i.iteration_id == f"it-{CUR}"]
    total = sum(i.est for r in ("o/1", "o/2") for i in in_cur(r))
    assert total <= 6 * 176 / 7 + 1e-9
    # 入るのは 15 件。o/1・o/2 の上から交互に 8 件と 7 件。Priority（o/2 が P0）は見ん
    assert [i.number for i in in_cur("o/1")] == list(range(400, 408))
    assert [i.number for i in in_cur("o/2")] == list(range(500, 507))
    # 二度目は何も動かさん
    n = len(item_writes(c))
    ticks(tmp_path, c, ["2026-11-03T00:45+09:00"], job="stalled", allow=allow)
    assert len(item_writes(c)) == n


# 19b. 1 つでも読めん Project があると量が分からん。量を使う仕事（capacity・stalled-b・okawari・埋め）は
#      その回まるごと飛ばして 1 で終わる。量を使わん繰り越しは回る
def test_19b_unread_board_skips_every_shared_capacity_job(tmp_path, capsys):
    boards = tmp_path / "boards.txt"
    boards.write_text("o/1\no/2\n")  # o/2 は読めん
    over = [{"n": 1100 + k, "week": CUR, "est": 10} for k in range(20)]           # 200 点。押し出す量がある
    closed = [{"n": 1150, "state": "CLOSED", "closed": "2026-10-28T01:00:00Z", "est": 40}]  # 量を 13 点に置き換える材料
    carry = [{"n": 1160, "week": "2026-10-26"}]
    mk = lambda: make_board(over + closed + carry, number=1)
    c = make_client(mk())
    capsys.readouterr()
    assert run(tmp_path, c, "--boards-file", str(boards), board=None, job="all", now=NOW_WED) == 1
    err = capsys.readouterr().err
    for job in ("capacity", "stalled-b", "okawari"):
        assert f"skipped job={job}: shared capacity unknown, unread boards=o/2" in err
    assert [w[2] for w in item_writes(c)] == ["o/r#1160"]  # 繰り越しだけ
    assert c.boards["o/1"].description == DESC  # 量の目印は書き換えん
    assert not (tmp_path / "state" / "last-run").exists()
    # 全部読めれば同じ盤で押し出しも量の置き換えも動く（上の試験が空振りでない証拠）
    ok = make_client(mk())
    assert run(tmp_path / "ok", ok, "--board", "o/1", board=None, job="all", now=NOW_WED) == 0
    assert len(item_writes(ok)) > 1 and ok.boards["o/1"].description != DESC
    # おかわり: 今週が空の盤でも、読めん盤が居れば引かん
    empty = make_client(make_board([{"n": 1200, "week": "2026-11-09", "est": 1}], number=1, description=FUTURE_COUNT))
    assert run(tmp_path / "e", empty, "--boards-file", str(boards), board=None, job="okawari", now=NOW_WED) == 1
    assert not item_writes(empty)
    # 埋め: 新しい一番先の週ができても、読めん盤が居れば埋めん（週は足す）
    weeks = monday_range(n=5)
    fill = make_client(make_board([{"n": 1300 + k, "week": w, "est": 176} for k, w in enumerate(weeks[2:])]
                                  + [{"n": 1310, "est": 1}], number=1, weeks=weeks, description=FUTURE_COUNT))
    capsys.readouterr()
    assert run(tmp_path / "f", fill, "--boards-file", str(boards), board=None, job="extend") == 1
    assert "skipped job=fill-new-week" in capsys.readouterr().err
    assert [w for w in fill.writes if w[0] == "iterations"] and week(fill, "o/1", 1310) is None


# 20. 終わった週のどこからでも繰り越す。月曜の窓を Mac が寝とっても、次の回で拾い、取りこぼしは報告に出る
def test_20_carryover_from_any_ended_week_without_monday_window(tmp_path):
    desc = DESC.replace("2026-10-26", "2026-10-12")
    c = make_client(make_board([{"n": 600, "week": "2026-10-19", "prio": "P1"}, {"n": 601, "week": "2026-10-26"}],
                               description=desc))
    ticks(tmp_path, c, ["2026-11-02T07:00+09:00"], job="carryover")
    assert week(c, "o/1", 600) == CUR and week(c, "o/1", 601) == CUR
    # 次の月曜まで放っておかれた物は、報告の stranded に出る
    stuck = make_client(make_board([{"n": 602, "week": "2026-10-19"}], description=desc))
    ticks(tmp_path / "s", stuck, ["2026-11-02T07:00+09:00"], job="report")
    assert "stranded=1" in stuck.status_updates["o/1"][0]


# 21. 月曜の回: 終わった週の物は今週へ、日曜までに入らん分は来週へ。報告に待ちと繰り越し回数の行は無い
def test_21_monday_tick_carries_then_trims_and_reports_without_wait(tmp_path):
    many = [{"n": 710 + k, "week": "2026-10-26", "prio": "P1", "est": 10} for k in range(20)]
    c = make_client(make_board(many, description=FUTURE_COUNT))
    ticks(tmp_path, c, [NOW_MON])
    weeks = [week(c, "o/1", 710 + k) for k in range(20)]
    assert weeks.count(CUR) == 17 and weeks.count("2026-11-09") == 3  # 176 点に 10 点が 17 件
    body = c.status_updates["o/1"][0]
    assert "stalled-wait" not in body and "resplit" not in body and "empty-story-point=0" in body


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


# 23. 同じ値の書き込みはせん。何度回しても一度動かした物を書き直さん
def test_23_no_repeated_identical_writes(tmp_path):
    c = make_client(make_board([
        {"n": 3, "week": CUR},
        {"n": 20, "week": "2026-11-09", "children": ["o/r#21"], "est": 1},
        {"n": 21, "week": "2026-11-09", "parent": "o/r#20", "est": 1},
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


# 25. 複数の Project を 1 回で回す。1 つが読めんでも、Iteration の項目が無くても、残りは回り、失敗はログに出る
def test_25_many_boards_and_one_failure_does_not_stop_others(tmp_path, capsys):
    rows = [{"n": 1000, "week": "2026-10-26"}]
    b1 = make_board(rows, number=1, description=FUTURE_COUNT)
    b3 = make_board(rows, number=3, description="no marker")       # 一覧に書いた物はマーカー無しでも相手
    no_iter = make_board(rows, number=4)
    del no_iter.field_ids["Iteration"]
    c = make_client(b1, b3, no_iter)
    boards = tmp_path / "boards.txt"
    boards.write_text("# 相手\no/1\no/2   # 無い盤\no/3\no/4\no/1\n")
    capsys.readouterr()
    assert run(tmp_path, c, "--boards-file", str(boards), board=None, job="carryover") == 1
    err = capsys.readouterr().err
    assert "board failed board=o/2 step=read" in err and "board failed board=o/4 step=read: no Iteration field" in err
    assert "tick boards=2 failed=2" in err
    assert {w[1] for w in moved(c)} == {"o/1", "o/3"} and len(moved(c)) == 2
    assert not (tmp_path / "state" / "last-run").exists()
    # --board は何回でも書ける
    c2 = make_client(make_board(rows, number=1), make_board(rows, number=3, description="no marker"))
    assert run(tmp_path / "m", c2, "--board", "o/3", job="carryover") == 0
    assert {w[1] for w in moved(c2)} == {"o/1", "o/3"}

    # 1 つの Project の仕事の途中で gh が落ちても、次の Project の仕事は回る
    class FlakySync(FakeClient):
        def list_mapped_repos(self, board):
            if board.number == 1:
                raise GhError("boom")
            return super().list_mapped_repos(board)

    repos = {"o/r3": {"yml": "owner: o\nproject_number: 3\n", "issues": [(5, "o/r3|5", "Issue")]}}
    c3 = FlakySync({"o/1": make_board([], number=1), "o/3": make_board([], number=3)}, repos)
    capsys.readouterr()
    assert run(tmp_path / "f", c3, "--board", "o/3", job="sync") == 1
    assert "board failed board=o/1 job=sync: boom" in capsys.readouterr().err
    # 1 つの Project の仕事が落ちても、読めた回なら last-run は進める（日・週に 1 回の仕事を 15 分ごとに回さん）
    c4 = FlakySync({"o/1": make_board([], number=1)}, repos)
    assert run(tmp_path / "f4", c4, job="all") == 1
    assert (tmp_path / "f4" / "state" / "last-run").exists()
    assert [w[2] for w in c3.writes if w[0] == "add"] == ["o/r3#5"]
    # 一覧のファイルが無い・書き間違いは引数の誤り
    assert run(tmp_path / "x", c, "--boards-file", str(tmp_path / "none.txt"), board=None, job="carryover") == 2
    (tmp_path / "bad.txt").write_text("o-1\n")
    assert run(tmp_path / "x", c, "--boards-file", str(tmp_path / "bad.txt"), board=None, job="carryover") == 2


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
    c = make_client(make_board([{"n": 20, "week": "2026-11-09"}]))
    board = c.read_board("o", 1)
    pbi = board.items[0]
    rn = Runner(c, Ledger(tmp_path / "l", None), False, 50)
    assert rn.execute_moves(board, "okawari", [Move(pbi, "it-2026-11-09"), Move(pbi, "it-2026-11-09")]) == 0
    assert c.writes == [] and not (tmp_path / "l" / "K.jsonl").exists()


# 28. 返事に null が混ざっても（消えた repo の課題・盤から外れた課題）回ごと落ちん
def test_28_null_nodes_do_not_crash_the_tick(tmp_path, capsys):
    from scrum_keeper.github_client import ITEM_GONE, parse_board
    from helpers import raw_project
    node = raw_project([{"n": 1, "week": CUR}, {"n": 2, "week": CUR}])
    node["items"]["nodes"][0]["content"]["repository"] = None   # 消えた・見えん repo の課題
    node["items"]["nodes"][1]["content"]["subIssues"]["nodes"] = [None, {"number": 3, "repository": None}]
    node["items"]["nodes"].insert(1, None)                       # 読んどる間に消えた課題
    b = parse_board(node, "o", 1, node["items"]["nodes"])
    assert [i.number for i in b.items if i.kind == "Issue"] == [2]
    assert b.items[0].children == []

    gh = GhClient()
    gh._gql = lambda q, **kw: {"data": {"node": None}}
    assert gh.read_item_fields(b, b.items[0]) == ITEM_GONE      # 書く直前に盤から外れとった
    for call in (lambda: gh.list_status_updates(b), lambda: gh.list_mapped_repos(b), lambda: gh.list_open_issues("o/r")):
        try:
            call()
        except GhError:
            pass
        else:
            raise AssertionError("null の返事は GhError にする")

    # 書く直前の読み直しで消えとったら race として飛ばす（書かん）
    c = make_client(make_board([{"n": 20, "week": "2026-10-26"}]))
    c.read_item_fields = lambda board, it: ITEM_GONE
    assert run(tmp_path, c, job="carryover") == 0
    assert not [w for w in c.writes if w[0] != "add"]

    # 想定外の形（KeyError・TypeError）はその Project だけの失敗。残りの Project は回る
    class Odd(FakeClient):
        def read_board(self, owner, number):
            if number == 2:
                raise TypeError("'NoneType' object is not subscriptable")
            return super().read_board(owner, number)

    rows = [{"n": 1000, "week": "2026-10-26"}]
    c2 = Odd({"o/1": make_board(rows, number=1), "o/2": make_board(rows, number=2)})
    capsys.readouterr()
    assert run(tmp_path / "odd", c2, "--board", "o/2", job="carryover") == 1
    err = capsys.readouterr().err
    assert "board failed board=o/2 step=read: TypeError" in err
    assert {w[1] for w in moved(c2)} == {"o/1"}

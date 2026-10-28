"""linear_sweep の試験。Linear には触らず、本物の応答と同じ形の dict を FakeLinearClient に読ませる。

op・Linear・台帳以外の外側は全部偽物。題名（SECRET-TITLE-XYZ）はどこにも出てはいかん。
"""
import re

from helpers import (NOW_WED, TITLE, ledger_lines, linear_fixture, make_linear_client, raw_linear_issue,
                     run_linear)
from scrum_keeper import linear_client


def csv_actions(out):
    lines = [x for x in out.splitlines() if x and not x.startswith("board,")]
    return [x.split(",")[2] for x in lines]  # action は 3 列目


# L1. 今の Cycle の終わってへん件で、Estimate 無しと Priority 無しを拾う。終わった件と Cycle の外は拾わん
def test_l1_flags_missing_estimate_and_priority_in_current_cycle(tmp_path, capsys):
    data = linear_fixture(
        cycle_issues=[
            raw_linear_issue(43, priority=1, estimate=3, cycle="cyc-3"),                    # 完備
            raw_linear_issue(44, priority=2, cycle="cyc-3"),                                # Estimate 無し
            raw_linear_issue(45, priority=0, estimate=2, cycle="cyc-3"),                    # Priority 無し
            raw_linear_issue(46, priority=0, state_type="completed", cycle="cyc-3"),        # 終わり
            raw_linear_issue(47, state_type="canceled", cycle="cyc-3"),                     # 取りやめ
        ],
        team_issues=[raw_linear_issue(99, priority=0)])                                     # Cycle の外は拾わん
    c = make_linear_client(data)
    assert run_linear(tmp_path, c, "--dry-run") == 0
    out = capsys.readouterr().out
    rows = [x for x in out.splitlines() if x and not x.startswith("board,")]
    assert [x.split(",")[4] for x in rows if ",no-estimate," in x] == ["RIT-44"]
    assert [x.split(",")[4] for x in rows if ",no-priority," in x] == ["RIT-45"]
    assert "unfinished=3" in out and "workable=3" in out


# L2. 今の Cycle にやれる物が無い時だけ、優先度の順で進める候補を出す（おかわりの Linear 版。提案だけ）
def test_l2_okawari_suggests_backlog_top_when_cycle_has_no_workable(tmp_path, capsys):
    cyc = "cyc-3"
    data = linear_fixture(
        cycle_issues=[raw_linear_issue(43, priority=1, estimate=3, labels=["待ち:局長"], cycle=cyc)],
        team_issues=[
            raw_linear_issue(43, priority=1, estimate=3, labels=["待ち:局長"], cycle=cyc),
            raw_linear_issue(48, priority=2, state_type="backlog", due="2026-11-10"),       # High・締め早い
            raw_linear_issue(49, priority=1, state_type="unstarted"),                       # Urgent → 先頭
            raw_linear_issue(50, priority=0, state_type="backlog"),                         # 無し → 最後
            raw_linear_issue(51, priority=1, state_type="backlog", labels=["待ち:局長"]),   # 待ちは引かん
            raw_linear_issue(52, priority=1, state_type="triage"),                          # 検討中は引かん
            raw_linear_issue(53, priority=1, state_type="canceled"),
        ])
    c = make_linear_client(data)
    assert run_linear(tmp_path, c, "--dry-run") == 0
    rows = [x.split(",") for x in capsys.readouterr().out.splitlines()
            if x and not x.startswith("board,")]
    sug = [r[4] for r in rows if r[2] == "okawari-suggest"]
    assert sug == ["RIT-49", "RIT-48", "RIT-50"]
    assert all(r[9] == "cycle-empty" for r in rows if r[2] == "okawari-suggest")
    # やれる物が居る回は候補を出さん
    c2 = make_linear_client(linear_fixture(
        cycle_issues=[raw_linear_issue(43, priority=1, estimate=3, cycle="cyc-3")],
        team_issues=[raw_linear_issue(48, priority=1, state_type="backlog")]))
    capsys.readouterr()
    assert run_linear(tmp_path / "b", c2, "--dry-run") == 0
    assert "okawari-suggest" not in capsys.readouterr().out


# L3. Cycle 自体が無い時（S16 の前）も、理由 no-active-cycle で候補を出す
def test_l3_no_active_cycle_still_suggests(tmp_path, capsys):
    c = make_linear_client(linear_fixture(no_cycle=True, team_issues=[
        raw_linear_issue(60, priority=2, state_type="backlog"),
        raw_linear_issue(61, priority=1, state_type="backlog")]))
    assert run_linear(tmp_path, c, "--dry-run") == 0
    out = capsys.readouterr().out
    rows = [x.split(",") for x in out.splitlines() if x and not x.startswith("board,")]
    assert [r[4] for r in rows if r[2] == "okawari-suggest"] == ["RIT-61", "RIT-60"]
    assert "cycle=none" in out and "unfinished=0" in out


# L4. 台帳に 1 行。identifier の一覧と件数だけ。題名は出ん。読むだけなので LinearClient に書き込み面は無い
def test_l4_ledger_row_has_lists_not_titles(tmp_path, capsys):
    data = linear_fixture(
        cycle_issues=[raw_linear_issue(43, priority=1, cycle="cyc-3"),
                      raw_linear_issue(44, priority=0, estimate=2, cycle="cyc-3")])
    c = make_linear_client(data)
    assert run_linear(tmp_path, c) == 0
    lines = ledger_lines(tmp_path)
    assert len(lines) == 1 and lines[0]["phase"] == "done" and lines[0]["result"] == "ok"
    assert lines[0]["step"] == "keeper-linear" and lines[0]["target"] == "linear/RIT"
    assert lines[0]["no_estimate"] == ["RIT-43"] and lines[0]["no_priority"] == ["RIT-44"]
    assert lines[0]["unfinished"] == 2 and lines[0]["suggest"] == []
    assert not hasattr(c, "set_item_fields") and not hasattr(c, "issue_update")
    seen = capsys.readouterr()
    blob = seen.out + seen.err + (tmp_path / "ledger" / "K.jsonl").read_text()
    assert TITLE not in blob


# L5. op が無い・鍵が読めん時は rc 6（Linear だけ不可）。罠にも計画にも進まん
def test_l5_op_missing_degrades_to_rc6(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(linear_client.shutil, "which", lambda name: None)
    from scrum_keeper.linear_sweep import main
    assert main(["--now", NOW_WED, "--ledger-dir", str(tmp_path / "ledger"),
                 "--state-dir", str(tmp_path / "state"),
                 "--halt-file", str(tmp_path / "ledger" / "HALT")]) == 6
    assert "unavailable" in capsys.readouterr().err
    assert ledger_lines(tmp_path) == []
    # op の返事が空・非 0 でも同じ
    import subprocess

    def fake_run(*a, **kw):
        class R:
            returncode = 1
            stdout = "should-never-appear\n"
            stderr = "secret-ish stderr"
        return R()
    monkeypatch.setattr(linear_client.shutil, "which", lambda name: "/usr/bin/op")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert main(["--now", NOW_WED, "--ledger-dir", str(tmp_path / "ledger"),
                 "--state-dir", str(tmp_path / "state2")]) == 6
    err = capsys.readouterr().err
    assert "should-never-appear" not in err and "secret-ish" not in err


# L6. schema の確かめ: 要るフィールドが欠けとれば rc 6（名前は未試験なので先に __type で見る）
def test_l6_schema_gate(tmp_path, capsys):
    types = {t: sorted(v) for t, v in linear_client.REQUIRED_FIELDS.items()}
    types["Team"] = [x for x in types["Team"] if x != "activeCycle"]
    c = make_linear_client(linear_fixture(types=types))
    assert run_linear(tmp_path, c) == 6
    assert "Team.activeCycle" in capsys.readouterr().err
    assert ledger_lines(tmp_path) == []


# L7. HALT は何もせず 0。team が無ければ 1
def test_l7_halt_and_missing_team(tmp_path, capsys):
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "HALT").write_text("stop\n")
    c = make_linear_client(linear_fixture())
    assert run_linear(tmp_path, c) == 0
    assert "HALT" in capsys.readouterr().out and c.calls == 0
    (tmp_path / "ledger" / "HALT").unlink()
    assert run_linear(tmp_path, c, team="NOPE") == 1


# L8. --fixture の実経路: op も Linear も触らず計画が出る。2 回走らせても台帳が増えるだけで誤差は出ん
def test_l8_fixture_end_to_end(tmp_path, capsys):
    import shutil
    from pathlib import Path
    fx = tmp_path / "fx"
    fx.mkdir()
    src = Path(__file__).resolve().parent / "fixture" / "linear-rit.json"
    shutil.copy(src, fx / "linear-rit.json")
    args = ["--now", NOW_WED, "--fixture", str(fx), "--dry-run",
            "--state-dir", str(tmp_path / "state")]
    from scrum_keeper.linear_sweep import main
    assert main(args) == 0
    out = capsys.readouterr().out
    assert re.search(r"linear/RIT,linear,okawari-suggest,linear,RIT-49", out)
    assert "RIT-44" in out and "RIT-45" in out and "unfinished=3" in out
    # --fixture で台帳を書く経路も通る（読むだけの Linear、記録は付く）
    args2 = ["--now", NOW_WED, "--fixture", str(fx), "--ledger-dir", str(tmp_path / "ledger"),
             "--state-dir", str(tmp_path / "state"), "--halt-file", str(tmp_path / "ledger" / "HALT")]
    assert main(args2) == 0 and main(args2) == 0
    assert len(ledger_lines(tmp_path)) == 2  # 回ごとに 1 行。誤差行は無い


# L9. 並びは 優先度 → 締め → 番号（ordering.py の order_key と同じ決まり）
def test_l9_suggestion_order(tmp_path, capsys):
    issues = [
        raw_linear_issue(70, priority=3, due="2026-11-11", state_type="backlog"),
        raw_linear_issue(71, priority=2, due="2026-11-12", state_type="backlog"),
        raw_linear_issue(72, priority=2, due="2026-11-10", state_type="backlog"),
        raw_linear_issue(73, priority=4, state_type="backlog"),                       # Low は無しの前
        raw_linear_issue(74, priority=0, state_type="backlog"),                       # 無しは最後
    ]
    c = make_linear_client(linear_fixture(no_cycle=True, team_issues=issues))
    assert run_linear(tmp_path, c, "--dry-run") == 0
    sug = [x.split(",")[4] for x in capsys.readouterr().out.splitlines() if "okawari-suggest" in x]
    assert sug == ["RIT-72", "RIT-71", "RIT-70", "RIT-73", "RIT-74"]


# L10. 台帳の共有の鍵が取れん時は 4 で止まる（Ledger の仕組みは共通）
def test_l10_lock_busy_returns_4(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "write.lock").mkdir()  # 誰かが書き込み中（30 秒待っても取れん体）
    monkeypatch.setattr("scrum_keeper.ledger.LOCK_WAIT_SECONDS", 0)
    c = make_linear_client(linear_fixture())
    assert run_linear(tmp_path, c) == 4
    assert ledger_lines(tmp_path) == []

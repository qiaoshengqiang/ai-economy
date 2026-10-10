"""
观察实验的查看器（`view_division.py`）—— 轻量守卫，只读，不调 LLM
====================================================================

【为什么需要它】

    用户 2026-10-10：「把实验结果查询的命令再发我一下我想看看实验结果。」

    实测：这个实验跑完之后**除了 `division.db` 和一份 JSON 什么都没有** ——
    控制台分析没落盘，也没有任何「看结果」的命令
    （`user_demand/` 有 `view_result.py`，这里没有）。

    这个脚本补上那一步。它坏掉的话，用户就又只能自己去翻 JSON 了 ——
    所以值得有几条测试。

【钉住什么】

    ① 老格式（多动作之前、follow-up 之前）的 run **不能崩**
    ② 新格式（有 `segments` / `reasonings` / `follow_up_used`）能显示两段判断
    ③ `--list` 认得出真实活动时间
    ④ 只读：跑完之后 run 目录里的文件**没有被改过**
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import experiments.agent_division.view_division as V                 # noqa: E402


def _mk_run(tmp_path: Path, log: dict, *, name="1010-1200-abcdef12") -> Path:
    """造一个最小可用的 run 目录（真 SQLite + 真 JSON）。"""
    run = tmp_path / "runs" / name
    (run / "results").mkdir(parents=True)
    (run / "results" / "division_result.json").write_text(
        json.dumps({"log": log}, ensure_ascii=False), encoding="utf-8")

    con = sqlite3.connect(run / "division.db")
    try:
        con.execute("CREATE TABLE llm_calls (created_at TEXT, total_tokens INT,"
                    " cost_ai REAL)")
        con.execute("INSERT INTO llm_calls VALUES ('2026-10-10T04:00:00Z', "
                    "100, 0.01)")
        con.execute("CREATE TABLE task_context_revisions "
                    "(revision INT, payload TEXT, created_by TEXT,"
                    " change_reason TEXT)")
        con.execute("INSERT INTO task_context_revisions VALUES "
                    "(1, '{\"assignments\": [], \"objective\": \"\"}', "
                    "'agent_a', '初始化共享上下文')")
        con.execute("INSERT INTO task_context_revisions VALUES "
                    "(2, '{\"assignments\": [{\"responsibility\": "
                    "\"acceptance\", \"agent_id\": \"agent_d\", "
                    "\"assigned_by\": \"agent_a\", \"status\": \"ACTIVE\"}], "
                    "\"objective\": \"\"}', 'agent_a', 'ASSIGN 自批')")
        con.commit()
    finally:
        con.close()
    return run


def _base_log(**over) -> dict:
    log = {
        "demand": "写一个长篇网络小说的第一章",
        "first_agent": "agent_a",
        "stop_reason": "NOBODY_WOKEN",
        "rounds": 2, "wall_seconds": 12.0, "task_status": "CREATED",
        "agents": ["agent_a", "agent_b"],
        "comm_events": [],
        "final_workspace": [],
        "report": {"observed": True, "agents_acted": ["agent_a"],
                   "agents_never_woken": ["agent_b"],
                   "messages": [], "per_agent_actions": {
                       "agent_a": {"workspace_write": 1}}},
        "turns": [{"round": 1, "agent_id": "agent_a", "ok": True,
                   "tokens": 100, "reasoning": "先写",
                   "actions": [{"type": "workspace_write", "ok": True,
                                "path": "a.md"}]}],
    }
    log.update(over)
    return log


def test_it_renders_an_old_format_run(tmp_path, capsys):
    """🔴 多动作 / follow-up **之前**的 run 不能崩 —— 用户会回头翻它们。"""
    run = _mk_run(tmp_path, _base_log())

    assert V.main(["--path", str(run)]) == 0

    out = capsys.readouterr().out
    assert "获得观察" in out
    assert "agent_a" in out


def test_it_shows_two_segments_when_the_follow_up_happened(tmp_path, capsys):
    """🔴 读到了内容的那一轮有**两段判断** —— 第二段才是读完之后的决定。"""
    log = _base_log(turns=[{
        "round": 1, "agent_id": "agent_a", "ok": True, "tokens": 300,
        "reasoning": "先读一下再决定",
        "follow_up_used": True,
        "reasonings": ["先读一下再决定", "看完了，把活交给 agent_b"],
        "tokens_by_segment": [100, 200],
        "segments": [{"index": 0, "reasoning": "先读一下再决定"},
                     {"index": 1, "reasoning": "看完了，把活交给 agent_b"}],
        "actions": [{"type": "workspace_read", "ok": True, "path": "a.md"},
                    {"type": "message_send", "ok": True, "to": "agent_b"}],
    }])
    run = _mk_run(tmp_path, log)

    assert V.main(["--path", str(run), "--turns"]) == 0

    out = capsys.readouterr().out
    assert "当场回喂" in out
    assert "第0段判断（读之前）" in out
    assert "第1段判断（读之后）" in out
    assert "把活交给 agent_b" in out, "第二段的判断没有显示出来"


def test_it_shows_the_acceptance_assignment(tmp_path, capsys):
    """「有没有真的把活分出去」最直接的证据之一 —— 必须显示。"""
    run = _mk_run(tmp_path, _base_log())

    V.main(["--path", str(run)])
    out = capsys.readouterr().out

    assert "指派 acceptance → agent_d" in out
    assert "rev2" in out


def test_it_lists_runs_by_real_activity(tmp_path, capsys, monkeypatch):
    old = _mk_run(tmp_path, _base_log(), name="1010-0100-old00000")
    new = _mk_run(tmp_path, _base_log(), name="1010-1200-new00000")
    # 把新那个的 llm_calls 时间改晚
    con = sqlite3.connect(new / "division.db")
    con.execute("UPDATE llm_calls SET created_at = '2026-10-10T09:00:00Z'")
    con.commit()
    con.close()

    monkeypatch.setattr(V, "RUNS_DIR", tmp_path / "runs")
    assert V.main(["--list"]) == 0

    out = capsys.readouterr().out
    assert new.name in out and old.name in out
    assert out.index(new.name) < out.index(old.name), (
        "最近活动的 run 没有排在最前面")


def test_it_does_not_touch_the_run_directory(tmp_path):
    """🔒 只读：跑完之后 run 目录里的文件一个字节都不许变。"""
    run = _mk_run(tmp_path, _base_log())
    before = {p.name: p.stat().st_mtime_ns for p in run.rglob("*")
              if p.is_file()}

    V.main(["--path", str(run), "--turns"])

    after = {p.name: p.stat().st_mtime_ns for p in run.rglob("*")
             if p.is_file()}
    assert before == after, "查看器改动了 run 目录里的文件"


def test_a_run_without_json_is_reported_not_crashed(tmp_path, capsys):
    run = tmp_path / "runs" / "broken"
    run.mkdir(parents=True)
    (run / "division.db").touch()

    with pytest.raises(SystemExit):
        V.main(["--path", str(run)])

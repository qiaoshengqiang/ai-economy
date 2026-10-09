"""
用户入口 + 目标单一来源 —— 测试
====================================

🔴 修的三个缺陷（都由**用户的真实运行**暴露，不是设计出来的）：

    ① **Task 目标 / Stage 目标 / `DEMAND` 是同一个事实的三份拷贝**，
       而且三份文字**各不相同**：
           DEMAND  : 【Human Demand】请创作一部约 10000 字的原创中文科幻悬疑小说…
           Task    : 创作一部约 10000 字的原创中文科幻悬疑小说。主题：AI Agent…
           Stage   : Produce a complete ~10,000-word original Chinese sci-fi…
       违反本项目自己的「一个事实只有一个权威来源」。

       后果（实测）：`run_demand.py` 覆盖了 Task，却覆盖不到 Stage ——
       Agent 同时收到
           Task : 写修仙第一章
           Stage: 写 10000 字科幻悬疑小说
       两个**互相矛盾**的目标。你那次运行里 `novel_reader`
       **自己在消息里指出了这个冲突**并裁决「以当前 Task 为目标」。

    ② **`creator="human_demand"`** —— 违反 AGENTS.md §6 的
       `Human ≠ Task Creator`（ADR-0024：Creator 始终是 Agent）。
       它平时被入口的包装盖住，但源码里就在那。

    ③ **入口里"抢建 Task"的包装** —— 为了顶掉写死的 description 而存在。
       ① 修好之后它就是多余的，而且它让 Task 有**两条**创建路径。

🔒 这些测试**不调用任何 LLM**。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                  # noqa: E402

load_dotenv(ROOT / ".env")

PROD = ROOT / "experiments" / "heterogeneous_production" / "production.py"
ENTRY = ROOT / "experiments" / "user_demand" / "run_demand.py"

CUSTOM_DEMAND = "写一个传统修仙小说的第一章，约 3000 字。主角叫林阳。"


# ======================================================================
# ① 单一来源：Task 描述 与 Stage goal 都从 DEMAND 派生
# ======================================================================
@pytest.fixture
def env(tmp_path, monkeypatch):
    """用**自定义** DEMAND 建一次真实环境（零 LLM 调用）。"""
    from experiments.heterogeneous_production import production as P
    from main import build_runtime

    db = tmp_path / "demand_env.db"
    monkeypatch.setattr(P, "DB", db)
    monkeypatch.setattr(P, "TASK_ID", "task_single_source")
    monkeypatch.setattr(P, "WS_ID", "WS-SINGLE-SOURCE")
    monkeypatch.setattr(P, "RESULTS", tmp_path / "results")
    P.RESULTS.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(P, "DEMAND", CUSTOM_DEMAND)

    rt = build_runtime(db_path=str(db), agent_ids=list(P.AGENTS))
    try:
        P._ensure_environment(rt)
    finally:
        rt.close()
    return P, db


def _rows(db, sql):
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_task_description_is_the_demand(env):
    _P, db = env
    (desc,) = _rows(db, "SELECT description FROM tasks")[0]
    assert desc.strip() == CUSTOM_DEMAND, (
        f"Task 描述不是 DEMAND —— 说明又出现了一份独立拷贝：\n{desc!r}")


def test_stage_goal_is_the_demand(env):
    _P, db = env
    (goal,) = _rows(db, "SELECT goal FROM production_stages")[0]
    assert goal.strip() == CUSTOM_DEMAND, (
        f"Stage goal 不是 DEMAND —— 又出现了一份独立拷贝：\n{goal!r}")


def test_task_and_stage_agree(env):
    """🔴 这一条就是那次真实 bug 的回归测试：两者**必须**一致。"""
    _P, db = env
    (desc,) = _rows(db, "SELECT description FROM tasks")[0]
    (goal,) = _rows(db, "SELECT goal FROM production_stages")[0]
    assert desc.strip() == goal.strip(), (
        "Task 与 Stage 的目标不一致 —— Agent 会收到两个矛盾目标。\n"
        f"  Task : {desc[:80]}\n  Stage: {goal[:80]}")


def test_no_genre_left_in_the_stage_goal(env):
    """Stage 不该再限定题材 —— 它的职责是「切出有界时段」，不是选类型。"""
    _P, db = env
    (goal,) = _rows(db, "SELECT goal FROM production_stages")[0]
    low = goal.lower()
    for word in ("sci-fi", "science fiction", "10,000-word", "10000 字"):
        assert word not in low, f"Stage goal 里还残留题材限定 {word!r}：{goal[:80]}"


# ======================================================================
# ② Creator 必须是 Agent（ADR-0024 / `Human ≠ Task Creator`）
# ======================================================================
def test_creator_is_an_agent(env):
    P, db = env
    (creator,) = _rows(db, "SELECT creator FROM tasks")[0]
    assert creator != "human_demand", (
        "🔴 creator 又变回 human_demand 了 —— 违反 ADR-0024"
        "（Creator 始终是 Agent；Human 提供的是 Demand，那是 Origin）")
    assert creator in P.AGENTS, (
        f"creator={creator!r} 不在参与者里 —— Creator 必须是 Agent")


def test_source_has_no_human_creator_literal():
    """结构守卫：源码里不许再出现 `creator="human_..."`。"""
    src = PROD.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg != "creator":
                    continue
                if isinstance(kw.value, ast.Constant) and \
                        isinstance(kw.value.value, str) and \
                        not kw.value.value.startswith(("agent", "novel")):
                    bad.append(f"line {node.lineno}: creator={kw.value.value!r}")
    assert not bad, f"又出现非 Agent 的硬编码 creator：{bad}"


# ======================================================================
# ③ 结构守卫：目标文本不许再有第二份
# ======================================================================
def test_goal_text_appears_exactly_once():
    """
    那句「约 10000 字」的**可执行代码**里只许出现一次。

    ⚠️ 只扫字符串字面量（AST），不看注释 —— 否则解释文字会把
       自己绊倒（这个坑在本项目里已经踩过两次）。
    """
    tree = ast.parse(PROD.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "10000 字" in node.value or "10,000-word" in node.value:
                hits.append((node.lineno, node.value[:50]))
    assert len(hits) == 1, (
        f"目标文本在代码里出现了 {len(hits)} 次（应当只有 DEMAND 那一处）：\n"
        + "\n".join(f"  line {ln}: {txt!r}" for ln, txt in hits))


def test_entry_point_does_not_precreate_the_task():
    """
    🔒 入口不该再"抢建" Task —— 那是为了顶掉写死的 description 而存在的补丁。
       现在 `production.py` 是单一来源，抢建只会让 Task 有两条创建路径。
    """
    # 🔒 必须查 **AST**，不能查原文 —— 入口的 docstring 里**正好**
    #    用 `Task.create(creator=..., description=...)` 描述机制流程，
    #    按原文匹配会假红（这个坑本项目记录过：查原文而不是纯代码）。
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    calls = [
        f"line {n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "create"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "Task"
    ]
    assert not calls, (
        f"入口里又出现 Task.create 调用（{calls}）—— "
        f"说明又在绕过 _ensure_environment")


# ======================================================================
# ④ A：stdout 落盘
# ======================================================================
def test_tee_writes_to_both(tmp_path):
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location("rd_tee", ENTRY)
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    log = tmp_path / "run.log"
    console = io.StringIO()
    tee = rd._Tee(log, console)
    tee.write("第一行\n")
    tee.write("第二行\n")
    tee.flush()
    tee.close()

    assert console.getvalue() == "第一行\n第二行\n"
    assert log.read_text(encoding="utf-8") == console.getvalue(), (
        "落盘内容与控制台不一致 —— 日志就不可信了")


def test_tee_survives_a_broken_console(tmp_path):
    """控制台断了（管道被关）也不能丢日志。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("rd_tee2", ENTRY)
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)

    class Broken:
        def write(self, _s):
            raise BrokenPipeError("pipe closed")

        def flush(self):
            raise BrokenPipeError("pipe closed")

    log = tmp_path / "run.log"
    tee = rd._Tee(log, Broken())
    tee.write("必须活下来\n")
    tee.close()
    assert "必须活下来" in log.read_text(encoding="utf-8")


# ======================================================================
# ⑤ B + C：报告要能看出"没做成的事"
# ======================================================================
def _entry_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("rd_report", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_flags_an_agent_that_never_ran(tmp_path, capsys):
    rd = _entry_module()
    (tmp_path / "r.json").write_text(json.dumps({
        "exit_reason": "TOTAL_WALL_LIMIT_REACHED",
        "sessions_run": 1,
        "sessions": [{"index": 1, "turns": [
            {"round": 1, "agent_id": "novel_world_builder",
             "write": {"ok": True, "mode": "create"}},
        ]}],
    }), encoding="utf-8")

    rd._report_turns(tmp_path, ["novel_world_builder", "novel_reader"])
    out = capsys.readouterr().out
    assert "从没轮到" in out
    assert "novel_reader" in out, "没轮到的 Agent 必须被点名"


def test_report_flags_a_silent_write_failure(tmp_path, capsys):
    """🔴 `locked_by_other` 这类失败**不报错**，turn 还是 ok=True。"""
    rd = _entry_module()
    (tmp_path / "r.json").write_text(json.dumps({
        "exit_reason": "TOTAL_WALL_LIMIT_REACHED", "sessions_run": 1,
        "sessions": [{"index": 1, "turns": [
            {"round": 2, "agent_id": "novel_world_builder", "ok": True,
             "write": {"ok": False, "code": "locked_by_other",
                       "path": "manuscript/chapter_001.md", "mode": "create"}},
            {"round": 2, "agent_id": "novel_author", "ok": True,
             "write": None},
        ]}],
    }), encoding="utf-8")

    rd._report_turns(tmp_path, ["novel_world_builder", "novel_author"])
    out = capsys.readouterr().out
    assert "locked_by_other" in out
    assert "NO_WRITE" in out
    assert "2/2" in out, "两个失败都要被数出来"


def test_report_reads_message_bodies(tmp_path, capsys, monkeypatch):
    """Agent 之间的消息正文必须有出口（以前终端上只有一个 ✉️ 箭头）。"""
    import sqlite3

    rd = _entry_module()
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE communication_messages (created_at TEXT, "
                "from_agent TEXT, to_agent TEXT, message_type TEXT, "
                "status TEXT, payload TEXT)")
    con.execute("INSERT INTO communication_messages VALUES (?,?,?,?,?,?)",
                ("2026-10-09T09:49:39+00:00", "novel_world_builder",
                 "novel_plot_architect", "TEXT", "PROCESSED",
                 json.dumps({"text": "世界观已写好，请细化大纲"},
                            ensure_ascii=False)))
    con.commit()
    con.close()

    rd._report_messages(db)
    out = capsys.readouterr().out
    assert "世界观已写好，请细化大纲" in out, "消息正文没有被打印出来"
    assert "novel_world_builder → novel_plot_architect" in out


def test_report_flags_truncated_calls(tmp_path, capsys):
    """`finish_reason=length` 必须显眼 —— 它会静默丢掉整轮。"""
    import sqlite3

    rd = _entry_module()
    db = tmp_path / "c.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE llm_calls (created_at TEXT, agent_id TEXT, "
                "finish_reason TEXT, total_tokens INT, actual_max_tokens INT)")
    con.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?)",
                ("2026-10-09T08:09:57+00:00", "novel_author", "length",
                 12192, 8192))
    con.commit()
    con.close()

    rd._report_calls(db)
    out = capsys.readouterr().out
    assert "length" in out
    assert "整轮不写入" in out

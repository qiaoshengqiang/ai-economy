"""
Session Checkpoint 的「交班原因」必须如实 —— 测试
=====================================================

🔴 修的 Bug：

    `experiments/heterogeneous_production/production.py::_run_session`
    收尾时写 Checkpoint 用的是**写死的** `reason="hard_limit"`：

        if not sess.is_exceeded():
            sess.stop_by_agent("max_rounds_reached")   # ← 因轮数用尽而结束
        else:
            sess.enforce()                             # ← 因墙钟而结束
        ...
        st.save_checkpoint(..., reason="hard_limit")   # ← 两种都标 hard_limit

    于是「**正常跑完配置轮数**」的那一班，也被标成撞墙钟被打断。
    两者含义完全不同，看日志的人会被误导。

🔒 这两条测试**不调用任何 LLM**：
   把 `AGENTS` 临时置空，`_run_session` 就会跑「空的一轮」，
   分别在「轮数用尽」和「撞墙钟」两种收尾下检查 Checkpoint 的 reason。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                  # noqa: E402

load_dotenv(ROOT / ".env")


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """
    真实 Runtime + 已建好的 Task / Workspace / Stage，但 `AGENTS` 置空。

    🔒 置空只影响 `_run_session` 的轮次循环（不调用任何 Agent），
       `_ensure_environment` 仍然用真实 Agent 集建环境。
    """
    from experiments.heterogeneous_production import production as P
    from main import build_runtime

    db = tmp_path / "reason.db"
    monkeypatch.setattr(P, "DB", db)
    monkeypatch.setattr(P, "TASK_ID", "task_reason_test")
    monkeypatch.setattr(P, "WS_ID", "WS-REASON-TEST")
    monkeypatch.setattr(P, "RESULTS", tmp_path / "results")
    P.RESULTS.mkdir(parents=True, exist_ok=True)
    # 快照：这些是我们要临时改的模块全局
    for name in ("AGENTS", "MAX_ROUNDS_PER_SESSION", "TOTAL_WALL_LIMIT"):
        monkeypatch.setattr(P, name, getattr(P, name))

    rt = build_runtime(db_path=str(db), agent_ids=list(P.AGENTS))
    try:
        P._ensure_environment(rt)
        yield P, rt
    finally:
        rt.close()


def _run(P, rt, limit: float):
    """跑一班（AGENTS 为空 → 零 LLM 调用），返回 s_log。"""
    P.AGENTS = []                      # 🔒 只影响轮次循环
    P.MAX_ROUNDS_PER_SESSION = 1
    log = {"comm_events": [], "sessions": []}
    stats = defaultdict(lambda: defaultdict(int))
    sess, _bridge, s_log = P._run_session(rt, None, 1, limit, log, stats)
    return sess, s_log


# ============================================================
# ① 轮数用尽 —— 不该标成 hard_limit
# ============================================================
def test_rounds_exhausted_is_not_labelled_hard_limit(prepared):
    P, rt = prepared

    sess, s_log = _run(P, rt, limit=300.0)      # 墙钟远远没到

    assert sess.state == "STOPPED_AGENT", sess.state
    assert sess.stop_reason == "max_rounds_reached", sess.stop_reason
    assert s_log["hard_limit"] is False

    ck = s_log["checkpoint"]
    assert ck["reason"] == "max_rounds_reached", (
        f"🔴 因轮数用尽而结束的一班被标成了 {ck['reason']!r} —— "
        f"它会让人以为这一班是撞墙钟被打断的")
    assert ck["reason"] != "hard_limit"


def test_rounds_exhausted_metadata_records_session_state(prepared):
    """metadata 留下 session 状态，便于事后核对（不靠 reason 单字段猜）。"""
    P, rt = prepared
    _sess, s_log = _run(P, rt, limit=300.0)
    meta = s_log["checkpoint"].get("metadata") or {}
    assert meta.get("session_state") == "STOPPED_AGENT"
    assert meta.get("session_stop_reason") == "max_rounds_reached"
    assert meta.get("hard_limit") is False


# ============================================================
# ② 撞墙钟 —— 仍然必须是 hard_limit（不能修坏这一支）
# ============================================================
def test_hard_limit_is_still_labelled_hard_limit(prepared):
    P, rt = prepared

    # 🔒 1 微秒 —— 到收尾时**必然**已超（循环里至少有一次 SQLite 查询，
    #    那是几百微秒量级）。用 0.001s 反而不稳：空的一轮跑得太快，
    #    收尾时可能还没到 1 毫秒，于是走成「轮数用尽」那一支。
    sess, s_log = _run(P, rt, limit=1e-6)

    assert sess.state == "STOPPED_HARD_LIMIT", sess.state
    assert sess.stop_reason == "hard_limit"
    assert s_log["hard_limit"] is True

    ck = s_log["checkpoint"]
    assert ck["reason"] == "hard_limit", ck["reason"]


# ============================================================
# ③ 结构守卫：不许再写死
# ============================================================
def test_checkpoint_reason_is_not_hardcoded():
    """
    🔒 AST 守卫：`_run_session` 里给 checkpoint 的 `reason` 不许是字符串字面量。

    只检查**传给 save_checkpoint 的 reason 实参**，不看注释/docstring ——
    否则说明文字里提到 `"hard_limit"` 会把自己绊倒。
    """
    import ast

    src = (ROOT / "experiments" / "heterogeneous_production"
           / "production.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_session")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "save_checkpoint"]
    assert calls, "找不到 save_checkpoint 调用"

    for call in calls:
        for kw in call.keywords:
            if kw.arg != "reason":
                continue
            assert not isinstance(kw.value, ast.Constant), (
                "checkpoint 的 reason 又被写死成字面量了 —— "
                "它必须来自 sess.stop_reason")


# ============================================================
# ④ RuntimeSession 自身的词汇（对照，说明为什么不用 REASON_* 硬映射）
# ============================================================
def test_session_stop_reason_vocabulary():
    from stage import RuntimeSession
    from stage.limit import SESSION_STOPPED_AGENT, SESSION_STOPPED_HARD_LIMIT

    s = RuntimeSession(limit_seconds=1).start()
    s.stop_by_agent("max_rounds_reached")
    assert s.state == SESSION_STOPPED_AGENT
    assert s.stop_reason == "max_rounds_reached"

    s2 = RuntimeSession(limit_seconds=-1).start()   # <=0 不会超时
    s2.stop_by_agent()
    assert s2.stop_reason == "agent_decision", "没给原因时的默认值"

    s3 = RuntimeSession(limit_seconds=0.001).start()
    import time
    time.sleep(0.01)
    assert s3.enforce() is True
    assert s3.state == SESSION_STOPPED_HARD_LIMIT
    assert s3.stop_reason == "hard_limit"

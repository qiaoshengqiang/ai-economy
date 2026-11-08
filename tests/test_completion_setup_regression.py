"""
修复回归：① 已有空 Shared Context 时仍要写 DoD；② 结项由 Agent 驱动。

这两条都对应真实运行发现的问题（见
`experiments/web_collab/runs/1107-1602-8c42c6d6/HUMAN_QA.md`）：

① `Runtime.create_task` 会先建一个**空** Shared Context，
   而旧 `_ensure_completion_setup` 只在 `current() is None` 时写 DoD
   ⇒ DoD 永远为空 ⇒ `_try_complete_task` 永远 `no-definition-of-done`。

② 旧 `_try_complete_task` 会由 runner 代发 VERIFY / COMPLETE
   ⇒ “验收”没有真的经过验收 Agent。
   新的 `agent_driven=True` 模式只观察 Task Core，不代发任何协议消息。

🔒 不调用真实 LLM API。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest                                                     # noqa: E402

from core.task import Task, TaskStatus                            # noqa: E402


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """真实 Runtime + 两个真实 Agent（不调用模型）。"""
    from main import build_runtime
    from experiments.heterogeneous_production import production as P

    monkeypatch.setattr(P, "TASK_ID", "task_fix_regression")
    monkeypatch.setattr(P, "WS_ID", "WS-FIX-REGRESSION")
    monkeypatch.setattr(P, "DEMAND", "完成一个网页扫雷小游戏。")
    monkeypatch.setattr(P, "DEFINITION_OF_DONE", (
        "交付物存在且非空",
        "至少一个非实现者写出独立验收结论",
    ))
    monkeypatch.setattr(P, "PRODUCER", "agent_b")
    monkeypatch.setattr(P, "ACCEPTOR", "agent_a")
    monkeypatch.setattr(P, "OWNER", "agent_a")
    monkeypatch.setattr(P, "ARTIFACT_NAMESPACES", {})

    rt = build_runtime(
        db_path=str(tmp_path / "fix_regression.db"),
        agent_ids=["agent_a", "agent_b"],
    )
    try:
        yield rt
    finally:
        rt.close()


def _create_empty_context_task(rt, P):
    rt.create_task(Task.create(
        creator="agent_b",
        description=P.DEMAND,
        task_id=P.TASK_ID,
        participants=["agent_a", "agent_b"],
    ))


def test_existing_empty_context_gets_objective_dod_and_acceptor(runtime):
    """① 空容器存在 ≠ 内容已经建立。补 objective / DoD / 验收责任人。"""
    from experiments.heterogeneous_production import production as P

    rt = runtime
    _create_empty_context_task(rt, P)

    before = rt.alignment.require_current(P.TASK_ID).context
    assert before.objective == ""
    assert before.acceptance_criteria == ()

    P._ensure_completion_setup(rt)

    after = rt.alignment.require_current(P.TASK_ID).context
    assert after.objective == P.DEMAND
    assert after.acceptance_criteria == P.DEFINITION_OF_DONE
    assert "agent_a" in after.acceptors()
    assert rt.tasks.get(P.TASK_ID).status == TaskStatus.IN_PROGRESS


def test_completion_setup_is_idempotent(runtime):
    """① 第二次调用不重复写 DoD，也不推进 revision。"""
    from experiments.heterogeneous_production import production as P

    rt = runtime
    _create_empty_context_task(rt, P)

    P._ensure_completion_setup(rt)
    first = rt.alignment.require_current(P.TASK_ID)

    P._ensure_completion_setup(rt)
    second = rt.alignment.require_current(P.TASK_ID)

    assert second.revision == first.revision
    assert second.context.acceptance_criteria == P.DEFINITION_OF_DONE
    assert second.context.acceptors() == first.context.acceptors()


def test_agent_driven_completion_never_auto_sends(runtime):
    """② agent_driven=True 时 runner 只观察，不代发 SUBMIT / VERIFY / COMPLETE。"""
    from experiments.heterogeneous_production import production as P

    rt = runtime
    _create_empty_context_task(rt, P)
    P._ensure_completion_setup(rt)
    assert rt.tasks.get(P.TASK_ID).status == TaskStatus.IN_PROGRESS

    outcome = P._try_complete_task(
        rt, rt.workspace_gateway, {},
        {"agents_claim_done": True},
        agent_driven=True,
    )

    assert outcome == "waiting-for-producer-submit"
    assert rt.tasks.get(P.TASK_ID).status == TaskStatus.IN_PROGRESS

    # 没有 runner 代发的执行事件 —— START 是 _ensure_completion_setup 发的；
    # SUBMIT / VERIFY / COMPLETE 一条都不该出现。
    import sqlite3
    connection = sqlite3.connect(str(rt.economy.db.path))
    try:
        events = [row[0] for row in connection.execute(
            "SELECT event_type FROM task_executions WHERE task_id = ? "
            "ORDER BY created_at", (P.TASK_ID,))]
    finally:
        connection.close()
    assert events == ["START"]


def test_prompt_documents_multi_action_and_verify_payload():
    """② Agent 需要看见 actions 列表与 VERIFY 的 payload 形状。"""
    from experiments.heterogeneous_production import production as P

    assert '"actions"' in P.PROMPT
    assert '"message_type": "VERIFY"' in P.PROMPT
    assert "task_id = {task_id}" in P.PROMPT
    assert "DSML" in P.PROMPT

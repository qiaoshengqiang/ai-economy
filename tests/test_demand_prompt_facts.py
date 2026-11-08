"""
Demand prompt 的事实投递守卫
==============================

真实运行暴露：

    · Agent 不知道 `run_tool` 的写**不会回写** Workspace；
    · 不知道 Playwright 要先写 spec；
    · 验收人无法验证时只读循环，不发 `VERIFY(passed=false)`；
    · 预算被裁剪 / 被截断后，下一轮 prompt 里没有任何反馈。

本文件钉住这些事实**真的进入 prompt**，而不只是躺在代码/文档里。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demand.prompt import build_intake_prompt, build_task_prompt  # noqa: E402


class _Directory:
    def all(self):
        return []


class _Gateway:
    def list(self, workspace_id, path, agent_id):
        return SimpleNamespace(success=True, result={
            "effective_controller": "agent_a",
            "effective_lock_holder": "agent_a",
            "entries": [{
                "name": "index.html",
                "type": "file",
                "revision": 4,
                "effective_controller": "agent_a",
                "effective_lock_holder": None,
            }],
        })


class _Alignment:
    def current(self, task_id):
        return None


class _Execution:
    def acceptors_of(self, task):
        return {"agent_b"}


class _Memory:
    def render(self, *args, **kwargs):
        return "（空）"

    def render_top(self, *args, **kwargs):
        return "（空）"


class _Brain:
    def cognitive_budget_preferences(self):
        return {"config_default": 16384,
                "per_phase": {"work": 32768}}


class _Agent:
    brain = _Brain()


class _Economy:
    def agent(self, agent_id):
        return _Agent()


def _runtime():
    return SimpleNamespace(
        directory=_Directory(),
        workspace_gateway=_Gateway(),
        alignment=_Alignment(),
        task_execution=_Execution(),
        public_working_memory=_Memory(),
        private_working_memory=_Memory(),
        private_short_term_memory=_Memory(),
        private_long_term_memory=_Memory(),
        economy=_Economy(),
    )


def _task():
    return SimpleNamespace(
        task_id="task_x",
        status="SUBMITTED",
        creator="agent_a",
        contractor="agent_a",
        participants=["agent_a", "agent_b"],
        workspace=SimpleNamespace(workspace_id="WS-X"),
        description="build it",
    )


def _run():
    return SimpleNamespace(
        demand="做一个网页",
        budget_aic=20.0,
        wall_limit_seconds=1200.0,
        max_agents=5,
        run_id="demand_x",
        root_task_id="task_x",
    )


def _feedback():
    return {
        "turn": 7,
        "phase": "work",
        "finish_reason": "length",
        "output_status": "EMPTY_CONTENT",
        "cognitive_status": "TRUNCATED",
        "truncated": True,
        "requested": 32768,
        "actual": 16384,
        "clipped": "above_caller_limit",
        "segments": 2,
    }


def test_task_prompt_delivers_tool_and_budget_facts():
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_a", inbox=[], budget_cap=16384,
        budget_feedback=_feedback())

    # 三条关键事实
    assert "不会回写 Task Workspace" in prompt
    assert "tests/foo.spec.js" in prompt
    assert "VERIFY(passed=false)" in prompt
    # 工具文档指针
    assert 'public_query(name="tools/playwright.md")' in prompt
    # 预算 / 截断事实
    assert "硬上限（Human）：16384" in prompt
    assert "requested=32768" in prompt
    assert "actual=16384" in prompt
    assert "clipped=above_caller_limit" in prompt
    assert "truncated=True" in prompt
    assert "cognitive_budget" in prompt


def test_intake_prompt_delivers_budget_facts():
    prompt = build_intake_prompt(
        run=_run(), runtime=_runtime(), front_agent="agent_a",
        budget_cap=16384, budget_feedback=_feedback())
    assert "认知预算" in prompt
    assert "硬上限（Human）：16384" in prompt
    assert "requested=32768" in prompt
    assert "truncated=True" in prompt


def test_work_unit_pressure_prompts_rearchitecture():
    """预算/输出反复撞上限时，必须给 Agent 一个“拆小”的明确信号。"""
    feedback = {
        "turn": 9,
        "phase": "work",
        "finish_reason": "length",
        "output_status": "EMPTY_CONTENT",
        "cognitive_status": "TRUNCATED",
        "truncated": True,
        "truncated_segments": 1,
        "requested": 32768,
        "actual": 32768,
        "clipped": None,
        "segments": 1,
        "pressure": True,
        "pressure_kinds": "truncated",
        "pressure_streak": 2,
        "pressure_total": 3,
        "pressure_last_turn": 9,
    }
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_a", inbox=[], budget_cap=32768,
        budget_feedback=feedback)

    assert "工作单元压力" in prompt
    assert "连续第 2 次" in prompt
    assert "本 run 累计 3 次" in prompt
    assert "拆成更小的文件/模块/步骤" in prompt
    assert "重新评估工作拆分" in prompt


def test_pressure_history_is_shown_after_budget_increase():
    """预算提高后即使当前轮正常，也保留本 run 的历史压力事实。"""
    feedback = {
        "turn": 10,
        "phase": "decision",
        "finish_reason": "stop",
        "output_status": "VALID_CONTENT",
        "cognitive_status": "COMPLETE",
        "truncated": False,
        "truncated_segments": 0,
        "requested": 32768,
        "actual": 32768,
        "clipped": None,
        "segments": 1,
        "pressure": False,
        "pressure_kinds": "",
        "pressure_streak": 0,
        "pressure_total": 3,
        "pressure_last_turn": 9,
    }
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_a", inbox=[], budget_cap=32768,
        budget_feedback=feedback)
    assert "本 run 已累计 3 次" in prompt
    assert "重新评估工作拆分" in prompt


def test_second_total_pressure_stops_big_append():
    """
    total >= 2 但 streak 只有 1（中间有正常轮）时，也必须升级为
    “停止整文件/大块追加”，否则 Agent 会继续原策略。
    """
    feedback = {
        "turn": 26,
        "phase": "decision",
        "finish_reason": "length",
        "output_status": "EMPTY_CONTENT",
        "cognitive_status": "TRUNCATED",
        "truncated": True,
        "truncated_segments": 1,
        "requested": 16384,
        "actual": 16384,
        "clipped": None,
        "segments": 1,
        "pressure": True,
        "pressure_kinds": "truncated",
        "pressure_streak": 1,
        "pressure_total": 2,
        "pressure_last_turn": 26,
    }
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_a", inbox=[], budget_cap=32768,
        budget_feedback=feedback)
    assert "停止大块写入" in prompt
    assert "拆成小文件/模块" in prompt
    assert "交给其他 participant" in prompt


def test_workspace_prompt_shows_controller_and_acceptance_path():
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_e", inbox=[], budget_cap=16384,
        budget_feedback=None)
    assert "root controller=agent_a" in prompt
    assert "root lock=agent_a" in prompt
    assert "父目录" in prompt
    assert "resource_handoff" in prompt
    assert 'run_tool(source="private")' in prompt
    # 展示相对路径，动作参数也要相对路径
    assert "- index.html (R4)" in prompt
    assert "- /index.html" not in prompt
    assert "不要带开头 `/`" in prompt


def test_prompt_separates_lifecycle_and_coordination_messages():
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_a", inbox=[], budget_cap=16384,
        budget_feedback=None)
    assert "普通协调消息" in prompt
    assert "TEXT / INFO / NOTE / QUESTION / REQUEST" in prompt
    assert "不要用 START" in prompt
    assert "ERROR 回执" in prompt
    assert "发送成功只代表**已投递**" in prompt


def test_error_inbox_is_explained_as_protocol_rejection():
    msg = SimpleNamespace(
        message_type="ERROR", from_agent="agent_c", to_agent="agent_a",
        text="", payload={"errors": ["缺少 task_id"],
                          "rejected_message_type": "START"})
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_a", inbox=[msg], budget_cap=16384,
        budget_feedback=None)
    assert "协议拒绝原因" in prompt
    assert "缺少 task_id" in prompt
    assert "不要重发同一 message_type" in prompt


def test_pending_request_is_prioritized_at_top():
    pending = [{
        "message_id": "m1",
        "from_agent": "agent_e",
        "message_type": "REQUEST",
        "text": "请建 parts/ 并 handoff 给我",
        "turn": 45,
    }]
    prompt = build_task_prompt(
        run=_run(), task=_task(), runtime=_runtime(),
        agent_id="agent_a", inbox=[], budget_cap=32768,
        budget_feedback=None, pending_requests=pending)
    assert "未回应的请求（优先处理）" in prompt
    assert "请建 parts/ 并 handoff 给我" in prompt
    assert "不要继续无关的重复读取" in prompt
    # 必须出现在 Task Core 之前
    assert prompt.index("未回应的请求") < prompt.index("Task Core")

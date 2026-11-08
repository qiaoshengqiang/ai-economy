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
        return SimpleNamespace(success=True, result={"entries": []})


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

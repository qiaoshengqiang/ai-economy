"""
两阶段 Demand Intake —— Front brief + Creator 规划
===================================================

真实 run 暴露：入口 Agent 在同一次 intake 里既解读 Human Demand，
又做架构/分工，且没有把 root lock 归 producer 纳入计划。

本文件钉住：

    ① Front 阶段只产出 brief，不创建 Task；
    ② brief 解析失败有明确错误，不猜；
    ③ Creator prompt 带 brief + previous_error + workspace_plan 要求；
    ④ workspace_plan 的 root_owner / 根级文件 owner 与 producer 一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demand.intake import fallback_brief, parse_brief          # noqa: E402
from demand.prompt import (                                     # noqa: E402
    build_brief_prompt, build_intake_prompt,
)


def _run():
    from types import SimpleNamespace
    return SimpleNamespace(
        demand="做一个 3D 火车世界",
        budget_aic=20.0,
        wall_limit_seconds=1500.0,
        max_agents=5,
        run_id="demand_x",
        root_task_id=None,
    )


def _runtime():
    from types import SimpleNamespace

    class _Directory:
        def all(self):
            return []

    class _Memory:
        def render(self, *args, **kwargs):
            return "（空）"

        def render_top(self, *args, **kwargs):
            return "（空）"

    return SimpleNamespace(
        directory=_Directory(),
        private_short_term_memory=_Memory(),
        private_long_term_memory=_Memory(),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            brain=SimpleNamespace(
                cognitive_budget_preferences=lambda: {
                    "config_default": 16384, "per_phase": {}}))),
    )


class TestBriefParsing:
    def test_parses_brief_object(self):
        brief, error = parse_brief(json.dumps({
            "brief": {
                "goal": "做一个页面",
                "deliverables": ["index.html"],
                "constraints": ["无外部模型"],
                "acceptance_expectations": ["能打开"],
                "ambiguities": [],
                "non_goals": [],
            }
        }, ensure_ascii=False))
        assert error == ""
        assert brief["goal"] == "做一个页面"
        assert brief["deliverables"] == ["index.html"]
        assert brief["constraints"] == ["无外部模型"]

    def test_parses_direct_goal_object(self):
        brief, error = parse_brief('{"goal":"x","deliverables":["a"]}')
        assert error == ""
        assert brief["goal"] == "x"

    def test_bad_output_is_an_error_not_a_guess(self):
        brief, error = parse_brief("我先说明一下，然后再规划。")
        assert brief is None
        assert error == "brief_not_found"

    def test_missing_goal_is_an_error(self):
        brief, error = parse_brief('{"brief":{"deliverables":["a"]}}')
        assert brief is None
        assert error == "brief_missing_goal"

    def test_fallback_keeps_the_human_demand(self):
        brief = fallback_brief("原始需求")
        assert brief["goal"] == "原始需求"
        assert brief["fallback"] is True


class TestBriefPrompt:
    def test_front_prompt_does_not_ask_for_task_create(self):
        prompt = build_brief_prompt(run=_run(), front_agent="agent_a")
        assert "Front / 解读" in prompt
        assert "只负责解读 Human Demand" in prompt
        assert "task_create" in prompt  # 只作为“不要调用”的禁止项
        assert "不要调用 `task_create`" in prompt
        assert "不要做架构设计" in prompt
        assert '"brief"' in prompt


class TestCreatorPrompt:
    def test_contains_brief_and_lock_facts(self):
        prompt = build_intake_prompt(
            run=_run(), runtime=_runtime(), front_agent="agent_a",
            budget_cap=32768,
            brief={"goal": "做页面", "deliverables": ["index.html"],
                   "constraints": [], "acceptance_expectations": [],
                   "ambiguities": [], "non_goals": []})
        assert "Front brief" in prompt
        assert "做页面" in prompt
        assert "Workspace 锁与责任" in prompt
        assert "root_owner" in prompt
        assert "workspace_plan" in prompt
        assert "root_owner 必须是 producer" in prompt

    def test_contains_previous_create_error(self):
        prompt = build_intake_prompt(
            run=_run(), runtime=_runtime(), front_agent="agent_a",
            budget_cap=32768, brief=None,
            previous_error="根级文件 index.html 的 owner 必须是 root_owner")
        assert "上一轮 task_create 失败" in prompt
        assert "根级文件 index.html" in prompt

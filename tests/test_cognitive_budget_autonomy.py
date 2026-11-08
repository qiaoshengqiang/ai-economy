"""
认知预算自主控制 —— 测试
==========================

用户 2026-11-08 确认的语义：

    Agent 自己决定“我想用多少认知预算”（可以和 thinking 一起放进信封）
        ↓
    Human / 系统给的是**硬上限**（`cognitive_budget_cap`）
        ↓
    实际值 = min(偏好, 硬上限)；Provider 再做 MIN / MAX 结构裁剪
        ↓
    requested / actual / budget_clipped 全部可见

🔒 这些测试不调用真实 LLM。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import ActionResult, run_cognition                    # noqa: E402
from actions.parse import (                                        # noqa: E402
    decision_from_text, normalize_cognitive_budget,
)
from agents.brain import Brain, BrainSettings                      # noqa: E402
from llm.base import LLMResponse                                   # noqa: E402
from llm.openai_compatible import OpenAICompatibleProvider         # noqa: E402


# ============================================================
# ① 信封解析
# ============================================================
class TestEnvelopeBudget:
    def test_single_int_becomes_current_phase(self):
        assert normalize_cognitive_budget(32768) == {"*": 32768}

    def test_phase_map(self):
        assert normalize_cognitive_budget(
            {"intake": 8192, "work": 32768}) == {
                "intake": 8192, "work": 32768}

    def test_invalid_shape_does_not_drop_action(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "cognitive_budget": "a lot",
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.kind == "act"
        assert len(d.intents) == 1
        assert d.cognitive_budget is None
        assert d.cognitive_budget_error == "invalid_cognitive_budget"

    def test_bool_is_not_an_int(self):
        assert normalize_cognitive_budget(True) is None
        assert normalize_cognitive_budget({"work": False}) is None

    def test_non_positive_is_invalid(self):
        assert normalize_cognitive_budget(0) is None
        assert normalize_cognitive_budget({"work": -1}) is None


# ============================================================
# ② Brain：偏好 / 硬上限 / 实际值
# ============================================================
class _CaptureRouter:
    def __init__(self):
        self.calls: List[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            provider="fake", model="fake", content="ok",
            input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=0.0, simulated=True)


def _brain(router, max_tokens: int = 16384):
    return Brain(
        router=router,
        settings=BrainSettings(agent_id="a", provider="deepseek",
                               model="deepseek-flash",
                               max_tokens=max_tokens))


class TestBrainBudget:
    def test_default_uses_config_max_tokens(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.generate("hi", phase="decision")
        assert r.calls[-1]["max_tokens"] == 16384
        assert r.calls[-1]["requested_cognitive_budget"] == 16384

    def test_agent_preference_can_exceed_config_default(self):
        """Agent 自己可以把 work 相从 16k 提到 32k（Provider MAX 内）。"""
        r = _CaptureRouter()
        b = _brain(r)
        b.set_cognitive_budget(32768, phase="work")
        b.generate("hi", phase="work")
        assert r.calls[-1]["max_tokens"] == 32768
        assert r.calls[-1]["requested_cognitive_budget"] == 32768

    def test_human_cap_wins_over_agent_preference(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_cognitive_budget(32768, phase="work")
        b.generate("hi", phase="work", cognitive_budget_cap=8192)
        assert r.calls[-1]["max_tokens"] == 8192
        assert r.calls[-1]["requested_cognitive_budget"] == 32768
        assert b.last_cognitive_budget["clipped"] == "above_cap"

    def test_explicit_request_wins_over_phase_preference(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_cognitive_budget(8192, phase="work")
        b.generate("hi", phase="work", requested_cognitive_budget=20000)
        assert r.calls[-1]["max_tokens"] == 20000
        assert r.calls[-1]["requested_cognitive_budget"] == 20000

    def test_explicit_max_tokens_is_a_hard_cap(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_cognitive_budget(32768, phase="work")
        b.generate("hi", phase="work", max_tokens=4096)
        assert r.calls[-1]["max_tokens"] == 4096

    def test_preferences_are_per_phase(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_cognitive_budget(32768, phase="work")
        b.set_cognitive_budget(4096, phase="decision")
        b.generate("d", phase="decision")
        b.generate("w", phase="work")
        assert r.calls[0]["max_tokens"] == 4096
        assert r.calls[1]["max_tokens"] == 32768

    def test_invalid_values_are_rejected(self):
        b = _brain(_CaptureRouter())
        for bad in (0, -1, True, "many"):
            try:
                b.set_cognitive_budget(bad, phase="work")
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} 应该被拒绝")


# ============================================================
# ③ Provider 裁剪：调用方上限优先于结构上限
# ============================================================
class TestProviderClip:
    def test_caller_cap_is_not_bypassed_by_provider_max(self):
        """
        🔴 旧顺序会返回 32768（越过调用方 cap=16384）。
        """
        eff, why = OpenAICompatibleProvider.clip_cognitive_budget(
            99999, 16384)
        assert (eff, why) == (16384, "above_caller_limit")

    def test_provider_max_still_applies_without_caller_cap(self):
        eff, why = OpenAICompatibleProvider.clip_cognitive_budget(
            99999, 99999)
        assert (eff, why) == (
            OpenAICompatibleProvider.MAX_COGNITIVE_BUDGET, "above_max")

    def test_budget_clipped_is_visible_on_response(self):
        resp = LLMResponse(
            provider="fake", model="fake", content="ok",
            requested_cognitive_budget=32768,
            actual_max_tokens=16384,
            budget_clipped="above_caller_limit",
            simulated=True)
        assert resp.to_dict()["budget_clipped"] == "above_caller_limit"


# ============================================================
# ④ run_cognition：指令在下一段之前生效
# ============================================================
@dataclass
class _Resp:
    content: str
    total_tokens: int = 10
    output_status: str = "VALID_CONTENT"
    cognitive_status: str = "COMPLETE"
    finish_reason: str = "stop"
    requested_cognitive_budget: Optional[int] = None
    actual_max_tokens: Optional[int] = None
    budget_clipped: Optional[str] = None


@dataclass
class _BrainState:
    budgets: Dict[str, int] = field(default_factory=dict)
    calls: List[tuple] = field(default_factory=list)

    def set_thinking(self, enabled, effort=None, *, phase="work"):
        return None

    def set_cognitive_budget(self, value, *, phase="work"):
        self.calls.append((phase, value))
        self.budgets[phase] = value


@dataclass
class _Agent:
    script: List[str]
    brain: _BrainState = field(default_factory=_BrainState)
    generates: List[Any] = field(default_factory=list)
    agent_id: str = "agent_a"
    _i: int = 0

    def generate(self, messages=None, phase="decision", **kwargs):
        self.generates.append((phase, self.brain.budgets.copy()))
        content = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        return _Resp(content)


class _ReadDispatcher:
    def dispatch(self, intent):
        return ActionResult(
            success=True,
            output={"path": "x", "content": "hello"},
            action_id=intent.intent_id,
            action_type=intent.action_type)


FIRST = json.dumps({
    "decision": "act",
    "cognitive_budget": {"work": 32768},
    "action": {"type": "workspace_read", "parameters": {"path": "x"}},
})


class TestRunCognitionBudget:
    def test_directive_is_applied_before_work_call(self):
        a = _Agent(script=[FIRST, "读完了。"])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(), follow_up_on_reads=True)

        assert a.brain.calls == [("work", 32768)]
        assert a.generates[0][0] == "decision"
        assert a.generates[0][1] == {}
        assert a.generates[1][0] == "work"
        assert a.generates[1][1]["work"] == 32768
        assert run.cognitive_budget == {"work": 32768}

    def test_single_int_overrides_current_phase(self):
        first = json.dumps({
            "decision": "act",
            "cognitive_budget": 8192,
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        })
        a = _Agent(script=[first, "读完了。"])
        run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(), follow_up_on_reads=True)
        assert a.brain.calls == [("decision", 8192)]


# ============================================================
# ⑤ 端到端（无网络）：run_cognition → Brain → Provider 参数
# ============================================================
class _SeqRouter:
    def __init__(self, contents: List[str]):
        self.contents = list(contents)
        self.calls: List[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0) if self.contents else "done"
        return LLMResponse(
            provider="fake", model="fake", content=content,
            # 模拟 Provider 回执，让端到端断言能看到真实参数。
            requested_cognitive_budget=kwargs.get(
                "requested_cognitive_budget"),
            actual_max_tokens=kwargs.get("max_tokens"),
            input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=0.0, simulated=True)


class _BrainAgent:
    """只做一件事：把 generate 原样交给真实 Brain。"""

    agent_id = "agent_a"

    def __init__(self, brain):
        self.brain = brain

    def generate(self, messages=None, **kwargs):
        return self.brain.generate(messages=messages, **kwargs)


class TestEndToEndBudget:
    def test_cap_flows_to_the_work_segment(self):
        first = json.dumps({
            "decision": "act",
            "cognitive_budget": {"work": 32768},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        })
        router = _SeqRouter([first, "读完了。"])
        brain = _brain(router)
        agent = _BrainAgent(brain)

        run = run_cognition(
            agent, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            follow_up_on_reads=True,
            cognitive_budget_cap=8192)

        assert len(router.calls) == 2
        # decision 段：config 默认 16384，被 cap 压到 8192
        assert router.calls[0]["max_tokens"] == 8192
        assert router.calls[0]["requested_cognitive_budget"] == 16384
        # work 段：Agent 请求 32768，仍被 cap 压到 8192
        assert router.calls[1]["max_tokens"] == 8192
        assert router.calls[1]["requested_cognitive_budget"] == 32768
        assert run.segments[1].requested_cognitive_budget == 32768
        assert run.segments[1].actual_max_tokens == 8192

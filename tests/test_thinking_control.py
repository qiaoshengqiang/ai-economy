"""
Agent 自主思考模式：分相 + 最小工作循环
==========================================

【方案（用户 2026-11-07 确认）】

    · decision 相：优先 disabled / low（不是强制）；
    · work 相：优先 enabled / high（不是强制）；
    · Agent 可以在信封里覆盖**任一相**；
    · 一轮可以是 decision → work（多步）→ decision checkpoint。

本文件锁住：
    ① 信封解析：单相 / 分相 / 非法；
    ② Brain 注入：按 phase 选择策略；默认 None = 不注入；
    ③ 不 clamp：decision 可以被 Agent 设成 high；
    ④ 读取回喂 / 工作循环之前就应用指令；
    ⑤ 最小工作循环的 phase 顺序；
    ⑥ 实验初始偏好是 decision=off/low、work=on/high。
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

from actions import ActionResult, run_cognition                # noqa: E402
from actions.parse import decision_from_text                   # noqa: E402
from agents.brain import Brain, BrainSettings                  # noqa: E402
from llm.base import LLMResponse                               # noqa: E402


# ------------------------------------------------------------
# ① 信封解析
# ------------------------------------------------------------
class TestEnvelopeThinking:
    def test_single_directive_becomes_current_phase(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"enabled": True, "effort": "high"},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.kind == "act"
        assert d.thinking == {"*": {"enabled": True, "effort": "high"}}
        assert d.thinking_error == ""

    def test_phase_map(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {
                "decision": {"enabled": False, "effort": "low"},
                "work": {"enabled": True, "effort": "high"},
            },
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking == {
            "decision": {"enabled": False, "effort": "low"},
            "work": {"enabled": True, "effort": "high"},
        }

    def test_invalid_directive_does_not_drop_actions(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"enabled": "yes"},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.kind == "act"
        assert len(d.intents) == 1
        assert d.thinking is None
        assert d.thinking_error == "invalid_thinking"
        assert d.thinking_raw == {"enabled": "yes"}

    def test_provider_style_type_is_normalized(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"type": "enabled", "effort": "high"},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking == {"*": {"enabled": True, "effort": "high"}}
        assert d.thinking_error == ""

    def test_string_effort_shorthand_is_normalized(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": "high",
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking == {"*": {"enabled": True, "effort": "high"}}
        assert d.thinking_error == ""

    def test_effort_only_is_normalized_to_enabled(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"effort": "high"},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking == {"*": {"enabled": True, "effort": "high"}}
        assert d.thinking_error == ""

    def test_intake_phase_is_supported(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"intake": {"enabled": True, "effort": "max"}},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking == {"intake": {"enabled": True, "effort": "max"}}

    def test_partial_phase_map_is_allowed(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"work": {"enabled": True, "effort": "max"}},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking == {"work": {"enabled": True, "effort": "max"}}


# ------------------------------------------------------------
# ② Brain 注入（按 phase）
# ------------------------------------------------------------
class _CaptureRouter:
    def __init__(self):
        self.calls: List[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            provider="fake", model="fake", content="ok",
            input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=0.0, simulated=True)


def _brain(router):
    return Brain(
        router=router,
        settings=BrainSettings(agent_id="a", provider="deepseek",
                               model="deepseek-flash"),
    )


class TestBrainThinking:
    def test_default_none_does_not_inject(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.generate("hi", phase="decision")
        b.generate("hi", phase="work")
        assert "thinking" not in r.calls[-1]
        assert "reasoning_effort" not in r.calls[-1]

    def test_decision_disabled_injects_only_thinking(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_thinking(False, phase="decision")
        b.generate("hi", phase="decision")
        assert r.calls[-1]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in r.calls[-1]

    def test_work_enabled_injects_effort(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_thinking(True, "high", phase="work")
        b.generate("hi", phase="work")
        assert r.calls[-1]["thinking"] == {"type": "enabled"}
        assert r.calls[-1]["reasoning_effort"] == "high"

    def test_phases_are_independent(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_thinking(False, "low", phase="decision")
        b.set_thinking(True, "max", phase="work")
        b.generate("d", phase="decision")
        b.generate("w", phase="work")
        assert r.calls[0]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in r.calls[0]
        assert r.calls[1]["thinking"] == {"type": "enabled"}
        assert r.calls[1]["reasoning_effort"] == "max"

    def test_no_clamp_decision_can_be_high(self):
        """🔴 “决策优先低思考”是默认值，不是 Runtime 的强制规则。"""
        r = _CaptureRouter()
        b = _brain(r)
        b.set_thinking(True, "high", phase="decision")
        b.generate("hi", phase="decision")
        assert r.calls[-1]["thinking"] == {"type": "enabled"}
        assert r.calls[-1]["reasoning_effort"] == "high"

    def test_intake_can_use_max_thinking(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_thinking(True, "max", phase="intake")
        b.generate("hi", phase="intake")
        assert r.calls[-1]["thinking"] == {"type": "enabled"}
        assert r.calls[-1]["reasoning_effort"] == "max"

    def test_temperature_defaults_to_config(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.generate("hi", phase="decision")
        b.generate("hi", phase="work")
        assert r.calls[0]["temperature"] == 0.7
        assert r.calls[1]["temperature"] == 0.7

    def test_temperature_can_differ_by_phase(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_temperature(0.2, phase="decision")
        b.set_temperature(None, phase="work")      # 回到 config 默认
        b.generate("d", phase="decision")
        b.generate("w", phase="work")
        assert r.calls[0]["temperature"] == 0.2
        assert r.calls[1]["temperature"] == 0.7
        assert b.last_temperature == 0.7

    def test_explicit_temperature_wins_over_phase(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_temperature(0.2, phase="decision")
        b.generate("hi", phase="decision", temperature=0.9)
        assert r.calls[-1]["temperature"] == 0.9

    def test_temperature_range_is_validated(self):
        b = _brain(_CaptureRouter())
        try:
            b.set_temperature(3.0, phase="decision")
        except ValueError as exc:
            assert "0..2" in str(exc)
        else:
            raise AssertionError("temperature=3.0 应该被拒绝")

    def test_provider_capabilities_are_declared_by_providers(self):
        from llm.bailian import BailianProvider
        from llm.deepseek import DeepSeekProvider
        from llm.doubao import DoubaoProvider

        assert DeepSeekProvider.supports_thinking is True
        assert DeepSeekProvider.supports_reasoning_effort is True
        assert DoubaoProvider.supports_thinking is True
        assert DoubaoProvider.supports_reasoning_effort is False
        assert BailianProvider.supports_thinking is False
        assert BailianProvider.supports_reasoning_effort is False


# ------------------------------------------------------------
# ③ run_cognition：phase 顺序 / 工作循环
# ------------------------------------------------------------
@dataclass
class _Resp:
    content: str
    total_tokens: int = 10
    output_status: str = "VALID_CONTENT"
    cognitive_status: str = "COMPLETE"
    finish_reason: str = "stop"


@dataclass
class _BrainState:
    calls: List[tuple] = field(default_factory=list)
    policy: Dict[str, tuple] = field(default_factory=dict)

    def set_thinking(self, enabled, effort=None, *, phase="work"):
        self.calls.append((phase, enabled, effort))
        self.policy[phase] = (enabled, effort)


@dataclass
class _Agent:
    script: List[str]
    brain: _BrainState = field(default_factory=_BrainState)
    generates: List[Any] = field(default_factory=list)
    agent_id: str = "agent_a"
    _i: int = 0

    def generate(self, messages=None, phase="decision", **kwargs):
        self.generates.append((phase, self.brain.policy.copy()))
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
    "thinking": {"work": {"enabled": True, "effort": "high"}},
    "action": {"type": "workspace_read", "parameters": {"path": "x"}},
})


class TestRunCognitionPhases:
    def test_directive_is_applied_before_work_call(self):
        a = _Agent(script=[FIRST, "读完了。"])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            follow_up_on_reads=True)

        assert a.brain.calls == [("work", True, "high")]
        assert a.generates[0][0] == "decision"
        assert a.generates[1][0] == "work"
        assert a.generates[0][1] == {}
        assert a.generates[1][1]["work"] == (True, "high")
        assert run.thinking == {"work": {"enabled": True, "effort": "high"}}

    def test_initial_phase_can_be_intake(self):
        a = _Agent(script=[FIRST, "读完了。"])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            follow_up_on_reads=True,
            initial_phase="intake")
        assert a.generates[0][0] == "intake"
        assert run.segments[0].phase == "intake"
        assert a.generates[1][0] == "work"

    def test_work_loop_runs_decision_work_decision(self):
        # decision → read；work → read；checkpoint → message
        work = json.dumps({
            "decision": "act",
            "action": {"type": "workspace_read",
                       "parameters": {"path": "y"}},
        })
        checkpoint = json.dumps({
            "decision": "act",
            "action": {"type": "message_send",
                       "parameters": {"to": "agent_b", "text": "done"}},
        })
        a = _Agent(script=[FIRST, work, checkpoint])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            work_loop=True,
            max_work_steps=1)

        phases = [g[0] for g in a.generates]
        assert phases == ["decision", "work", "decision"]
        assert [s.phase for s in run.segments] == phases
        # 最后一个 decision 的动作也被执行
        assert [i.action_type for i in run.action_intents] == [
            "workspace_read", "workspace_read", "message_send"]

    def test_work_loop_can_end_with_summary(self):
        work = json.dumps({
            "decision": "act",
            "action": {"type": "workspace_read",
                       "parameters": {"path": "y"}},
        })
        a = _Agent(script=[FIRST, work, "本轮总结：读完了 x 和 y。"])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            work_loop=True,
            max_work_steps=1,
            summary_phase=True)

        assert [g[0] for g in a.generates] == [
            "decision", "work", "summary"]
        assert run.summary == "本轮总结：读完了 x 和 y。"
        assert [s.phase for s in run.segments] == [
            "decision", "work", "summary"]

    def test_summary_runs_even_without_work_steps(self):
        write = json.dumps({
            "decision": "act",
            "action": {"type": "workspace_write",
                       "parameters": {"path": "x", "write_mode": "create",
                                      "content": "hi"}},
        })
        a = _Agent(script=[write, "本轮总结：写完了 x。"])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            work_loop=True,
            max_work_steps=2,
            summary_phase=True)

        assert [g[0] for g in a.generates] == ["decision", "summary"]
        assert run.summary == "本轮总结：写完了 x。"

    def test_work_loop_does_not_run_without_observable_results(self):
        a = _Agent(script=[json.dumps({
            "decision": "act",
            "action": {"type": "workspace_write",
                       "parameters": {"path": "x", "write_mode": "create",
                                      "content": "hi"}},
        })])
        run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),   # 返回 content，但 write 不是 observable
            work_loop=True,
            max_work_steps=2)
        assert [g[0] for g in a.generates] == ["decision"]


# ------------------------------------------------------------
# ④ 实验初始偏好
# ------------------------------------------------------------
class TestExperimentInitialState:
    def _src(self) -> str:
        return (ROOT / "experiments" / "heterogeneous_production"
                / "production.py").read_text(encoding="utf-8")

    def test_initial_preferences(self):
        from experiments.heterogeneous_production import production as P
        assert P.INITIAL_DECISION_THINKING == {
            "enabled": False, "effort": "low"}
        assert P.INITIAL_WORK_THINKING == {
            "enabled": True, "effort": "high"}
        assert P.INITIAL_INTAKE_THINKING == {
            "enabled": True, "effort": "max"}
        assert P.INITIAL_INTAKE_TEMPERATURE == 0.2
        assert P.INITIAL_DECISION_TEMPERATURE == 0.2
        assert P.INITIAL_WORK_TEMPERATURE is None
        src = self._src()
        assert 'phase="intake"' in src
        assert 'phase="decision"' in src
        assert 'phase="work"' in src
        assert "set_temperature(" in src
        assert "思考模式（环境能力）" in src
        assert "intake 首轮" in src

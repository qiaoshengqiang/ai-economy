"""
Agent 自主思考模式：最小开关
================================

【方案（用户 2026-11-07 确认）】

    初始状态：thinking = disabled
    之后由 Agent 在结构化信封里决定**下一步**是否开启思考、强度多少。

    {"decision":"act",
     "thinking":{"enabled":true,"effort":"high"},
     "action":{...}}

本文件锁住：
    ① 信封解析：合法 / 非法 thinking；
    ② Brain 注入：disabled 只发 thinking；enabled 才发 reasoning_effort；
    ③ 默认 None = 不注入（旧 Agent / 旧测试行为不变）；
    ④ 读取回喂（follow-up）之前就应用指令；
    ⑤ 实验初始状态确实是 disabled。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

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
    def test_valid_directive_is_parsed(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"enabled": True, "effort": "high"},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.kind == "act"
        assert d.thinking == {"enabled": True, "effort": "high"}
        assert d.thinking_error == ""

    def test_disable_without_effort(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "thinking": {"enabled": False},
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking == {"enabled": False}

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

    def test_no_directive_keeps_none(self):
        d = decision_from_text("a", json.dumps({
            "decision": "act",
            "action": {"type": "workspace_read",
                       "parameters": {"path": "x"}},
        }))
        assert d.thinking is None
        assert d.thinking_error == ""


# ------------------------------------------------------------
# ② Brain 注入
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


def _brain(router, provider="deepseek"):
    return Brain(
        router=router,
        settings=BrainSettings(agent_id="a", provider=provider,
                               model="deepseek-flash"),
    )


class TestBrainThinking:
    def test_default_none_does_not_inject(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.generate("hi")
        assert "thinking" not in r.calls[-1]
        assert "reasoning_effort" not in r.calls[-1]

    def test_disabled_injects_only_thinking(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_thinking(False)
        b.generate("hi")
        assert r.calls[-1]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in r.calls[-1]

    def test_enabled_injects_effort(self):
        r = _CaptureRouter()
        b = _brain(r)
        b.set_thinking(True, "high")
        b.generate("hi")
        assert r.calls[-1]["thinking"] == {"type": "enabled"}
        assert r.calls[-1]["reasoning_effort"] == "high"

    def test_provider_capabilities_are_declared_by_providers(self):
        """
        🔒 Brain 不认识厂商名；支持与否必须由 Provider 自己声明。
        """
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
# ③ run_cognition：读取回喂之前应用
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
    state: Optional[tuple] = None

    def set_thinking(self, enabled, effort=None):
        self.calls.append((enabled, effort))
        self.state = (enabled, effort)


@dataclass
class _Agent:
    script: List[str]
    brain: _BrainState = field(default_factory=_BrainState)
    generates: List[Any] = field(default_factory=list)
    agent_id: str = "agent_a"
    _i: int = 0

    def generate(self, messages=None, **kwargs):
        self.generates.append((messages, self.brain.state))
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
    "thinking": {"enabled": True, "effort": "high"},
    "action": {"type": "workspace_read", "parameters": {"path": "x"}},
})


class TestRunCognitionAppliesBeforeFollowUp:
    def test_directive_is_applied_before_second_call(self):
        a = _Agent(script=[FIRST, "读完了。"])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            follow_up_on_reads=True)

        assert a.brain.calls == [(True, "high")]
        # 第一段生成时仍是初始 None；第二段必须已经看到新状态
        assert a.generates[0][1] is None
        assert a.generates[1][1] == (True, "high")
        assert run.thinking == {"enabled": True, "effort": "high"}

    def test_no_directive_keeps_state(self):
        a = _Agent(script=[
            json.dumps({"decision": "act",
                        "action": {"type": "workspace_read",
                                   "parameters": {"path": "x"}}}),
            "ok"])
        a.brain.state = (False, "low")
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            follow_up_on_reads=True)
        assert a.brain.calls == []
        assert a.generates[1][1] == (False, "low")
        assert run.thinking is None


# ------------------------------------------------------------
# ④ 实验初始状态
# ------------------------------------------------------------
class TestExperimentInitialState:
    def _src(self) -> str:
        return (ROOT / "experiments" / "heterogeneous_production"
                / "production.py").read_text(encoding="utf-8")

    def test_initial_state_is_disabled(self):
        from experiments.heterogeneous_production import production as P
        assert P.INITIAL_THINKING_ENABLED is False
        src = self._src()
        assert "set_thinking(INITIAL_THINKING_ENABLED" in src
        assert "思考模式（环境能力）" in src

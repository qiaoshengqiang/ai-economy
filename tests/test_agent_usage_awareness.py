"""
Agent 进程内用量认知 —— 回归测试。

目标：
    · 每次 LLM 调用后，Brain/Agent 能拿到 tokens + cost_ai + token_source；
    · EconomicContext 能渲染最近一次 / 最近 N 次 / task / session 用量；
    · 历史记录来自 llm_calls，不来自私有空间；
    · token 未知时显示 unknown，不伪装成 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.brain import Brain, BrainSettings                       # noqa: E402
from cognition.economic import EconomicContextBuilder               # noqa: E402
from llm.base import LLMResponse                                    # noqa: E402
from llm.cost import CostCalculator, CostRule                       # noqa: E402
from llm.recorder import LLMCallRecorder                            # noqa: E402
from storage.database import create_memory_database                 # noqa: E402


class _Router:
    def __init__(self, response: LLMResponse):
        self.response = response

    def generate(self, **kwargs):
        return self.response


def _brain(recorder, response, *, builder=None):
    return Brain(
        router=_Router(response),
        settings=BrainSettings(agent_id="agent_a", provider="mock", model="m"),
        recorder=recorder,
        context_builder=builder,
        default_context_query={},
    )


def test_brain_exposes_last_usage_with_cost():
    db = create_memory_database()
    db.initialize()
    recorder = LLMCallRecorder(
        db, tokens_per_ai_coin=10000,
        cost_calculator=CostCalculator(CostRule(ai_per_1k_tokens=0.01)))
    response = LLMResponse(
        provider="mock", model="m", content="ok",
        input_tokens=400, output_tokens=600, total_tokens=1000,
        token_source="api")

    brain = _brain(recorder, response)
    brain.generate(messages="x")

    usage = brain.last_usage
    assert usage is not None
    assert usage["total_tokens"] == 1000
    assert usage["cost_ai"] == 0.01
    assert usage["cost_known"] is True
    assert usage["token_source"] == "api"
    assert usage["call_id"]

    # 同一条回执也落进了 llm_calls（唯一历史源）。
    row = recorder.recent("agent_a", 1)[0]
    assert row["total_tokens"] == 1000
    assert row["cost_ai"] == 0.01


def test_brain_marks_unknown_tokens_as_unknown():
    db = create_memory_database()
    db.initialize()
    recorder = LLMCallRecorder(
        db, tokens_per_ai_coin=10000,
        cost_calculator=CostCalculator(CostRule(ai_per_1k_tokens=0.01)))
    response = LLMResponse(
        provider="mock", model="m", content="ok",
        input_tokens=None, output_tokens=None, total_tokens=None,
        token_source="unknown")

    brain = _brain(recorder, response)
    brain.generate(messages="x")

    assert brain.last_usage["cost_known"] is False
    assert brain.last_usage["total_tokens"] == 0


def test_economic_context_renders_last_recent_task_and_session_usage():
    calls = [
        {"call_id": "call_2", "provider": "deepseek", "model": "flash",
         "total_tokens": 600, "cost_ai": 0.006, "cost_known": True,
         "token_source": "api"},
        {"call_id": "call_1", "provider": "deepseek", "model": "flash",
         "total_tokens": 400, "cost_ai": 0.004, "cost_known": True,
         "token_source": "api"},
    ]
    usage = {"calls": 2, "input_tokens": 500, "output_tokens": 500,
             "total_tokens": 1000, "cost_ai": 0.01, "unknown_calls": 0}

    b = EconomicContextBuilder(
        balance_of=lambda aid: 99.0,
        recent_calls_of=lambda aid, limit: calls,
        usage_of_task=lambda tid: usage,
        usage_since=lambda aid, since: usage,
    )
    ctx = b.build("agent_a", task_id="task_1",
                  session_started_at="2026-10-07T00:00:00+00:00")
    text = "\n".join(ctx.to_lines())

    assert ctx.last_call["call_id"] == "call_2"
    assert len(ctx.recent_calls) == 2
    assert ctx.current_task_usage["cost_ai"] == 0.01
    assert ctx.current_session_usage["total_tokens"] == 1000
    assert "Last LLM call:" in text
    assert "Recent LLM calls" in text
    assert "Current task usage:" in text
    assert "Current session usage:" in text
    assert "0.010000 AIC" in text


def test_unknown_usage_renders_unknown_not_zero():
    b = EconomicContextBuilder(
        balance_of=lambda aid: 100.0,
        recent_calls_of=lambda aid, limit: [{
            "call_id": "call_x", "provider": "p", "model": "m",
            "total_tokens": 0, "cost_ai": 0.0, "cost_known": False,
            "token_source": "unknown",
        }],
    )
    ctx = b.build("agent_a")
    text = "\n".join(ctx.to_lines())
    assert "tokens unknown" in text
    assert "cost unknown" in text


def test_main_usage_aggregates_read_from_llm_calls():
    from main import _recent_llm_usage, _sum_llm_usage

    db = create_memory_database()
    db.initialize()
    recorder = LLMCallRecorder(
        db, tokens_per_ai_coin=10000,
        cost_calculator=CostCalculator(CostRule(ai_per_1k_tokens=0.01)))
    for tokens in (1000, 500):
        recorder.record(
            "agent_a",
            response=LLMResponse(
                provider="mock", model="m", content="ok",
                input_tokens=tokens // 2, output_tokens=tokens // 2,
                total_tokens=tokens, token_source="api"),
            task_id="task_1")

    usage = _sum_llm_usage(db, "task_id = ?", ("task_1",))
    assert usage["calls"] == 2
    assert usage["total_tokens"] == 1500
    assert usage["cost_ai"] == 0.015

    recent = _recent_llm_usage(db, "agent_a", 5)
    assert len(recent) == 2
    assert recent[0]["cost_known"] is True


def test_brain_passes_task_and_session_into_context_query():
    class _Builder:
        def __init__(self):
            self.kwargs = None

        def build(self, agent_id, **kwargs):
            self.kwargs = dict(kwargs)
            from cognition.context import CognitiveContext
            return CognitiveContext()

    builder = _Builder()
    brain = _brain(None, LLMResponse(
        provider="mock", model="m", content="ok",
        input_tokens=1, output_tokens=1, total_tokens=2),
        builder=builder)
    brain.generate(messages="x", task_id="task_9",
                   session_started_at="2026-10-07T00:00:00+00:00")

    assert builder.kwargs["task_id"] == "task_9"
    assert builder.kwargs["session_started_at"] == \
        "2026-10-07T00:00:00+00:00"

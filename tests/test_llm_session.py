"""LLM 会话 ID 与缓存遥测。"""

from __future__ import annotations

from types import SimpleNamespace

from llm.base import LLMResponse
from llm.recorder import LLMCallRecorder
from llm.session import (
    SESSION_PREFIX,
    alarm_scope,
    build_session_id,
    front_scope,
    task_scope,
)
from llm.openai_compatible import OpenAICompatibleProvider
from storage.database import create_memory_database


def test_session_id_is_stable_and_scoped():
    first = build_session_id("front|conv=conv_1", provider="opencode",
                             model="glm-5.3")
    again = build_session_id("front|conv=conv_1", provider="opencode",
                             model="glm-5.3")
    other = build_session_id("front|conv=conv_2", provider="opencode",
                             model="glm-5.3")
    other_model = build_session_id("front|conv=conv_1", provider="opencode",
                                   model="kimi-k3")
    assert first == again
    assert first.startswith(SESSION_PREFIX)
    assert first != other
    assert first != other_model
    assert build_session_id("") == ""


def test_scope_builders_are_stable():
    assert front_scope("conv_1", agent_id="agent_a") == (
        "front|conv=conv_1|agent=agent_a")
    assert task_scope("task_1", agent_id="agent_b") == (
        "task|task_1|agent=agent_b")
    assert alarm_scope("alarm_1", agent_id="agent_a") == (
        "alarm|alarm_1|agent=agent_a")
    assert task_scope("", agent_id="agent_b") == ""


def test_provider_session_header_is_per_request(monkeypatch):
    calls = []

    def fake_http(endpoint, payload, headers, timeout):
        calls.append(dict(headers))
        return SimpleNamespace(
            ok=True, status=200, text="", headers={},
            json=lambda: {
                "model": "glm-5.3",
                "choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            })

    provider = OpenAICompatibleProvider(
        api_key_override="test-key",
        http_post=fake_http,
        provider_config={
            "base_url": "https://example.invalid",
            "session_header": "x-opencode-session",
            "extra_headers": {"x-opencode-session": "ses_static"},
        },
        default_model="glm-5.3",
    )
    provider.generate(messages="hi", llm_session_id="ses_one")
    provider.generate(messages="hi", llm_session_id="ses_two")
    provider.generate(messages="hi")

    assert calls[0]["x-opencode-session"] == "ses_one"
    assert calls[1]["x-opencode-session"] == "ses_two"
    # 没传 session 时仍保留静态兜底，但不应该覆盖动态值。
    assert calls[2]["x-opencode-session"] == "ses_static"


def test_read_cache_usage_from_opencode_and_openai():
    opencode = {
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "total_tokens": 1500,
        "cost": 0.0,
        "opencode": {
            "billing_source": "go",
            "allowance": 0.00412,
            "tokens": {
                "input": 1200,
                "output": 300,
                "cache_read": 8000,
                "cache_write_5m": 10,
                "cache_write_1h": 20,
            },
        },
    }
    parsed = OpenAICompatibleProvider._read_cache_usage(opencode)
    assert parsed["cache_read_tokens"] == 8000
    assert parsed["cache_write_tokens"] == 30
    assert parsed["allowance_usd"] == 0.00412
    assert parsed["billing_source"] == "go"
    assert parsed["provider_cost_usd"] == 0.0

    openai = {
        "prompt_tokens_details": {"cached_tokens": 64},
        "cache_creation_input_tokens": 7,
    }
    parsed = OpenAICompatibleProvider._read_cache_usage(openai)
    assert parsed["cache_read_tokens"] == 64
    assert parsed["cache_write_tokens"] == 7
    assert parsed["allowance_usd"] is None


def test_recorder_persists_cache_telemetry():
    db = create_memory_database()
    try:
        recorder = LLMCallRecorder(db)
        response = LLMResponse(
            content="ok",
            provider="opencode",
            model="glm-5.3",
            input_tokens=1200,
            output_tokens=300,
            total_tokens=1500,
            cache_read_tokens=8000,
            cache_write_tokens=30,
            provider_cost_usd=0.0,
            allowance_usd=0.00412,
            billing_source="go",
            llm_session_id="ses_test",
        )
        recorder.record(agent_id="agent_a", response=response)
        row = db.fetchone("SELECT * FROM llm_calls")
        assert row["cache_read_tokens"] == 8000
        assert row["cache_write_tokens"] == 30
        assert row["allowance_usd"] == 0.00412
        assert row["billing_source"] == "go"
        assert row["llm_session_id"] == "ses_test"
    finally:
        db.close()


def test_brain_derives_task_session_and_respects_scope():
    from agents.brain import Brain, BrainSettings

    class _Router:
        def __init__(self):
            self.kwargs = {}

        def generate(self, **kwargs):
            self.kwargs = kwargs
            return LLMResponse(
                content="ok", provider="opencode", model="glm-5.3",
                input_tokens=1, output_tokens=1, total_tokens=2)

    router = _Router()
    brain = Brain(
        router=router,
        settings=BrainSettings(
            agent_id="agent_a", provider="opencode", model="glm-5.3"),
    )

    brain.generate(messages="hi", task_id="task_1", record=False)
    first = router.kwargs["llm_session_id"]
    brain.generate(messages="hi", task_id="task_1", record=False)
    second = router.kwargs["llm_session_id"]
    brain.generate(messages="hi", task_id="task_2", record=False)
    other_task = router.kwargs["llm_session_id"]
    brain.generate(
        messages="hi", task_id="task_1",
        llm_session_scope="front|conv=conv_1", record=False)
    front = router.kwargs["llm_session_id"]

    assert first == second
    assert first != other_task
    assert first != front
    assert first.startswith(SESSION_PREFIX)

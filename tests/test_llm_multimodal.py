"""
原生多模态（读图）回归测试
============================

验证四件事：

1. `normalize_messages` 保留 content parts，而不是把图片强转成字符串；
2. 图片 token 估算不把 base64 当文本数；
3. Provider 能力门禁：不支持图片的 Provider 明确拒绝；
4. `run_tool` 的截图能作为 `image_url` part 回喂到同一次认知的下一条消息。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from actions import (
    ACTION_RUN_TOOL,
    ActionDispatcher,
    ActionIntent,
    ActionResult,
    RunToolExecutor,
    render_read_results,
    run_cognition,
)
from agents.brain import Brain
from llm.base import (
    IMAGE_TOKEN_ESTIMATE,
    LLMRequestError,
    estimate_messages_tokens,
    normalize_messages,
)
from llm.deepseek import DeepSeekProvider
from llm.mock import MockProvider


def _image_message(data_url: str = "data:image/png;base64,AAAA"):
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url",
             "image_url": {"url": data_url, "detail": "low"}},
        ],
    }]


class TestNormalizeContentParts:
    def test_image_parts_are_preserved(self):
        normalized = normalize_messages(_image_message())
        content = normalized[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "看看这张图"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["detail"] == "low"

    def test_plain_string_is_unchanged(self):
        assert normalize_messages("你好") == [
            {"role": "user", "content": "你好"}]

    def test_image_in_system_message_is_rejected(self):
        with pytest.raises(LLMRequestError, match="user"):
            normalize_messages([{
                "role": "system",
                "content": [{"type": "image_url",
                             "image_url": {"url": "data:image/png;base64,AA"}}],
            }])

    def test_non_image_url_is_rejected(self):
        with pytest.raises(LLMRequestError, match="http"):
            normalize_messages([{
                "role": "user",
                "content": [{"type": "image_url",
                             "image_url": {"url": "file:///etc/passwd"}}],
            }])

    def test_unknown_part_is_rejected(self):
        with pytest.raises(LLMRequestError, match="未知"):
            normalize_messages([{
                "role": "user",
                "content": [{"type": "audio", "data": "x"}],
            }])


class TestImageTokenEstimate:
    def test_image_adds_conservative_tokens(self):
        text_only = [{"role": "user", "content": "hello"}]
        with_image = _image_message()
        assert (estimate_messages_tokens(with_image)
                - estimate_messages_tokens(text_only)
                >= IMAGE_TOKEN_ESTIMATE)

    def test_base64_is_not_counted_as_text(self):
        big = "data:image/png;base64," + ("A" * 200_000)
        tokens = estimate_messages_tokens(_image_message(big))
        # 如果按 base64 文本估算，会是 5 万级；这里必须是 1k 级。
        assert tokens < 5_000


class TestMockProviderWithImages:
    def test_mock_accepts_image_parts_without_crashing(self):
        provider = MockProvider()
        response = provider.generate(
            messages=_image_message(), system="你是 Agent A")
        assert response.content
        assert response.total_tokens >= IMAGE_TOKEN_ESTIMATE


class TestProviderCapability:
    def test_deepseek_accepts_image_parts(self):
        provider = DeepSeekProvider()
        payload = provider.build_payload(
            normalize_messages(_image_message()),
            "deepseek-flash", 0.2, 100)
        assert isinstance(payload["messages"][0]["content"], list)

    def test_text_only_provider_rejects_image_parts(self):
        provider = DeepSeekProvider()
        provider.supports_image_input = False
        with pytest.raises(LLMRequestError, match="不支持图片"):
            provider.build_payload(
                normalize_messages(_image_message()),
                "deepseek-flash", 0.2, 100)


class TestEvidenceDoesNotStoreBase64:
    def test_request_evidence_redacts_image_data(self):
        big = "data:image/png;base64," + ("A" * 100_000)
        evidence = Brain._capture_request_evidence(
            messages=_image_message(big),
            system=None,
            ctx=None,
            call_purpose="cognition",
            cognition_turn_id=None,
            experiment_run_id=None,
        )
        # 如果 base64 进了证据，request_chars 会是 10 万级。
        assert evidence["request_chars"] < 2_000
        assert "data:image" not in json.dumps(evidence, ensure_ascii=False)


class TestRenderReadResultsWithImages:
    def _pairs(self, images):
        intent = ActionIntent(
            action_type=ACTION_RUN_TOOL,
            agent_id="agent_test",
            parameters={"tool": "playwright"},
        )
        result = ActionResult(
            success=True,
            action_type=ACTION_RUN_TOOL,
            output={
                "ran": True, "ok": True, "tool": "playwright",
                "run_id": "run_test", "exit_code": 0,
                "stdout": "2 passed", "stderr": "", "images": images,
            },
        )
        return [(intent, result)]

    def test_images_become_content_parts(self):
        pairs = self._pairs([
            {"path": "shot.png", "mime": "image/png", "bytes": 10},
        ])
        rendered = render_read_results(
            pairs,
            image_resolver=lambda run_id, path: (
                "data:image/png;base64,AAAA"),
        )
        assert isinstance(rendered, list)
        assert rendered[0]["type"] == "text"
        assert "shot.png" in rendered[0]["text"]
        assert rendered[1]["type"] == "image_url"
        assert rendered[1]["image_url"]["detail"] == "low"

    def test_at_most_two_images(self):
        pairs = self._pairs([
            {"path": f"shot{i}.png", "mime": "image/png", "bytes": 10}
            for i in range(5)
        ])
        rendered = render_read_results(
            pairs,
            image_resolver=lambda run_id, path: (
                "data:image/png;base64,AAAA"),
        )
        assert isinstance(rendered, list)
        assert sum(1 for p in rendered if p["type"] == "image_url") == 2

    def test_without_resolver_returns_text(self):
        pairs = self._pairs([
            {"path": "shot.png", "mime": "image/png", "bytes": 10},
        ])
        rendered = render_read_results(pairs)
        assert isinstance(rendered, str)
        assert "shot.png" in rendered


class _FakeResponse:
    def __init__(self, content: str, total_tokens: int = 10):
        self.content = content
        self.total_tokens = total_tokens
        self.finish_reason = "stop"
        self.output_status = "VALID_CONTENT"


class _FakeAgent:
    agent_id = "agent_test"

    def __init__(self) -> None:
        self.calls = []

    def generate(self, messages=None, phase="decision", **kwargs):
        self.calls.append({"messages": messages, "phase": phase})
        if len(self.calls) == 1:
            return _FakeResponse(json.dumps({
                "decision": "act",
                "reasoning": "run the browser test",
                "action": {
                    "type": ACTION_RUN_TOOL,
                    "parameters": {"tool": "playwright", "args": ["test"]},
                },
            }))
        return _FakeResponse("done")


class TestCognitionWorkLoopCheckpointWithImage:
    def test_checkpoint_does_not_concat_list_and_str(self):
        """work_loop 的 checkpoint 不能对图片 content parts 做 list + str。"""
        def run_fn(agent_id, **kwargs):
            return {
                "ran": True, "ok": True, "tool": "playwright",
                "run_id": "run_test", "exit_code": 0,
                "stdout": "1 passed", "stderr": "",
                "images": [{"path": "shot.png", "mime": "image/png",
                            "bytes": 10}],
            }

        class _AgentWithWork(_FakeAgent):
            def generate(self, messages=None, phase="decision", **kwargs):
                self.calls.append({"messages": messages, "phase": phase})
                if len(self.calls) == 1:
                    return _FakeResponse(json.dumps({
                        "decision": "act",
                        "reasoning": "run the browser test",
                        "action": {
                            "type": ACTION_RUN_TOOL,
                            "parameters": {"tool": "playwright",
                                           "args": ["test"]},
                        },
                    }))
                return _FakeResponse("done")

        dispatcher = ActionDispatcher([RunToolExecutor(run_fn)])
        agent = _AgentWithWork()
        run_cognition(
            agent,
            "hello",
            dispatcher=dispatcher,
            work_loop=True,
            max_work_steps=2,
            follow_up_on_reads=True,
            image_resolver=lambda run_id, path: (
                "data:image/png;base64,AAAA"),
        )
        assert len(agent.calls) == 3
        checkpoint = agent.calls[2]["messages"][-1]
        assert checkpoint["role"] == "user"
        content = checkpoint["content"]
        assert isinstance(content, list)
        assert any(part.get("type") == "image_url" for part in content)
        assert any("工作阶段结束" in str(part.get("text") or "")
                   for part in content if isinstance(part, dict))


class TestCognitionFollowUpWithImage:
    def test_tool_image_is_attached_to_follow_up(self):
        def run_fn(agent_id, **kwargs):
            return {
                "ran": True, "ok": True, "tool": "playwright",
                "run_id": "run_test", "exit_code": 0,
                "stdout": "1 passed", "stderr": "",
                "images": [{"path": "shot.png", "mime": "image/png",
                            "bytes": 10}],
            }

        dispatcher = ActionDispatcher([RunToolExecutor(run_fn)])
        agent = _FakeAgent()
        run = run_cognition(
            agent,
            "hello",
            dispatcher=dispatcher,
            follow_up_on_reads=True,
            image_resolver=lambda run_id, path: (
                "data:image/png;base64,AAAA"),
        )
        assert run.follow_up_used is True
        assert len(agent.calls) == 2
        last_user = agent.calls[1]["messages"][-1]
        assert last_user["role"] == "user"
        content = last_user["content"]
        assert isinstance(content, list)
        assert any(part.get("type") == "image_url" for part in content)

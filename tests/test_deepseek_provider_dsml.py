"""DeepSeek Provider 的 DSML 后处理：shadow / execute / 标准 tool_calls 优先。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.base import LLMResponse
from llm.deepseek import DeepSeekProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dsml"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _provider() -> DeepSeekProvider:
    return DeepSeekProvider({}, api_key_override="test-key")


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, provider="deepseek",
                       model="deepseek-flash")


def test_shadow_parses_but_keeps_content(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "shadow")
    text = _fixture("tool_help_double_bar.txt")
    response = _response(text)

    out = _provider()._augment_response(response, {"content": text})

    assert out.tool_calls_source == "dsml"
    assert len(out.tool_calls) == 2
    assert out.content == text, "shadow 不能吞掉 content"
    assert out.metadata["dsml"]["mode"] == "shadow"


def test_execute_strips_dsml_from_content(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "execute")
    text = _fixture("tool_help_double_bar.txt")
    response = _response(text)

    out = _provider()._augment_response(response, {"content": text})

    assert out.tool_calls_source == "dsml"
    assert len(out.tool_calls) == 2
    assert "DSML" not in out.content
    assert out.metadata["dsml"]["parsed"] is True


def test_standard_tool_calls_win_over_dsml(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "execute")
    text = _fixture("tool_help_double_bar.txt")
    response = _response(text)
    message = {
        "content": text,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "workspace_read",
                "arguments": json.dumps({"path": "index.html"}),
            },
        }],
    }

    out = _provider()._augment_response(response, message)

    assert out.tool_calls_source == "api"
    assert out.tool_calls[0]["function"]["name"] == "workspace_read"
    assert out.content == text, "有标准通道时不应改 content"


def test_truncated_dsml_is_reported_not_executed(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "execute")
    text = _fixture("workspace_write_double_bar.txt")
    truncated = text[: len(text) // 2]
    response = _response(truncated)

    out = _provider()._augment_response(response, {"content": truncated})

    assert out.tool_calls == []
    assert out.metadata["dsml"]["parsed"] is False
    assert out.metadata["dsml"]["error"]
    assert out.content == truncated, "解析失败不能丢原文"

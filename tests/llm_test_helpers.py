"""
LLM Provider 测试的共用工具
============================================================

【为什么要有这个文件】

    DeepSeek 和豆包共用同一个基类（OpenAI 兼容格式），
    所以它们的「假 HTTP 层」「样例响应体」完全一样。

    如果两个测试文件各写一份：
        · 改一处要改两处
        · 迟早有一份会漏改，然后两个 provider 的测试行为不一致
        · 而且看不出「哪些行为是共用的、哪些是各家特有的」

    集中在这里之后，「共用行为」由 test_provider_contract.py
    参数化地跑一遍两个 provider，各家特有的写在各自的文件里。

    文件名不匹配 test_*.py，所以 pytest 不会把它当测试文件收集。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from llm.base import LLMProvider
from llm.openai_compatible import HTTPResult


# ============================================================
# 假的 HTTP 层
# ============================================================
class FakeHTTP:
    """
    替代真实的 HTTP 调用。

    记录每一次调用的参数，这样测试可以检查
    「我们到底发出去了什么」。
    """

    def __init__(
        self,
        result: Optional[HTTPResult] = None,
        raises: Optional[BaseException] = None,
    ):
        self.result = result
        self.raises = raises
        self.calls: list = []

    def __call__(
        self,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResult:
        # 深拷贝一份，避免测试之间互相污染
        self.calls.append(
            {
                "url": url,
                "payload": json.loads(json.dumps(payload)),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )

        if self.raises is not None:
            raise self.raises

        return self.result or HTTPResult(status=200, body="{}")

    @property
    def last_call(self) -> Dict[str, Any]:
        assert self.calls, "FakeHTTP 一次都没被调用"
        return self.calls[-1]


# ============================================================
# 样例响应体（OpenAI 兼容格式，两家通用）
# ============================================================
def openai_success_body(
    content: str = "你好。",
    model: str = "some-model",
    prompt_tokens: int = 26,
    completion_tokens: int = 9,
    total_tokens: Optional[int] = 35,
    include_usage: bool = True,
    finish_reason: str = "stop",
) -> str:
    """
    拼一个 OpenAI 兼容格式的成功返回。

    故意做成可配置的，方便测各种边界：
    缺 usage、缺 total、空 content 等。
    """
    body: Dict[str, Any] = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }

    if include_usage:
        usage: Dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        if total_tokens is not None:
            usage["total_tokens"] = total_tokens
        body["usage"] = usage

    return json.dumps(body, ensure_ascii=False)


def error_body(message: str, code: str = "InvalidParameter") -> str:
    """拼一个错误返回体（火山方舟 / DeepSeek 的结构基本一致）。"""
    return json.dumps(
        {"error": {"code": code, "message": message, "type": "BadRequest"}},
        ensure_ascii=False,
    )


# ============================================================
# 构造 Provider 的便捷函数
# ============================================================
def make_provider(
    provider_class,
    monkeypatch,
    api_key_env: str,
    http: Optional[FakeHTTP] = None,
    api_key: Optional[str] = "sk-fake-key-for-tests-only-1234567890",
    config: Optional[Mapping[str, Any]] = None,
    default_model: str = "",
) -> LLMProvider:
    """
    造一个把 HTTP 层换掉的 Provider。

    统一在这里处理「Key 从环境变量来」这件事，
    免得每个测试文件各写一遍 monkeypatch。
    """
    if api_key is None:
        monkeypatch.delenv(api_key_env, raising=False)
    else:
        monkeypatch.setenv(api_key_env, api_key)

    return provider_class(
        provider_config=dict(config or {}),
        default_model=default_model,
        http_post=http or FakeHTTP(),
    )

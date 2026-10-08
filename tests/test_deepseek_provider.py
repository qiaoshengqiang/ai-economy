"""
Test: DeepSeek Provider（用假的 HTTP 层，不需要网络和 Key）
============================================================

这个文件把 DeepSeek 的接入测透，但【一次真实网络请求都不发】。

怎么做到的？
    DeepSeekProvider 的 HTTP 调用被抽成了一个可替换的函数
    （构造参数 http_post）。测试注入一个假的，
    于是「构造请求」和「解析响应」这两件事就能单独验证。

为什么值得这么设计？
    真实 API 测试有几个没法回避的问题：
        · 需要 Key，CI 上跑不了
        · 要花钱
        · 网络抖动会让测试随机失败
        · 错误码（401/429/500）很难按需复现

    把 HTTP 层做成可替换的，上面这些问题就全没了 ——
    而且还能覆盖那些真实环境里几乎不可能稳定复现的错误分支。

覆盖范围：
    1. 请求序列化（messages / model / temperature / max_tokens）
    2. 响应解析（content / usage / latency / raw_response / simulated）
    3. 错误翻译（401 / 429 / 500 / 超时 / JSON 坏 / 缺 content）
    4. API Key 安全（只从环境变量读、绝不出现在日志或异常里）
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest

from llm.base import (
    TOKEN_SOURCE_API,
    TOKEN_SOURCE_UNKNOWN,
    AuthenticationError,
    LLMError,
    ProviderDisabledError,
    InvalidResponseError,
    LLMHTTPError,
    LLMRequestError,
    LLMResponse,
    LLMTimeoutError,
    RateLimitError,
)
from llm.deepseek import DeepSeekProvider, HTTPResult, default_http_post
from llm.router import LLMRouter, build_default_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FAKE_KEY = "sk-fake-key-for-tests-only-1234567890"
API_KEY_ENV = "DEEPSEEK_API_KEY"


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
        # 深拷贝一份，避免测试之间的互相污染
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
# 构造 DeepSeek 真实返回的样例 JSON
# ============================================================
def deepseek_success_body(
    content: str = "你好，我是 DeepSeek。",
    model: str = "deepseek-chat",
    prompt_tokens: int = 26,
    completion_tokens: int = 9,
    total_tokens: Optional[int] = 35,
    include_usage: bool = True,
    finish_reason: str = "stop",
) -> str:
    """
    拼一个和 DeepSeek 官方返回结构一致的 JSON。

    故意做成可配置的，方便测各种边界（缺 usage、缺 total 等）。
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
        usage: Dict[str, Any] = {"prompt_tokens": prompt_tokens,
                                 "completion_tokens": completion_tokens}
        if total_tokens is not None:
            usage["total_tokens"] = total_tokens
        body["usage"] = usage

    return json.dumps(body, ensure_ascii=False)


def make_provider(
    monkeypatch,
    http: Optional[FakeHTTP] = None,
    api_key: Optional[str] = FAKE_KEY,
    config: Optional[Mapping[str, Any]] = None,
) -> DeepSeekProvider:
    """造一个把 HTTP 层换掉的 DeepSeekProvider。"""
    if api_key is None:
        monkeypatch.delenv(API_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(API_KEY_ENV, api_key)

    return DeepSeekProvider(
        provider_config=dict(config or {}),
        default_model="deepseek-chat",
        http_post=http or FakeHTTP(),
    )


# ============================================================
# 测试 1：初始化与配置
# ============================================================
class TestInitialization:
    def test_reads_key_from_env(self, monkeypatch):
        provider = make_provider(monkeypatch)

        assert provider.is_available() is True
        assert provider.name == "deepseek"
        assert provider.requires_api_key is True

    def test_missing_key_makes_it_unavailable(self, monkeypatch):
        provider = make_provider(monkeypatch, api_key=None)

        assert provider.is_available() is False
        reason = provider.unavailable_reason()
        assert reason
        assert API_KEY_ENV in reason

    def test_custom_key_env_name(self, monkeypatch):
        monkeypatch.setenv("MY_OWN_KEY", "sk-from-custom-env")
        monkeypatch.delenv(API_KEY_ENV, raising=False)

        provider = DeepSeekProvider(
            provider_config={"api_key_env": "MY_OWN_KEY"},
            default_model="deepseek-chat",
        )

        assert provider.is_available() is True

    def test_default_endpoint(self, monkeypatch):
        provider = make_provider(monkeypatch)

        assert provider.endpoint == "https://api.deepseek.com/chat/completions"

    def test_base_url_from_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://my-proxy.example.com/v1")
        provider = make_provider(
            monkeypatch, config={"base_url": "https://ignored.example.com"}
        )

        assert provider.base_url == "https://my-proxy.example.com/v1"
        assert provider.endpoint == "https://my-proxy.example.com/v1/chat/completions"

    def test_base_url_from_config(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        provider = make_provider(
            monkeypatch, config={"base_url": "https://config.example.com/"}
        )

        # 结尾的斜杠要被去掉，避免拼出 //chat/completions
        assert provider.endpoint == "https://config.example.com/chat/completions"

    def test_model_from_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")
        provider = make_provider(monkeypatch, config={"model": "deepseek-chat"})

        assert provider.default_model == "deepseek-reasoner"

    def test_timeout_from_config(self, monkeypatch):
        provider = make_provider(monkeypatch, config={"timeout_seconds": 12})
        assert provider.timeout == 12.0

    def test_describe_never_reveals_the_key(self, monkeypatch):
        """
        【安全】自述信息里只能说「Key 配了没有」，不能说 Key 是什么。
        """
        provider = make_provider(monkeypatch)
        info = provider.describe()

        text = json.dumps(info, ensure_ascii=False, default=str)
        assert FAKE_KEY not in text

        assert info["api_key_configured"] is True
        assert info["api_key_env"] == API_KEY_ENV


# ============================================================
# 测试 2：请求序列化
# ============================================================
class TestRequestSerialization:
    """确认我们真的把 messages 正确转成了 DeepSeek 要的 HTTP 请求。"""

    def test_sends_all_parameters(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        provider.generate(
            messages="你好",
            system="你是一个助手",
            temperature=0.3,
            max_tokens=128,
        )

        call = http.last_call
        assert call["url"] == "https://api.deepseek.com/chat/completions"
        assert call["timeout"] == 60.0

        payload = call["payload"]
        assert payload["model"] == "deepseek-chat"
        assert payload["temperature"] == 0.3
        assert payload["max_tokens"] == 128
        assert payload["stream"] is False

    def test_serializes_messages_in_order(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        provider.generate(messages="你好", system="你是 Agent A")

        messages = http.last_call["payload"]["messages"]

        assert messages == [
            {"role": "system", "content": "你是 Agent A"},
            {"role": "user", "content": "你好"},
        ]

    def test_serializes_multi_turn_conversation(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        provider.generate(messages=[
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ])

        messages = http.last_call["payload"]["messages"]
        assert len(messages) == 4
        assert [m["role"] for m in messages] == [
            "system", "user", "assistant", "user"
        ]

    def test_sends_bearer_authorization(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        provider.generate(messages="hi")

        headers = http.last_call["headers"]
        assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
        assert headers["Content-Type"] == "application/json"

    def test_explicit_model_overrides_default(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        provider.generate(messages="hi", model="deepseek-reasoner")

        assert http.last_call["payload"]["model"] == "deepseek-reasoner"

    def test_build_payload_is_testable_without_http(self, monkeypatch):
        """序列化逻辑可以脱离 HTTP 单独验证。"""
        provider = make_provider(monkeypatch)

        payload = provider.build_payload(
            messages=[{"role": "user", "content": "hi"}],
            model="deepseek-chat",
            temperature=0.5,
            max_tokens=64,
        )

        assert payload["model"] == "deepseek-chat"
        assert payload["temperature"] == 0.5
        assert payload["max_tokens"] == 64
        assert payload["stream"] is False
        assert payload["messages"] == [{"role": "user", "content": "hi"}]

    def test_unicode_is_preserved(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        provider.generate(messages="中文和 emoji 🎉")

        assert http.last_call["payload"]["messages"][0]["content"] == "中文和 emoji 🎉"


# ============================================================
# 测试 3：响应解析
# ============================================================
class TestResponseParsing:
    def test_parses_content(self, monkeypatch):
        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(content="你好，我是 DeepSeek。"))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert isinstance(response, LLMResponse)
        assert response.content == "你好，我是 DeepSeek。"

    def test_parses_real_token_usage(self, monkeypatch):
        """
        【Step 2.5 最重要的验证点之一】

        真实 API 返回的 usage 必须原样进入 LLMResponse，
        不能被 MockProvider 那套估算算法覆盖。
        """
        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(
                prompt_tokens=26, completion_tokens=9, total_tokens=35
            ))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="你好")

        assert response.input_tokens == 26
        assert response.output_tokens == 9
        assert response.total_tokens == 35

    def test_marks_usage_as_from_api(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert response.token_source == TOKEN_SOURCE_API
        assert response.usage_from_api is True
        assert response.simulated is False
        assert response.usage_is_estimated is False

    def test_keeps_latency(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert response.latency_ms >= 0.0

    def test_keeps_raw_response(self, monkeypatch):
        body = deepseek_success_body()
        http = FakeHTTP(HTTPResult(200, body))
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert isinstance(response.raw_response, dict)
        # 原始 JSON 要完整保留，方便排查
        assert response.raw_response["id"] == "chatcmpl-abc123"
        assert response.raw_response["object"] == "chat.completion"

    def test_parses_finish_reason(self, monkeypatch):
        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(finish_reason="length"))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")
        assert response.finish_reason == "length"

    def test_uses_model_name_from_response(self, monkeypatch):
        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(model="deepseek-chat-0724"))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")
        assert response.model == "deepseek-chat-0724"

    def test_provider_name_is_deepseek(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        assert provider.generate(messages="hi").provider == "deepseek"

    # ------------------------------------------------------------
    # usage 缺失的情况
    # ------------------------------------------------------------
    def test_missing_usage_stays_unknown(self, monkeypatch):
        """
        【关键】API 没返回 usage 时，token 必须是 None / unknown。

        绝不能「补一个估算值凑上」——
        因为 token 在这个项目里要换钱，把猜测当事实会让账永远对不上。
        """
        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(include_usage=False))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert response.input_tokens is None
        assert response.output_tokens is None
        assert response.total_tokens is None
        assert response.token_source == TOKEN_SOURCE_UNKNOWN
        assert response.usage_known is False
        assert response.usage_from_api is False
        # 内容本身还是拿到了
        assert response.content

    def test_missing_usage_is_not_silently_estimated(self, monkeypatch):
        """
        明确验证：缺 usage 时【没有】偷偷用估算算法顶上。
        """
        from llm.base import estimate_messages_tokens

        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(include_usage=False))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="这是一段比较长的中文输入" * 5)

        # 估算出来的数肯定不是 None
        estimate = estimate_messages_tokens(
            [{"role": "user", "content": "这是一段比较长的中文输入" * 5}]
        )
        assert estimate > 0
        assert response.input_tokens is None
        assert response.input_tokens != estimate

    def test_usage_notes_explain_the_unknown(self, monkeypatch):
        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(include_usage=False))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")
        notes = response.metadata["usage_notes"]

        assert notes.get("usage_missing") is True
        assert "warning" in notes

    def test_partial_usage_keeps_missing_fields_none(self, monkeypatch):
        """只给了一部分字段时，缺的那些保持 None。"""
        body = json.dumps({
            "model": "deepseek-chat",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10},   # 只给了输入
        })
        http = FakeHTTP(HTTPResult(200, body))
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert response.input_tokens == 10
        assert response.output_tokens is None
        # 输入有、输出没有 → 推不出总计
        assert response.total_tokens is None
        assert response.token_source == TOKEN_SOURCE_UNKNOWN

    def test_total_is_derived_only_when_both_parts_known(self, monkeypatch):
        """
        输入和输出都给全了，但缺 total —— 这时允许精确相加。

        注意这不叫「估算」：两个精确值相加还是精确值。
        """
        http = FakeHTTP(
            HTTPResult(200, deepseek_success_body(
                prompt_tokens=100, completion_tokens=50, total_tokens=None
            ))
        )
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert response.total_tokens == 150
        assert response.token_source == TOKEN_SOURCE_API
        assert "total_derived" in response.metadata["usage_notes"]

    def test_empty_content_is_allowed(self, monkeypatch):
        """
        content 是空字符串是合法的（模型可能什么都没说），
        只要字段存在就不该报错。
        """
        http = FakeHTTP(HTTPResult(200, deepseek_success_body(content="")))
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")

        assert response.content == ""
        assert response.is_empty is True

    def test_error_inside_http_200_is_detected(self, monkeypatch):
        """
        有些网关会把错误塞在 HTTP 200 里返回。
        这种也要被识别出来，不能当成正常响应。
        """
        body = json.dumps({
            "error": {"message": "Rate limit exceeded", "code": "rate_limit"}
        })
        http = FakeHTTP(HTTPResult(200, body))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMRequestError) as excinfo:
            provider.generate(messages="hi")

        assert "Rate limit exceeded" in str(excinfo.value)


# ============================================================
# 测试 4：错误翻译
# ============================================================
class TestErrorMapping:
    """
    各种失败都必须翻译成项目统一的异常体系。

    Agent 层永远不该看到 urllib.error / socket.timeout 这种东西。
    """

    def test_401_becomes_authentication_error(self, monkeypatch):
        body = json.dumps({"error": {"message": "Authentication Fails"}})
        http = FakeHTTP(HTTPResult(401, body))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(AuthenticationError):
            provider.generate(messages="hi")

    def test_403_becomes_authentication_error(self, monkeypatch):
        http = FakeHTTP(HTTPResult(403, "forbidden"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(AuthenticationError):
            provider.generate(messages="hi")

    def test_429_becomes_rate_limit_error(self, monkeypatch):
        http = FakeHTTP(HTTPResult(429, json.dumps({"error": "slow down"})))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(RateLimitError):
            provider.generate(messages="hi")

    def test_402_becomes_http_error_with_balance_hint(self, monkeypatch):
        http = FakeHTTP(HTTPResult(402, "insufficient balance"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMHTTPError) as excinfo:
            provider.generate(messages="hi")

        assert excinfo.value.status_code == 402
        assert "余额不足" in str(excinfo.value)

    def test_500_becomes_http_error(self, monkeypatch):
        http = FakeHTTP(HTTPResult(500, "internal server error"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMHTTPError) as excinfo:
            provider.generate(messages="hi")

        assert excinfo.value.status_code == 500

    def test_503_becomes_http_error(self, monkeypatch):
        http = FakeHTTP(HTTPResult(503, "unavailable"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMHTTPError):
            provider.generate(messages="hi")

    def test_400_becomes_http_error(self, monkeypatch):
        http = FakeHTTP(HTTPResult(400, "bad request"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMHTTPError) as excinfo:
            provider.generate(messages="hi")

        assert excinfo.value.status_code == 400

    def test_unexpected_status_becomes_http_error(self, monkeypatch):
        http = FakeHTTP(HTTPResult(418, "I'm a teapot"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMHTTPError):
            provider.generate(messages="hi")

    def test_timeout_becomes_timeout_error(self, monkeypatch):
        http = FakeHTTP(raises=LLMTimeoutError("请求超时（60 秒）"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMTimeoutError):
            provider.generate(messages="hi")

    def test_network_error_becomes_request_error(self, monkeypatch):
        http = FakeHTTP(raises=LLMRequestError("网络错误：连接被拒绝"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMRequestError):
            provider.generate(messages="hi")

    def test_unexpected_exception_is_wrapped(self, monkeypatch):
        """没预料到的异常也要收进异常体系，不能漏出 urllib 之类的东西。"""
        http = FakeHTTP(raises=RuntimeError("某个底层库炸了"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(LLMRequestError) as excinfo:
            provider.generate(messages="hi")

        assert "RuntimeError" in str(excinfo.value)

    # ------------------------------------------------------------
    # 响应体本身有问题
    # ------------------------------------------------------------
    def test_invalid_json_becomes_invalid_response(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, "这不是 JSON {{{"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(InvalidResponseError):
            provider.generate(messages="hi")

    def test_missing_choices_becomes_invalid_response(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, json.dumps({"id": "x", "model": "m"})))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(InvalidResponseError) as excinfo:
            provider.generate(messages="hi")

        assert "choices" in str(excinfo.value)

    def test_empty_choices_becomes_invalid_response(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, json.dumps({"choices": []})))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(InvalidResponseError):
            provider.generate(messages="hi")

    def test_missing_content_becomes_invalid_response(self, monkeypatch):
        body = json.dumps({
            "model": "deepseek-chat",
            "choices": [{"index": 0, "message": {"role": "assistant"}}],
        })
        http = FakeHTTP(HTTPResult(200, body))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(InvalidResponseError) as excinfo:
            provider.generate(messages="hi")

        assert "content" in str(excinfo.value)

    def test_missing_message_becomes_invalid_response(self, monkeypatch):
        body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop"}]})
        http = FakeHTTP(HTTPResult(200, body))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(InvalidResponseError):
            provider.generate(messages="hi")

    def test_top_level_array_is_rejected(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, json.dumps([1, 2, 3])))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(InvalidResponseError):
            provider.generate(messages="hi")

    # ------------------------------------------------------------
    def test_all_errors_are_llm_request_errors(self, monkeypatch):
        """
        所有调用失败都必须是 LLMRequestError 的子类。

        这样上层只需要 `except LLMRequestError` 就能兜住全部，
        同时还能按细分类型做不同处理（比如 401 不重试、429 重试）。
        """
        cases = [
            HTTPResult(401, "x"),
            HTTPResult(403, "x"),
            HTTPResult(429, "x"),
            HTTPResult(500, "x"),
            HTTPResult(200, "not json"),
            HTTPResult(200, json.dumps({"choices": []})),
        ]

        for result in cases:
            monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
            provider = DeepSeekProvider(
                default_model="deepseek-chat",
                http_post=FakeHTTP(result),
            )
            with pytest.raises(LLMRequestError):
                provider.generate(messages="hi")

    def test_error_count_increases(self, monkeypatch):
        http = FakeHTTP(HTTPResult(401, "x"))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(AuthenticationError):
            provider.generate(messages="hi")

        assert provider.error_count == 1
        assert provider.call_count == 0

    def test_missing_key_raises_authentication_error(self, monkeypatch):
        provider = make_provider(monkeypatch, api_key=None)

        with pytest.raises(AuthenticationError) as excinfo:
            provider.generate(messages="hi")

        assert API_KEY_ENV in str(excinfo.value)


# ============================================================
# 测试 5：API Key 安全
# ============================================================
class TestApiKeySafety:
    """Key 绝对不能泄露到任何用户能看到的地方。"""

    def test_key_never_in_success_response(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, deepseek_success_body()))
        provider = make_provider(monkeypatch, http=http)

        response = provider.generate(messages="hi")
        text = json.dumps(response.to_dict(), ensure_ascii=False, default=str)

        assert FAKE_KEY not in text

    def test_key_never_in_error_message_when_api_echoes_it(self, monkeypatch):
        """
        【防御性测试】假设 API 把整个 Key 回显在错误信息里，
        我们的 _scrub 必须把它抹掉。

        正常情况下 Key 不会出现在错误里，但泄露一次就够麻烦了。
        """
        body = json.dumps({
            "error": {"message": f"Invalid key: {FAKE_KEY} - please check"}
        })
        http = FakeHTTP(HTTPResult(401, body))
        provider = make_provider(monkeypatch, http=http)

        with pytest.raises(AuthenticationError) as excinfo:
            provider.generate(messages="hi")

        message = str(excinfo.value)
        assert FAKE_KEY not in message
        assert "***REDACTED***" in message

    def test_key_never_in_repr_or_str(self, monkeypatch):
        provider = make_provider(monkeypatch)

        assert FAKE_KEY not in repr(provider)
        assert FAKE_KEY not in str(provider)

    def test_unavailable_reason_has_no_key_value(self, monkeypatch):
        provider = make_provider(monkeypatch, api_key=None)
        reason = provider.unavailable_reason() or ""

        # 只提环境变量的名字，不提它的值
        assert API_KEY_ENV in reason
        assert "sk-" not in reason

    def test_key_is_not_stored_in_config(self, monkeypatch):
        """
        Key 只存在于 provider 内部的私有属性里，
        不能进 self.config（那会被序列化出去）。
        """
        provider = make_provider(monkeypatch)

        text = json.dumps(provider.config, ensure_ascii=False, default=str)
        assert FAKE_KEY not in text

    def test_source_code_has_no_hardcoded_key(self):
        """
        【架构守卫】源码里不许出现真实的 Key。

        检查所有 .py 文件里没有 sk- 开头的长字符串字面量
        （测试文件自己的假 Key 除外，它明显不是真的）。
        """
        import re

        pattern = re.compile(r"sk-[A-Za-z0-9]{20,}")

        for source in sorted(PROJECT_ROOT.rglob("*.py")):
            # 跳过虚拟环境
            if ".venv" in source.parts:
                continue
            # 测试文件里允许出现明显的假 Key
            if source.parent.name == "tests":
                continue

            text = source.read_text(encoding="utf-8")
            hits = pattern.findall(text)
            assert not hits, (
                f"{source.relative_to(PROJECT_ROOT)} 里出现了疑似真实 Key：{hits}"
            )

    def test_env_example_has_empty_key(self):
        """
        `.env.example` 里必须是空值，不能带真 Key。

        ⚠️ **规则变了**（Configuration V1 收口）：以前这里查的是
           `DEEPSEEK_API_KEY`（provider 级全局 fallback key）。
           收口之后 Runtime 完全从 ResolvedConfig 取凭据，
           那个 fallback 层已从 `.env` / `.env.example` 删除。

           所以守卫改成查**全部 canonical credential 变量名** ——
           覆盖面反而**更宽**（5 把 key 而不是 1 把），
           而且检查的是「配置里真正在用的那些」。
        """
        from core.config_resolver import load_resolved_config
        from core.paths import GLOBAL_CONFIG_FILE
        from llm.router import adapter_specs

        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        entries = {}
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, _, value = stripped.partition("=")
                entries[key.strip()] = value.strip()

        resolved = load_resolved_config(GLOBAL_CONFIG_FILE,
                                        adapter_specs=adapter_specs)
        canonical = {c.env for c in resolved.credentials.values()}
        assert canonical, "canonical 配置里应当有 credentials"

        # ① 每个 canonical secret 都必须有占位行，且**必须是空的**
        for env_name in sorted(canonical):
            assert env_name in entries, (
                f".env.example 缺少 canonical secret {env_name}")
            assert entries[env_name] == "", (
                f".env.example 里 {env_name} 不该填任何真实 Key")

        # ② 反过来：example 里不该出现**多余的**变量
        assert set(entries) == canonical, (
            f".env.example 与 canonical secret 不一致 —— "
            f"多余：{sorted(set(entries) - canonical)} / "
            f"缺少：{sorted(canonical - set(entries))}")


# ============================================================
# 测试 6：与 Router / 注册表集成
# ============================================================
class TestRouterIntegration:
    def test_registered_in_default_registry(self):
        registry = build_default_registry()

        assert "deepseek" in registry.implemented
        assert "deepseek" not in registry.unimplemented

    def test_router_can_create_it(self, monkeypatch, config):
        monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)

        router = LLMRouter(config)
        provider = router.get_provider("deepseek")

        assert isinstance(provider, DeepSeekProvider)

    def test_router_generate_dispatches_to_it(self, monkeypatch, config):
        """Router 按名字就能找到它 —— Router 里没有任何 DeepSeek 专属代码。"""
        monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)

        router = LLMRouter(config)
        provider = router.get_provider("deepseek")

        # 把 HTTP 层换掉，避免真发请求
        http = FakeHTTP(HTTPResult(200, deepseek_success_body(content="来自 Router")))
        provider._http_post = http

        response = router.generate(provider="deepseek", messages="hi")

        assert response.provider == "deepseek"
        assert response.content == "来自 Router"

    def test_router_reports_unavailable_without_key(self, monkeypatch, config):
        """
        没配 Key 时，Router 应该在调用【之前】就拦下来，
        并告诉用户去配哪个环境变量。

        注意异常类型是 ProviderDisabledError 而不是 LLMRequestError ——
        这个区分是刻意的：
            ProviderDisabledError  provider 根本不可用，调用还没发生
            LLMRequestError        调用真的发出去了然后失败了
        """
        monkeypatch.delenv(API_KEY_ENV, raising=False)

        router = LLMRouter(config)

        with pytest.raises(ProviderDisabledError) as excinfo:
            router.generate(provider="deepseek", messages="hi")

        assert API_KEY_ENV in str(excinfo.value)
        # 连调用都不该发生 —— 所以不应该有网络开销
        assert isinstance(excinfo.value, LLMError)

    def test_registry_create_passes_config_through(self, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
        registry = build_default_registry()

        provider = registry.create(
            "deepseek",
            {"model": "deepseek-reasoner", "timeout_seconds": 5},
            "",
        )

        assert provider.default_model == "deepseek-reasoner"
        assert provider.timeout == 5.0

    def test_real_http_flag_is_true_by_default(self, monkeypatch):
        """不注入 http_post 时，用的就是真实的 urllib 实现。"""
        monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
        provider = DeepSeekProvider(default_model="deepseek-chat")

        assert provider.describe()["real_http"] is True

    def test_injected_http_flag_is_false(self, monkeypatch):
        provider = make_provider(monkeypatch)
        assert provider.describe()["real_http"] is False


# ============================================================
# 测试 7：架构守卫（Step 2.5 的最终判据）
# ============================================================
def _code_strings_and_names(path: Path):
    """取出代码部分的字符串与标识符，排除文档字符串。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstring_nodes.add(id(value))

    strings, names = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_nodes:
                strings.append(node.value)
        if isinstance(node, ast.Name):
            names.append(node.id)
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)

    return strings, names


class TestArchitectureAfterRealProvider:
    """
    【本步骤最重要的验收】

    接入真实厂商【之后】，Agent 层依然不能出现厂商专属代码。

    这条判据的意义：
        如果接 DeepSeek 必须改 Agent，那 Step 2 的抽象就是失败的。
        现在这些测试全绿，说明抽象成立。
    """

    def test_agents_have_no_deepseek_imports(self):
        for source in sorted((PROJECT_ROOT / "agents").rglob("*.py")):
            strings, names = _code_strings_and_names(source)

            for name in names:
                if name.startswith("llm."):
                    assert "deepseek" not in name.lower(), (
                        f"{source.name} 导入了 {name}"
                    )
                assert "deepseek" not in name.lower(), (
                    f"{source.name} 的标识符 {name!r} 含 deepseek"
                )

            for text in strings:
                assert "deepseek" not in text.lower(), (
                    f"{source.name} 的代码字符串含 deepseek：{text[:60]!r}"
                )

    def test_no_provider_branching_anywhere_outside_providers(self):
        """
        core/ 和 agents/ 里不能出现针对【任何】provider 的分支判断。

        允许出现 provider 名字的只有两个地方：
            · llm/ 目录（Provider 自己的实现）
            · tests/ 目录（测试需要点名）

        注意这里检查的是【所有】已知 provider 名字，
        而不是只有 deepseek —— 否则加了豆包之后，
        `if provider == "doubao"` 就能溜进 core/ 而没人发现。
        """
        known_providers = {
            "mock", "deepseek", "doubao", "grok",
        }

        forbidden_roots = [PROJECT_ROOT / "core", PROJECT_ROOT / "agents"]

        for root in forbidden_roots:
            for source in sorted(root.rglob("*.py")):
                tree = ast.parse(source.read_text(encoding="utf-8"),
                                 filename=str(source))

                for node in ast.walk(tree):
                    if not isinstance(node, ast.Compare):
                        continue
                    for comparator in node.comparators:
                        if (isinstance(comparator, ast.Constant)
                                and isinstance(comparator.value, str)):
                            value = comparator.value.lower()
                            assert value not in known_providers, (
                                f"{source.name} 里出现了和 {value!r} 的比较，"
                                f"这是厂商绑定"
                            )

    def test_deepseek_provider_does_not_touch_storage(self):
        """Provider 不许碰数据库，哪怕它对接的是真实 API。"""
        source = PROJECT_ROOT / "llm" / "deepseek.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]

            for module in modules:
                assert not module.startswith("storage"), (
                    f"llm/deepseek.py 导入了 {module} —— Provider 不该碰数据库"
                )

    def test_llm_layer_uses_only_stdlib_for_http(self):
        """
        真实 HTTP 只用标准库，没有引入 requests / httpx。

        这是项目原则：能用标准库就不引第三方库。

        注意：HTTP 实现现在住在 llm/openai_compatible.py
        （因为豆包 / DeepSeek / Grok 共用同一套形状），
        所以这条检查要扫整个 llm/ 目录，而不是只看某一个文件 ——
        否则把代码挪个位置就能绕过检查。
        """
        llm_dir = PROJECT_ROOT / "llm"
        third_party = (
            "import requests",
            "import httpx",
            "from requests",
            "from httpx",
        )

        combined = ""
        for source in sorted(llm_dir.rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            combined += text
            for bad in third_party:
                assert bad not in text, (
                    f"{source.name} 引入了第三方 HTTP 库：{bad}"
                )

        assert "urllib" in combined, "HTTP 实现应该用标准库 urllib"


# ============================================================
# 测试 8：Mock 没被破坏
# ============================================================
class TestMockStillWorks:
    def test_mock_is_unaffected(self, monkeypatch):
        """接入真实 provider 不能影响 MockProvider。"""
        from llm.mock import MockProvider

        response = MockProvider().generate(messages="hi", system="你是 Agent A")

        assert response.content == "Mock response generated for Agent A."
        assert response.token_source == "estimated"
        assert response.simulated is True

    def test_default_provider_is_still_mock(self, config):
        """默认 provider 仍然是 mock —— 没有 Key 的人照样能用整个系统。"""
        assert LLMRouter(config).default_provider == "mock"

    def test_agents_still_use_mock_by_default(self, economy):
        from agents.brain import Brain, BrainSettings

        agent = economy.create_agent("agent_a")
        settings = BrainSettings.from_agent_config(agent.config)

        assert settings.provider == "mock"

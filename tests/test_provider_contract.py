"""
Test: OpenAI 兼容 Provider 的共享契约
============================================================

【这个文件为什么这么写】

    DeepSeek 和豆包共用同一个基类。
    它们的「形状」必须完全一致 —— 否则换 provider 就会出意外。

    test_deepseek_provider.py 里已经有 72 项测试，如果给豆包
    再抄一遍，就会出现两份几乎相同的测试。那样以后改基类，
    你得记得同时改两处，而且两份可能悄悄跑偏。

    所以这里用参数化：同一批断言，跑两个 provider。

        参数 = [DeepSeekProvider, DoubaoProvider]

    谁要是哪天偏离了契约，这里立刻红一条，并告诉你
    「是哪个 provider 在哪一点上不一致」。

【这里测什么】

    只测【共用的形状】：
        · 请求序列化
        · 响应解析
        · token usage 处理
        · 错误翻译
        · Key 安全

    各家【特有】的东西（默认模型、错误码含义、模型名规则）
    写在各自的文件里。
"""

from __future__ import annotations

import json

import pytest

from llm.base import (
    TOKEN_SOURCE_API,
    TOKEN_SOURCE_UNKNOWN,
    AuthenticationError,
    InvalidResponseError,
    LLMHTTPError,
    LLMRequestError,
    LLMResponse,
    LLMTimeoutError,
    RateLimitError,
)
from llm.doubao import DoubaoProvider
from llm.deepseek import DeepSeekProvider
from llm.openai_compatible import HTTPResult

from tests.llm_test_helpers import (
    FakeHTTP,
    error_body,
    make_provider,
    openai_success_body,
)


# ============================================================
# 参数化：两个 provider 跑同一批断言
# ============================================================
# 注意：每个 pytest.param 只给【一个】值（一个四元组）。
# 给多个值那是 @pytest.mark.parametrize 的写法，
# 用在 fixture(params=...) 上会报「名字数量对不上」。
PROVIDERS = [
    pytest.param(
        (
            DeepSeekProvider,
            "DEEPSEEK_API_KEY",
            "deepseek-chat",
            "https://api.deepseek.com/chat/completions",
        ),
        id="deepseek",
    ),
    pytest.param(
        (
            DoubaoProvider,
            "DOUBAO_API_KEY",
            "doubao-seed-1-6-250615",
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        ),
        id="doubao",
    ),
]


@pytest.fixture(params=PROVIDERS)
def provider_case(request):
    """(Provider 类, Key 环境变量名, 模型名, endpoint)"""
    return request.param


@pytest.fixture
def provider(provider_case, monkeypatch):
    cls, key_env, model, _ = provider_case
    return make_provider(
        cls, monkeypatch, key_env, config={"model": model}
    )


# ============================================================
# 1. 身份与可用性
# ============================================================
class TestIdentity:
    def test_is_a_provider(self, provider_case, provider):
        from llm.base import LLMProvider

        cls, _, _, _ = provider_case
        assert isinstance(provider, LLMProvider)
        assert provider.name == cls.name
        assert provider.requires_api_key is True

    def test_endpoint_is_correct(self, provider_case, provider):
        _, _, _, expected_endpoint = provider_case
        assert provider.endpoint == expected_endpoint

    def test_available_with_key(self, provider):
        assert provider.is_available() is True
        assert provider.unavailable_reason() is None

    def test_key_env_name_is_reported(self, provider_case, provider):
        _, key_env, _, _ = provider_case
        assert provider.api_key_env == key_env
        assert provider.describe()["api_key_configured"] is True


# ============================================================
# 2. 请求序列化
# ============================================================
class TestRequestShape:
    def test_sends_openai_compatible_payload(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        p.generate(
            messages="你好",
            system="你是一个助手",
            temperature=0.3,
            max_tokens=128,
        )

        payload = http.last_call["payload"]

        assert payload["model"] == model
        assert payload["temperature"] == 0.3
        assert payload["max_tokens"] == 128
        assert payload["stream"] is False
        assert payload["messages"] == [
            {"role": "system", "content": "你是一个助手"},
            {"role": "user", "content": "你好"},
        ]

    def test_sends_bearer_auth(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model}, api_key="sk-abc123")

        p.generate(messages="hi")

        assert http.last_call["headers"]["Authorization"] == "Bearer sk-abc123"
        assert http.last_call["headers"]["Content-Type"] == "application/json"

    def test_unicode_survives_serialization(self, provider, provider_case,
                                            monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        p.generate(messages="中文和 emoji 🎉")

        assert http.last_call["payload"]["messages"][0]["content"] == "中文和 emoji 🎉"


# ============================================================
# 3. 响应解析
# ============================================================
class TestResponseShape:
    def test_parses_content(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body(content="你好呀")))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        response = p.generate(messages="hi")

        assert isinstance(response, LLMResponse)
        assert response.content == "你好呀"
        assert response.provider == cls.name

    def test_parses_usage_from_api(self, provider_case, monkeypatch):
        """两家都必须能正确读出 usage.prompt_tokens 等字段。"""
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body(
            prompt_tokens=26, completion_tokens=9, total_tokens=35
        )))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        response = p.generate(messages="hi")

        assert response.input_tokens == 26
        assert response.output_tokens == 9
        assert response.total_tokens == 35
        assert response.token_source == TOKEN_SOURCE_API
        assert response.simulated is False

    def test_keeps_raw_response_and_latency(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        response = p.generate(messages="hi")

        assert isinstance(response.raw_response, dict)
        assert response.raw_response["object"] == "chat.completion"
        assert response.latency_ms >= 0.0

    def test_missing_usage_stays_unknown(self, provider_case, monkeypatch):
        """两家在「API 没给 usage」时都必须留 None，不许估算顶替。"""
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body(include_usage=False)))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        response = p.generate(messages="hi")

        assert response.input_tokens is None
        assert response.output_tokens is None
        assert response.total_tokens is None
        assert response.token_source == TOKEN_SOURCE_UNKNOWN
        assert response.usage_known is False

    def test_derives_total_when_both_parts_known(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body(
            prompt_tokens=100, completion_tokens=50, total_tokens=None
        )))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        response = p.generate(messages="hi")

        assert response.total_tokens == 150
        assert response.token_source == TOKEN_SOURCE_API

    def test_parses_finish_reason(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body(finish_reason="length")))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        assert p.generate(messages="hi").finish_reason == "length"

    def test_error_inside_http_200_is_detected(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, error_body("Rate limit exceeded")))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        with pytest.raises(LLMRequestError) as excinfo:
            p.generate(messages="hi")

        assert "Rate limit exceeded" in str(excinfo.value)


# ============================================================
# 4. 错误翻译（两家统一）
# ============================================================
class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (401, AuthenticationError),
            (403, AuthenticationError),
            (429, RateLimitError),
            (400, LLMHTTPError),
            (404, LLMHTTPError),
            (500, LLMHTTPError),
            (503, LLMHTTPError),
            (418, LLMHTTPError),
        ],
    )
    def test_status_codes_map_to_project_exceptions(
        self, provider_case, monkeypatch, status, expected
    ):
        """
        同一个状态码，两家必须映射到同一个异常类型。

        否则上层就没法写出「不管是哪家都能正确处理」的代码 ——
        而那正是抽象的意义。
        """
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(status, error_body(f"status {status}")))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        with pytest.raises(expected):
            p.generate(messages="hi")

    def test_timeout_maps_to_timeout_error(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(raises=LLMTimeoutError("请求超时（60 秒）"))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        with pytest.raises(LLMTimeoutError):
            p.generate(messages="hi")

    def test_unexpected_exception_is_wrapped(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(raises=RuntimeError("底层库炸了"))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        with pytest.raises(LLMRequestError) as excinfo:
            p.generate(messages="hi")

        assert "RuntimeError" in str(excinfo.value)

    @pytest.mark.parametrize(
        "body",
        [
            "这不是 JSON {{{",
            json.dumps({"id": "x"}),
            json.dumps({"choices": []}),
            json.dumps({"choices": [{"index": 0}]}),
            json.dumps({"choices": [{"message": {"role": "assistant"}}]}),
            json.dumps([1, 2, 3]),
        ],
        ids=[
            "invalid_json",
            "missing_choices",
            "empty_choices",
            "missing_message",
            "missing_content",
            "top_level_array",
        ],
    )
    def test_bad_response_bodies_are_rejected(
        self, provider_case, monkeypatch, body
    ):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, body))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        with pytest.raises(InvalidResponseError):
            p.generate(messages="hi")

    def test_error_count_tracks_failures(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(401, "x"))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model})

        with pytest.raises(AuthenticationError):
            p.generate(messages="hi")

        assert p.error_count == 1
        assert p.call_count == 0


# ============================================================
# 5. Key 安全（两家统一）
# ============================================================
class TestKeySafety:
    SECRET = "sk-super-secret-key-value-9876543210"

    def test_key_never_in_response(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model}, api_key=self.SECRET)

        response = p.generate(messages="hi")
        text = json.dumps(response.to_dict(), ensure_ascii=False, default=str)

        assert self.SECRET not in text

    def test_key_is_scrubbed_from_error_messages(self, provider_case, monkeypatch):
        """假设 API 把 Key 回显在错误里，必须被抹掉。"""
        cls, key_env, model, _ = provider_case
        body = error_body(f"Invalid key: {self.SECRET} — please check")
        http = FakeHTTP(HTTPResult(401, body))
        p = make_provider(cls, monkeypatch, key_env, http=http,
                          config={"model": model}, api_key=self.SECRET)

        with pytest.raises(AuthenticationError) as excinfo:
            p.generate(messages="hi")

        message = str(excinfo.value)
        assert self.SECRET not in message
        assert "***REDACTED***" in message

    def test_key_never_in_repr_or_describe(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        p = make_provider(cls, monkeypatch, key_env,
                          config={"model": model}, api_key=self.SECRET)

        assert self.SECRET not in repr(p)
        assert self.SECRET not in json.dumps(p.describe(), default=str)
        assert self.SECRET not in json.dumps(p.config, default=str)

    def test_missing_key_raises_authentication_error(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        p = make_provider(cls, monkeypatch, key_env,
                          config={"model": model}, api_key=None)

        with pytest.raises(AuthenticationError) as excinfo:
            p.generate(messages="hi")

        assert key_env in str(excinfo.value)
        assert "sk-" not in str(excinfo.value) or "REDACTED" in str(excinfo.value)


# ============================================================
# 6. 可配置项（两家统一支持）
# ============================================================
class TestConfigurability:
    def test_max_tokens_field_can_be_renamed(self, provider_case, monkeypatch):
        """
        请求体里「最大输出长度」的字段名可以改。

        为什么这条重要？
            实测发现某些豆包模型会忽略 max_tokens，
            必须换成 max_completion_tokens。有了这个开关，
            遇到这种情况改配置就行，不用改代码。
        """
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        p = make_provider(
            cls, monkeypatch, key_env, http=http,
            config={"model": model, "max_tokens_field": "max_completion_tokens"},
        )

        p.generate(messages="hi", max_tokens=16)

        payload = http.last_call["payload"]
        assert payload["max_completion_tokens"] == 16
        assert "max_tokens" not in payload

    def test_extra_body_is_merged_into_request(self, provider_case, monkeypatch):
        """extra_body 可以往请求里塞任意字段（各家小差异的兜底）。"""
        cls, key_env, model, _ = provider_case
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        p = make_provider(
            cls, monkeypatch, key_env, http=http,
            config={"model": model, "extra_body": {"thinking": {"type": "disabled"}}},
        )

        p.generate(messages="hi")

        assert http.last_call["payload"]["thinking"] == {"type": "disabled"}

    def test_chat_path_is_configurable(self, provider_case, monkeypatch):
        cls, key_env, model, base_endpoint = provider_case
        p = make_provider(
            cls, monkeypatch, key_env,
            config={"model": model, "chat_path": "/v2/chat"},
        )

        assert p.endpoint.endswith("/v2/chat")

    def test_timeout_is_configurable(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        p = make_provider(
            cls, monkeypatch, key_env,
            config={"model": model, "timeout_seconds": 12},
        )

        assert p.timeout == 12.0

    def test_base_url_env_overrides_config(self, provider_case, monkeypatch):
        cls, key_env, model, _ = provider_case
        base_url_env = cls.DEFAULT_BASE_URL_ENV
        monkeypatch.setenv(base_url_env, "https://proxy.example.com/v1")
        monkeypatch.setenv(key_env, "sk-x")

        p = cls(
            provider_config={"model": model, "base_url": "https://ignored.example.com"},
        )

        assert p.base_url == "https://proxy.example.com/v1"

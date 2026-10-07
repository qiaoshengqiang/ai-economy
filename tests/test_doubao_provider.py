"""
Test: 豆包 Provider（豆包特有的部分）
============================================================

共用的形状在 tests/test_provider_contract.py 里已经跑过了
（同一批断言同时跑 DeepSeek 和豆包）。

这个文件只测【豆包自己的事】：

    · 没有默认模型 —— 必须显式配置，且提示要讲清两种写法
    · 模型名的两种形态（ep- 接入点 / doubao 模型名）
    · 404 的特殊含义（接入点或模型不存在）
    · 401 的特殊提示（方舟 Key ≠ 火山引擎 AK/SK）
    · 429 的两种可能（限流 / 配额用尽）
"""

from __future__ import annotations

import json

import pytest

from llm.base import (
    AuthenticationError,
    LLMHTTPError,
    LLMRequestError,
    RateLimitError,
)
from llm.doubao import DoubaoProvider
from llm.openai_compatible import HTTPResult, OpenAICompatibleProvider
from llm.router import build_default_registry

from tests.llm_test_helpers import FakeHTTP, error_body, make_provider, openai_success_body

KEY_ENV = "DOUBAO_API_KEY"
MODEL_ENV = "DOUBAO_MODEL"


# ============================================================
# 1. 继承关系：确认它复用了共用基类
# ============================================================
class TestInheritance:
    def test_extends_openai_compatible_base(self):
        """
        豆包必须复用共用基类，而不是自己重写一套 HTTP / 解析逻辑。

        为什么这条重要？
            如果哪天有人给豆包复制了一份实现，这条测试会失败，
            提醒他「形状应该共用，别分叉」。
        """
        assert issubclass(DoubaoProvider, OpenAICompatibleProvider)

    def test_is_not_deepseek(self):
        assert DoubaoProvider is not None
        assert DoubaoProvider.name == "doubao"


# ============================================================
# 2. 模型名：豆包必须显式配置
# ============================================================
class TestModelRequirement:
    def test_has_no_default_model(self):
        """
        豆包【故意】没有默认模型。

        因为填接入点 ID 还是模型名取决于用户自己的账号设置，
        猜一个只会让人困惑（而且大概率调不通）。
        """
        assert DoubaoProvider.DEFAULT_MODEL == ""
        assert DoubaoProvider.requires_explicit_model is True

    def test_available_even_with_key_but_no_model(self, monkeypatch):
        """只配了 Key、没配模型时，不能算可用。"""
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv(MODEL_ENV, raising=False)

        provider = DoubaoProvider({"model": ""}, "")

        assert provider.is_available() is False
        assert "没有配置模型" in (provider.unavailable_reason() or "")

    def test_missing_model_message_explains_both_options(self, monkeypatch):
        """
        提示必须讲清楚两种写法。

        豆包的模型名规则比 DeepSeek 绕，光说「请配置模型」
        用户会不知道该填什么。所以提示里要有：
            ① 模型名称的例子
            ② 推理接入点 ID 的例子
            ③ 去哪里找
        """
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv(MODEL_ENV, raising=False)

        provider = DoubaoProvider({"model": ""}, "")
        message = provider.unavailable_reason() or ""

        assert "模型名称" in message
        assert "推理接入点" in message
        assert "ep-" in message              # 接入点的形态
        assert "doubao" in message           # 模型名的形态
        assert "火山方舟" in message          # 去哪里找

    def test_generate_without_model_raises_request_error(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv(MODEL_ENV, raising=False)

        provider = DoubaoProvider({"model": ""}, "", http_post=FakeHTTP())

        with pytest.raises(LLMRequestError) as excinfo:
            provider.generate(messages="hi")

        assert "模型" in str(excinfo.value)

    def test_model_from_env(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.setenv(MODEL_ENV, "doubao-from-env")

        provider = DoubaoProvider({"model": "doubao-from-config"}, "")

        assert provider.default_model == "doubao-from-env"

    def test_model_from_config(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv(MODEL_ENV, raising=False)

        provider = DoubaoProvider({"model": "doubao-from-config"}, "")

        assert provider.default_model == "doubao-from-config"


# ============================================================
# 3. 模型名的「形状」识别（排查 404 时很有用）
# ============================================================
class TestModelKindDetection:
    @pytest.mark.parametrize(
        "model,expected_fragment",
        [
            ("ep-20250101-abcdef", "推理接入点"),
            ("doubao-seed-1-6-250615", "模型名称"),
            ("something-weird", "未知格式"),
            ("", "未配置"),
        ],
    )
    def test_model_kind_is_reported(self, monkeypatch, model, expected_fragment):
        """
        describe() 里报告模型名的「形状」。

        为什么只报告形状、不报告完整值？
            因为 describe() 的输出会进日志。
            而且判断形状就足够排查「我到底填了接入点还是模型名」了。
        """
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv(MODEL_ENV, raising=False)

        provider = DoubaoProvider({"model": model}, "")
        kind = provider.describe()["model_kind"]

        assert expected_fragment in kind

    def test_model_kind_is_only_a_category(self, monkeypatch):
        """
        model_kind 只报告【类别】，不重复模型名本身。

        注意：describe() 里的 `model` 字段【是】会带上完整模型名的 ——
        那是有意为之的（providers 命令要显示它），
        而且模型名本来也不是机密。

        真正必须保密的是 API Key，那个由 test_provider_contract.py
        和 TestDoubaoErrorHints 负责验证。
        """
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv(MODEL_ENV, raising=False)
        model = "ep-20250101-some-endpoint-id"

        provider = DoubaoProvider({"model": model}, "")
        kind = provider.describe()["model_kind"]

        # 类别里不该出现完整的模型名
        assert model not in kind
        assert kind == "推理接入点 ID"


# ============================================================
# 4. 豆包特有的错误码含义
# ============================================================
class TestDoubaoErrorHints:
    def test_404_hint_mentions_model_and_endpoint(self, monkeypatch):
        """
        豆包的 404 几乎总是「模型名/接入点写错」，
        提示要具体，不能只说「找不到资源」。
        """
        http = FakeHTTP(HTTPResult(404, error_body("endpoint not found")))
        provider = make_provider(
            DoubaoProvider, monkeypatch, KEY_ENV, http=http,
            config={"model": "doubao-seed-1-6-250615"},
        )

        with pytest.raises(LLMHTTPError) as excinfo:
            provider.generate(messages="hi")

        message = str(excinfo.value)
        assert "模型名" in message or "接入点" in message
        assert "控制台" in message          # 告诉他去哪儿改
        assert excinfo.value.status_code == 404

    def test_401_hint_mentions_ark_key_vs_ak_sk(self, monkeypatch):
        """
        豆包这里有个经典坑：
        用「火山引擎账号的 AK/SK」当 API Key 会 401，
        两者不通用。提示里要提前说，省得用户查半天。
        """
        http = FakeHTTP(HTTPResult(401, error_body("invalid api key")))
        provider = make_provider(
            DoubaoProvider, monkeypatch, KEY_ENV, http=http,
            config={"model": "doubao-seed-1-6-250615"},
        )

        with pytest.raises(AuthenticationError) as excinfo:
            provider.generate(messages="hi")

        message = str(excinfo.value)
        assert "方舟" in message
        assert "AK/SK" in message or "AK" in message

    def test_429_hint_mentions_quota(self, monkeypatch):
        """豆包的 429 可能是限流，也可能是配额用完 —— 两种都要提。"""
        http = FakeHTTP(HTTPResult(429, error_body("quota exceeded")))
        provider = make_provider(
            DoubaoProvider, monkeypatch, KEY_ENV, http=http,
            config={"model": "doubao-seed-1-6-250615"},
        )

        with pytest.raises(RateLimitError) as excinfo:
            provider.generate(messages="hi")

        message = str(excinfo.value)
        assert "配额" in message
        assert "频繁" in message

    def test_error_messages_never_contain_key(self, monkeypatch):
        """错误提示里不能泄露 Key。"""
        secret = "ark-super-secret-0987654321"
        http = FakeHTTP(HTTPResult(401, error_body(f"bad key {secret}")))
        provider = make_provider(
            DoubaoProvider, monkeypatch, KEY_ENV, http=http, api_key=secret,
            config={"model": "doubao-seed-1-6-250615"},
        )

        with pytest.raises(AuthenticationError) as excinfo:
            provider.generate(messages="hi")

        message = str(excinfo.value)
        assert secret not in message
        assert "***REDACTED***" in message


# ============================================================
# 5. 默认值
# ============================================================
class TestDoubaoDefaults:
    def test_default_base_url(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv("DOUBAO_BASE_URL", raising=False)

        provider = DoubaoProvider({"model": "doubao-x"}, "")

        assert provider.base_url == "https://ark.cn-beijing.volces.com/api/v3"

    def test_endpoint(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.delenv("DOUBAO_BASE_URL", raising=False)

        provider = DoubaoProvider({"model": "doubao-x"}, "")

        assert provider.endpoint == (
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        )

    def test_label_is_human_readable(self):
        assert "豆包" in DoubaoProvider.PROVIDER_LABEL


# ============================================================
# 6. 注册表集成
# ============================================================
class TestRegistryIntegration:
    def test_registered_as_implemented(self):
        registry = build_default_registry()

        assert "doubao" in registry.implemented
        assert "doubao" not in registry.unimplemented

    def test_grok_is_still_unimplemented(self):
        """加了豆包之后，grok 仍然是「已登记但未实现」。"""
        registry = build_default_registry()

        assert "grok" in registry.unimplemented
        assert "grok" not in registry.implemented

    def test_all_implemented_providers(self):
        """
        【迁移说明】原名 `test_all_three_providers_implemented`。

            旧前提：「恰好三家已实现」。
            → 该前提**已过期**：又接入了阿里云百炼的千问 / GLM 两条。
            已更新为五项。
        """
        registry = build_default_registry()
        assert registry.implemented == ["bailian_glm", "bailian_qwen", "deepseek", "doubao", "mock"]

    def test_created_by_router(self, monkeypatch, config):
        from llm.router import LLMRouter

        monkeypatch.setenv(KEY_ENV, "sk-fake")
        monkeypatch.setenv(MODEL_ENV, "doubao-seed-1-6-250615")

        provider = LLMRouter(config).get_provider("doubao")

        assert isinstance(provider, DoubaoProvider)
        assert provider.default_model == "doubao-seed-1-6-250615"

    def test_registry_does_not_need_special_casing(self):
        """
        Router 里不该有豆包专属分支。

        豆包能跑起来，靠的是「注册表里多一行」，
        而不是「Router 里多一个 if」。
        """
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "llm" / "router.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(
                    comparator.value, str
                ):
                    assert comparator.value.lower() != "doubao", (
                        "router.py 里出现了和 'doubao' 的比较 —— 这是厂商绑定"
                    )


# ============================================================
# 7. 实测发现的配置逃生舱（用假 HTTP 验证，不联网）
# ============================================================
class TestEscapeHatches:
    """
    这两个开关是为真实踩到的坑准备的。

    实测（2025-09）在豆包某个模型上发现：
        · 传 max_tokens=16，实际输出 2586 tokens（被忽略）
          → 换成 max_completion_tokens 就正常
        · 带思考时成本是关掉思考的 160 倍
          → 用 extra_body 的 thinking 开关控制
    """

    def test_max_completion_tokens_field(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        provider = make_provider(
            DoubaoProvider, monkeypatch, KEY_ENV, http=http,
            config={
                "model": "doubao-seed-1-6-250615",
                "max_tokens_field": "max_completion_tokens",
            },
        )

        provider.generate(messages="hi", max_tokens=16)

        payload = http.last_call["payload"]
        assert payload["max_completion_tokens"] == 16
        assert "max_tokens" not in payload

    def test_thinking_can_be_disabled_via_extra_body(self, monkeypatch):
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        provider = make_provider(
            DoubaoProvider, monkeypatch, KEY_ENV, http=http,
            config={
                "model": "doubao-seed-1-6-250615",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        )

        provider.generate(messages="hi")

        assert http.last_call["payload"]["thinking"] == {"type": "disabled"}

    def test_default_request_has_no_thinking_field(self, monkeypatch):
        """默认不塞 thinking —— 不替用户做决定。"""
        http = FakeHTTP(HTTPResult(200, openai_success_body()))
        provider = make_provider(
            DoubaoProvider, monkeypatch, KEY_ENV, http=http,
            config={"model": "doubao-seed-1-6-250615"},
        )

        provider.generate(messages="hi")

        assert "thinking" not in http.last_call["payload"]

"""
Test: LLM 层（契约 / MockProvider / 注册表 / Router）
============================================================

这个文件验证 Step 2 的核心目标：

    Agent 与具体 LLM 厂商彻底解耦。

所以除了功能测试，这里还有几条【架构守卫】测试 ——
它们检查的不是「能不能跑」，而是「有没有人把厂商名字写进了不该写的地方」。

另一个重点是：Router 和 Provider 必须能被【单独】测试，
不需要数据库、不需要 Agent、不需要经济系统。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from llm.base import (
    LLMError,
    LLMProvider,
    LLMRequestError,
    LLMResponse,
    ProviderDisabledError,
    ProviderNotFoundError,
    ProviderNotImplementedError,
    estimate_messages_tokens,
    estimate_tokens,
    normalize_messages,
)
from llm.mock import MockProvider
from llm.router import LLMRouter, ProviderRegistry, build_default_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 测试 1：LLMResponse 统一结构
# ============================================================
class TestLLMResponse:
    """所有 Provider 都必须返回这个结构，所以它本身要稳。"""

    def test_has_all_required_fields(self):
        """
        需求里点名的字段一个都不能少：
            content / provider / model
            input_tokens / output_tokens / total_tokens
            latency / raw_response
        """
        response = LLMResponse(
            content="你好",
            provider="mock",
            model="mock-model-v1",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            latency_ms=12.5,
            raw_response={"raw": True},
        )

        assert response.content == "你好"
        assert response.provider == "mock"
        assert response.model == "mock-model-v1"
        assert response.input_tokens == 10
        assert response.output_tokens == 5
        assert response.total_tokens == 15
        assert response.raw_response == {"raw": True}

    def test_latency_alias_exists(self):
        """需求里叫 latency，实现里叫 latency_ms，两个都要能用。"""
        response = LLMResponse(content="x", provider="mock", model="m", latency_ms=42.0)

        assert response.latency == 42.0
        assert response.latency_ms == 42.0
        assert response.latency_seconds == pytest.approx(0.042)

    def test_created_at_is_auto_filled(self):
        response = LLMResponse(content="x", provider="mock", model="m")
        assert response.created_at

    def test_to_dict(self):
        response = LLMResponse(
            content="x", provider="mock", model="m",
            input_tokens=1, output_tokens=2, total_tokens=3,
        )
        data = response.to_dict()

        for key in (
            "content", "provider", "model",
            "input_tokens", "output_tokens", "total_tokens",
            "latency_ms", "simulated", "created_at",
        ):
            assert key in data, f"to_dict 缺少 {key}"

    def test_is_empty(self):
        assert LLMResponse(content="", provider="m", model="m").is_empty is True
        assert LLMResponse(content="  \n ", provider="m", model="m").is_empty is True
        assert LLMResponse(content="x", provider="m", model="m").is_empty is False

    def test_repr_is_readable(self):
        response = LLMResponse(content="嗨", provider="mock", model="m")
        assert "mock" in repr(response)


# ============================================================
# 测试 2：消息归一化
# ============================================================
class TestNormalizeMessages:
    """
    调用方会用各种姿势传 messages。统一在入口处收敛，
    这样每个 Provider 内部就不用各自处理格式差异。
    """

    def test_bare_string(self):
        assert normalize_messages("你好") == [{"role": "user", "content": "你好"}]

    def test_standard_list(self):
        messages = [{"role": "user", "content": "你好"}]
        assert normalize_messages(messages) == messages

    def test_system_passed_separately(self):
        result = normalize_messages("你好", system="你是 A")
        assert result == [
            {"role": "system", "content": "你是 A"},
            {"role": "user", "content": "你好"},
        ]

    def test_system_inside_messages(self):
        result = normalize_messages([
            {"role": "system", "content": "你是 A"},
            {"role": "user", "content": "你好"},
        ])
        assert len(result) == 2
        assert result[0]["role"] == "system"

    def test_none_gives_empty_list(self):
        assert normalize_messages(None) == []

    def test_role_is_lowercased(self):
        result = normalize_messages([{"role": "USER", "content": "hi"}])
        assert result[0]["role"] == "user"

    def test_invalid_role_raises(self):
        with pytest.raises(LLMRequestError):
            normalize_messages([{"role": "root", "content": "hi"}])

    def test_invalid_type_raises(self):
        with pytest.raises(LLMRequestError):
            normalize_messages(123)  # type: ignore[arg-type]

    def test_invalid_message_item_raises(self):
        with pytest.raises(LLMRequestError):
            normalize_messages([123])  # type: ignore[list-item]


# ============================================================
# 测试 3：token 估算
# ============================================================
class TestTokenEstimation:
    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_english_about_four_chars_per_token(self):
        # "Hello" = 5 字符 → 向上取整 2
        assert estimate_tokens("Hello") == 2
        # 40 个 ASCII 字符 → 10
        assert estimate_tokens("a" * 40) == 10

    def test_chinese_is_heavier_than_ascii(self):
        """
        【重要】中文不能按「4 字符 = 1 token」算。

        如果按 4 字符算，一段中文的 token 数会被低估 3 倍左右，
        而 token 在这个项目里是要换钱的（tokens_per_ai_coin），
        低估 3 倍就等于成本算错 3 倍。
        """
        chinese = "你是 Agent A，一个独立的经济主体。"
        naive = -(-len(chinese) // 4)     # 一律 4 字符/token 的错误算法

        assert estimate_tokens(chinese) > naive * 2

    def test_monotonic(self):
        """更长的文本不能算出更少的 token。"""
        previous = 0
        for length in range(0, 200, 7):
            current = estimate_tokens("x" * length)
            assert current >= previous
            previous = current

    def test_messages_include_per_message_overhead(self):
        """每条消息本身还有结构开销，所以两条消息 > 两条内容之和。"""
        one = estimate_messages_tokens([{"role": "user", "content": "hi"}])
        two = estimate_messages_tokens([
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "hi"},
        ])
        assert two > one


# ============================================================
# 测试 4：MockProvider
# ============================================================
class TestMockProvider:
    """你要求的测试 1~4 都在这里。"""

    # --- 测试 1：可以初始化 ---
    def test_can_initialize_without_api_key(self):
        provider = MockProvider()

        assert provider.name == "mock"
        assert provider.requires_api_key is False
        assert provider.is_available() is True
        assert provider.default_model == "mock-model-v1"

    def test_reads_model_from_config(self):
        provider = MockProvider({"model": "custom-mock-9"}, default_model="ignored")
        assert provider.default_model == "custom-mock-9"

    # --- 测试 2：可以 generate ---
    def test_can_generate(self):
        provider = MockProvider()
        response = provider.generate(messages="Hello")

        assert response is not None
        assert response.content

    # --- 测试 3：返回 LLMResponse ---
    def test_returns_llm_response(self):
        response = MockProvider().generate(messages="Hello")
        assert isinstance(response, LLMResponse)

    # --- 测试 4：token usage 存在 ---
    def test_token_usage_present(self):
        response = MockProvider().generate(messages="Hello, Agent A.")

        assert response.input_tokens > 0
        assert response.output_tokens > 0
        assert response.total_tokens > 0
        assert response.total_tokens == response.input_tokens + response.output_tokens

    # --- 内容生成 ---
    def test_detects_agent_name_from_system_prompt(self):
        """需求里的例子：system 里写了 Agent 名字，回复里要体现出来。"""
        response = MockProvider().generate(
            messages="Hello",
            system="你是 Agent A，一个独立的经济主体。",
        )
        assert response.content == "Mock response generated for Agent A."

    def test_detects_english_agent_name(self):
        response = MockProvider().generate(
            messages="hi",
            system="You are Agent B",
        )
        assert "Agent B" in response.content

    def test_echoes_user_message_without_system(self):
        """没有 system prompt 时回显用户输入，证明 messages 真的传进来了。"""
        response = MockProvider().generate(messages="Hello")
        assert response.content == "Mock response: Hello"

    def test_is_not_hardcoded_to_one_string(self):
        """
        MockProvider 不能只会处理某一句写死的话。
        不同的输入必须给出不同的响应。
        """
        provider = MockProvider()
        first = provider.generate(messages="AAA").content
        second = provider.generate(messages="BBB").content

        assert first != second
        assert "AAA" in first
        assert "BBB" in second

    def test_fixed_content_override(self):
        """测试里需要完全可预测的输出时，可以钉死回复。"""
        provider = MockProvider(fixed_content="每次都是这句")
        assert provider.generate(messages="任意").content == "每次都是这句"

    def test_fixed_token_override(self):
        provider = MockProvider(fixed_input_tokens=111, fixed_output_tokens=222)
        response = provider.generate(messages="hi")

        assert response.input_tokens == 111
        assert response.output_tokens == 222
        assert response.total_tokens == 333

    # --- 元信息 ---
    def test_marks_itself_as_simulated(self):
        """
        Mock 必须自报家门说是模拟数据。

        因为以后要拿这些 token 数去算钱 ——
        如果分不清模拟和真实，就会拿假数据去对真账单。
        """
        response = MockProvider().generate(messages="hi")
        assert response.simulated is True

    def test_records_latency(self):
        response = MockProvider().generate(messages="hi")
        assert response.latency_ms >= 0.0

    def test_keeps_raw_response(self):
        """raw_response 这条链路也要真的走一遍，不能永远是 None。"""
        response = MockProvider().generate(messages="hi", system="你是 A")
        assert response.raw_response is not None
        assert response.raw_response["mock"] is True

    def test_passes_temperature_and_max_tokens_through(self):
        response = MockProvider().generate(
            messages="hi", temperature=0.1, max_tokens=99
        )
        assert response.raw_response["temperature"] == 0.1
        assert response.raw_response["max_tokens"] == 99

    def test_counts_calls(self):
        provider = MockProvider()
        assert provider.call_count == 0

        provider.generate(messages="1")
        provider.generate(messages="2")
        assert provider.call_count == 2

    def test_generate_accepts_plain_string_list_and_system(self):
        """三种传参姿势都要能用。"""
        provider = MockProvider()

        assert provider.generate(messages="hi").content
        assert provider.generate(messages=[{"role": "user", "content": "hi"}]).content
        assert provider.generate(messages=None, system="你是 A").content

    def test_does_not_touch_database(self):
        """Provider 不应该有任何数据库相关的属性或方法。"""
        provider = MockProvider()
        for name in ("db", "database", "recorder", "wallet", "ledger"):
            assert not hasattr(provider, name), (
                f"Provider 不应该有 {name} —— "
                f"它只负责把 messages 变成 LLMResponse"
            )


# ============================================================
# 测试 5：Provider 契约
# ============================================================
class TestProviderContract:
    def test_cannot_instantiate_abstract_base(self):
        """抽象基类不能被直接实例化 —— 否则契约就是空的。"""
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_subclass_must_implement_generate(self):
        class Incomplete(LLMProvider):
            name = "incomplete"

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_mock_signature_matches_base(self):
        """
        MockProvider 的 generate 签名必须和基类一致。

        如果签名不一致，以后换成真实 Provider 时调用方就可能报错 ——
        而这种错误在换的那一天才会暴露，代价很大。
        """
        import inspect

        base = inspect.signature(LLMProvider.generate)
        mock = inspect.signature(MockProvider.generate)

        assert list(base.parameters) == list(mock.parameters)

    def test_base_provides_token_estimation_helpers(self):
        """真实 Provider 拿不到 usage 字段时，可以复用基类的估算工具。"""
        from llm import base

        assert base.estimate_tokens("hello") > 0

    def test_resolve_model_falls_back_to_default(self):
        provider = MockProvider({"model": "m1"}, default_model="m2")
        assert provider.resolve_model() == "m1"
        assert provider.resolve_model("explicit") == "explicit"


# ============================================================
# 测试 6：注册表
# ============================================================
class TestProviderRegistry:
    def test_default_registry_has_mock(self):
        registry = build_default_registry()
        assert "mock" in registry.implemented
        assert registry.is_registered("mock")

    def test_default_registry_declares_future_providers(self):
        """deepseek / doubao / grok 要「已登记但未实现」，而不是「不存在」。"""
        registry = build_default_registry()

        for name in ("deepseek", "doubao", "grok"):
            assert name in registry.unimplemented
            assert registry.is_known(name)
            assert not registry.is_registered(name)

    def test_create_mock(self):
        provider = build_default_registry().create("mock", {}, "mock-model-v1")
        assert isinstance(provider, MockProvider)

    def test_unknown_provider_raises_not_found(self):
        with pytest.raises(ProviderNotFoundError):
            build_default_registry().create("deepsek", {}, "")

    def test_unimplemented_provider_raises_not_implemented(self):
        with pytest.raises(ProviderNotImplementedError):
            build_default_registry().create("deepseek", {}, "deepseek-chat")

    def test_unimplemented_error_tells_you_what_to_do(self):
        """
        报错信息必须告诉用户下一步做什么，
        而不是只说一句「未实现」让人去猜。
        """
        with pytest.raises(ProviderNotImplementedError) as excinfo:
            build_default_registry().create("deepseek", {}, "")

        message = str(excinfo.value)
        assert "llm/deepseek.py" in message
        assert "DeepSeekProvider" in message

    def test_registering_new_provider_needs_no_router_change(self):
        """
        【本阶段最重要的架构测试】

        加一个新厂商，只需要「注册」这一步。
        Router 的代码一行都不用改 —— 这就是注册表相对
        if/elif 链的核心好处。
        """

        class FakeProvider(LLMProvider):
            name = "fake"
            requires_api_key = False

            def generate(self, messages=None, system=None, model=None,
                         temperature=0.7, max_tokens=2048, **kwargs):
                normalized = normalize_messages(messages, system=system)
                return LLMResponse(
                    content=f"fake 收到了 {len(normalized)} 条消息",
                    provider=self.name,
                    model=self.resolve_model(model),
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                )

        router = LLMRouter({}, registry=build_default_registry())
        assert "fake" not in router.registry.implemented

        # 只要这一行
        router.registry.register("fake", FakeProvider)

        assert "fake" in router.registry.implemented
        response = router.generate(provider="fake", messages="你好")
        assert response.content == "fake 收到了 1 条消息"
        assert response.provider == "fake"

    def test_register_clears_unimplemented_mark(self):
        """先登记为未实现，后来实现了，就不该再报「未实现」。"""
        registry = build_default_registry()
        assert "deepseek" in registry.unimplemented

        registry.register("deepseek", MockProvider)

        assert "deepseek" in registry.implemented
        assert "deepseek" not in registry.unimplemented
        # 真的能造出来了
        assert isinstance(registry.create("deepseek", {}, ""), MockProvider)

    def test_create_rejects_non_provider(self):
        registry = ProviderRegistry()
        registry.register("bad", lambda cfg, model: "我不是 Provider")

        with pytest.raises(LLMError):
            registry.create("bad", {}, "")

    def test_names_are_case_insensitive(self):
        registry = build_default_registry()
        assert registry.is_registered("MOCK")
        assert isinstance(registry.create("MoCk", {}, ""), MockProvider)


# ============================================================
# 测试 7：Router
# ============================================================
class TestLLMRouter:
    # --- 测试 5：Router 可以找到 mock provider ---
    def test_router_finds_mock(self, config):
        router = LLMRouter(config)
        provider = router.get_provider("mock")

        assert isinstance(provider, MockProvider)
        assert provider.name == "mock"

    def test_generate_with_explicit_provider(self, config):
        response = LLMRouter(config).generate(provider="mock", messages="hi")
        assert response.provider == "mock"

    def test_generate_uses_configured_default(self, config):
        """不指定 provider 时，用配置里的 llm.default_provider。"""
        router = LLMRouter(config)
        assert router.default_provider == "mock"

        response = router.generate(messages="hi")
        assert response.provider == "mock"

    def test_works_with_plain_dict_config(self):
        """
        llm/ 这个包不应该依赖 core.config，
        所以传普通 dict 也必须能用 —— 这样单测最省事。
        """
        router = LLMRouter({
            "llm": {
                "default_provider": "mock",
                "providers": {"mock": {"model": "dict-mock"}},
            }
        })

        assert router.default_provider == "mock"
        assert router.provider_model("mock") == "dict-mock"

        response = router.generate(messages="hi")
        assert response.model == "dict-mock"

    def test_caches_provider_instances(self, config):
        """同一个 provider 不该每次调用都新建实例。"""
        router = LLMRouter(config)

        first = router.get_provider("mock")
        second = router.get_provider("mock")

        assert first is second
        assert first.call_count == 0
        first.generate(messages="hi")
        assert second.call_count == 1

    def test_disabled_provider_raises(self):
        router = LLMRouter({
            "llm": {
                "default_provider": "mock",
                "providers": {"mock": {"enabled": False, "model": "m"}},
            }
        })

        with pytest.raises(ProviderDisabledError):
            router.generate(messages="hi")

    def test_missing_enabled_key_means_enabled(self):
        """配置里没写 enabled 时应该当作开启，避免莫名其妙用不了。"""
        router = LLMRouter({
            "llm": {"default_provider": "mock", "providers": {"mock": {"model": "m"}}}
        })
        assert router.is_enabled("mock") is True
        assert router.generate(messages="hi").provider == "mock"

    def test_unimplemented_error_reaches_caller(self, config):
        """走 Router 调用未实现的 provider，要抛出正确的异常类型。"""
        with pytest.raises(ProviderNotImplementedError):
            LLMRouter(config).generate(provider="deepseek", messages="hi")

    def test_available_providers(self, config):
        router = LLMRouter(config)
        assert "mock" in router.available_providers()
        assert "deepseek" not in router.available_providers()

    def test_describe(self, config):
        info = LLMRouter(config).describe()

        assert info["default_provider"] == "mock"
        assert "mock" in info["implemented"]
        assert "deepseek" in info["unimplemented"]

    def test_passes_all_parameters_through(self, config):
        """system / temperature / max_tokens / model 都要真的传下去。"""
        response = LLMRouter(config).generate(
            messages="hi",
            system="你是 Agent Z",
            temperature=0.33,
            max_tokens=777,
        )

        assert response.raw_response["temperature"] == 0.33
        assert response.raw_response["max_tokens"] == 777
        # system 传下去了 —— 所以 Mock 认出了 Agent Z
        assert "Agent Z" in response.content

    def test_routing_does_not_need_agent_or_database(self, config):
        """
        Router 必须能脱离 Agent 和经济系统单独工作。
        这条测试就是「传一个 config，别的什么都不给」。
        """
        router = LLMRouter(config)
        response = router.generate(messages="裸调用")
        assert isinstance(response, LLMResponse)


# ============================================================
# 测试 8：架构守卫
# ============================================================
def _code_strings_and_names(path: Path):
    """
    取出一个 Python 文件里【代码部分】的字符串和标识符，
    排除文档字符串和注释。

    为什么要排除文档字符串？
        因为 agents/base_agent.py 的注释里写着
        「没有 DeepSeek、没有 Doubao、没有 Grok」——
        那句话恰恰是在解释【为什么代码里不该出现厂商名字】。
        如果连注释都算违规，这条守卫就会把自己的说明文档给判成违规。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # 收集所有 docstring 节点的 id
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstring_nodes.add(id(value))

    strings = []
    names = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_nodes:
                strings.append(node.value)

        # 标识符：变量名、属性名、函数名
        if isinstance(node, ast.Name):
            names.append(node.id)
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        # import 的模块名
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)

    return strings, names


VENDOR_NAMES = (
    "deepseek",
    "doubao",
    "grok",
    "openai",
    "claude",
    "anthropic",
    "gpt",
    "qwen",
    "gemini",
    "豆包",
    "通义",
)


class TestArchitectureGuards:
    """
    这些测试不检查功能，检查【边界有没有被破坏】。

    它们比功能测试更重要 —— 功能写错了会报错，
    边界破了往往不会报错，只会慢慢烂掉。
    """

    def test_agent_layer_has_no_vendor_names_in_code(self):
        """
        【本阶段第一验收标准】

        agents/ 目录下的【代码】里不能出现任何厂商名字。

        为什么这么严？
            因为 Agent 一旦知道厂商名字，就一定会有人写出
                if agent.provider == "deepseek": ...
            这种代码。那一刻，Agent 和厂商就绑死了，
            以后换模型必须改 Agent —— 而这正是本项目要避免的。

        注意：注释和文档字符串不算 —— 它们只是在解释设计。
        """
        violations = []

        for source in sorted((PROJECT_ROOT / "agents").rglob("*.py")):
            strings, names = _code_strings_and_names(source)

            for text in strings:
                lowered = text.lower()
                for vendor in VENDOR_NAMES:
                    if vendor in lowered:
                        violations.append(
                            f"{source.name}: 代码字符串里出现厂商名 {vendor!r} "
                            f"→ {text[:60]!r}"
                        )

            for name in names:
                # 用【子串】匹配而不是精确相等。
                #
                # 为什么？因为真正的风险长这样：
                #     DEEPSEEK_DEFAULT = "..."      ← 精确匹配抓不到
                #     use_doubao_api(...)           ← 精确匹配也抓不到
                #     grok_client
                # 这些都等于把厂商绑进了 Agent 层。
                lowered_name = name.lower()
                for vendor in VENDOR_NAMES:
                    if vendor in lowered_name:
                        violations.append(
                            f"{source.name}: 标识符 {name!r} 里含厂商名 {vendor!r}"
                        )

        assert not violations, (
            "agents/ 里出现了厂商名字。Agent 必须与厂商彻底解耦。\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_provider_layer_imports_no_storage(self):
        """
        Provider 不许碰数据库。

        如果 Provider 能写 llm_calls，它就会开始「顺便」算钱，
        然后经济逻辑就散落到各个厂商实现里去了 ——
        换一家厂商就要重新实现一遍记账，迟早不一致。
        """
        violations = []

        for source in sorted((PROJECT_ROOT / "llm").rglob("*.py")):
            # recorder.py 是唯一合法的例外：它的职责就是记账。
            if source.name == "recorder.py":
                continue

            text = source.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(source))

            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]

                for module in modules:
                    if module.startswith("storage"):
                        violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "llm/ 里（除 recorder.py 外）不应该导入 storage：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_router_has_no_if_elif_provider_chain(self):
        """
        Router 里不能有 if provider == "xxx" 这种链子。

        用注册表之后，加厂商不需要改 Router。
        如果这里失败了，说明有人图省事又写回了 if/elif ——
        那样每加一家厂商都要动 Router，就失去了扩展性。
        """
        tree = ast.parse((PROJECT_ROOT / "llm" / "router.py").read_text(encoding="utf-8"))

        comparisons = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    comparisons.append(comparator.value.lower())

        vendor_hits = [c for c in comparisons if c in VENDOR_NAMES]

        assert not vendor_hits, (
            f"router.py 里出现了和厂商名字的比较：{vendor_hits}。\n"
            f"请改用注册表（registry.register）。"
        )

    def test_only_mock_is_actually_implemented(self):
        """
        本阶段不应该偷偷实现真实厂商。

        验收标准里写明了：只让 MockProvider 真正工作。
        """
        registry = build_default_registry()
        assert registry.implemented == ["mock"]

    def test_llm_layer_does_not_import_agents_or_economy(self):
        """
        依赖方向必须是单向的：agents → llm，core 独立。
        llm/ 不能反过来依赖 agents/ 或 core.economy。

        否则就会出现循环依赖，而且「外部资源」的概念也乱了 ——
        电力公司不该知道工厂的内部结构。
        """
        violations = []

        for source in sorted((PROJECT_ROOT / "llm").rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(("agents", "core.economy")):
                            violations.append(f"{source.name} 导入了 {alias.name}")

                if module and module.startswith(("agents", "core.economy")):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "llm/ 不应该依赖 agents/ 或 core.economy：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

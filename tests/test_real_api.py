"""
Test: 真实 DeepSeek API 集成测试
============================================================

【这个文件默认不跑】

    它需要网络，需要真实 API Key，而且每次调用都真的花钱。
    所以它被标记成 `real_api`，默认的 `./dev.sh test` 会把它排除掉。

    跑它要用：

        ./dev.sh test-real deepseek

    没有配 Key 时会显示：

        Skipped: DEEPSEEK_API_KEY not configured

    而不是失败 —— 这是刻意的：没有 Key 的人不该看到一片红。

【它验证什么】

    前面那些测试用假的 HTTP 层验证了「我们的解析逻辑对不对」。
    这个文件验证的是另一件事，而且是假 HTTP 永远验证不了的：

        真实的 DeepSeek 返回，和我们解析代码的假设一致吗？

    具体来说：
        · 真实返回里真的有 choices[0].message.content 吗？
        · usage 的三个字段真的叫 prompt_tokens / completion_tokens /
          total_tokens 吗？
        · 我们估算的 token 数和真实 usage 差多少？
        · 整条 Agent → Brain → Router → DeepSeek 链路真的通吗？

    任何一条对不上，都说明我们的假设错了 —— 而这种错
    只有真调一次才会暴露。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from llm.base import (
    TOKEN_SOURCE_API,
    LLMError,
    LLMResponse,
)
from llm.deepseek import DeepSeekProvider
from llm.router import LLMRouter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

API_KEY_ENV = "DEEPSEEK_API_KEY"

# 所有测试都打上 real_api 标记
pytestmark = pytest.mark.real_api


def has_api_key() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


# ============================================================
# 【关键】没有 Key 就整体跳过，而不是失败
# ============================================================
if not has_api_key():
    pytest.skip(
        "Skipped: DEEPSEEK_API_KEY not configured",
        allow_module_level=True,
    )


# ============================================================
# 夹具
# ============================================================
@pytest.fixture(scope="module")
def provider(config) -> DeepSeekProvider:
    """
    真实的 DeepSeekProvider，用真实的 urllib HTTP 层。

    注意：这里不加任何 http_post 注入 —— 就是要真发请求。
    """
    return DeepSeekProvider(
        provider_config=config.get("llm.providers.deepseek", {}) or {},
        default_model="deepseek-chat",
    )


@pytest.fixture(scope="module")
def router(config) -> LLMRouter:
    return LLMRouter(config)


# 为了省钱：所有真实调用都把输出限制得很短
CHEAP_MAX_TOKENS = 32


# ============================================================
# 1. 最基本的连通性
# ============================================================
class TestRealConnectivity:
    def test_provider_is_available(self, provider):
        assert provider.is_available() is True
        assert provider.unavailable_reason() is None

    def test_simple_call_returns_response(self, provider):
        response = provider.generate(
            messages="用一句话回答：1+1 等于几？",
            max_tokens=CHEAP_MAX_TOKENS,
        )

        assert isinstance(response, LLMResponse)
        assert response.provider == "deepseek"
        assert response.content.strip(), "真实返回的内容不该是空的"

    def test_response_has_model_name(self, provider):
        response = provider.generate(messages="说：好", max_tokens=CHEAP_MAX_TOKENS)

        assert response.model
        # DeepSeek 会返回类似 deepseek-chat 的模型名
        assert "deepseek" in response.model.lower()

    def test_latency_is_measured(self, provider):
        response = provider.generate(messages="说：好", max_tokens=CHEAP_MAX_TOKENS)

        # 真实网络调用不可能 0 毫秒
        assert response.latency_ms > 0

    def test_raw_response_is_kept(self, provider):
        response = provider.generate(messages="说：好", max_tokens=CHEAP_MAX_TOKENS)

        assert isinstance(response.raw_response, dict)
        # 这几项是 DeepSeek 返回结构的锚点 —— 它们变了就说明接口改了
        assert "choices" in response.raw_response
        assert "model" in response.raw_response


# ============================================================
# 2. 真实 token usage（Step 2.5 的核心验证点）
# ============================================================
class TestRealTokenUsage:
    def test_usage_is_present_and_from_api(self, provider):
        """
        【本步骤最重要的一条】

        DeepSeek 真的会返回 usage 吗？字段名真的是
        prompt_tokens / completion_tokens / total_tokens 吗？
        """
        response = provider.generate(
            messages="用一句话介绍你自己。",
            max_tokens=CHEAP_MAX_TOKENS,
        )

        # 字段名如果错了，这里会是 None
        assert response.input_tokens is not None, (
            "没有拿到 input_tokens —— "
            "可能 usage 字段名和我们的假设不一致。"
            f"原始返回的键：{sorted((response.raw_response or {}).keys())}"
        )
        assert response.output_tokens is not None
        assert response.total_tokens is not None

    def test_usage_is_marked_as_api_data(self, provider):
        response = provider.generate(messages="说：好", max_tokens=CHEAP_MAX_TOKENS)

        assert response.token_source == TOKEN_SOURCE_API
        assert response.usage_from_api is True
        assert response.simulated is False, "真实调用不能被标成模拟"

    def test_token_counts_are_sane(self, provider):
        response = provider.generate(
            messages="用一句话介绍你自己。",
            max_tokens=CHEAP_MAX_TOKENS,
        )

        assert response.input_tokens > 0
        assert response.output_tokens > 0
        assert response.total_tokens >= response.input_tokens
        assert response.total_tokens >= response.output_tokens
        # 输出被 max_tokens 限制住了，不该离奇地大
        assert response.output_tokens <= CHEAP_MAX_TOKENS + 5

    def test_total_matches_the_parts(self, provider):
        response = provider.generate(messages="说：好", max_tokens=CHEAP_MAX_TOKENS)

        assert response.total_tokens == (
            response.input_tokens + response.output_tokens
        )

    def test_real_usage_differs_from_our_estimate(self, provider):
        """
        对比「我们的估算」和「真实 usage」。

        这不是断言谁对谁错，而是把差距记录下来 ——
        因为以后算钱要用这个数字，你得知道估算能差多少。

        期待的结论：我们按中日韩字符加权后的估算，
        应该和真实值同一个量级（差距在 2 倍以内），
        而不是差好几倍。
        """
        from llm.base import estimate_messages_tokens, normalize_messages

        prompt = "用一句话介绍你自己。"
        response = provider.generate(messages=prompt, max_tokens=CHEAP_MAX_TOKENS)

        normalized = normalize_messages(prompt, system=None)
        our_estimate = estimate_messages_tokens(normalized)

        print(
            f"\n  [估算对比] 我们估算 {our_estimate} / "
            f"真实 {response.input_tokens} tokens"
        )

        # 只要在同一个量级就算通过（容差给得很宽）
        assert our_estimate > 0
        ratio = our_estimate / response.input_tokens
        assert 0.2 < ratio < 5.0, (
            f"估算({our_estimate}) 和真实({response.input_tokens}) "
            f"差了 {ratio:.1f} 倍，量级不对"
        )


# ============================================================
# 3. 通过 Router
# ============================================================
class TestThroughRouter:
    def test_router_dispatches_to_deepseek(self, router):
        response = router.generate(
            provider="deepseek",
            messages="说：好",
            max_tokens=CHEAP_MAX_TOKENS,
        )

        assert response.provider == "deepseek"
        assert response.usage_from_api is True

    def test_router_passes_system_prompt(self, router):
        response = router.generate(
            provider="deepseek",
            messages="你是谁？",
            system="你叫测试机器人，请用一句话回答。",
            max_tokens=CHEAP_MAX_TOKENS,
        )

        assert response.content.strip()

    def test_router_uses_config_model(self, router):
        provider = router.get_provider("deepseek")

        assert provider.default_model == "deepseek-chat"


# ============================================================
# 4. 完整链路：Agent → Brain → Router → DeepSeek
# ============================================================
class TestFullAgentChain:
    def test_agent_generates_through_real_deepseek(self, economy, config):
        """
        【抽象是否成立的最终判据】

        让一个真实的 Agent 用真实的 DeepSeek 思考一次。
        如果这需要改 Agent 的任何一行代码，抽象就是失败的。

        注意这里 Agent 的配置里 provider 仍然写着 "mock" ——
        我们只是通过 generate(provider=...) 临时指定用 deepseek，
        这正是「运行时换模型不用改代码」的证明。
        """
        from agents.brain import Brain, BrainSettings
        from llm import LLMCallRecorder

        agent = economy.create_agent("agent_a")

        recorder = LLMCallRecorder(
            db=economy.db,
            tokens_per_ai_coin=economy.tokens_per_ai_coin,
        )
        brain = Brain(
            router=LLMRouter(config),
            settings=BrainSettings.from_agent_config(agent.config),
            recorder=recorder,
        )
        agent.attach_brain(brain)

        # Agent 的代码不知道 DeepSeek 存在；这里只是换了个字符串
        assert agent.config.provider == "mock"

        response = agent.generate(
            messages="用一句话说明你是什么。",
            provider="deepseek",
            max_tokens=CHEAP_MAX_TOKENS,
        )

        # 拿到的仍然是统一的 LLMResponse
        assert isinstance(response, LLMResponse)
        assert response.provider == "deepseek"
        assert response.content.strip()
        assert response.usage_from_api is True

    def test_real_call_is_recorded_in_llm_calls(self, economy, config):
        """真实调用的 token 用量必须落到数据库里。"""
        from agents.brain import Brain, BrainSettings
        from llm import LLMCallRecorder

        agent = economy.create_agent("agent_b")

        recorder = LLMCallRecorder(
            db=economy.db,
            tokens_per_ai_coin=economy.tokens_per_ai_coin,
        )
        agent.attach_brain(
            Brain(
                router=LLMRouter(config),
                settings=BrainSettings.from_agent_config(agent.config),
                recorder=recorder,
            )
        )

        agent.generate(
            messages="说：好",
            provider="deepseek",
            max_tokens=CHEAP_MAX_TOKENS,
        )

        rows = recorder.recent(agent_id="agent_b")
        assert len(rows) == 1

        row = rows[0]
        assert row["provider"] == "deepseek"
        assert row["simulated"] == 0
        assert row["token_source"] == "api"
        assert row["total_tokens"] > 0
        assert row["latency_ms"] > 0

    def test_real_call_does_not_touch_wallet_or_ledger(self, economy, config):
        """
        【本阶段的边界】即使是真实花钱的调用，也【不】扣 AI 币。

        只记 token，不接经济系统 —— 那是 Step 6 的事。
        """
        from agents.brain import Brain, BrainSettings
        from llm import LLMCallRecorder

        agent = economy.create_agent("agent_c")

        recorder = LLMCallRecorder(
            db=economy.db, tokens_per_ai_coin=economy.tokens_per_ai_coin
        )
        agent.attach_brain(
            Brain(
                router=LLMRouter(config),
                settings=BrainSettings.from_agent_config(agent.config),
                recorder=recorder,
            )
        )

        balance_before = agent.balance
        ledger_before = economy.ledger.count()

        agent.generate(
            messages="说：好", provider="deepseek", max_tokens=CHEAP_MAX_TOKENS
        )

        assert agent.balance == balance_before, "本阶段不该扣钱"
        assert economy.ledger.count() == ledger_before, "账本不该变化"
        assert economy.audit()["balanced"] is True


# ============================================================
# 5. 真实错误路径
# ============================================================
class TestRealErrorPaths:
    def test_bad_key_gives_authentication_error(self, config, monkeypatch):
        """
        用一个格式合法但无效的 Key，验证真实的 401 会被正确翻译。

        这是唯一能验证「真实 401 长什么样」的办法。
        """
        from llm.base import AuthenticationError

        monkeypatch.setenv(API_KEY_ENV, "sk-" + "0" * 32)

        bad_provider = DeepSeekProvider(
            provider_config=config.get("llm.providers.deepseek", {}) or {},
            default_model="deepseek-chat",
        )

        with pytest.raises(AuthenticationError):
            bad_provider.generate(messages="hi", max_tokens=8)

    def test_timeout_is_configurable(self, provider):
        """超时设置真的被用上了（这里只验证值被读到，不发慢请求）。"""
        assert provider.timeout > 0

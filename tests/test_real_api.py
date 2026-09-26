"""
Test: 真实 API 集成测试（DeepSeek + 豆包）
============================================================

【这个文件默认不跑】

    它需要网络、需要真实 API Key，而且每次调用都真的花钱。
    所以被打上 `real_api` 标记，默认的 `./dev.sh test` 会排除它。

    跑它要用：

        ./dev.sh test-real deepseek
        ./dev.sh test-real doubao

    没配 Key 的 provider 会被单独 skip，而不是让整份报告变红。

【它验证什么（假 HTTP 层永远验证不了的）】

    test_deepseek_provider.py / test_provider_contract.py
    验证的是「我们的解析逻辑对不对」。
    这个文件验证的是另一件事：

        真实 API 的返回，和我们解析代码的假设一致吗？

    具体：
        · 真实返回里真的有 choices[0].message.content 吗？
        · usage 的字段名真的叫 prompt_tokens / completion_tokens /
          total_tokens 吗？
        · 我们估算的 token 和真实 usage 差多少？
        · Agent → Brain → Router → 真实 API 整条链路通不通？

    任何一条对不上，都说明假设错了 —— 而这种错只有真调一次才会暴露。

【关于豆包的 thinking（省钱要点）】

    实测发现豆包某些模型默认开启「思考」：
        同一个问题，带思考输出约 2500 tokens，
        关掉思考只输出十几 tokens，而可见答案几乎一样。

    所以这个文件里豆包的默认测试配置会关掉思考 ——
    既快又省。另外单独留了一条测试去测量这个差异，
    因为「同样的答案成本差上百倍」这件事，
    对 AI Economy 这个项目来说本身就是重要的经济数据。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

from llm.base import (
    TOKEN_SOURCE_API,
    LLMResponse,
)
from llm.doubao import DoubaoProvider
from llm.deepseek import DeepSeekProvider
from llm.router import LLMRouter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 所有测试都打上 real_api 标记
pytestmark = pytest.mark.real_api


# ------------------------------------------------------------
# 显式把 .env 读进来
#
# 为什么必须在这里读，而不是等夹具？
#     下面组装待测清单是在【导入阶段】执行的，
#     那时任何夹具都还没运行。如果不先读 .env，
#     即使你配了 Key 也会被判定成「没配」而被跳过。
# ------------------------------------------------------------
from core.config import load_env_file, load_global_config  # noqa: E402

load_env_file()

CONFIG = load_global_config()


# ============================================================
# 待测 provider 清单
# ============================================================
def _provider_cases() -> list:
    """
    组装待测清单。

    豆包的默认测试配置里关掉了 thinking：
        实测带思考时输出约 2500 tokens、耗时 5 秒以上；
        关掉之后十几 tokens、一秒内返回，可见答案没明显差别。
        跑集成测试没必要为「思考过程」付钱。
    """
    return [
        {
            "name": "deepseek",
            "cls": DeepSeekProvider,
            "key_env": "DEEPSEEK_API_KEY",
            "default_model": "deepseek-chat",
            "extra_config": {},
            "max_tokens": 32,
        },
        {
            "name": "doubao",
            "cls": DoubaoProvider,
            "key_env": "DOUBAO_API_KEY",
            "default_model": "",
            "extra_config": {"extra_body": {"thinking": {"type": "disabled"}}},
            "max_tokens": 64,
        },
    ]


PROVIDER_CASES = _provider_cases()


def _has_key(case: Dict[str, Any]) -> bool:
    return bool(os.environ.get(case["key_env"], "").strip())


# 一个 Key 都没配 → 整个文件跳过，而不是失败
if not any(_has_key(c) for c in PROVIDER_CASES):
    pytest.skip(
        "Skipped: 没有配置任何 provider 的 API Key "
        "（DEEPSEEK_API_KEY / DOUBAO_API_KEY）",
        allow_module_level=True,
    )


# ============================================================
# 夹具
# ============================================================
@pytest.fixture(params=PROVIDER_CASES, ids=[c["name"] for c in PROVIDER_CASES])
def case(request) -> Dict[str, Any]:
    """当前测试针对哪个 provider。"""
    return request.param


def _provider_config(case) -> Dict[str, Any]:
    config = dict(CONFIG.get(f"llm.providers.{case['name']}", {}) or {})
    config.update(case["extra_config"])
    return config


@pytest.fixture
def provider(case) -> Any:
    """
    真实的 Provider，用真实的 urllib HTTP 层。

    没配这个 provider 的 Key 就跳过它 —— 不影响另一个 provider。
    """
    if not _has_key(case):
        pytest.skip(f"Skipped: {case['key_env']} not configured")

    return case["cls"](
        provider_config=_provider_config(case),
        default_model=case["default_model"],
    )


@pytest.fixture
def router() -> LLMRouter:
    return LLMRouter(CONFIG)


def cheap(case) -> int:
    return case["max_tokens"]


# ============================================================
# 1. 连通性与基本形状
# ============================================================
class TestRealConnectivity:
    def test_provider_is_available(self, provider, case):
        assert provider.is_available() is True
        assert provider.unavailable_reason() is None
        assert provider.default_model, f"{case['name']} 应该有解析出的模型名"

    def test_simple_call_returns_response(self, provider, case):
        response = provider.generate(
            messages="用一句话回答：1+1 等于几？",
            max_tokens=cheap(case),
        )

        assert isinstance(response, LLMResponse)
        assert response.provider == case["name"]
        assert response.content.strip(), "真实返回的内容不该是空的"

    def test_latency_is_measured(self, provider, case):
        response = provider.generate(messages="说：好", max_tokens=cheap(case))
        assert response.latency_ms > 0, "真实网络调用不可能 0 毫秒"

    def test_raw_response_is_kept(self, provider, case):
        response = provider.generate(messages="说：好", max_tokens=cheap(case))

        assert isinstance(response.raw_response, dict)
        # 这两个是 OpenAI 兼容格式的锚点 —— 它们变了说明接口改了
        assert "choices" in response.raw_response
        assert "model" in response.raw_response


# ============================================================
# 2. 真实 token usage（最关键的部分）
# ============================================================
class TestRealTokenUsage:
    def test_usage_fields_match_our_assumptions(self, provider, case):
        """
        【最重要的一条】

        真实 API 真的会返回 usage 吗？
        字段名真的是 prompt_tokens / completion_tokens / total_tokens 吗？

        如果字段名假设错了，这里会是 None —— 并且会打印出
        真实返回的顶层字段，方便立刻定位问题。
        """
        response = provider.generate(
            messages="用一句话介绍你自己。",
            max_tokens=cheap(case),
        )

        raw_keys = sorted((response.raw_response or {}).keys())

        assert response.input_tokens is not None, (
            f"{case['name']} 没有拿到 input_tokens —— "
            f"可能 usage 字段名和我们的假设不一致。"
            f"原始返回的顶层字段：{raw_keys}"
        )
        assert response.output_tokens is not None
        assert response.total_tokens is not None

    def test_usage_marked_as_api_data(self, provider, case):
        response = provider.generate(messages="说：好", max_tokens=cheap(case))

        assert response.token_source == TOKEN_SOURCE_API
        assert response.usage_from_api is True
        assert response.simulated is False, "真实调用不能被标成模拟"

    def test_token_counts_are_sane(self, provider, case):
        response = provider.generate(
            messages="用一句话介绍你自己。",
            max_tokens=cheap(case),
        )

        assert response.input_tokens > 0
        assert response.output_tokens > 0
        assert response.total_tokens >= response.input_tokens
        assert response.total_tokens >= response.output_tokens

    def test_total_matches_the_parts(self, provider, case):
        """total 要么是 API 给的，要么是我们精确相加的 —— 两种都该相等。"""
        response = provider.generate(messages="说：好", max_tokens=cheap(case))

        assert response.total_tokens == (
            response.input_tokens + response.output_tokens
        )

    def test_estimate_versus_real_usage(self, provider, case):
        """
        对比「我们的估算」和「真实 usage」。

        这不是断言谁对谁错，而是把差距记录下来 ——
        因为以后算钱要用这个数字，你得知道估算能差多少。
        """
        from llm.base import estimate_messages_tokens, normalize_messages

        prompt = "用一句话介绍你自己。"
        response = provider.generate(messages=prompt, max_tokens=cheap(case))

        our_estimate = estimate_messages_tokens(normalize_messages(prompt))

        ratio = our_estimate / response.input_tokens
        print(
            f"\n  [{case['name']} 估算对比] "
            f"我们估算 {our_estimate} / 真实 {response.input_tokens} tokens "
            f"（比值 {ratio:.2f}）"
        )

        assert our_estimate > 0
        # 容差给得很宽：只要在同一个量级就算通过
        assert 0.2 < ratio < 5.0, (
            f"估算({our_estimate}) 和真实({response.input_tokens}) "
            f"差了 {ratio:.1f} 倍，量级不对"
        )


# ============================================================
# 3. 通过 Router
# ============================================================
class TestThroughRouter:
    def test_router_dispatches_to_provider(self, router, case):
        if not _has_key(case):
            pytest.skip(f"Skipped: {case['key_env']} not configured")

        response = router.generate(
            provider=case["name"],
            messages="说：好",
            max_tokens=cheap(case),
        )

        assert response.provider == case["name"]
        assert response.usage_from_api is True

    def test_router_knows_every_provider(self, router):
        """Router 应该能找到清单里的每一个 provider。"""
        for case_dict in PROVIDER_CASES:
            assert case_dict["name"] in router.registry.implemented


# ============================================================
# 4. 完整链路：Agent → Brain → Router → 真实 API
# ============================================================
class TestFullAgentChain:
    """
    【抽象是否成立的最终判据】

    让真实的 Agent 用真实的 API 思考一次。
    如果这需要改 Agent 的任何一行代码，抽象就是失败的。

    注意 Agent 的配置里 provider 仍然写着 "mock" ——
    我们只是通过 generate(provider=...) 临时指定，
    这正是「运行时换模型不用改代码」的证明。
    """

    def test_agent_generates_through_real_api(self, economy, case):
        if not _has_key(case):
            pytest.skip(f"Skipped: {case['key_env']} not configured")

        from agents.brain import Brain, BrainSettings
        from llm import LLMCallRecorder

        agent = economy.create_agent("agent_a")

        recorder = LLMCallRecorder(
            db=economy.db,
            tokens_per_ai_coin=economy.tokens_per_ai_coin,
        )
        brain = Brain(
            router=LLMRouter(CONFIG),
            settings=BrainSettings.from_agent_config(agent.config),
            recorder=recorder,
        )
        agent.attach_brain(brain)

        # Agent 的代码不知道这些厂商存在；这里只是换了个字符串
        assert agent.config.provider == "mock"

        response = agent.generate(
            messages="用一句话说明你是什么。",
            provider=case["name"],
            max_tokens=cheap(case),
        )

        assert isinstance(response, LLMResponse)
        assert response.provider == case["name"]
        assert response.content.strip()
        assert response.usage_from_api is True

    def test_real_call_is_recorded_with_api_tokens(self, economy, case):
        """真实调用的 token 用量必须落到数据库里，且标成 api。"""
        if not _has_key(case):
            pytest.skip(f"Skipped: {case['key_env']} not configured")

        from agents.brain import Brain, BrainSettings
        from llm import LLMCallRecorder

        agent = economy.create_agent("agent_b")

        recorder = LLMCallRecorder(
            db=economy.db,
            tokens_per_ai_coin=economy.tokens_per_ai_coin,
        )
        agent.attach_brain(
            Brain(
                router=LLMRouter(CONFIG),
                settings=BrainSettings.from_agent_config(agent.config),
                recorder=recorder,
            )
        )

        # provider 的 extra_config（比如关掉 thinking）要通过配置生效，
        # 所以这里直接用 provider 实例发一次，再验证记账通路。
        provider = case["cls"](
            provider_config=_provider_config(case),
            default_model=case["default_model"],
        )
        response = provider.generate(messages="说：好", max_tokens=cheap(case))
        recorder.record(agent_id="agent_b", response=response)

        rows = recorder.recent(agent_id="agent_b")
        assert len(rows) == 1

        row = rows[0]
        assert row["provider"] == case["name"]
        assert row["simulated"] == 0
        assert row["token_source"] == "api"
        assert row["total_tokens"] > 0
        assert row["latency_ms"] > 0

    def test_real_call_does_not_touch_wallet_or_ledger(self, economy, case):
        """
        【本阶段的边界】即使是真实花钱的调用，也【不】扣 AIC。

        只记 token，不接经济系统 —— 那是 Step 6 的事。
        """
        if not _has_key(case):
            pytest.skip(f"Skipped: {case['key_env']} not configured")

        from agents.brain import Brain, BrainSettings
        from llm import LLMCallRecorder

        agent = economy.create_agent("agent_c")

        recorder = LLMCallRecorder(
            db=economy.db, tokens_per_ai_coin=economy.tokens_per_ai_coin
        )
        agent.attach_brain(
            Brain(
                router=LLMRouter(CONFIG),
                settings=BrainSettings.from_agent_config(agent.config),
                recorder=recorder,
            )
        )

        balance_before = agent.balance
        ledger_before = economy.ledger.count()

        agent.generate(
            messages="说：好",
            provider=case["name"],
            max_tokens=cheap(case),
        )

        assert agent.balance == balance_before, "本阶段不该扣钱"
        assert economy.ledger.count() == ledger_before, "账本不该变化"
        assert economy.audit()["balanced"] is True


# ============================================================
# 5. 真实错误路径
# ============================================================
class TestRealErrorPaths:
    def test_bad_key_gives_authentication_error(self, case, monkeypatch):
        """
        用一个格式合法但无效的 Key，验证真实的鉴权失败
        会被正确翻译成 AuthenticationError。

        这是唯一能验证「真实 401 长什么样」的办法。
        """
        from llm.base import AuthenticationError

        monkeypatch.setenv(case["key_env"], "sk-" + "0" * 32)

        bad = case["cls"](
            provider_config=_provider_config(case),
            default_model=case["default_model"],
        )

        with pytest.raises(AuthenticationError):
            bad.generate(messages="hi", max_tokens=8)


# ============================================================
# 6. 豆包 thinking 的经济成本实测（只有豆包跑）
# ============================================================
class TestDoubaoThinkingCost:
    """
    【这是一条「经济观察」测试，不是普通的功能测试】

    它测量同一句问题在「带思考」和「不带思考」下的 token 消耗差异。

    为什么放在 AI Economy 项目里？
        因为这个差异非常夸张，而它正好说明这个项目的核心命题：

            Agent 的智力成本，和它产出的可见结果完全不成比例。

        普通 Agent 项目看不到这件事（调用是免费的）。
        在这里，它决定了 Agent 能不能活下去。
    """

    def test_thinking_versus_not(self):
        key_env = "DOUBAO_API_KEY"
        if not os.environ.get(key_env, "").strip():
            pytest.skip(f"Skipped: {key_env} not configured")

        base = dict(CONFIG.get("llm.providers.doubao", {}) or {})
        if not base.get("model"):
            base["model"] = os.environ.get("DOUBAO_MODEL", "").strip()
        if not base.get("model"):
            pytest.skip("Skipped: 豆包没有配置模型名")

        prompt = "请用大约三百字介绍一下人工智能的发展历史。"

        # 带思考（默认）
        with_thinking = DoubaoProvider(provider_config=dict(base))
        r1 = with_thinking.generate(messages=prompt, max_tokens=64)

        # 关掉思考
        quiet_cfg = dict(base)
        quiet_cfg["extra_body"] = {"thinking": {"type": "disabled"}}
        without_thinking = DoubaoProvider(provider_config=quiet_cfg)
        r2 = without_thinking.generate(messages=prompt, max_tokens=64)

        print(
            f"\n  [豆包 thinking 成本对比]\n"
            f"    带思考:   输出 {r1.output_tokens} tokens, "
            f"可见 {len(r1.content)} 字, 耗时 {r1.latency_ms:.0f}ms\n"
            f"    不带思考: 输出 {r2.output_tokens} tokens, "
            f"可见 {len(r2.content)} 字, 耗时 {r2.latency_ms:.0f}ms"
        )

        # 不断言谁更便宜（那取决于模型和版本），
        # 只断言两者都拿到了来自 API 的真实 usage。
        assert r1.token_source == TOKEN_SOURCE_API
        assert r2.token_source == TOKEN_SOURCE_API
        assert r1.output_tokens is not None
        assert r2.output_tokens is not None

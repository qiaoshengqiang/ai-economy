"""
Test: Brain 与 Agent 的接入
============================================================

验证 Step 2 的最终目标链路：

    Agent
     ↓
    Brain
     ↓
    Router
     ↓
    Provider
     ↓
    LLMResponse
     ↑
    Agent

以及最重要的一条：Agent 完全不知道 Provider 是谁。

你要求的测试 6（Brain 能调用 Router）和测试 7（Agent 能通过 Brain
拿到 MockProvider 的结果）都在这个文件里。
"""

from __future__ import annotations

import pytest

from agents.base_agent import BaseAgent
from agents.brain import Brain, BrainSettings
from llm.base import LLMProvider, LLMResponse, ProviderNotImplementedError, normalize_messages
from llm.mock import MockProvider
from llm.recorder import LLMCallRecorder
from llm.router import LLMRouter, build_default_registry


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def router(config) -> LLMRouter:
    return LLMRouter(config)


@pytest.fixture
def recorder(economy) -> LLMCallRecorder:
    return LLMCallRecorder(
        db=economy.db,
        tokens_per_ai_coin=economy.tokens_per_ai_coin,
    )


@pytest.fixture
def agent_a(economy, router, recorder):
    """一个装好大脑的 Agent A。"""
    agent = economy.create_agent("agent_a")
    agent.attach_brain(
        Brain(
            router=router,
            settings=BrainSettings.from_agent_config(agent.config),
            recorder=recorder,
        )
    )
    return agent


@pytest.fixture
def agent_b(economy, router, recorder):
    agent = economy.create_agent("agent_b")
    agent.attach_brain(
        Brain(
            router=router,
            settings=BrainSettings.from_agent_config(agent.config),
            recorder=recorder,
        )
    )
    return agent


# ============================================================
# Brain 基本行为
# ============================================================
class TestBrain:
    def test_brain_reads_settings_from_agent_config(self, economy, router):
        """Brain 的默认参数必须来自这个 Agent 自己的配置。"""
        agent = economy.create_agent("agent_a")
        settings = BrainSettings.from_agent_config(agent.config)

        assert settings.agent_id == "agent_a"
        assert settings.provider == "mock"
        assert settings.system_prompt            # A 的配置里有 system_prompt
        assert settings.temperature > 0
        assert settings.max_tokens > 0

    def test_brain_has_no_vendor_dependency(self, economy, router):
        """
        Brain 的 provider 只是一个【字符串】，不是某个类。
        这就是「换模型不用改代码」的技术前提。
        """
        agent = economy.create_agent("agent_a")
        brain = Brain(router=router, settings=BrainSettings.from_agent_config(agent.config))

        assert isinstance(brain.provider, str)

    # --- 测试 6：Brain 可以正常调用 Router ---
    def test_brain_calls_router(self, router, recorder):
        brain = Brain(
            router=router,
            settings=BrainSettings(agent_id="agent_x", provider="mock"),
            recorder=recorder,
        )

        response = brain.generate(messages="Hello")

        assert isinstance(response, LLMResponse)
        assert response.provider == "mock"
        assert brain.call_count == 1

    def test_brain_applies_agent_system_prompt(self, router, recorder):
        """
        Agent 的 system_prompt 必须被真的传下去。

        怎么验证？MockProvider 会从 system prompt 里认 Agent 的名字。
        如果认出来了，说明 prompt 确实传到了 Provider 那一层。
        """
        brain = Brain(
            router=router,
            settings=BrainSettings(
                agent_id="agent_a",
                provider="mock",
                system_prompt="你是 Agent A，一个独立的经济主体。",
            ),
            recorder=recorder,
        )

        response = brain.generate(messages="Hello")
        assert response.content == "Mock response generated for Agent A."

    def test_explicit_system_overrides_agent_default(self, router, recorder):
        brain = Brain(
            router=router,
            settings=BrainSettings(
                agent_id="agent_a", provider="mock",
                system_prompt="你是 Agent A",
            ),
            recorder=recorder,
        )

        response = brain.generate(messages="hi", system="你是 Agent Q")
        assert "Agent Q" in response.content

    def test_brain_passes_temperature_and_max_tokens(self, router, recorder):
        brain = Brain(
            router=router,
            settings=BrainSettings(
                agent_id="agent_a", provider="mock",
                temperature=0.11, max_tokens=555,
            ),
            recorder=recorder,
        )

        response = brain.generate(messages="hi")
        assert response.raw_response["temperature"] == 0.11
        assert response.raw_response["max_tokens"] == 555

    def test_think_returns_text_only(self, router, recorder):
        brain = Brain(
            router=router,
            settings=BrainSettings(agent_id="agent_a", provider="mock"),
            recorder=recorder,
        )

        text = brain.think("Hello")
        assert isinstance(text, str)
        assert text

    def test_brain_without_recorder_still_works(self, router):
        """没有数据库也必须能用 —— 记账是可选能力，不是依赖。"""
        brain = Brain(
            router=router,
            settings=BrainSettings(agent_id="agent_a", provider="mock"),
            recorder=None,
        )

        response = brain.generate(messages="hi")
        assert response.content

    def test_brain_propagates_provider_errors(self, router, recorder):
        """
        Provider 出错时 Brain 必须把异常抛出去。

        为什么？因为 Agent 需要知道「这次思考失败了」。
        如果 Brain 把异常吞掉返回一句空话，Agent 会以为
        任务完成了，然后在经济上做出错误决策。

        这里用 grok 来触发错误 —— 目前只剩它还没实现。
        （deepseek 和 doubao 都已经实现了，不再会抛这个错。）
        """
        brain = Brain(
            router=router,
            settings=BrainSettings(agent_id="agent_a", provider="grok"),
            recorder=recorder,
        )

        with pytest.raises(ProviderNotImplementedError):
            brain.generate(messages="hi")

        assert brain.failure_count == 1

    def test_describe(self, router, recorder):
        brain = Brain(
            router=router,
            settings=BrainSettings(agent_id="agent_a", provider="mock", model="m"),
            recorder=recorder,
        )
        info = brain.describe()

        assert info["agent_id"] == "agent_a"
        assert info["provider"] == "mock"
        assert info["has_recorder"] is True


# ============================================================
# Agent 接入
# ============================================================
class TestAgentWithBrain:
    def test_agent_has_no_brain_by_default(self, economy):
        """没装大脑的 Agent 是明确可检测的状态，而不是塞个假的掩盖问题。"""
        agent = economy.create_agent("agent_a")

        assert agent.has_brain is False
        assert agent.brain is None

    def test_agent_cannot_generate_without_brain(self, economy):
        agent = economy.create_agent("agent_a")

        with pytest.raises(RuntimeError, match="还没有大脑"):
            agent.generate(messages="hi")

    def test_attach_brain(self, agent_a):
        assert agent_a.has_brain is True
        assert isinstance(agent_a.brain, Brain)

    # --- 测试 7：Agent 可以通过 Brain 得到 MockProvider 的结果 ---
    def test_agent_generates_through_brain(self, agent_a):
        response = agent_a.generate(messages="Hello, Agent A.")

        assert isinstance(response, LLMResponse)
        assert response.provider == "mock"
        assert "Agent A" in response.content
        assert response.total_tokens > 0

    def test_agent_think(self, agent_a):
        assert agent_a.think("Hello")

    def test_agent_remembers_its_thinking(self, agent_a):
        """
        Agent 思考完应该留下痕迹，方便回头 debug
        「我刚才为什么这么决定」。
        """
        agent_a.generate(messages="Hello")

        entries = agent_a.recall("brain_generate")
        assert len(entries) == 1
        assert entries[0]["provider"] == "mock"
        assert entries[0]["total_tokens"] > 0

    def test_two_agents_have_separate_brains(self, agent_a, agent_b):
        assert agent_a.brain is not agent_b.brain
        assert agent_a.brain.agent_id == "agent_a"
        assert agent_b.brain.agent_id == "agent_b"

    def test_two_agents_get_their_own_identity_in_reply(self, agent_a, agent_b):
        """
        A 和 B 的 system_prompt 不同，所以 Mock 认出的是各自的名字。
        这证明「每个 Agent 有独立的大脑配置」是通的。
        """
        assert "Agent A" in agent_a.generate(messages="hi").content
        assert "Agent B" in agent_b.generate(messages="hi").content

    def test_switching_provider_needs_zero_agent_code_change(self, economy, router, recorder):
        """
        【Step 2 最重要的架构验收】

        把 Agent 的 provider 从 mock 换成另一个实现，
        Agent 的代码一行都不改 —— 只换 Brain 里那个字符串。

        这里用一个自己写的 FakeProvider 来模拟「以后接入真实厂商」。
        """
        class FakeProvider(LLMProvider):
            name = "fake"
            requires_api_key = False

            def generate(self, messages=None, system=None, model=None,
                         temperature=0.7, max_tokens=2048, **kwargs):
                normalized = normalize_messages(messages, system=system)
                return LLMResponse(
                    content="我是另一个厂商",
                    provider=self.name,
                    model=self.resolve_model(model) or "fake-model",
                    input_tokens=5,
                    output_tokens=5,
                    total_tokens=10,
                )

        # 注册新 provider —— 注意 Router 的代码没有被修改
        router.registry.register("fake", FakeProvider)

        agent = economy.create_agent("agent_a")

        # 装一个用 fake 的大脑
        agent.attach_brain(
            Brain(
                router=router,
                settings=BrainSettings(agent_id="agent_a", provider="fake"),
                recorder=recorder,
            )
        )

        response = agent.generate(messages="hi")

        # Agent 完全不知道发生了什么 —— 它拿到的还是统一的 LLMResponse
        assert response.provider == "fake"
        assert response.content == "我是另一个厂商"
        assert response.total_tokens == 10

        # 而 Agent 的代码里没有任何一行提到 fake：换 provider 只改了配置字符串
        assert isinstance(agent, BaseAgent)


# ============================================================
# 调用记录（llm_calls）
# ============================================================
class TestCallRecording:
    def test_every_call_is_recorded(self, agent_a, recorder):
        assert recorder.count("agent_a") == 0

        agent_a.generate(messages="第一次")
        agent_a.generate(messages="第二次")

        assert recorder.count("agent_a") == 2

    def test_record_contains_token_usage(self, agent_a, recorder):
        response = agent_a.generate(messages="hi")
        row = recorder.recent(agent_id="agent_a", limit=1)[0]

        assert row["input_tokens"] == response.input_tokens
        assert row["output_tokens"] == response.output_tokens
        assert row["total_tokens"] == response.total_tokens

    def test_record_contains_provider_and_model(self, agent_a, recorder):
        agent_a.generate(messages="hi")
        row = recorder.recent(agent_id="agent_a", limit=1)[0]

        assert row["provider"] == "mock"
        assert row["model"] == "mock-model-v1"

    def test_record_contains_latency(self, agent_a, recorder):
        """你要求 latency 必须留下来，而表里原本没有这一列。"""
        agent_a.generate(messages="hi")
        row = recorder.recent(agent_id="agent_a", limit=1)[0]

        assert row["latency_ms"] is not None
        assert row["latency_ms"] >= 0

    def test_record_marks_simulated(self, agent_a, recorder):
        """
        Mock 的记录必须被标记成模拟数据。

        否则以后拿模拟 token 去算真实成本，账永远对不上。
        """
        agent_a.generate(messages="hi")
        row = recorder.recent(agent_id="agent_a", limit=1)[0]

        assert row["simulated"] == 1

    def test_record_snapshots_exchange_rate(self, agent_a, recorder, economy):
        """
        汇率快照要存下来。

        这样以后你调整了 tokens_per_ai_coin，
        历史记录仍然能按当时的比例复核。
        """
        agent_a.generate(messages="hi")
        row = recorder.recent(agent_id="agent_a", limit=1)[0]

        assert row["tokens_per_ai_coin"] == economy.tokens_per_ai_coin

    def test_cost_is_computed_from_tokens_but_usd_is_unknown(
            self, agent_a, recorder):
        """
        【迁移说明】本测试原名 `test_cost_is_not_computed_yet`。

            旧前提：「只记 token，不算钱。算钱的逻辑属于 Step 6。」
            → 该前提**已过期**：§0.5 审计 A-02 与 H-2 正是要实现它，
              现在 `cost_ai` 由 `llm.cost.CostCalculator` 算出。

            仍然有效的问题（本测试现在守的就是这个）：
              · `cost_ai` 必须**由 token 推出**，不能凭空造
              · `api_cost_usd` 仍是 0 —— 那是 Provider 侧的真实美元账单，
                我们**不知道**，不能编
              · 0 的含义是「真的没花」或「不知道」，**不是**「免费」
        """
        agent_a.generate(messages="hi")
        row = recorder.recent(agent_id="agent_a", limit=1)[0]

        # cost_ai 必须是「由 token 算出来的」——
        # 只要真的用了 token，它就不该是 0。
        if row["total_tokens"] > 0:
            assert row["cost_ai"] > 0, (
                "有 token 却算出 0 成本：CostCalculator 没接上"
            )
        # token 拿不到时，0 的含义是「不知道」，不是「免费」
        else:
            assert row["cost_ai"] == 0

        # 🔒 这一条**没有变**：真实美元账单属于 Provider 侧事实，
        #    我们不编。
        assert row["api_cost_usd"] == 0

    def test_llm_call_does_not_touch_wallet(self, agent_a):
        """
        【关键边界】调用模型【暂时】不扣钱。

        本阶段只记录 token 消耗，不连接经济系统。
        等 Step 6 接上 Cost Calculator 之后，这里才会开始扣费。
        """
        before = agent_a.balance

        agent_a.generate(messages="hi")

        assert agent_a.balance == before

    def test_ledger_is_untouched_by_llm_calls(self, agent_a, economy):
        """账本不该因为一次模型调用而变化。"""
        before = economy.ledger.count()

        agent_a.generate(messages="hi")

        assert economy.ledger.count() == before

    def test_failed_call_is_recorded(self, economy, router, recorder):
        """
        失败的调用也要记。

        因为失败率是重要的经济信息：它会影响 Agent 的声誉，
        以及它要不要换一个更可靠的模型。
        而且失败的调用也可能已经消耗了 token。
        """
        agent = economy.create_agent("agent_a")
        agent.attach_brain(
            Brain(
                router=router,
                # grok 还没实现 —— 用它来制造一次失败
                settings=BrainSettings(agent_id="agent_a", provider="grok"),
                recorder=recorder,
            )
        )

        with pytest.raises(ProviderNotImplementedError):
            agent.generate(messages="hi")

        rows = recorder.recent(agent_id="agent_a")
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert "ProviderNotImplementedError" in rows[0]["error"]

    def test_can_disable_recording(self, economy, router, recorder):
        """
        record=False 时不记账。

        什么场景用？比如系统自己发一个探活请求 ——
        那不是 Agent 的经营行为，不该算在它的成本里。
        """
        agent = economy.create_agent("agent_a")
        agent.attach_brain(
            Brain(
                router=router,
                settings=BrainSettings(agent_id="agent_a", provider="mock"),
                recorder=recorder,
            )
        )

        agent.generate(messages="hi", record=False)

        assert recorder.count("agent_a") == 0

    def test_summary_aggregates_tokens(self, agent_a, agent_b, recorder):
        agent_a.generate(messages="第一次调用")
        agent_a.generate(messages="第二次调用")
        agent_b.generate(messages="B 的调用")

        summary_a = recorder.summary("agent_a")
        assert summary_a["calls"] == 2
        assert summary_a["total_tokens"] > 0
        assert summary_a["failures"] == 0

        summary_b = recorder.summary("agent_b")
        assert summary_b["calls"] == 1

        # 两个 Agent 的记录互不干扰
        assert recorder.count() == 3

    def test_summary_of_empty_agent_is_zeros(self, economy, agent_a, recorder):
        summary = recorder.summary("agent_a")

        assert summary["calls"] == 0
        assert summary["total_tokens"] == 0
        assert summary["failures"] == 0

    def test_recorder_is_append_only(self, agent_a, recorder):
        """
        调用记录和账本一样是历史事实，不该被追改。
        """
        for name in ("update", "delete", "edit", "remove"):
            assert not hasattr(recorder, name), (
                f"Recorder 不应该有 {name} —— 调用记录是历史事实"
            )

    def test_records_survive_across_recorder_instances(self, economy, agent_a, recorder):
        """
        记录真的落到了数据库，不是只活在内存里。

        怎么验证？新建一个 Recorder 读同一个库，应该还能看到。
        """
        agent_a.generate(messages="hi")

        fresh = LLMCallRecorder(db=economy.db, tokens_per_ai_coin=1234)
        assert fresh.count("agent_a") == 1
        # 新 Recorder 的汇率不同，但旧记录里的快照没变
        assert fresh.recent(agent_id="agent_a")[0]["tokens_per_ai_coin"] == 10000

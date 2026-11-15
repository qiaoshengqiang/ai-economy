"""
Runtime Wiring —— 默认路径是否自动接入 Cognitive Context
============================================================

§十：**这是本轮最重要的测试。**
§十九 的验收命题：
    「如果用户直接启动当前默认 Agent，不手工传入 context_query，
      Agent 能不能自动把自己相关的持久 Self Model 读取出来
      并放进当前 LLM Cognitive Run？」
必须是**能**。

所以这里的测试**不**调用 CognitiveContextBuilder，
也**不**显式传 context_query —— 它们走 `build_runtime()`。
"""

from __future__ import annotations

import pytest

from selfmodel import (
    EVIDENCE_REFLECTION, Evidence, SelfModelStore,
)


class CapturingProvider:
    """deterministic fake provider：记下收到的 system。"""

    def __init__(self):
        self.seen = []

    def generate(self, messages=None, system=None, **kwargs):
        from llm.base import LLMResponse
        self.seen.append({"messages": messages, "system": system,
                          "kwargs_keys": sorted(kwargs)})
        return LLMResponse(
            provider="capture", model="capture", content="ok",
            input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=1.0, simulated=True,
        )


class CapturingRouter:
    """替换真 router 用 —— 只实现 Brain 需要的那两个方法。

    ⚠️ 注意 system 是**关键字**传给 provider 的，不是塞进 messages。
    """

    def __init__(self, provider):
        self.provider = provider

    def get_provider(self, name, credential=None):      # noqa: ARG002
        return self.provider

    def generate(self, messages=None, provider=None, model=None,
                 system=None, **kwargs):
        return self.provider.generate(messages=messages, system=system,
                                      **kwargs)


@pytest.fixture()
def runtime(tmp_path):
    from main import build_runtime
    rt = build_runtime(db_path=str(tmp_path / "wire.db"),
                       agent_ids=["agent_a"])
    yield rt
    try:
        rt.economy.close()
    except Exception:                                   # noqa: BLE001
        pass


def _agent(rt, agent_id="agent_a"):
    return next(a for a in rt.economy.all_agents()
                if a.agent_id == agent_id)


def _capture(rt, agent_id="agent_a"):
    """把某个 Agent 的 router 换成 capturing fake，返回 (agent, provider)。"""
    ag = _agent(rt, agent_id)
    prov = CapturingProvider()
    ag.brain.router = CapturingRouter(prov)
    return ag, prov


# ================================================================
# §十 / §十九 核心：默认路径自动接入
# ================================================================
class TestDefaultRuntimeWiring:
    def test_build_runtime_installs_context_builder(self, runtime):
        """build_runtime 真的装了 builder —— 不是测试自己装的。"""
        ag = _agent(runtime)
        assert ag.brain.context_builder is not None

    def test_default_path_builds_context_without_explicit_query(
            self, runtime):
        """
        🔒 **不传 context_query**，Context 依然被构建。

        🔴 2026-10-10 修一个**假绿**（不是我的改动引入的，是被暴露出来的）：

            这条以前用**真实 router** 调 `ag.generate()` —— 于是它只有在
            「**别的测试碰巧留下了 `DEEPSEEK_API_KEY`**」时才通过。

            而 `tests/conftest.py` 有一个 `autouse` fixture
            **故意清掉** `DEEPSEEK_API_KEY`（"让单元测试不读用户本机的 .env"）。
            所以孤立跑它必然 `ProviderDisabledError` ——
            它之前绿的是**别的测试的环境残留**，不是它自己。

            🔒 本测试测的是**接线**（"默认路径会不会构建 Context"），
               **不是** "真实 API 通不通" —— 所以应该用同文件里
               其它 8 条测试都在用的 `_capture`（假 provider）。
        """
        ag, _prov = _capture(runtime)
        SelfModelStore(runtime.economy.db).add_strategy(
            "agent_a", "默认路径也要能看到我", domain="fiction",
            confidence=0.9)

        ag.generate(messages="hi")                      # ← 不传 query

        assert ag.brain.last_context_stats is not None, \
            "默认路径没有构建 Cognitive Context"
        assert ag.brain.last_context_stats["selected_strategies"] >= 1

    def test_context_separates_name_and_agent_id(self, runtime):
        """名字和 Agent ID 是两条系统事实，不能合并成一句。"""
        ag, prov = _capture(runtime)
        ag.generate(messages="hi")
        system = prov.seen[-1]["system"] or ""
        assert "我的名字是：小艾" in system
        assert "我的 Agent ID 是：agent_a" in system
        assert "小艾（agent_a）" not in system

    def test_relevant_strategy_reaches_provider_by_default(self, runtime):
        """§十一 场景 A：相关 Self Model **自动**进入 Provider。"""
        SelfModelStore(runtime.economy.db).add_strategy(
            "agent_a", "先确认核心冲突再扩展人物关系", domain="fiction",
            situation="story_planning", confidence=0.9,
            evidence=[Evidence(EVIDENCE_REFLECTION, "复盘")])
        ag, prov = _capture(runtime)

        ag.generate(messages="开始写小说")               # ← 不传 query

        system = prov.seen[-1]["system"] or ""
        assert "先确认核心冲突" in system, \
            "默认路径没有把相关 Strategy 放进 LLM request"

    def test_irrelevant_strategy_does_not_reach_provider(self, runtime):
        """§十一 场景 B：无关 Strategy 不能因为接线了就跑进来。"""
        store = SelfModelStore(runtime.economy.db)
        store.add_strategy("agent_a", "FICTION_ONLY", domain="fiction",
                           confidence=0.9)
        ag, prov = _capture(runtime)

        ag.generate(messages="hi",
                    context_query={"domain": "robotics"})

        assert "FICTION_ONLY" not in (prov.seen[-1]["system"] or "")

    def test_no_self_model_still_works(self, runtime):
        """
        §十一 场景 C：没有任何 Self Model 也必须正常调用。

        🔴 **前提迁移（Economy Awareness V1）**：本测试原断言
           「空上下文 → 不注入任何东西」(`"Context about myself" not in system`)。

        那个前提**过期了** —— Context 不再为空：**经济事实**现在总是在里面
        （余额来自 Ledger，与有没有 Self Model 无关）。

        所以迁移为断言**原本真正想保证的事**：
            没有 Self Model → 没有任何 Self Model 内容被注入，调用照常发生。
        另外钉住新行为：经济事实**确实**出现了 ——
        它不该因为「没有 Self Model」而消失。
        """
        ag, prov = _capture(runtime)
        resp = ag.generate(messages="hi")
        assert resp is not None
        assert prov.seen, "没有 Self Model 时 LLM 调用没发出去"

        system = prov.seen[-1]["system"] or ""

        # ① 没有 Self Model 内容（原意）
        assert "Relevant beliefs I currently hold about myself" not in system
        assert "Relevant strategies I currently hold" not in system

        # ② 经济事实仍然在（新行为）
        stats = ag.brain.last_context_stats or {}
        assert stats.get("economic_context_present") is True
        assert "Economic context:" in system

    def test_cognitive_false_skips_context(self, runtime):
        """§十五：内部 LLM utility 可以显式关闭认知上下文。"""
        SelfModelStore(runtime.economy.db).add_strategy(
            "agent_a", "UTILITY_SHOULD_NOT_SEE_ME", domain="fiction",
            confidence=0.9)
        ag, prov = _capture(runtime)

        ag.brain.generate(messages="hi", cognitive=False)

        assert "UTILITY_SHOULD_NOT_SEE_ME" not in \
            (prov.seen[-1]["system"] or "")

    def test_explicit_query_overrides_default(self, runtime):
        store = SelfModelStore(runtime.economy.db)
        store.add_strategy("agent_a", "FICTION", domain="fiction",
                           confidence=0.9)
        store.add_strategy("agent_a", "ROBOTICS", domain="robotics",
                           confidence=0.9)
        ag, prov = _capture(runtime)

        ag.generate(messages="hi", context_query={"domain": "robotics"})

        system = prov.seen[-1]["system"] or ""
        assert "ROBOTICS" in system
        assert "FICTION" not in system


# ================================================================
# §十二 重启后仍然自动进入
# ================================================================
class TestWiringPersistence:
    def test_self_model_survives_restart_and_auto_enters(self, tmp_path):
        from main import build_runtime

        db = str(tmp_path / "restart.db")

        # --- Runtime 1：写入 Self Model ---
        rt1 = build_runtime(db_path=db, agent_ids=["agent_a"])
        SelfModelStore(rt1.economy.db).add_strategy(
            "agent_a", "跨重启依然记得的策略", domain="fiction",
            confidence=0.9)
        rt1.economy.close()

        # --- Runtime 2：全新 build_runtime，同一个 db ---
        rt2 = build_runtime(db_path=db, agent_ids=["agent_a"])
        try:
            ag, prov = _capture(rt2)
            ag.generate(messages="hi")                  # ← 不传 query
            system = prov.seen[-1]["system"] or ""
            assert "跨重启依然记得的策略" in system, \
                "重启后默认路径没把持久 Self Model 带上"
        finally:
            try:
                rt2.economy.close()
            except Exception:                           # noqa: BLE001
                pass


# ================================================================
# §十三 Fail-soft 但**不能** Fail-silent
# ================================================================
class TestFailureHandling:
    def test_build_failure_is_observable(self, runtime):
        class Boom:
            def build(self, *a, **k):
                raise RuntimeError("injected failure")

        ag, prov = _capture(runtime)
        ag.brain.context_builder = Boom()
        ag.brain.last_context_error = None
        before = ag.brain.context_failures

        resp = ag.generate(messages="hi")               # 不能崩

        assert resp is not None, "Context 构建失败把 LLM 调用拖垮了"
        assert prov.seen, "失败后 LLM 调用没有发出去"
        err = ag.brain.last_context_error
        assert err is not None, "§十三：失败必须**可观测**"
        assert err["agent_id"] == "agent_a"
        assert err["error_type"] == "RuntimeError"
        assert ag.brain.context_failures == before + 1

    def test_error_record_has_no_context_text(self, runtime):
        """§十三：不记完整 Prompt / Memory。"""
        class Boom:
            def build(self, *a, **k):
                raise RuntimeError("SECRET_PROMPT_CONTENT")

        ag, _ = _capture(runtime)
        ag.brain.context_builder = Boom()
        ag.generate(messages="hi")
        err = ag.brain.last_context_error
        # reason 截断到 200 字符，且**不含**上下文原文
        assert len(err["reason"]) <= 200
        assert "system" not in err and "messages" not in err


# ================================================================
# §十六 Provider 不拥有 Agent State
# ================================================================
class TestProviderStateless:
    def test_provider_gets_only_messages_and_system(self, runtime):
        SelfModelStore(runtime.economy.db).add_strategy(
            "agent_a", "S", domain="fiction", confidence=0.9)
        ag, prov = _capture(runtime)
        ag.generate(messages="hi")
        seen = prov.seen[-1]
        assert set(seen) == {"messages", "system", "kwargs_keys"}
        assert not hasattr(prov, "self_model")

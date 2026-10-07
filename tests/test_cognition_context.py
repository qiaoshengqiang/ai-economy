"""
Cognitive Context V1 —— 测试
============================================================

对应验收标准（§二十五）：
    ✅ Self Model 可以进入 Cognitive Context
    ✅ Context 是动态构建而不是静态复制
    ✅ Context 是选择性而不是全部注入
    ✅ Relevant / Irrelevant 有明确测试
    ✅ Self Model 修改后下一次 Context 能反映变化
    ✅ Cognitive Context 真正进入 LLM Request
    ✅ Provider 不拥有 Self Model
    ✅ Strategy 仍然只是 Agent 可参考的信息
    ✅ Runtime 不替 Agent 选择 Strategy
"""

from __future__ import annotations

import ast

import pytest

from cognition import (
    BANNED_RULE_WORDS, CognitiveContext, CognitiveContextBuilder,
)
from selfmodel import (
    EVIDENCE_REFLECTION, Evidence, Scope, SelfModelStore,
)
from storage.database import Database


# ================================================================
# Fixtures：一个 deterministic fake LLM（记录它收到的 system）
# ================================================================
class CapturingProvider:
    """不联网、不随机的 provider —— 只为证明「Context 真的发出去了」。"""

    def __init__(self):
        self.seen = []

    def generate(self, messages=None, system=None, **kwargs):
        from llm.base import LLMResponse
        self.seen.append({"messages": messages, "system": system})
        return LLMResponse(
            provider="fake", model="fake", content="ok",
            input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=1.0, simulated=True,
        )


class FakeRouter:
    """只认识「拿到 provider」这一件事 —— 与 LLMRouter 的公开契约同形。"""

    def __init__(self, provider):
        self.provider = provider

    def get_provider(self, name, credential=None):    # noqa: ARG002
        return self.provider

    def generate(self, messages=None, provider=None, model=None, **kwargs):
        return self.provider.generate(messages=messages, **kwargs)


@pytest.fixture()
def store(tmp_path):
    db = Database(str(tmp_path / "ctx.db"))
    db.initialize()
    yield SelfModelStore(db)
    try:
        db.close()
    except Exception:                                  # noqa: BLE001
        pass


@pytest.fixture()
def seeded(store):
    store.add_strategy("agent_a", "先确认核心冲突再扩展人物关系",
                       domain="fiction", situation="story_planning",
                       rationale="过去直接扩写导致返工", confidence=0.68,
                       evidence=[Evidence(EVIDENCE_REFLECTION, "复盘")])
    store.add_claim("agent_a", "我在人物塑造方面表现较好",
                    scope=Scope("digital_production", "fiction",
                                "character_design"), confidence=0.72)
    store.add_strategy("agent_a", "导航时先建地图再规划路径",
                       domain="robotics", situation="navigation",
                       confidence=0.9)
    return store


# ================================================================
# 1. §十八 关键测试：相关 / 不相关
# ================================================================
class TestRelevance:
    def test_relevant_strategy_enters_context(self, seeded):
        b = CognitiveContextBuilder(seeded)
        ctx = b.build("agent_a", world="digital_production",
                      domain="fiction", situation="story_planning")
        names = [s.name for s in ctx.strategies]
        assert "先确认核心冲突再扩展人物关系" in names

    def test_irrelevant_strategy_stays_out(self, seeded):
        b = CognitiveContextBuilder(seeded)
        ctx = b.build("agent_a", world="digital_production",
                      domain="fiction", situation="story_planning")
        assert all("导航" not in s.name for s in ctx.strategies)

    def test_robotics_context_excludes_fiction(self, seeded):
        b = CognitiveContextBuilder(seeded)
        ctx = b.build("agent_a", domain="robotics", situation="navigation")
        assert all("核心冲突" not in s.name for s in ctx.strategies)
        assert any("导航" in s.name for s in ctx.strategies)

    def test_same_domain_different_situation_still_retrievable(self, seeded):
        """
        🔒 §九：「优先看到」不是「只看到」。

        一条 scope=character_design 的 claim，在 story_planning 场景下
        应当**仍然可检索**（只是排序靠后），不能被静默漏掉。
        """
        b = CognitiveContextBuilder(seeded)
        ctx = b.build("agent_a", domain="fiction",
                      situation="story_planning")
        assert any("人物塑造" in c.content for c in ctx.self_claims), \
            "同 domain 不同 situation 的 claim 被静默漏掉了"

    def test_other_agent_context_is_isolated(self, seeded):
        seeded.add_claim("agent_b", "B 的自我认知")
        b = CognitiveContextBuilder(seeded)
        ctx = b.build("agent_a")
        assert all("B 的自我认知" != c.content for c in ctx.self_claims)


# ================================================================
# 2. §十四 没有相关 Self Model 也必须正常
# ================================================================
class TestEmptyIsFine:
    def test_empty_store_still_builds(self, store):
        b = CognitiveContextBuilder(store)
        ctx = b.build("agent_a", domain="fiction")
        assert ctx.self_claims == [] and ctx.strategies == []
        # 仍然能渲染（因为还有 stimulus / task）
        ctx2 = b.build("agent_a", stimulus="hi", task="t")
        assert ctx2.to_text()

    def test_no_builder_configured(self):
        """没有 Self Model 时，builder 只给别的内容，不报错。"""
        b = CognitiveContextBuilder(None)
        ctx = b.build("agent_a", stimulus="hi")
        assert ctx.strategies == []
        assert "hi" in ctx.to_text()

    def test_totally_empty_context_renders_empty_string(self, store):
        """什么上下文都没有 → 空字符串（**不是**一段空话术）。"""
        b = CognitiveContextBuilder(store)
        assert b.build("agent_a").to_text() == ""


# ================================================================
# 3. §十九 真正进入 LLM Request
# ================================================================
class TestEntersLLMRequest:
    def _brain(self, store, provider):
        from agents.brain import Brain, BrainSettings
        return Brain(FakeRouter(provider),
                     BrainSettings(agent_id="agent_a"),
                     context_builder=CognitiveContextBuilder(store))

    def test_strategy_reaches_provider_system(self, seeded):
        """Self Model → Retrieval → Context → **Provider 真的收到了**。"""
        prov = CapturingProvider()
        brain = self._brain(seeded, prov)
        brain.generate(
            messages="开始写小说",
            context_query={"world": "digital_production", "domain": "fiction",
                           "situation": "story_planning",
                           "stimulus": "开始写小说", "task": "小说创作"},
        )
        system = prov.seen[-1]["system"] or ""
        assert "先确认核心冲突" in system, "Strategy 没有进入 LLM request"

    def test_irrelevant_strategy_does_not_reach_provider(self, seeded):
        prov = CapturingProvider()
        brain = self._brain(seeded, prov)
        brain.generate(
            messages="导航任务",
            context_query={"domain": "robotics", "situation": "navigation"},
        )
        system = prov.seen[-1]["system"] or ""
        assert "核心冲突" not in system

    def test_without_context_query_behaviour_is_unchanged(self, seeded):
        """🔒 不传 context_query → 与接入前完全一样（零回归）。"""
        prov = CapturingProvider()
        brain = self._brain(seeded, prov)
        brain.generate(messages="hi")
        assert "先确认核心冲突" not in (prov.seen[-1]["system"] or "")

    def test_provider_does_not_own_self_model(self, seeded):
        """§十七：Provider 只收到文本，拿不到 store / agent 内部对象。"""
        prov = CapturingProvider()
        brain = self._brain(seeded, prov)
        brain.generate(messages="x", context_query={"domain": "fiction"})
        seen = prov.seen[-1]
        assert set(seen.keys()) == {"messages", "system"}
        assert not hasattr(prov, "self_model")
        assert "self_model" not in str(seen.get("system") or "").lower()

    def test_context_builder_failure_does_not_break_call(self, seeded):
        """§十四：Context 是增量增强，构建失败不该让 LLM 调用失败。"""
        class Boom:
            def build(self, *a, **k):
                raise RuntimeError("boom")
        from agents.brain import Brain, BrainSettings
        prov = CapturingProvider()
        brain = Brain(FakeRouter(prov), BrainSettings(agent_id="agent_a"),
                      context_builder=Boom())
        resp = brain.generate(messages="hi", context_query={"domain": "x"})
        assert resp is not None
        assert prov.seen, "LLM 调用被 Context 构建失败拖垮了"


# ================================================================
# 4. §二十 Context 是动态的，不是静态快照
# ================================================================
class TestDynamicContext:
    def test_context_reflects_self_model_change(self, seeded):
        b = CognitiveContextBuilder(seeded)
        q = {"domain": "fiction", "situation": "story_planning"}

        first = b.build("agent_a", **q)
        assert any("核心冲突" in s.name for s in first.strategies)

        # 废弃旧的，换新的
        old = seeded.list_strategies("agent_a")[0]
        seeded.abandon_strategy(old.strategy_id)
        seeded.add_strategy("agent_a", "先写完整大纲再动笔",
                            domain="fiction", situation="story_planning",
                            confidence=0.8)

        second = b.build("agent_a", **q)
        names = [s.name for s in second.strategies]
        assert any("先写完整大纲" in n for n in names), "新 Strategy 没进来"
        assert all("核心冲突" not in n for n in names), "已废弃的还在"

    def test_lowering_confidence_does_not_remove_it(self, seeded):
        """
        🔴 §九 / §十 核心：降低 confidence **不是**禁用。

            Runtime 只能说「这条信心不高了」，
            不能说「所以禁止 Agent 使用它」——那是 Agent 的决定。
        """
        b = CognitiveContextBuilder(seeded)
        q = {"domain": "fiction", "situation": "story_planning"}
        s = seeded.list_strategies("agent_a")[0]
        seeded.revise_strategy(s.strategy_id, confidence=0.01)

        ctx = b.build("agent_a", **q)
        assert any("核心冲突" in x.name for x in ctx.strategies), \
            "降低 confidence 被当成了禁用 —— Strategy 退化成 Rule"

    def test_max_limits_are_context_budget_not_policy(self, seeded):
        """§十五：max_strategies 是「本次最多给你看 N 个」，不是「只许用 N 个」。"""
        for i in range(6):
            seeded.add_strategy("agent_a", f"F{i}", domain="fiction",
                                confidence=0.5 + i * 0.01)
        b = CognitiveContextBuilder(seeded, max_strategies=3)
        ctx = b.build("agent_a", domain="fiction")
        assert len(ctx.strategies) == 3
        # 但底层数据一条没少 —— 只是本次没全给
        assert len(seeded.list_strategies("agent_a")) >= 6


# ================================================================
# 5. 🔴 §十三 禁止把 Self Model 伪装成 System Prompt Rules
# ================================================================
class TestContextIsNotSystemRules:
    def test_rendered_text_has_no_rule_words(self, seeded):
        b = CognitiveContextBuilder(seeded)
        text = b.build("agent_a", domain="fiction",
                       situation="story_planning").to_text().lower()
        hits = [w for w in BANNED_RULE_WORDS if w in text]
        assert not hits, (
            f"Context 里出现了规则化措辞 {hits} —— "
            f"§十三 禁止把 Strategy 伪装成 System Prompt Rules"
        )

    def test_rendered_text_has_no_internal_identifiers(self, seeded):
        """§十一：LLM 不该看到数据库 id / 内部标识。"""
        b = CognitiveContextBuilder(seeded)
        text = b.build("agent_a", domain="fiction").to_text()
        assert "claim_" not in text
        assert "strat_" not in text
        assert "SELECT" not in text

    def test_context_has_no_decision_methods(self):
        """🔴 Context Builder / Context 都不能替 Agent 做 Decision。"""
        banned = {"decide", "should_use", "best_strategy", "recommend",
                  "recommended_action", "to_instruction", "pick", "select"}
        for cls in (CognitiveContext, CognitiveContextBuilder):
            found = {n for n in dir(cls)
                     if not n.startswith("_") and n in banned}
            assert not found, f"{cls.__name__} 出现决策方法：{sorted(found)}"

    def test_no_threshold_gating_in_source(self):
        """
        反向守卫（AST）：源码里不应出现「按 confidence 阈值决定是否采用」。
        """
        import cognition.context as mod
        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                src = ast.dump(node.test)
                if "confidence" in src and "min_confidence" not in src:
                    raise AssertionError(
                        f"context.py 出现按 confidence 的门控分支：{src[:120]}"
                    )


# ================================================================
# 6. §十六 / §二十二 结构与可观测性
# ================================================================
class TestStructureAndStats:
    def test_context_keeps_structure_not_just_string(self, seeded):
        """§十：内部保留结构，文本渲染只是最后一步。"""
        b = CognitiveContextBuilder(seeded)
        ctx = b.build("agent_a", domain="fiction", situation="story_planning",
                      stimulus="s", task="t", identity="Agent A")
        sources = ctx.sources()
        labels = {s.source for s in sources}
        assert {"identity", "stimulus", "task"} <= labels
        assert "strategy" in labels
        assert all(isinstance(s.priority, float) for s in sources)

    def test_stats_contain_no_raw_text(self, seeded):
        """§二十二：可观测信息不含上下文原文。"""
        b = CognitiveContextBuilder(seeded)
        ctx = b.build("agent_a", domain="fiction",
                      stimulus="非常敏感的输入内容", task="t")
        stats = ctx.stats()
        assert stats["stimulus_present"] is True
        blob = str(stats)
        assert "非常敏感的输入内容" not in blob

    def test_stats_shape(self, seeded):
        b = CognitiveContextBuilder(seeded)
        s = b.build("agent_a", domain="fiction").stats()
        assert set(s) >= {"selected_claims", "selected_strategies",
                          "task_present", "stimulus_present"}

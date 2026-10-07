"""
Self Model V1 —— 持久化 / 检索 / Strategy 生命周期
============================================================

对应 Self Model V1 第一阶段验收标准（§二十一 / §二十六）：
    1. Self Model Persistence（含 restart）
    2. Self Model Retrieval（只检索相关内容）
    3. Strategy Lifecycle（create / retrieve / revise / abandon）
    4. 无关 Self Model 不会无条件全部进入
🔒 以及 §十 的关键验收：**Strategy 没有被 Runtime 变成 Rule**。
"""

from __future__ import annotations

import ast
import os

import pytest

from selfmodel import (
    EVIDENCE_EXPERIENCE, EVIDENCE_REFLECTION,
    Evidence, Scope, SelfModelStore, SelfModelView, Strategy,
)
from storage.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "selfmodel.db"))
    d.initialize()
    yield d
    try:
        d.close()
    except Exception:                                  # noqa: BLE001
        pass


@pytest.fixture()
def store(db):
    return SelfModelStore(db)


# ================================================================
# 1. 持久化 + restart
# ================================================================
class TestPersistence:
    def test_claim_survives_reconstruction(self, tmp_path):
        """§十一：Agent 重启后 Self Model 仍然存在。"""
        path = str(tmp_path / "p.db")
        d1 = Database(path)
        d1.initialize()
        s1 = SelfModelStore(d1)
        claim = s1.add_claim(
            "agent_a", "我比较擅长人物塑造",
            scope=Scope("Digital Production", "Fiction", "Character Design"),
            confidence=0.7,
            evidence=[Evidence(EVIDENCE_EXPERIENCE, "3 个任务")],
        )
        s1.add_strategy("agent_a", "先确定核心冲突，再扩展人物关系",
                        domain="fiction", situation="early_story_design",
                        rationale="过去直接扩写导致大量返工", confidence=0.68)
        d1.close()

        # 全新的 Database + Store（模拟重启）
        d2 = Database(path)
        d2.initialize()
        s2 = SelfModelStore(d2)
        got = s2.get_claim(claim.claim_id)
        assert got is not None, "重启后 claim 消失了"
        assert got.content == "我比较擅长人物塑造"
        assert got.confidence == pytest.approx(0.7)
        assert got.scope.domain == "Fiction"
        assert len(got.evidence) == 1
        assert got.evidence[0].source == EVIDENCE_EXPERIENCE

        strategies = s2.list_strategies("agent_a")
        assert len(strategies) == 1
        assert strategies[0].confidence == pytest.approx(0.68)
        d2.close()

    def test_revise_keeps_created_at(self, store):
        """修正不改变 created_at —— 「这条认知怎么演化的」要可追溯。"""
        s = store.add_strategy("agent_a", "S1", confidence=0.8)
        revised = store.revise_strategy(s.strategy_id, confidence=0.3)
        assert revised.created_at == s.created_at
        assert revised.confidence == pytest.approx(0.3)
        assert revised.updated_at >= s.updated_at


# ================================================================
# 2. 检索 —— 只返回相关内容
# ================================================================
class TestRetrieval:
    def _seed(self, store):
        store.add_claim(
            "agent_a", "我比较擅长人物塑造",
            scope=Scope("Digital Production", "Fiction", "Character Design"),
            confidence=0.7)
        store.add_claim(
            "agent_a", "我擅长后端性能优化",
            scope=Scope("Digital Production", "Software", "Performance"),
            confidence=0.6)
        store.add_strategy("agent_a", "先确定核心冲突再扩展人物",
                           domain="fiction", situation="early_story_design",
                           confidence=0.68)
        store.add_strategy("agent_a", "先写测试再改代码",
                           domain="software", situation="refactor",
                           confidence=0.75)

    def test_only_relevant_claims_returned(self, store):
        """§十二：检索是有选择的，不是全量注入。"""
        self._seed(store)
        v = store.retrieve("agent_a", domain="fiction")
        assert len(v.claims) == 1
        assert "人物塑造" in v.claims[0].content

    def test_irrelevant_claims_excluded(self, store):
        """§二十一.4：无关 Self Model 不应无条件全部进入。"""
        self._seed(store)
        v = store.retrieve("agent_a", domain="software")
        assert all("人物塑造" not in c.content for c in v.claims)
        assert len(v.claims) < 2, "检索把无关的也带进来了"

    def test_strategies_filtered_by_domain_and_situation(self, store):
        self._seed(store)
        v = store.retrieve("agent_a", domain="fiction",
                           situation="early_story_design")
        assert len(v.strategies) == 1
        assert v.strategies[0].domain == "fiction"

    def test_no_context_returns_everything(self, store):
        """不给情境 → 不筛（这是调用方的选择，不是 Runtime 的门控）。"""
        self._seed(store)
        v = store.retrieve("agent_a")
        assert len(v.claims) == 2
        assert len(v.strategies) == 2

    def test_scope_comparison_is_case_insensitive(self, store):
        """scope 是自由文本，大小写不应造成静默漏检。"""
        self._seed(store)
        v = store.retrieve("agent_a", domain="FICTION")
        assert len(v.claims) == 1
        assert len(v.strategies) == 1

    def test_other_agent_isolation(self, store):
        """一个 Agent 的 Self Model 不能被另一个检索到。"""
        store.add_claim("agent_a", "A 的自我认知")
        store.add_claim("agent_b", "B 的自我认知")
        va = store.retrieve("agent_a")
        assert len(va.claims) == 1
        assert va.claims[0].content == "A 的自我认知"

    def test_min_confidence_is_a_filter_not_a_decision(self, store):
        """min_confidence 是检索过滤，不是「够不够好才用」的门控。"""
        self._seed(store)
        v = store.retrieve("agent_a", min_confidence=0.65)
        assert all(c.confidence >= 0.65 for c in v.claims)
        assert all(s.confidence >= 0.65 for s in v.strategies)


# ================================================================
# 3. Strategy 生命周期
# ================================================================
class TestStrategyLifecycle:
    def test_create_and_retrieve(self, store):
        s = store.add_strategy("agent_a", "先确定核心冲突",
                               domain="fiction", rationale="返工教训",
                               confidence=0.68)
        got = store.get_strategy(s.strategy_id)
        assert got is not None and got.name == "先确定核心冲突"
        assert got.rationale == "返工教训"

    def test_revise_confidence(self, store):
        s = store.add_strategy("agent_a", "S", confidence=0.8)
        store.revise_strategy(s.strategy_id, confidence=0.4)
        assert store.get_strategy(s.strategy_id).confidence == \
            pytest.approx(0.4)

    def test_abandon_hides_from_default_retrieval(self, store):
        s = store.add_strategy("agent_a", "S", domain="fiction")
        store.abandon_strategy(s.strategy_id)
        assert store.list_strategies("agent_a") == []
        assert len(store.list_strategies("agent_a", include_retired=True)) == 1

    def test_abandon_does_not_delete(self, store):
        """🔒 放弃 ≠ 删除 —— provenance 是 Self Model 的价值。"""
        s = store.add_strategy("agent_a", "S", confidence=0.9)
        store.abandon_strategy(s.strategy_id)
        got = store.get_strategy(s.strategy_id)
        assert got is not None
        assert not got.is_active
        assert got.confidence == pytest.approx(0.9), "放弃不该抹掉原信心"

    def test_confidence_is_clamped(self, store):
        assert store.add_strategy("agent_a", "S", confidence=5.0) \
            .confidence == 1.0
        assert store.add_strategy("agent_a", "T", confidence=-3) \
            .confidence == 0.0

    def test_empty_name_rejected(self, store):
        from selfmodel import SelfModelError
        with pytest.raises(SelfModelError):
            store.add_strategy("agent_a", "   ")


# ================================================================
# 4. 证据必须有来源（§八）
# ================================================================
class TestEvidence:
    def test_unknown_source_rejected(self):
        with pytest.raises(ValueError):
            Evidence(source="made_up_source")

    def test_evidence_round_trips(self, store):
        c = store.add_claim("agent_a", "C", evidence=[
            Evidence(EVIDENCE_EXPERIENCE, "task-1", ref="task_1"),
            Evidence(EVIDENCE_REFLECTION, "复盘"),
        ])
        got = store.get_claim(c.claim_id)
        assert [e.source for e in got.evidence] == \
            [EVIDENCE_EXPERIENCE, EVIDENCE_REFLECTION]
        assert got.evidence[0].ref == "task_1"


# ================================================================
# 5. 🔴 核心验收：Strategy 没有被 Runtime 变成 Rule（§十）
# ================================================================
class TestStrategyIsNotRule:
    """
    这是本阶段最重要的架构守卫。

    §十 明确禁止：
        if applicability > 0.7: use_strategy()
        if expected_benefit > cost * 1.5: apply()

    Runtime（也就是 selfmodel 包）**只能提供数据**，
    不能替 Agent 判断「该不该用这条策略」。
    """

    BANNED_METHOD_NAMES = {
        "should_use", "should_apply", "is_applicable",
        "best_strategy", "pick_strategy", "choose_strategy",
        "select_strategy", "recommend", "recommended_strategy",
        "decide", "decide_strategy", "apply_strategy",
        "auto_apply", "use_strategy",
    }

    def _public_methods(self, cls):
        return {
            n for n in dir(cls)
            if not n.startswith("_") and callable(getattr(cls, n, None))
        }

    def test_self_model_view_has_no_decision_methods(self):
        """🔒 检索结果必须是一袋数据，不能是「最佳策略」。"""
        banned = self._public_methods(SelfModelView) & self.BANNED_METHOD_NAMES
        assert not banned, (
            f"SelfModelView 出现了替 Agent 决策的方法：{sorted(banned)}。"
            f"§十 禁止把 Strategy 变成 Rule。"
        )

    def test_store_has_no_decision_methods(self):
        banned = self._public_methods(SelfModelStore) & self.BANNED_METHOD_NAMES
        assert not banned, (
            f"SelfModelStore 出现了替 Agent 决策的方法：{sorted(banned)}"
        )

    def test_strategy_class_has_no_gating_method(self):
        banned = self._public_methods(Strategy) & self.BANNED_METHOD_NAMES
        assert not banned, (
            f"Strategy 出现了门控方法：{sorted(banned)}。"
            f"Strategy 的属性应当是**供 Agent 比较的数据**，"
            f"不是由 Runtime 求值的条件。"
        )

    def test_no_threshold_comparison_in_source(self):
        """
        反向守卫：源码里不应出现「按阈值决定是否采用策略」的比较。

        用 AST 而不是字符串搜索 —— 因为字符串搜索在本项目里
        反复被子串误报坑过（如 release 含 lease）。
        """
        import selfmodel.store as store_mod
        import selfmodel.models as models_mod

        for mod in (store_mod, models_mod):
            tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
            for node in ast.walk(tree):
                if isinstance(node, ast.If):
                    src = ast.dump(node.test)
                    # 只看「拿 strategy 的某个属性去比大小」的形状
                    if ("confidence" in src or "applicability" in src
                            or "expected_benefit" in src):
                        # 允许：min_confidence 过滤（那是检索条件）
                        if "min_confidence" in src:
                            continue
                        raise AssertionError(
                            f"{mod.__name__} 里出现了按属性阈值判断的分支："
                            f"{src[:120]} —— 这是 §十 禁止的 Strategy→Rule"
                        )

    def test_strategy_view_is_data_not_instruction(self):
        """检索结果只有数据字段，没有任何「该做什么」的字段。"""
        v = SelfModelView()
        assert hasattr(v, "claims") and hasattr(v, "strategies")
        for forbidden in ("instruction", "action", "command", "decision",
                          "recommendation"):
            assert not hasattr(v, forbidden)

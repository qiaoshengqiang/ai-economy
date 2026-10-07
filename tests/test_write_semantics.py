"""
Controlled Write 的正式语义
============================================================

    committed=True  ⇔  请求的**目标语义已经实际持久化发生**

🔴 本轮修复的核心：消除 **Silent Semantic No-op** ——
   以前 `committed=True` 可能只意味着「evidence 被更新了，
   而 Strategy 一点没变」。
"""

from __future__ import annotations

import pytest

from experience import SOURCE_FEEDBACK, ExperienceStore
from reflection import (
    KIND_ABANDON_STRATEGY, KIND_CREATE_STRATEGY, KIND_REVISE_STRATEGY,
    ReflectionProposal, SelfModelWriteService,
)
from selfmodel import SelfModelStore
from storage.database import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(str(tmp_path / "w.db"))
    db.initialize()
    sm = SelfModelStore(db)
    ex = ExperienceStore(db)
    svc = SelfModelWriteService(sm, experience_store=ex)
    yield sm, ex, svc
    try:
        db.close()
    except Exception:                                   # noqa: BLE001
        pass


def _p(agent_id, kind, *, target="", change=None, evidence=(), pid="p"):
    # 🔒 迁移（不是删除）：`fact_domain` 成为**必填声明**后，
    #    这些「合法写入」测试需要显式说明「这是 Agent 对自己的认识」。
    #    测试的**前提没变**（它们验证的是合法字段真的落库），
    #    只是多了一个必填声明。见 selfmodel/fact_domains.py。
    _c = dict(change or {})
    if kind in ("create_claim", "revise_claim",
                "create_strategy", "revise_strategy"):
        _c.setdefault("fact_domain", "self")
    return ReflectionProposal(
        proposal_id=pid, agent_id=agent_id, kind=kind, target=target,
        proposed_change=_c, reason="r",
        evidence=list(evidence), scope={})


# ================================================================
# §十六 Test A：合法字段 → 真的改变
# ================================================================
class TestLegalChange:
    def test_confidence_and_rationale_commit_with_changed_fields(self, env):
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S", confidence=0.8,
                            rationale="旧理由")
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")

        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id,
                           change={"confidence": 0.45, "rationale": "新理由"},
                           evidence=[e.experience_id]))

        assert out.committed is True
        assert out.changed_fields, "committed=True 却没有 changed_fields"
        assert set(out.changed_fields) == {"confidence", "rationale"}

        # §十七：重新读数据库证明**真的变了**
        after = sm.get_strategy(s.strategy_id)
        assert after.confidence == pytest.approx(0.45)
        assert after.rationale == "新理由"

    def test_create_strategy_reports_changed_fields(self, env):
        sm, ex, svc = env
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_p("agent_a", KIND_CREATE_STRATEGY,
                           change={"name": "新策略", "confidence": 0.6},
                           evidence=[e.experience_id]))
        assert out.committed
        assert "name" in out.changed_fields

    def test_abandon_reports_status_change(self, env):
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S")
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_p("agent_a", KIND_ABANDON_STRATEGY,
                           target=s.strategy_id,
                           evidence=[e.experience_id]))
        assert out.committed
        assert out.changed_fields == ["status"]
        assert not sm.get_strategy(s.strategy_id).is_active


# ================================================================
# §十六 Test B：只有未知字段 → 明确拒绝
# ================================================================
class TestUnknownFields:
    def test_summary_and_rules_are_rejected(self, env):
        """🔴 真实实验（eebf420）里 B 条件就是这个形状 ——
        它返回了 committed=True 却什么都没改。"""
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S", confidence=0.8)
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")

        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id,
                           change={"summary": "…", "rules": "…"},
                           evidence=[e.experience_id]))

        assert out.committed is False
        assert out.validation.code == "unsupported_change_fields"
        assert set(out.validation.fields) == {"summary", "rules"}
        assert out.changed_fields == []

        # 原对象完全不变
        after = sm.get_strategy(s.strategy_id)
        assert after.confidence == pytest.approx(0.8)
        assert after.rationale == ""

    def test_evidence_alone_is_not_a_revision(self, env):
        """§十二：只更新 evidence **不算**成功修订 Strategy。"""
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S", confidence=0.8)
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")

        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id, change={},
                           evidence=[e.experience_id]))
        assert out.committed is False
        assert out.validation.code == "no_supported_changes"

    def test_setting_same_value_is_not_a_change(self, env):
        """把 confidence 设成它已经是的值 → 没有语义变化 → 拒绝。"""
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S", confidence=0.8)
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id, change={"confidence": 0.8},
                           evidence=[e.experience_id]))
        assert out.committed is False
        assert out.validation.code == "no_supported_changes"


# ================================================================
# §十六 Test C：合法 + 非法混合 → **整体拒绝**（§十五）
# ================================================================
class TestTransactionSemantics:
    def test_mixed_valid_and_invalid_is_fully_rejected(self, env):
        """
        🔴 §十五：非法 Proposal **不得产生部分写入**。

            confidence=0.4（合法） + summary（非法）
            → 整体 rejected，且 **confidence 也不能被改**。
        """
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S", confidence=0.8)
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")

        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id,
                           change={"confidence": 0.4, "summary": "…"},
                           evidence=[e.experience_id]))

        assert out.committed is False
        assert out.validation.code == "unsupported_change_fields"
        assert out.validation.fields == ["summary"]
        # 🔴 关键：合法的那个字段**也不能**被写进去
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.8)

    def test_scope_fields_are_allowed(self, env):
        """`domain` / `situation` 既可放 proposed_change，也可放 scope。"""
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S", domain="fiction")
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id,
                           change={"domain": "software"},
                           evidence=[e.experience_id]))
        assert out.committed
        assert sm.get_strategy(s.strategy_id).domain == "software"

    def test_ownership_still_enforced(self, env):
        sm, ex, svc = env
        s = sm.add_strategy("agent_b", "B", confidence=0.9)
        e = ex.record("agent_b", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id,
                           change={"confidence": 0.1},
                           evidence=[e.experience_id]))
        assert out.committed is False
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.9)


# ================================================================
# §七 / §十一：committed 的语义
# ================================================================
class TestCommittedSemantics:
    def test_committed_always_implies_changed_fields(self, env):
        sm, ex, svc = env
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        s = sm.add_strategy("agent_a", "S", confidence=0.8)

        cases = [
            _p("agent_a", KIND_CREATE_STRATEGY, change={"name": "N"},
               evidence=[e.experience_id], pid="c1"),
            _p("agent_a", KIND_REVISE_STRATEGY, target=s.strategy_id,
               change={"confidence": 0.3}, evidence=[e.experience_id],
               pid="c2"),
            _p("agent_a", KIND_ABANDON_STRATEGY, target=s.strategy_id,
               evidence=[e.experience_id], pid="c3"),
        ]
        for p in cases:
            out = svc.apply(p)
            if out.committed:
                assert out.changed_fields, \
                    f"{p.kind} committed 但 changed_fields 为空"

    def test_changed_fields_empty_when_rejected(self, env):
        _, ex, svc = env
        out = svc.apply(_p("agent_a", KIND_CREATE_STRATEGY,
                           change={"name": "S"}, evidence=["nope"]))
        assert out.committed is False
        assert out.changed_fields == []

    def test_to_dict_exposes_changed_fields(self, env):
        sm, ex, svc = env
        s = sm.add_strategy("agent_a", "S", confidence=0.8)
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_p("agent_a", KIND_REVISE_STRATEGY,
                           target=s.strategy_id, change={"confidence": 0.2},
                           evidence=[e.experience_id]))
        d = out.to_dict()
        assert d["committed"] is True
        assert d["changed_fields"] == ["confidence"]

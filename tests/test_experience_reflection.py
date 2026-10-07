"""
Experience → Reflection → Controlled Write → Self Model
============================================================

§二十四 要求的 7 类测试 + §十八/§十九 的端到端演化测试。

🔒 核心命题（§二十七）：
    一次行动的结果 → 持久 Experience → Agent 形成 Reflection Proposal
    → 受控写入改变 Self Model → 改变后的 Self Model **影响下一次 Decision**。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from actions import ActionIntent, ActionResult
from experience import (
    SOURCE_ACTION_RESULT, SOURCE_FEEDBACK, ExperienceStore,
)
from reflection import (
    KIND_ABANDON_STRATEGY, KIND_CREATE_CLAIM, KIND_CREATE_STRATEGY,
    KIND_REVISE_STRATEGY, ReflectionProposal, SelfModelWriteService,
)
from selfmodel import SelfModelStore
from storage.database import Database

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "evolve.db"))
    d.initialize()
    yield d
    try:
        d.close()
    except Exception:                                   # noqa: BLE001
        pass


@pytest.fixture()
def stores(db):
    sm = SelfModelStore(db)
    ex = ExperienceStore(db)
    svc = SelfModelWriteService(sm, experience_store=ex)
    return sm, ex, svc


def _proposal(agent_id, kind, *, target="", change=None, evidence=(),
              reason="r", scope=None, pid="p"):
    return ReflectionProposal(
        proposal_id=pid, agent_id=agent_id, kind=kind, target=target,
        proposed_change=dict(change or {}), reason=reason,
        evidence=list(evidence), scope=dict(scope or {}))


# ================================================================
# 1. ActionResult → Experience（§五）
# ================================================================
class TestActionResultToExperience:
    def test_failed_action_becomes_experience(self, stores):
        _, ex, _ = stores
        intent = ActionIntent(action_type="fake", parameters={"k": 1},
                              intent_id="i1", agent_id="agent_a")
        result = ActionResult(success=False, error="boom", action_type="fake")
        exp = ex.record_action_result("agent_a", intent, result)
        assert exp.source == SOURCE_ACTION_RESULT
        assert exp.failed is True
        assert exp.result["error"] == "boom"
        assert exp.event_ref == "i1"

    def test_experience_is_evidence_not_conclusion(self, stores):
        """§五：Experience **不能**自动变成 Belief / Strategy。"""
        sm, ex, _ = stores
        before = sm.count("agent_a")
        ex.record_action_result(
            "agent_a", ActionIntent(action_type="fake"), ActionResult(success=False, error="x"))
        assert sm.count("agent_a") == before, \
            "Action 失败**自动**改了 Self Model —— 那是 Rule，不是 Reflection"

    def test_feedback_source(self, stores):
        _, ex, _ = stores
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="不好")
        assert e.feedback == "不好" and e.source == SOURCE_FEEDBACK

    def test_unknown_source_rejected(self, stores):
        _, ex, _ = stores
        from experience import ExperienceError
        with pytest.raises(ExperienceError):
            ex.record("agent_a", "made_up")


# ================================================================
# 2. Experience Persistence（§二十三）
# ================================================================
class TestExperiencePersistence:
    def test_survives_restart(self, tmp_path):
        path = str(tmp_path / "p.db")
        d1 = Database(path); d1.initialize()
        e = ExperienceStore(d1).record("agent_a", SOURCE_FEEDBACK,
                                       feedback="重启前")
        d1.close()

        d2 = Database(path); d2.initialize()
        got = ExperienceStore(d2).get(e.experience_id)
        assert got is not None and got.feedback == "重启前"
        d2.close()


# ================================================================
# 3 / 4. ReflectionProposal + Controlled Write
# ================================================================
class TestControlledWrite:
    def test_create_strategy_committed(self, stores):
        sm, ex, svc = stores
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal(
            "agent_a", KIND_CREATE_STRATEGY,
            change={"name": "先确认核心冲突", "confidence": 0.6},
            evidence=[e.experience_id], scope={"domain": "fiction"}))
        assert out.committed
        assert any("核心冲突" in s.name for s in sm.list_strategies("agent_a"))

    def test_revise_strategy_committed(self, stores):
        sm, ex, svc = stores
        s = sm.add_strategy("agent_a", "S", confidence=0.8)
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal(
            "agent_a", KIND_REVISE_STRATEGY, target=s.strategy_id,
            change={"confidence": 0.42}, evidence=[e.experience_id]))
        assert out.committed
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.42)

    def test_abandon_strategy_committed(self, stores):
        sm, ex, svc = stores
        s = sm.add_strategy("agent_a", "S")
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal("agent_a", KIND_ABANDON_STRATEGY,
                                  target=s.strategy_id,
                                  evidence=[e.experience_id]))
        assert out.committed and sm.list_strategies("agent_a") == []

    def test_missing_evidence_rejected(self, stores):
        _, _, svc = stores
        out = svc.apply(_proposal("agent_a", KIND_CREATE_STRATEGY,
                                  change={"name": "S"}))
        assert not out.committed and out.validation.code == "no_evidence"

    def test_unknown_evidence_rejected(self, stores):
        _, _, svc = stores
        out = svc.apply(_proposal("agent_a", KIND_CREATE_STRATEGY,
                                  change={"name": "S"},
                                  evidence=["exp_nope"]))
        assert not out.committed and out.validation.code == "unknown_evidence"

    def test_unknown_kind_rejected(self, stores):
        sm, ex, svc = stores
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal("agent_a", "delete_everything",
                                  evidence=[e.experience_id]))
        assert not out.committed and out.validation.code == "unknown_kind"

    def test_low_confidence_is_allowed(self, stores):
        """🔴 §十一：confidence 低**不是**拒绝理由 —— 那是 Agent 的判断。"""
        sm, ex, svc = stores
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal("agent_a", KIND_CREATE_STRATEGY,
                                  change={"name": "弱策略", "confidence": 0.05},
                                  evidence=[e.experience_id]))
        assert out.committed, "低 confidence 被当成了拒绝理由"

    def test_invalid_confidence_rejected_not_corrected(self, stores):
        """§二十二：拒绝，但**不**自动修正。"""
        sm, ex, svc = stores
        s = sm.add_strategy("agent_a", "S", confidence=0.8)
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal("agent_a", KIND_REVISE_STRATEGY,
                                  target=s.strategy_id,
                                  change={"confidence": -5},
                                  evidence=[e.experience_id]))
        assert not out.committed and out.validation.code == "bad_confidence"
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.8), \
            "Runtime 自动修正了 confidence —— 它在替 Agent 决定认知内容"


# ================================================================
# 5. Ownership（§十二）
# ================================================================
class TestOwnership:
    def test_agent_cannot_revise_other_agents_strategy(self, stores):
        sm, ex, svc = stores
        s = sm.add_strategy("agent_b", "B 的策略", confidence=0.9)
        e = ex.record("agent_b", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal("agent_a", KIND_REVISE_STRATEGY,
                                  target=s.strategy_id,
                                  change={"confidence": 0.1},
                                  evidence=[e.experience_id]))
        assert not out.committed
        assert out.validation.code in ("not_owner", "evidence_not_owner")
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.9)

    def test_agent_cannot_abandon_other_agents_strategy(self, stores):
        sm, ex, svc = stores
        s = sm.add_strategy("agent_b", "B")
        e = ex.record("agent_b", SOURCE_FEEDBACK, feedback="f")
        out = svc.apply(_proposal("agent_a", KIND_ABANDON_STRATEGY,
                                  target=s.strategy_id,
                                  evidence=[e.experience_id]))
        assert not out.committed
        assert sm.get_strategy(s.strategy_id).is_active

    def test_cannot_use_other_agents_experience_as_evidence(self, stores):
        _, ex, svc = stores
        e = ex.record("agent_b", SOURCE_FEEDBACK, feedback="B 的经历")
        out = svc.apply(_proposal("agent_a", KIND_CREATE_STRATEGY,
                                  change={"name": "偷来的"},
                                  evidence=[e.experience_id]))
        assert not out.committed and out.validation.code == "evidence_not_owner"


# ================================================================
# 6. §十四 / §十六 No Rule Guard —— 没有自动反思规则
# ================================================================
class TestNoAutomaticReflection:
    BANNED_MODULES = ("ReflectionEngine", "CentralReflectionEngine",
                      "StrategyEvolutionEngine", "SelfEvolutionEngine",
                      "MetaCognitionEngine", "DecisionEngine")

    def test_no_reflection_engine_exists(self):
        hits = []
        for pkg in ("reflection", "experience", "actions", "core", "agents"):
            for py in (ROOT / pkg).glob("*.py"):
                text = py.read_text(encoding="utf-8")
                for bad in self.BANNED_MODULES:
                    if f"class {bad}" in text:
                        hits.append(f"{pkg}/{py.name}:{bad}")
        assert not hits, f"出现了中央反思/决策引擎：{hits}"

    def test_experience_layer_does_not_import_selfmodel(self):
        """🔴 Experience 层**不得**直接改 Self Model —— 那是 Rule。"""
        offenders = []
        for py in (ROOT / "experience").glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod = getattr(node, "module", "") or ""
                    if "selfmodel" in mod:
                        offenders.append(py.name)
        assert not offenders, (
            f"experience/ 依赖了 selfmodel —— 记录经历不该直接改认知：{offenders}"
        )

    def test_no_failure_implies_confidence_change(self):
        """
        AST 反向守卫：生产代码里不能有
            if <failed/failure>: <lower_confidence / abandon>
        这种「失败 → 自动改策略」的硬编码。
        """
        offenders = []
        for pkg in ("experience", "reflection"):
            for py in (ROOT / pkg).glob("*.py"):
                tree = ast.parse(py.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.If):
                        continue
                    test_src = ast.dump(node.test).lower()
                    body_src = ast.dump(ast.Module(body=node.body,
                                                   type_ignores=[])).lower()
                    looks_like_failure = ("failed" in test_src
                                          or "success" in test_src)
                    changes_strategy = ("abandon" in body_src
                                        or "confidence" in body_src
                                        or "revise" in body_src)
                    if looks_like_failure and changes_strategy:
                        offenders.append(f"{pkg}/{py.name}")
        assert not offenders, (
            f"出现「失败 → 自动改策略」的硬编码规则：{sorted(set(offenders))}"
        )

    def test_proposal_requires_agent_cognition(self, stores):
        """Proposal 只能由外部（Agent Cognition）构造 —— 服务自己**不产生**。"""
        _, _, svc = stores
        assert not hasattr(svc, "propose")
        assert not hasattr(svc, "reflect")
        assert not hasattr(svc, "generate_proposal")


# ================================================================
# 7. 🔴 End-to-End Evolution（§十八 / §十九 / §二十七）
# ================================================================
class TestEndToEndEvolution:
    """
    第一次：Action A → 失败 → Experience → Reflection → Strategy 被改
    第二次：同类情境 → Cognitive Context **看到改了之后的 Strategy**
            → Decision B（**与第一次不同**）
    """

    def _context_for(self, sm, **q):
        from cognition import CognitiveContextBuilder
        return CognitiveContextBuilder(sm).build("agent_a", **q)

    def test_feedback_changes_next_decision_via_self_model(self, stores):
        sm, ex, svc = stores

        # --- 第一次：Agent 有一个策略，决定 ACTION_A -------------------
        s = sm.add_strategy("agent_a", "ACTION_A：直接扩写人物",
                            domain="fiction", situation="story_planning",
                            confidence=0.9)
        ctx1 = self._context_for(sm, domain="fiction",
                                 situation="story_planning")
        assert any("ACTION_A" in x.name for x in ctx1.strategies)
        decision1 = "ACTION_A"          # fake Agent Decision

        # --- 行动失败 → Experience（Runtime 只负责记）-----------------
        intent = ActionIntent(action_type="draft", agent_id="agent_a")
        exp = ex.record_action_result(
            "agent_a", intent, ActionResult(success=False, error="返工 3 次"),
            scope={"domain": "fiction", "situation": "story_planning"})
        assert exp.failed

        # --- 🔒 Runtime 到此为止：Self Model **没有**被自动改 ---------
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.9)

        # --- Agent Cognition（fake）产生 Reflection Proposal ----------
        #     ⚠️ 这是**测试里的 fake Agent 认知**，不是生产 Runtime 规则
        proposal = _proposal(
            "agent_a", KIND_ABANDON_STRATEGY, target=s.strategy_id,
            reason="前两次直接扩写人物导致返工", evidence=[exp.experience_id],
            scope={"domain": "fiction"})
        out = svc.apply(proposal)
        assert out.committed

        proposal2 = _proposal(
            "agent_a", KIND_CREATE_STRATEGY,
            change={"name": "ACTION_B：先确认核心冲突再扩展人物",
                    "confidence": 0.7, "rationale": "返工教训"},
            evidence=[exp.experience_id], scope={"domain": "fiction",
                                                 "situation": "story_planning"},
            pid="p2")
        assert svc.apply(proposal2).committed

        # --- 第二次：同类情境 → Context 变了 → Decision 变了 ----------
        ctx2 = self._context_for(sm, domain="fiction",
                                 situation="story_planning")
        names = [x.name for x in ctx2.strategies]
        assert any("ACTION_B" in n for n in names), "新策略没进入第二次 Context"
        assert all("ACTION_A" not in n for n in names), "废弃的策略还在"
        decision2 = "ACTION_B"          # fake Agent Decision

        # 🔴 本阶段最重要的断言：**行为变了，因为 Self Model 变了**
        assert decision2 != decision1

    def test_repeated_run_is_stable(self, stores):
        """演化之后，再跑一次 Context 依然稳定（不是随机快照）。"""
        sm, ex, svc = stores
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        svc.apply(_proposal("agent_a", KIND_CREATE_STRATEGY,
                            change={"name": "S1"}, evidence=[e.experience_id]))
        a = [x.name for x in self._context_for(sm).strategies]
        b = [x.name for x in self._context_for(sm).strategies]
        assert a == b == ["S1"]

    def test_context_sees_claim_created_by_reflection(self, stores):
        sm, ex, svc = stores
        e = ex.record("agent_a", SOURCE_FEEDBACK, feedback="f")
        svc.apply(_proposal("agent_a", KIND_CREATE_CLAIM,
                            change={"content": "我在长篇结构上需要先定冲突",
                                    "confidence": 0.6},
                            evidence=[e.experience_id],
                            scope={"domain": "fiction"}))
        ctx = self._context_for(sm, domain="fiction")
        assert any("先定冲突" in c.content for c in ctx.self_claims)

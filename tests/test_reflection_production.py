"""
Agent 自主 Reflection 进入生产 Cognitive Run
============================================================

§三十一 验收命题：
    Agent 能否在一次正常 Cognitive Run 中，读取相关历史 Experience，
    自主决定是否产生 ReflectionProposal，并通过 Controlled Write
    更新自己的 Self Model？
→ 必须是「能」，且 **Agent 决定不反思时 Runtime 必须尊重**。
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from actions import ActionDispatcher, ActionResult, ActionIntent
from cognition import CognitiveContextBuilder
from experience import SOURCE_ACTION_RESULT, ExperienceStore
from reflection import (
    KIND_ABANDON_STRATEGY, KIND_CREATE_STRATEGY, SelfModelWriteService,
)
from reflection.parse import parse_reflections
from selfmodel import SelfModelStore
from storage.database import Database

ROOT = pathlib.Path(__file__).resolve().parent.parent


class ScriptedProvider:
    """按脚本依次返回内容的确定性 provider（§二十一）。"""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    def generate(self, messages=None, system=None, **kwargs):
        from llm.base import LLMResponse
        content = self.contents[min(self.calls, len(self.contents) - 1)]
        self.calls += 1
        self.last_system = system
        return LLMResponse(provider="scripted", model="s", content=content,
                           input_tokens=1, output_tokens=1, total_tokens=2,
                           latency_ms=1.0, simulated=True)


class ScriptedRouter:
    def __init__(self, p): self.p = p
    def get_provider(self, name, credential=None): return self.p
    def generate(self, messages=None, provider=None, model=None,
                 system=None, **kwargs):
        return self.p.generate(messages=messages, system=system, **kwargs)


class RecordingExecutor:
    action_types = frozenset({"draft"})

    def __init__(self, success=True):
        self.success = success
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent.action_type)
        return ActionResult(success=self.success,
                            error=None if self.success else "返工 3 次",
                            action_id=intent.intent_id,
                            action_type=intent.action_type)


class StubAgent:
    """轻量 Agent —— 复用**真实的** BaseAgent.act 实现。"""

    def __init__(self, agent_id, brain):
        self.agent_id = agent_id
        self.name = agent_id
        self.brain = brain

    def generate(self, messages=None, **kwargs):
        return self.brain.generate(messages=messages, **kwargs)

    act = __import__("agents.base_agent",
                     fromlist=["BaseAgent"]).BaseAgent.act
    attach_self_model_services = __import__(
        "agents.base_agent", fromlist=["BaseAgent"]
    ).BaseAgent.attach_self_model_services


@pytest.fixture()
def env(tmp_path):
    db = Database(str(tmp_path / "prod.db"))
    db.initialize()
    sm = SelfModelStore(db)
    ex = ExperienceStore(db)
    svc = SelfModelWriteService(sm, experience_store=ex)
    yield sm, ex, svc, db
    try:
        db.close()
    except Exception:                                   # noqa: BLE001
        pass


def _agent(contents, sm, ex, svc):
    from agents.brain import Brain, BrainSettings
    prov = ScriptedProvider(contents)
    brain = Brain(ScriptedRouter(prov), BrainSettings(agent_id="agent_a"),
                  context_builder=CognitiveContextBuilder(
                      sm, experience_store=ex))
    a = StubAgent("agent_a", brain)
    a.attach_self_model_services(experience_store=ex, write_service=svc)
    return a, prov


def _env_json(action_type="draft", reflections=None):
    d = {"decision": "act",
         "action": {"type": action_type, "parameters": {}}}
    if reflections is not None:
        d["reflections"] = reflections
    return json.dumps(d, ensure_ascii=False)


# ================================================================
# A. Experience Retrieval（§四 / §五 / §二十五）
# ================================================================
class TestExperienceRetrieval:
    def test_selective_by_domain(self, env):
        _, ex, _, _ = env
        ex.record("agent_a", SOURCE_ACTION_RESULT,
                  result={"success": False}, scope={"domain": "fiction"})
        ex.record("agent_a", SOURCE_ACTION_RESULT,
                  result={"success": True}, scope={"domain": "robotics"})
        got = ex.retrieve_relevant("agent_a", domain="fiction")
        assert len(got) == 1 and got[0].scope["domain"] == "fiction"

    def test_top_k_limits(self, env):
        """§二十六：Context 不得无限增长。"""
        _, ex, _, _ = env
        for i in range(10):
            ex.record("agent_a", SOURCE_ACTION_RESULT, result={"success": True})
        assert len(ex.retrieve_relevant("agent_a", limit=3)) == 3

    def test_cross_agent_isolation(self, env):
        """§二十五：Agent A 看不到 Agent B 的 Experience。"""
        _, ex, _, _ = env
        ex.record("agent_b", SOURCE_ACTION_RESULT, result={"success": False})
        assert ex.retrieve_relevant("agent_a") == []

    def test_retrieval_does_not_change_self_model(self, env):
        """§六：检索不是 Reflection Policy —— 它不改任何认知。"""
        sm, ex, _, _ = env
        s = sm.add_strategy("agent_a", "S", confidence=0.9)
        ex.record("agent_a", SOURCE_ACTION_RESULT, result={"success": False})
        sm.retrieve("agent_a")
        ex.retrieve_relevant("agent_a")
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.9)

    def test_no_failure_count_rule_in_source(self):
        """AST 反向守卫：检索层不能有 failure_count 阈值触发。"""
        for py in (ROOT / "experience").glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.If):
                    src = ast.dump(node.test).lower()
                    if "failure_count" in src or "failed_count" in src:
                        raise AssertionError(
                            f"{py.name} 出现 failure_count 阈值 —— 那是 Rule")


# ================================================================
# B. Experience → Cognitive Context（§七 / §八）
# ================================================================
class TestExperienceInContext:
    def test_experience_enters_context(self, env):
        sm, ex, _, _ = env
        ex.record("agent_a", SOURCE_ACTION_RESULT,
                  result={"success": False, "error": "返工"},
                  scope={"domain": "fiction"})
        ctx = CognitiveContextBuilder(sm, experience_store=ex).build(
            "agent_a", domain="fiction")
        assert len(ctx.relevant_experiences) == 1
        assert "Relevant experiences" in ctx.to_text()

    def test_rendering_has_no_instruction_words(self, env):
        sm, ex, _, _ = env
        ex.record("agent_a", SOURCE_ACTION_RESULT,
                  result={"success": False, "error": "e"},
                  scope={"domain": "fiction"})
        text = CognitiveContextBuilder(sm, experience_store=ex).build(
            "agent_a", domain="fiction").to_text().lower()
        for w in ("must", "always", "never", "you should"):
            assert w not in text, f"经历被渲染成了指令：{w}"

    def test_experience_not_mixed_into_self_claims(self, env):
        """§八：Experience 不能伪装成 Self Claim。"""
        sm, ex, _, _ = env
        ex.record("agent_a", SOURCE_ACTION_RESULT, result={"success": False})
        ctx = CognitiveContextBuilder(sm, experience_store=ex).build("agent_a")
        assert ctx.self_claims == []
        assert len(ctx.relevant_experiences) == 1

    def test_stats_include_experiences(self, env):
        sm, ex, _, _ = env
        ex.record("agent_a", SOURCE_ACTION_RESULT, result={"success": False})
        st = CognitiveContextBuilder(sm, experience_store=ex).build(
            "agent_a").stats()
        assert st["selected_experiences"] == 1
        assert st["failed_experiences"] == 1


# ================================================================
# C. Reflection 是**可选**的（§十 / §十一 / §二十四）
# ================================================================
class TestReflectionIsOptional:
    def test_absent_reflections_is_empty(self):
        assert parse_reflections("agent_a", {"action": {}}) == []

    def test_empty_list_is_empty(self):
        assert parse_reflections("agent_a", {"reflections": []}) == []

    def test_zero_to_n_supported(self):
        env = {"reflections": [
            {"kind": KIND_ABANDON_STRATEGY, "target": "s1", "evidence": ["e1"]},
            {"kind": KIND_CREATE_STRATEGY,
             "proposed_change": {"name": "B", "fact_domain": "self"},
             "evidence": ["e1"]},
        ]}
        assert len(parse_reflections("agent_a", env)) == 2

    def test_unknown_kind_dropped_not_guessed(self):
        env = {"reflections": [{"kind": "delete_everything"}]}
        assert parse_reflections("agent_a", env) == []

    def test_agent_can_decide_not_to_reflect(self, env):
        """§二十四：Experience 存在 + Agent 决定不反思 → Self Model 不变。"""
        sm, ex, svc, _ = env
        s = sm.add_strategy("agent_a", "S", confidence=0.9)
        ex.record("agent_a", SOURCE_ACTION_RESULT, result={"success": False})

        agent, _ = _agent([_env_json("draft")], sm, ex, svc)   # 无 reflections
        agent.act(messages="继续", dispatcher=ActionDispatcher(
            [RecordingExecutor(success=False)]))

        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.9)
        assert sm.get_strategy(s.strategy_id).is_active


# ================================================================
# D. 🔴 No Automatic Reflection（§二十三 / §二十九）
# ================================================================
class TestNoAutomaticReflection:
    def test_failure_without_reflection_leaves_self_model_untouched(self, env):
        """🔴 最高原则守卫：Failure + No Reflection → No Self Model Change。"""
        sm, ex, svc, _ = env
        s = sm.add_strategy("agent_a", "ACTION_A", confidence=0.9)
        before = sm.count("agent_a")

        agent, _ = _agent([_env_json("draft")], sm, ex, svc)
        run = agent.act(messages="做", dispatcher=ActionDispatcher(
            [RecordingExecutor(success=False)]))

        assert run.action_result.success is False
        assert run.reflection_proposals == []
        assert sm.count("agent_a") == before
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.9)

    def test_experience_store_does_not_touch_self_model(self, env):
        """Experience 层不得 import selfmodel（已由上一阶段守卫覆盖）。"""
        for py in (ROOT / "experience").glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    assert "selfmodel" not in (getattr(node, "module", "") or "")

    def test_no_reflection_trigger_engine(self):
        banned = ("ReflectionEngine", "CentralReflectionEngine",
                  "ReflectionTrigger", "ReflectionScheduler",
                  "StrategyEvolutionEngine", "SelfEvolutionEngine")
        hits = []
        for pkg in ("reflection", "experience", "actions", "cognition"):
            for py in (ROOT / pkg).glob("*.py"):
                text = py.read_text(encoding="utf-8")
                for b in banned:
                    if f"class {b}" in text:
                        hits.append(f"{pkg}/{py.name}:{b}")
        assert not hits, f"出现反思触发/引擎：{hits}"


# ================================================================
# E. 🔴 End-to-End Production Path（§二十二 / §三十一）
# ================================================================
class TestEndToEndProduction:
    def test_three_run_evolution(self, env):
        """
        Run 1: Action A → failure → Experience persisted
        Run 2: Experience 进 Context → Agent **自己**提出 Reflection
               → Controlled Write → A abandoned, B created
        Run 3: Context 看到 B → Action B
        并断言 decision3 != decision1，且原因是 Agent 的 Reflection。
        """
        sm, ex, svc, _ = env

        # --- 第一次：Context 里有 ACTION_A，Agent 选 A，无反思 --------
        s = sm.add_strategy("agent_a", "ACTION_A", domain="fiction",
                            situation="planning", confidence=0.9)
        agent, prov = _agent([_env_json("draft")], sm, ex, svc)

        ctx1 = CognitiveContextBuilder(sm, experience_store=ex).build(
            "agent_a", domain="fiction", situation="planning")
        assert any("ACTION_A" in x.name for x in ctx1.strategies)
        assert ctx1.relevant_experiences == []      # 还没有经历

        run1 = agent.act(messages="开始", dispatcher=ActionDispatcher(
            [RecordingExecutor(success=False)]))
        assert run1.action_result.success is False
        assert ex.count("agent_a") == 1, "ActionResult 没变成 Experience"
        assert run1.reflection_proposals == []      # Agent 没反思
        assert sm.get_strategy(s.strategy_id).confidence == pytest.approx(0.9)

        exp_id = ex.list("agent_a")[0].experience_id

        # --- 第二次：Context 看到失败经历 → Agent **自己**提出反思 -----
        reflection_env = json.dumps({
            "decision": "act",
            "action": {"type": "draft", "parameters": {}},
            "reflections": [
                {"kind": KIND_ABANDON_STRATEGY, "target": s.strategy_id,
                 "reason": "两次返工", "evidence": [exp_id],
                 "scope": {"domain": "fiction"}},
                {"kind": KIND_CREATE_STRATEGY,
                 "proposed_change": {"name": "ACTION_B", "fact_domain": "self",
                                     "rationale": "先定核心冲突",
                                     "confidence": 0.7},
                 "reason": "返工教训", "evidence": [exp_id],
                 "scope": {"domain": "fiction",
                           "situation": "planning"}},
            ],
        }, ensure_ascii=False)
        agent2, prov2 = _agent([reflection_env], sm, ex, svc)

        ctx2 = CognitiveContextBuilder(sm, experience_store=ex).build(
            "agent_a", domain="fiction", situation="planning")
        assert len(ctx2.relevant_experiences) == 1, "经历没进第二次 Context"
        assert "返工" in ctx2.to_text()

        run2 = agent2.act(messages="继续", dispatcher=ActionDispatcher(
            [RecordingExecutor()]))

        assert len(run2.reflection_proposals) == 2
        assert all(o.committed for o in run2.write_outcomes)
        assert not sm.get_strategy(s.strategy_id).is_active, "A 没被废弃"
        assert any("ACTION_B" in x.name
                   for x in sm.list_strategies("agent_a")), "B 没被创建"

        # --- 第三次：Context 看到 B → Action B ------------------------
        ctx3 = CognitiveContextBuilder(sm, experience_store=ex).build(
            "agent_a", domain="fiction", situation="planning")
        names3 = [x.name for x in ctx3.strategies]
        assert any("ACTION_B" in n for n in names3)
        assert all("ACTION_A" not in n for n in names3)

        decision1, decision3 = "ACTION_A", "ACTION_B"
        assert decision3 != decision1

        # 🔴 关键：变化的原因是 **Agent 的 Reflection**，不是 Runtime 规则
        assert run2.reflection_proposals, "变化必须来自 Agent 提出的反思"

    def test_action_and_reflection_can_coexist(self, env):
        """§十八：Action 与 Reflection 可以同时存在，不强制一一对应。"""
        sm, ex, svc, _ = env
        e = ex.record("agent_a", SOURCE_ACTION_RESULT, result={"success": True})
        env_json = json.dumps({
            "decision": "act", "action": {"type": "draft", "parameters": {}},
            "reflections": [{"kind": KIND_CREATE_STRATEGY,
                             "proposed_change": {"name": "新策略", "fact_domain": "self"},
                             "evidence": [e.experience_id]}]})
        agent, _ = _agent([env_json], sm, ex, svc)
        ex_exec = RecordingExecutor()
        run = agent.act(messages="x", dispatcher=ActionDispatcher([ex_exec]))

        assert run.action_result is not None and run.action_result.success
        assert len(run.reflection_proposals) == 1
        assert run.write_outcomes[0].committed

    def test_reflection_only_without_action(self, env):
        """§十八：也可以只有 Reflection（Action 为空）。"""
        sm, ex, svc, _ = env
        e = ex.record("agent_a", SOURCE_ACTION_RESULT, result={"success": True})
        env_json = json.dumps({
            "decision": "reply",
            "reflections": [{"kind": KIND_CREATE_STRATEGY,
                             "proposed_change": {"name": "只有反思", "fact_domain": "self"},
                             "evidence": [e.experience_id]}]})
        agent, _ = _agent([env_json], sm, ex, svc)
        run = agent.act(messages="x", dispatcher=ActionDispatcher([]))
        assert run.action_intent is None
        assert len(run.reflection_proposals) == 1

    def test_rejected_proposal_does_not_break_run(self, env):
        """§十七：Reflection 写入失败不能破坏主要 Cognition。"""
        sm, ex, svc, _ = env
        env_json = json.dumps({
            "decision": "act", "action": {"type": "draft", "parameters": {}},
            "reflections": [{"kind": KIND_CREATE_STRATEGY,
                             "proposed_change": {"name": "X", "fact_domain": "self"},
                             "evidence": ["exp_does_not_exist"]}]})
        agent, _ = _agent([env_json], sm, ex, svc)
        ex_exec = RecordingExecutor()
        run = agent.act(messages="x", dispatcher=ActionDispatcher([ex_exec]))

        assert run.action_result.success, "主要 Cognition 被破坏了"
        assert run.write_outcomes[0].committed is False
        assert sm.list_strategies("agent_a") == []

    def test_normal_chat_still_works(self, env):
        """§十九：普通聊天继续兼容。"""
        sm, ex, svc, _ = env
        agent, prov = _agent(["就是一段普通回复"], sm, ex, svc)
        run = agent.act(messages="你好", dispatcher=ActionDispatcher([]))
        assert run.action_intent is None
        assert run.reflection_proposals == []
        assert run.decision.kind == "reply"

    def test_experience_recorded_from_production_run(self, env):
        """§二十三：生产路径真的把 ActionResult 记成了持久 Experience。"""
        sm, ex, svc, _ = env
        agent, _ = _agent([_env_json("draft")], sm, ex, svc)
        agent.act(messages="x",
                  dispatcher=ActionDispatcher([RecordingExecutor()]))
        assert ex.count("agent_a") == 1
        got = ex.list("agent_a")[0]
        assert got.source == SOURCE_ACTION_RESULT

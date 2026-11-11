"""
Minimal Action Seam —— 测试
============================================================

    Agent Decision → ActionIntent → Runtime → Executor → ActionResult

对应 §十九 要求的 5 类测试 + §二十 的 Decision-Owner 守卫。
"""

from __future__ import annotations

import ast
import json

import pytest

from actions import (
    ACTION_REPLY, ActionDispatcher, ActionIntent, ActionResult,
    CognitiveRunResult, Decision, NoopExecutor, ReplyExecutor,
    decision_from_text, extract_envelope, run_cognition,
)


class FakeExecutor:
    """§十九.4：记录它执行过什么的假执行器。"""

    action_types = frozenset({"fake_action"})

    def __init__(self):
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent)
        return ActionResult(success=True, output=f"did:{intent.action_type}",
                            action_id=intent.intent_id,
                            action_type=intent.action_type)


class FakeProvider:
    """§十九.5：按脚本返回文本的确定性 provider。"""

    def __init__(self, content):
        self.content = content
        self.seen = []

    def generate(self, messages=None, system=None, **kwargs):
        from llm.base import LLMResponse
        self.seen.append({"system": system})
        return LLMResponse(provider="fake", model="fake",
                           content=self.content, input_tokens=1,
                           output_tokens=1, total_tokens=2,
                           latency_ms=1.0, simulated=True)


class FakeRouter:
    def __init__(self, p): self.p = p
    def get_provider(self, name, credential=None): return self.p
    def generate(self, messages=None, provider=None, model=None,
                 system=None, **kwargs):
        return self.p.generate(messages=messages, system=system, **kwargs)


class StubAgent:
    """
    轻量 Agent —— 复用**真实的** `BaseAgent.act` 实现，
    但不构造完整 identity / wallet / workspace。

    🔒 用真实实现而不是测试里重写一遍逻辑，
       否则测的就不是产品代码了。
    """

    def __init__(self, agent_id, brain):
        self.agent_id = agent_id
        self.brain = brain

    def generate(self, messages=None, **kwargs):
        return self.brain.generate(messages=messages, **kwargs)

    # 直接用真实实现，保证测的是产品代码
    act = __import__("agents.base_agent", fromlist=["BaseAgent"]).BaseAgent.act


def _brain_for(content):
    from agents.brain import Brain, BrainSettings
    prov = FakeProvider(content)
    return Brain(FakeRouter(prov), BrainSettings(agent_id="agent_a")), prov


def _envelope(**action):
    return json.dumps({"decision": "act", "reasoning": "r",
                       "action": action}, ensure_ascii=False)


# ================================================================
# 1 / 2. 数据结构
# ================================================================
class TestData:
    def test_action_intent_fields(self):
        i = ActionIntent(action_type=ACTION_REPLY,
                         parameters={"content": "hi"}, agent_id="agent_a")
        assert i.action_type == ACTION_REPLY
        assert i.parameters == {"content": "hi"}
        assert i.to_dict()["agent_id"] == "agent_a"

    def test_action_intent_rejects_empty_type(self):
        with pytest.raises(ValueError):
            ActionIntent(action_type="  ")

    def test_action_result_success(self):
        r = ActionResult(success=True, output="ok")
        assert r.success and r.output == "ok" and r.error is None

    def test_action_result_failure(self):
        r = ActionResult(success=False, error="boom")
        assert not r.success and r.error == "boom"

    def test_cognitive_run_result_shape(self):
        r = CognitiveRunResult()
        assert r.decision is None and not r.has_action


class TestModelReasoning:
    def test_provider_reasoning_is_kept_separate_from_content(self):
        """
        🔴 UI 的 thinking 必须是 provider 的隐藏推理，不是回复原文。
        纯文本回复的 `decision.reasoning` 永远等于 content —— 那是信封
        字段，不是推理。两者必须是不同的字段。
        """
        from types import SimpleNamespace

        class _Agent:
            agent_id = "agent_a"

            def generate(self, messages=None, **kwargs):
                return SimpleNamespace(
                    content="done",
                    reasoning="THINK-THIS",
                    total_tokens=3,
                    finish_reason="stop",
                    cognitive_status="VALID_CONTENT",
                    output_status="VALID_CONTENT",
                )

        run = run_cognition(_Agent(), "hi")
        seg = run.segments[0]

        assert seg.content == "done"
        assert seg.model_reasoning == "THINK-THIS"
        assert seg.decision.reasoning == "done"
        assert seg.model_reasoning != seg.content


# ================================================================
# 3. Decision → ActionIntent（**不是** Runtime 猜的）
# ================================================================
class TestDecisionToIntent:
    def test_envelope_produces_intent(self):
        d = decision_from_text("agent_a", _envelope(type="reply",
                                                     parameters={"content": "x"}))
        assert d.is_action
        assert d.intent.action_type == "reply"
        assert d.intent.parameters == {"content": "x"}

    def test_plain_text_produces_no_intent(self):
        """§十四：Action 不是从自然语言里猜出来的。"""
        d = decision_from_text("agent_a", "我觉得应该写一个文件")
        assert not d.is_action
        assert d.intent is None
        assert d.is_reply_only

    def test_no_keyword_guessing(self):
        """提到 write / 我要 / I will 也**不**产生 Intent。"""
        for text in ("I will write the file now.",
                     "我要创建 outline.md",
                     'action is needed here'):
            d = decision_from_text("agent_a", text)
            assert d.intent is None, f"从自然语言猜出了 Action: {text!r}"

    def test_extract_envelope_only_accepts_explicit_json(self):
        assert extract_envelope("just text") is None
        assert extract_envelope('{"unrelated": 1}') is None
        assert extract_envelope('{"action": {"type": "x"}}') is not None

    def test_malformed_envelope_degrades_to_reply(self):
        d = decision_from_text("agent_a", '{"action": "not-an-object"}')
        assert d.intent is None and d.kind == "reply"

    def test_nested_json_is_parsed(self):
        text = '说明文字 ```json\n' + json.dumps(
            {"action": {"type": "reply",
                        "parameters": {"content": "a}b{c"}}}) + '\n```'
        d = decision_from_text("agent_a", text)
        assert d.intent.parameters["content"] == "a}b{c"


# ================================================================
# 4. ActionIntent → Executor → ActionResult
# ================================================================
class TestDispatch:
    def test_fake_executor_receives_intent(self):
        ex = FakeExecutor()
        disp = ActionDispatcher([ex])
        intent = ActionIntent(action_type="fake_action", agent_id="agent_a")
        result = disp.dispatch(intent)
        assert result.success
        assert ex.calls == [intent]

    def test_reply_executor_returns_content(self):
        disp = ActionDispatcher()
        r = disp.dispatch(ActionIntent(action_type=ACTION_REPLY,
                                       parameters={"content": "hi"}))
        assert r.success and r.output == "hi"

    def test_unknown_action_type_is_failure_not_crash(self):
        """§十七：失败是 ActionResult，不是 Runtime 崩溃。"""
        disp = ActionDispatcher()
        r = disp.dispatch(ActionIntent(action_type="does_not_exist"))
        assert r.success is False and "no executor" in r.error

    def test_executor_exception_becomes_failure(self):
        class Boom:
            action_types = frozenset({"boom"})
            def execute(self, intent):
                raise RuntimeError("kaboom")
        r = ActionDispatcher([Boom()]).dispatch(
            ActionIntent(action_type="boom"))
        assert r.success is False and "kaboom" in r.error

    def test_executor_is_replaceable(self):
        """§二十一：执行方式是可注入的。"""
        ex = FakeExecutor()
        disp = ActionDispatcher([ex])
        assert disp.supports("fake_action")
        assert not disp.supports("reply")


# ================================================================
# 5. End-to-End Fake
# ================================================================
class TestEndToEnd:
    def _agent(self, content):
        brain, prov = _brain_for(content)
        return StubAgent("agent_a", brain), prov

    def test_action_result_is_attached_to_its_segment(self):
        """
        🔴 轮次日志/UI 按 segment 读结果。只放在 run.action_results 上，
        详情页会把所有动作显示成「未执行」。
        """
        agent, _ = self._agent(_envelope(
            type="fake_action", parameters={"k": "v"}))
        ex = FakeExecutor()

        run = agent.act(messages="do something",
                        dispatcher=ActionDispatcher([ex]))

        seg = run.segments[0]
        assert seg.action_results, "结果没有挂回产生它的那一段"
        assert seg.action_results[0].success is True
        assert seg.action_results[0].output == "did:fake_action"
        assert seg.to_dict()["action_results"][0]["output"] == "did:fake_action"

    def test_full_chain_llm_to_action_result(self):
        """§十二：整条链真的跑通，不是只创建了几个数据类。"""
        agent, prov = self._agent(_envelope(
            type="fake_action", parameters={"k": "v"}))
        ex = FakeExecutor()

        run = agent.act(messages="do something",
                        dispatcher=ActionDispatcher([ex]))

        assert run.has_action
        assert run.action_intent.action_type == "fake_action"
        assert run.action_result is not None
        assert run.action_result.success
        assert run.action_result.output == "did:fake_action"
        assert ex.calls, "Executor 没有被调用"
        assert prov.seen, "LLM 没有被调用"

    def test_result_is_visible_to_upper_layer(self):
        """§十六：ActionResult 不能被 Runtime 吃掉。"""
        agent, _ = self._agent(_envelope(type="fake_action"))
        run = agent.act(messages="x",
                        dispatcher=ActionDispatcher([FakeExecutor()]))
        d = run.to_dict()
        assert d["action_result"] is not None
        assert d["action_intent"] is not None
        assert d["decision"] is not None

    def test_execute_false_only_forms_intent(self):
        agent, _ = self._agent(_envelope(type="fake_action"))
        ex = FakeExecutor()
        run = agent.act(messages="x", dispatcher=ActionDispatcher([ex]),
                        execute=False)
        assert run.action_intent is not None
        assert run.action_result is None
        assert ex.calls == []


# ================================================================
# §十八 向后兼容
# ================================================================
class TestBackwardCompatibility:
    def _agent(self, content):
        brain, _ = _brain_for(content)
        return StubAgent("agent_a", brain)

    def test_plain_reply_does_not_invoke_executor(self):
        agent = self._agent("你好，我是 Agent A。")
        ex = FakeExecutor()
        run = agent.act(messages="hi", dispatcher=ActionDispatcher([ex]))
        assert run.action_intent is None
        assert run.action_result is None
        assert ex.calls == [], "普通回复不该执行 Action"
        assert run.decision.kind == "reply"

    def test_generate_unchanged(self):
        """🔒 generate() 的既有行为完全不变（仍返回 LLMResponse）。"""
        agent = self._agent(_envelope(type="fake_action"))
        resp = agent.generate(messages="hi")
        assert hasattr(resp, "content")
        assert "fake_action" in resp.content


# ================================================================
# §二十 🔴 最重要：Runtime 没有替 Agent 决定 Action
# ================================================================
class TestDecisionOwnership:
    def test_two_decisions_produce_two_distinct_actions(self):
        """Decision A → Action A；Decision B → Action B。Runtime 不改写。"""
        class Ex:
            action_types = frozenset({"A", "B"})
            def __init__(self): self.done = []
            def execute(self, intent):
                self.done.append(intent.action_type)
                return ActionResult(success=True,
                                    action_type=intent.action_type)

        ex = Ex()
        disp = ActionDispatcher([ex])
        for expected in ("A", "B"):
            brain, _ = _brain_for(_envelope(type=expected))
            agent = StubAgent("agent_a", brain)
            run = agent.act(messages="x", dispatcher=disp)
            assert run.action_intent.action_type == expected
        assert ex.done == ["A", "B"]

    def test_dispatcher_executes_exact_intent(self):
        """§二十：Dispatcher 执行的 intent **就是**传进来的那一个。"""
        ex = FakeExecutor()
        disp = ActionDispatcher([ex])
        intent = ActionIntent(action_type="fake_action",
                              parameters={"n": 1}, agent_id="agent_a")
        disp.dispatch(intent)
        assert disp.last_dispatched is intent
        assert ex.calls[0] is intent

    def test_dispatcher_has_no_decision_methods(self):
        banned = {"decide", "choose", "should_act", "best_action",
                  "recommend", "select_action", "pick_action"}
        found = {n for n in dir(ActionDispatcher)
                 if not n.startswith("_") and n in banned}
        assert not found, f"Dispatcher 出现决策方法：{sorted(found)}"

    def test_no_central_decision_engine_module(self):
        """🔴 本项目不应存在 DecisionEngine / CentralDecisionEngine。"""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        hits = []
        for py in list(root.glob("actions/*.py")) + list(root.glob("core/*.py")):
            text = py.read_text(encoding="utf-8")
            for bad in ("class DecisionEngine", "class CentralDecisionEngine",
                        "class ActionDecisionEngine"):
                if bad in text:
                    hits.append(f"{py.name}:{bad}")
        assert not hits, f"出现了中央决策引擎：{hits}"

    def test_runtime_does_not_consult_strategy_to_pick_action(self):
        """
        反向守卫（AST）：action 层不能读 Self Model / Strategy 来决定 Action。
        —— 一旦读了，就是 Runtime 在替 Agent 做 Decision。
        """
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for py in root.glob("actions/*.py"):
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod = getattr(node, "module", "") or ""
                    names = [a.name for a in node.names]
                    if "selfmodel" in mod or "selfmodel" in names:
                        offenders.append(py.name)
        assert not offenders, (
            f"actions/ 依赖了 selfmodel —— Runtime 可能开始替 Agent 决策："
            f"{offenders}"
        )


# ================================================================
# 真 BaseAgent 上确实挂了 act()，且委托给 run_cognition
# ================================================================
class TestRealAgentHasAct:
    def test_base_agent_exposes_act(self):
        from agents.base_agent import BaseAgent
        assert hasattr(BaseAgent, "act")
        assert callable(BaseAgent.act)

    def test_act_delegates_to_run_cognition(self):
        import inspect
        from agents.base_agent import BaseAgent
        src = inspect.getsource(BaseAgent.act)
        assert "run_cognition" in src
        assert "dispatcher=dispatcher" in src

    def test_generate_still_returns_response(self):
        """🔒 act() 的加入不能破坏 generate() 的既有行为。"""
        import inspect
        from agents.base_agent import BaseAgent
        src = inspect.getsource(BaseAgent.generate)
        assert src.rstrip().endswith("return response")
        assert "def act" not in src, "act() 被误插进了 generate() 内部"


def test_segment_records_llm_latency_and_thinking():
    from types import SimpleNamespace

    class _Brain:
        def thinking_for(self, phase):
            assert phase == "decision"
            return {"enabled": True, "effort": "high"}

    class _Agent:
        agent_id = "agent_a"
        brain = _Brain()

        def generate(self, messages=None, **kwargs):
            return SimpleNamespace(
                content="done",
                reasoning="",
                total_tokens=1,
                finish_reason="stop",
                cognitive_status="VALID_CONTENT",
                output_status="VALID_CONTENT",
                latency_ms=123.4,
            )

    run = run_cognition(_Agent(), "hi")
    segment = run.segments[0]

    assert segment.latency_ms == 123.4
    assert segment.thinking == {"enabled": True, "effort": "high"}
    assert segment.to_dict()["latency_ms"] == 123.4
    assert segment.to_dict()["thinking"] == {
        "enabled": True, "effort": "high"}

"""
finish_reason=length 语义闭环 —— 测试
========================================

规格：把 `finish_reason=length` 从「技术上成功的输出」正确建模为
**认知输出被截断 / incomplete**，确保它不会被 Runtime 当成正常完成的
Cognitive Turn。

🔒 核心命题（正交的两个维度）：

    output_status    = 语法契约   —— 解析器能不能拿到结构？
    cognitive_status = 语义完成度 —— 这一轮认知**跑完了**吗？

    `VALID_CONTENT + finish_reason=length` 是合法组合，
    但它**不是**一次完整的认知。

🔒 本轮**不做 retry**、不改 AIC/成本语义、不调真实 API。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from actions.intent import CognitiveRunResult
from llm.base import (
    COGNITIVE_COMPLETE, COGNITIVE_INCOMPLETE, COGNITIVE_TRUNCATED,
    COGNITIVE_UNKNOWN, LLMResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 一段**语法完全合法**的结构化输出（截断测试的素材）
VALID_JSON = ('{"thought": "我在写第一章", '
              '"action": {"type": "write_artifact", '
              '"parameters": {"path": "a.md", "content": "x"}}}')


def _response(finish_reason, content=VALID_JSON, tokens=1000, **kw):
    return LLMResponse(content=content, provider="mock", model="mock_model",
                       finish_reason=finish_reason,
                       input_tokens=tokens // 2,
                       output_tokens=tokens - tokens // 2,
                       total_tokens=tokens, token_source="api", **kw)


class _Agent:
    """最小 Agent 桩 —— 只提供 `run_cognition` 需要的东西。"""

    agent_id = "agent_x"

    def __init__(self, response):
        self._response = response

    def generate(self, messages=None, **kwargs):
        return self._response


class _Dispatcher:
    """记录被派发过的 Action —— 用来断言「有没有真的执行」。"""

    def __init__(self):
        self.calls = []

    def dispatch(self, intent):
        self.calls.append(intent)
        from actions.intent import ActionResult
        return ActionResult(success=True, output="dispatched")


def _run(response, *, execute=True):
    from actions.run import run_cognition
    dispatcher = _Dispatcher()
    run = run_cognition(_Agent(response), messages="hi",
                        dispatcher=dispatcher, execute=execute)
    return run, dispatcher


# ============================================================
# 映射表本身
# ============================================================
class TestFinishReasonMapping:
    @pytest.mark.parametrize("reason,expected", [
        ("stop", COGNITIVE_COMPLETE),
        ("length", COGNITIVE_TRUNCATED),
        ("content_filter", COGNITIVE_TRUNCATED),
        ("tool_calls", COGNITIVE_INCOMPLETE),
        ("function_call", COGNITIVE_INCOMPLETE),
    ])
    def test_known_reasons_map(self, reason, expected):
        assert _response(reason).cognitive_status == expected

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_missing_reason_is_unknown_not_complete(self, reason):
        """🔒 未上报 ≠ COMPLETE，也 ≠ TRUNCATED。如实说不知道。"""
        status = _response(reason).cognitive_status
        assert status == COGNITIVE_UNKNOWN
        assert status != COGNITIVE_COMPLETE
        assert status != COGNITIVE_TRUNCATED

    def test_case_insensitive(self):
        assert _response("LENGTH").cognitive_status == COGNITIVE_TRUNCATED

    def test_unknown_reason_is_incomplete_not_truncated(self):
        """
        §3：**不要机械地把所有非-stop 都归类成截断。**

        认不出来的 finish_reason 如实说 INCOMPLETE ——
        那是一个诚实的「没正常跑完」，但**不是**「被截断」。
        """
        status = _response("some_future_reason").cognitive_status
        assert status == COGNITIVE_INCOMPLETE
        assert status != COGNITIVE_TRUNCATED
        assert status != COGNITIVE_COMPLETE

    def test_all_statuses_are_declared(self):
        from llm.base import VALID_COGNITIVE_STATUSES
        for reason in ("stop", "length", "content_filter", "tool_calls",
                       "nope", None):
            assert _response(reason).cognitive_status in VALID_COGNITIVE_STATUSES


# ============================================================
# Test A：正常 stop —— 行为完全不变
# ============================================================
class TestA_NormalStop:
    def test_stop_is_complete(self):
        run, _ = _run(_response("stop"))
        assert run.cognitive_status == COGNITIVE_COMPLETE
        assert run.is_truncated is False
        assert run.is_cognitive_complete is True

    def test_stop_still_executes_the_action(self):
        """🔒 回归：正常路径的 Action 执行**完全不变**。"""
        run, dispatcher = _run(_response("stop"))
        assert len(dispatcher.calls) == 1, "正常 stop 必须照常执行 Action"
        assert run.action_result is not None

    def test_stop_keeps_output_status_semantics(self):
        r = _response("stop")
        assert r.output_status == "VALID_CONTENT"
        assert r.has_valid_content is True


# ============================================================
# Test B：length —— 截断
# ============================================================
class TestB_LengthTruncated:
    def test_length_is_truncated(self):
        run, _ = _run(_response("length"))
        assert run.cognitive_status == COGNITIVE_TRUNCATED
        assert run.is_truncated is True
        assert run.is_cognitive_complete is False

    def test_length_keeps_syntactic_validity(self):
        """
        🔴 最关键的一条：`VALID_CONTENT` 与 `TRUNCATED` **可以同时成立**。

        §11：`VALID_CONTENT` 只表示「parser 能解析出合法结构」，
        它**不允许**被当成「语义已完成」。
        """
        r = _response("length")
        assert r.output_status == "VALID_CONTENT"      # 语法：合法
        assert r.cognitive_status == COGNITIVE_TRUNCATED  # 语义：截断
        assert r.is_truncated is True

    def test_length_is_not_a_provider_error(self):
        """
        §4：截断**不是** Provider Error。

        provider 正常完成了 API 请求，只是模型输出到了上限。
        `cognitive_status` 是一个**正常返回值**，不是异常。
        """
        r = _response("length")
        assert r.cognitive_status == COGNITIVE_TRUNCATED   # 正常返回
        # 取值本身不抛异常
        assert isinstance(r.cognitive_status, str)

    def test_length_carries_finish_reason_out(self):
        run, _ = _run(_response("length"))
        assert run.finish_reason == "length"
        assert run.output_status == "VALID_CONTENT"


# ============================================================
# Test C：length + 合法 Action —— 最重要的边界
# ============================================================
class TestC_LengthWithParseableAction:
    """
    🔴 **选择：保留 ActionIntent，但不执行。**

    理由：

      ① `finish_reason=length` 说明模型**还没输出完** ——
         截断点之后可能还有内容（一个更完整的 action 参数、
         一个本该跟着的 reflections 段）。
         「JSON 恰好能解析」**不能**证明「模型完成了这一轮认知」。
      ② 但 ActionIntent 是**真实发生过的事实**（模型确实输出了它），
         所以**保留**它，只是 `action_result` 为 None。
      ③ §6：**不发明新的失败行动** —— 没有 ActionFailure /
         RETRY / ABORT / RECOVER，也不引入新的 ActionResult 状态。
    """

    def test_action_is_not_dispatched(self):
        run, dispatcher = _run(_response("length"))
        assert dispatcher.calls == [], (
            "截断时绝不能执行 Action —— 完整认知可能还在后面")
        assert run.action_result is None

    def test_action_intent_is_still_visible(self):
        """事实保留：模型确实产出了这个 intent。"""
        run, _ = _run(_response("length"))
        assert run.has_action is True
        assert run.action_intent is not None

    def test_no_new_failure_action_is_invented(self):
        """§6：不产生 ActionFailure / RETRY / ABORT / RECOVER。"""
        run, _ = _run(_response("length"))
        kinds = {getattr(run.action_intent, "kind", None)}
        for banned in ("retry", "abort", "recover", "action_failure",
                       "ActionFailure"):
            assert banned not in kinds

    def test_empty_content_with_length_is_also_truncated(self):
        """截断 + 空内容：语义仍然是 TRUNCATED（不是 EMPTY 就完事）。"""
        run, dispatcher = _run(_response("length", content=""))
        assert run.cognitive_status == COGNITIVE_TRUNCATED
        assert run.output_status == "EMPTY_CONTENT"
        assert dispatcher.calls == []

    def test_execute_false_still_reports_truncated(self):
        run, dispatcher = _run(_response("length"), execute=False)
        assert run.is_truncated is True
        assert dispatcher.calls == []


# ============================================================
# Test D：下一轮 visibility
# ============================================================
class TestD_NextTurnVisibility:
    """
    §7：Turn N 截断 → 记成事实 → Turn N+1 的 CognitiveContext 能看到。

    🔒 复用 Experience 通道，**不新建表**、**不新建 CognitiveTurnStore**。
    """

    @pytest.fixture
    def agent_with_store(self, tmp_path):
        from experience.store import ExperienceStore
        from storage.database import Database

        db = Database(str(tmp_path / "exp.db"))
        db.initialize()
        store = ExperienceStore(db)

        class _Stub:
            agent_id = "agent_x"
        stub = _Stub()
        # 只给 act() 需要的两个东西
        stub.experience_store = store
        stub.action_dispatcher = None
        stub.self_model_write_service = None
        yield stub, store
        db.close()

    def _record_via_act(self, stub, response):
        """直接走 BaseAgent.act() 的记录逻辑（用一个最小的宿主）。"""
        from agents.base_agent import BaseAgent

        class _Host(_Agent):
            agent_id = "agent_x"
            experience_store = stub.experience_store
            action_dispatcher = None
            self_model_write_service = None

        host = _Host(response)
        # act() 定义在 BaseAgent 上，这里显式绑定到宿主桩
        return BaseAgent.act(host, messages="hi")

    def test_truncation_is_persisted_as_observation(self, agent_with_store):
        stub, store = agent_with_store
        self._record_via_act(stub, _response("length"))

        rows = store.list("agent_x")
        assert rows, "截断必须被持久化成一条 Experience"
        exp = rows[0]
        assert exp.result.get("observed_state") == "cognitive_output_truncated"
        assert exp.result.get("cognitive_output") == "TRUNCATED"
        assert exp.result.get("finish_reason") == "length"

    def test_normal_stop_records_no_truncation_experience(self,
                                                          agent_with_store):
        """🔒 回归：正常 stop 不产生截断 Experience。"""
        stub, store = agent_with_store
        self._record_via_act(stub, _response("stop"))
        rows = store.list("agent_x")
        assert not [r for r in rows
                    if r.result.get("observed_state")
                    == "cognitive_output_truncated"]

    def test_experience_renders_into_next_context(self, tmp_path):
        """
        下一轮 CognitiveContext 必须**看得见**这一事实。

        不是「只存在库里但 Agent 看不到」。
        """
        from cognition.context import CognitiveContext, ContextSource
        from experience.models import Experience

        exp = Experience(
            experience_id="exp_x", agent_id="agent_x", source="observation",
            result={"observed_state": "cognitive_output_truncated",
                    "cognitive_output": "TRUNCATED",
                    "finish_reason": "length"},
            feedback="上一轮认知输出未完整完成：模型输出达到 token 上限"
                     "（finish_reason=length）。这一轮认知被标记为 TRUNCATED。",
        )
        ctx = CognitiveContext(
            relevant_experiences=[exp],
            other=[ContextSource(source="self", content="(other)")])
        text = ctx.to_text()

        assert "TRUNCATED" in text or "truncated" in text, text
        assert "length" in text, text
        # 🔒 不写策略：不许出现「你必须重试」这类指令
        for banned in ("你必须要", "你必须", "must retry", "应该重试"):
            assert banned not in text, f"Context 里出现了策略指令：{banned}"


# ============================================================
# §16 Cost：AIC / token 语义不变
# ============================================================
class TestCostSemanticsUnchanged:
    def test_truncated_is_not_a_refund(self):
        """
        🔒 截断**不退款**：token 真的花了，账就要真的记。

        这里只断言语义（`cost_of` 照常计费），不碰 ledger 实现。
        """
        from llm.cost import CostCalculator

        calc = CostCalculator.from_config({})
        r = _response("length", content="x" * 4000)
        cost, tokens, known = calc.cost_of(r)
        assert tokens > 0, "截断的响应同样消耗了 token"
        assert known is True, "用了 API 报的 usage，应当 known"
        assert cost > 0, f"应当按费率计费，实际 {cost}"

    def test_cognitive_status_does_not_enter_cost_input(self):
        """`cognitive_status` 不参与计费 —— 成本只由 token 决定。"""
        from llm.cost import CostCalculator

        calc = CostCalculator.from_config({})
        stop = _response("stop", content="x" * 4000)
        trunc = _response("length", content="x" * 4000)
        assert calc.cost_of(stop)[:2] == calc.cost_of(trunc)[:2]


# ============================================================
# §14 反向守卫
# ============================================================
class TestGuardsAgainstRegression:
    """
    🔒 结构级（AST / 语义）守卫，不是简单子串匹配。

    目标是防止未来有人重新写出
        `if finish_reason == "length": output_status = VALID_CONTENT`
    或者
        `if parsed_action: execute()`
    从而绕过 TRUNCATED 语义。
    """

    def test_output_status_is_not_derived_from_finish_reason(self):
        """
        🔴 `output_status` 的判据**只能**是 content 是否为空。

        如果哪天它开始看 `finish_reason`，两个维度就退化了 ——
        那正是这个 Bug 的成因。
        """
        tree = ast.parse((PROJECT_ROOT / "llm" / "base.py").read_text(
            encoding="utf-8"))
        prop = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "output_status"):
                prop = node
        assert prop is not None, "找不到 output_status"

        body = self._body_without_docstring(prop)
        assert "finish_reason" not in body, (
            "output_status 不得从 finish_reason 推导 —— 两个维度必须正交")
        assert "self.content" in body

    @staticmethod
    def _body_without_docstring(func) -> str:
        """
        取函数体的**可执行代码**，去掉 docstring。

        ⚠️ 必须去 docstring：说明文字里正好会提到另一个属性名，
           直接 `ast.unparse(func)` 会把自己绊倒
           （AGENTS.md §12 的「子串匹配误报」，我已经栽过一次）。
        """
        clone = ast.parse(ast.unparse(func)).body[0]
        if (clone.body and isinstance(clone.body[0], ast.Expr)
                and isinstance(clone.body[0].value, ast.Constant)
                and isinstance(clone.body[0].value.value, str)):
            clone.body = clone.body[1:] or [ast.Pass()]
        return ast.unparse(clone)

    def test_cognitive_status_is_not_written_from_output_status(self):
        """反向也成立：`cognitive_status` 不看 content。"""
        tree = ast.parse((PROJECT_ROOT / "llm" / "base.py").read_text(
            encoding="utf-8"))
        prop = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "cognitive_status")
        body = self._body_without_docstring(prop)
        assert "output_status" not in body
        assert "self.content" not in body, (
            "cognitive_status 只由 finish_reason 决定")
        assert "finish_reason" in body

    def test_no_code_forces_valid_content_on_length(self):
        """
        🔴 全仓不许出现「把 length 强行说成 VALID_CONTENT」。

        只扫**可执行代码**（去注释），且用 AST 检查赋值语句。
        """
        offenders = []
        for folder in ("llm", "actions", "agents", "core", "services"):
            for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
                if "__pycache__" in str(path):
                    continue
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    # if <... finish_reason ...>:  <... VALID_CONTENT ...>
                    if not isinstance(node, (ast.If, ast.Assign)):
                        continue
                    text = ast.unparse(node)
                    if ("finish_reason" in text
                            and '"VALID_CONTENT"' in text):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            f"出现了「finish_reason → VALID_CONTENT」的回归：{offenders}")

    def test_action_dispatch_is_gated_on_truncation(self):
        """
        🔴 `actions/run.py` 的 Action 派发必须被截断挡住。

        🔒 用 AST 检查：`dispatcher.dispatch(...)` 必须位于一个
           受 `truncated` 保护的 if 里（或其 else 分支里）。
        """
        source = (PROJECT_ROOT / "actions" / "run.py").read_text(
            encoding="utf-8")
        assert "truncated" in source, "run.py 里没有截断判定"

        tree = ast.parse(source)
        # 找到含 dispatch 调用的那个 if（必须有 else 分支）
        gated = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            body = ast.unparse(node)
            if "dispatch(" in body and node.orelse:
                if "truncated" in ast.unparse(node.test) or \
                        "truncated" in ast.unparse(node.orelse):
                    gated = True
        assert gated, (
            "dispatcher.dispatch(...) 没有被「截断 → 不执行」的分支保护")

    def test_no_retry_engine_introduced(self):
        """§15：本轮不许出现自动 retry / RetryEngine。"""
        banned = ("RetryEngine", "RetryPolicy", "AutoRetry",
                  "CognitiveRetry", "RetryScheduler")
        offenders = []
        for folder in ("llm", "actions", "agents", "core", "services"):
            for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
                if "__pycache__" in str(path):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name in banned:
                        offenders.append(f"{path.name}::{node.name}")
        assert not offenders, f"出现了被禁止的 retry 组件：{offenders}"

    def test_truncation_does_not_raise(self):
        """§4：截断不伪装成 provider failure —— 取值不抛异常。"""
        for reason in ("length", "content_filter", "stop", None):
            r = _response(reason)
            assert r.cognitive_status                 # 不抛
            assert isinstance(r.is_truncated, bool)

"""
Provider 输出契约 —— §四 / §五 / §六 的确定性验证
========================================================

【这个文件为什么存在】

    真实实验（commit 6322092）里观察到 `deepseek-flash` 返回：

        {"content": "", "reasoning_content": "……很长的推理……"}

    而 `usage.completion_tokens` 依然很大（推理 token 也是输出 token）。

    当时的后果非常具体，而且**差点被写成一个假结论**：

        · API 调用「成功」            → success=True
        · token 照常计费              → 成本真的进了账
        · 但 external content 是空的  → Agent 一个字都没拿到
        · 日志里看起来像是「这个 Agent 选择不动手」

    我第一遍就是这么读的。**那是错的** —— 它首先是
    **Provider 输出契约**问题。

【本文件锁定的三条语义（§五）】

    1. `content="" + reasoning_content="…"` → **EMPTY_CONTENT**
       **不得**因为「有 reasoning 字段」就升级成 VALID_CONTENT
    2. `reasoning_content` **永远不是** content
       **禁止** `if not content: content = reasoning_content`
    3. EMPTY_CONTENT **仍然有成本**（§八）
       「没有生产产出」≠「没有经济成本」

【为什么用 Mock 而不是真实 API】

    §九 明确要求：「Mock / unit test 必须承担确定性验证」。
    真实 API **不保证**复现空产出，所以不能靠它验收。
"""

from __future__ import annotations

import json

import pytest

from llm.base import (
    OUTPUT_EMPTY_CONTENT,
    OUTPUT_VALID_CONTENT,
    VALID_OUTPUT_STATUSES,
    LLMResponse,
)

# ================================================================
# 构造返回体的工具
# ================================================================


def _provider():
    from llm.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        provider_config={"base_url": "https://example.invalid/v1",
                         "api_key_env": "NOT_USED"},
        default_model="test-model",
    )


def _http_result(body: dict):
    """造一个 HTTPResult，body 就是「网关返回的 JSON」。"""
    from llm.openai_compatible import HTTPResult

    return HTTPResult(status=200, body=json.dumps(body), headers={})


def _body(message: dict, *, usage=None, finish_reason="stop") -> dict:
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason}],
        "usage": usage if usage is not None else {
            "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _parse(message: dict, **kw) -> LLMResponse:
    return _provider().parse_response(
        _http_result(_body(message, **kw)), model="test-model", latency_ms=1.0)


# ================================================================
# §六 Case A–F
# ================================================================
class TestOutputContractCases:
    """§六 明确列出的 6 个 Case，逐条落成测试。"""

    # ---- Case A ----
    def test_case_a_valid_content_is_valid(self):
        """content="hello" + reasoning_content="internal reasoning" → VALID。"""
        resp = _parse({"role": "assistant", "content": "hello",
                       "reasoning_content": "internal reasoning"})

        assert resp.output_status == OUTPUT_VALID_CONTENT
        assert resp.content == "hello"
        # 🔒 reasoning **没有**被拼进 content
        assert "internal reasoning" not in resp.content
        # 但它被保留下来了（诊断用）
        assert resp.reasoning == "internal reasoning"

    # ---- Case B ----
    def test_case_b_empty_content_with_reasoning_is_empty(self):
        """
        真实实验遇到的那一种：content="" + reasoning_content="…"。
        必须归类为 EMPTY_CONTENT，**不得生成假 production content**。
        """
        resp = _parse({"role": "assistant", "content": "",
                       "reasoning_content": "some reasoning"},
                      usage={"prompt_tokens": 10, "completion_tokens": 4095,
                             "total_tokens": 4105})

        assert resp.output_status == OUTPUT_EMPTY_CONTENT
        assert resp.content == ""
        # 🔴 最关键的断言：reasoning 绝不能被当成 content
        assert resp.content != "some reasoning"
        assert "some reasoning" not in resp.content
        assert resp.reasoning == "some reasoning"
        assert resp.reasoning_only is True
        # §八：成本照样存在，且仍然可见
        assert resp.output_tokens == 4095

    # ---- Case C ----
    def test_case_c_null_content_is_an_explicit_failure(self):
        """
        content=null → 「EMPTY_CONTENT 或等价的明确失败状态」（§六 允许后者）。

        这里选择**明确失败**：网关返回畸形结构必须被看见。
        如果把它降级成普通的空产出，一个坏掉的网关就会伪装成
        「模型今天没说话」。
        """
        from llm.base import InvalidResponseError

        with pytest.raises(InvalidResponseError) as exc:
            _parse({"role": "assistant", "content": None,
                    "reasoning_content": "..."})

        assert "content" in str(exc.value)
        # 报错信息必须点明「reasoning 不能代替 content」
        assert "reasoning_content" in str(exc.value)

    # ---- Case D ----
    def test_case_d_missing_content_field_is_an_explicit_failure(self):
        """完全没有 content 字段 → 同样的明确 provider-level failure。"""
        from llm.base import InvalidResponseError

        with pytest.raises(InvalidResponseError) as exc:
            _parse({"role": "assistant"})

        assert "content" in str(exc.value)

    # ---- Case E ----
    def test_case_e_whitespace_only_content_is_empty(self):
        """content="   " → EMPTY_CONTENT（空白不是内容）。"""
        resp = _parse({"role": "assistant", "content": "   "})

        assert resp.output_status == OUTPUT_EMPTY_CONTENT
        assert resp.has_valid_content is False

    # ---- Case F ----
    def test_case_f_normal_response_does_not_regress(self):
        """正常 response 结构：现有行为不变。"""
        resp = _parse({"role": "assistant",
                       "content": "print('hello')"},
                      usage={"prompt_tokens": 5, "completion_tokens": 7,
                             "total_tokens": 12})

        assert resp.content == "print('hello')"
        assert resp.output_status == OUTPUT_VALID_CONTENT
        assert resp.provider == "openai_compatible"
        assert resp.model == "test-model"
        assert (resp.input_tokens, resp.output_tokens, resp.total_tokens) == \
            (5, 7, 12)
        assert resp.token_source == "api"
        assert resp.simulated is False
        assert resp.finish_reason == "stop"
        assert resp.reasoning is None


# ================================================================
# 反向测试：这些语义必须**真的**能变红
# ================================================================
class TestContractGuardsFire:
    """
    🔒 本项目反复栽在「守卫假绿」上。
       所以要证明：如果契约被写坏，测试**真的**会红。

    做法不是「另写一个近似断言」，而是直接把坏实现注入进来，
    用**同一套断言**跑，确认它失败。
    """

    @staticmethod
    def _assert_contract(resp):
        """
        与 Case A/B/E 同源的断言（抽出来供正反两用）。

        🔒 注意这里**必须**包含「reasoning 不得混进 content」这条 ——
           否则一个把 reasoning 回填成 content 的坏实现
           在 `output_status` 层面看起来完全正常
           （因为状态是从 content 派生的，它只会说「有内容」）。
           那正是本 Bug 的同一类错误在更高一层的复现。
        """
        assert resp.output_status in VALID_OUTPUT_STATUSES
        if not resp.content.strip():
            assert resp.output_status == OUTPUT_EMPTY_CONTENT
            assert resp.has_valid_content is False
        else:
            assert resp.output_status == OUTPUT_VALID_CONTENT
        # 🔴 关键那条：推理**不是**内容
        if resp.reasoning:
            assert resp.reasoning not in resp.content

    def test_reverse_a_broken_provider_that_backfills_reasoning_fails(self):
        """
        模拟 §五 明令禁止的那种修法：

            if not content:
                content = reasoning_content

        用一个「坏 Provider」跑同一套断言，确认它**会被抓住**。
        """
        class BrokenProvider:
            """故意写坏：把 reasoning 回填成 content。"""

            def parse(self, message: dict) -> LLMResponse:
                content = message.get("content") or ""
                reasoning = message.get("reasoning_content")
                if not content and reasoning:          # ← 被禁止的修法
                    content = reasoning
                return LLMResponse(content=content, provider="broken",
                                   model="m", reasoning=reasoning)

        broken = BrokenProvider().parse(
            {"content": "", "reasoning_content": "some reasoning"})

        # 坏实现确实把 reasoning 当成了 content
        assert broken.content == "some reasoning"
        # → 断言合同会红
        with pytest.raises(AssertionError):
            self._assert_contract(broken)

    def test_reverse_the_real_provider_passes_the_same_assertions(self):
        """修好之后，真 Provider 必须通过**同一套**断言。"""
        self._assert_contract(
            _parse({"content": "", "reasoning_content": "r"}))
        self._assert_contract(
            _parse({"content": "hello", "reasoning_content": "r"}))
        self._assert_contract(_parse({"content": "   "}))

    def test_output_status_cannot_contradict_content(self):
        """
        契约状态是**派生**的，不是可写字段 ——
        所以不可能构造出 `content=""` 但状态是 VALID_CONTENT 的对象。
        """
        resp = LLMResponse(content="", provider="p", model="m")
        assert resp.output_status == OUTPUT_EMPTY_CONTENT
        with pytest.raises(AttributeError):
            resp.output_status = OUTPUT_VALID_CONTENT   # 只读属性


# ================================================================
# §五：reasoning 的处理边界
# ================================================================
class TestReasoningIsNotContent:
    def test_reasoning_field_variants_are_recognised(self):
        """DeepSeek 用 reasoning_content，其它网关用 reasoning —— 都识别。"""
        a = _parse({"content": "x", "reasoning_content": "r1"})
        b = _parse({"content": "x", "reasoning": "r2"})
        c = _parse({"content": "x"})

        assert a.reasoning == "r1"
        assert b.reasoning == "r2"
        assert c.reasoning is None

    def test_reasoning_is_never_concatenated_into_content(self):
        resp = _parse({"content": "ANSWER", "reasoning_content": "THOUGHTS"})

        assert resp.content == "ANSWER"
        assert "THOUGHTS" not in resp.content

    def test_reasoning_chars_recorded_without_leaking_reasoning(self):
        """`to_dict()` 只暴露 reasoning 的**长度**，不泄露推理原文。"""
        resp = _parse({"content": "", "reasoning_content": "很长的推理" * 100})
        d = resp.to_dict()

        assert d["output_status"] == OUTPUT_EMPTY_CONTENT
        assert d["reasoning_chars"] == len("很长的推理" * 100)
        assert "很长的推理" not in json.dumps(d, ensure_ascii=False)


# ================================================================
# §七 / §八：空 content 不许悄悄成功，但成本必须留着
# ================================================================
class TestEmptyContentSemantics:
    def test_empty_content_is_flagged_in_metadata(self):
        resp = _parse({"content": "", "reasoning_content": "r"},
                      usage={"prompt_tokens": 10, "completion_tokens": 900,
                             "total_tokens": 910})

        marker = resp.metadata.get("empty_content")
        assert marker is not None
        assert marker["output_tokens"] == 900
        assert marker["reasoning_chars"] == 1

    def test_valid_content_gets_no_empty_marker(self):
        resp = _parse({"content": "ok"})
        assert "empty_content" not in resp.metadata

    def test_empty_content_still_reports_full_cost(self):
        """
        §八：**不许**为了让实验数据好看而冲销成本。

        EMPTY_CONTENT 的响应必须带着完整的 token 数字交给上层，
        这样 recorder 才能照常记账。
        """
        resp = _parse({"content": "", "reasoning_content": "r"},
                      usage={"prompt_tokens": 2273, "completion_tokens": 3781,
                             "total_tokens": 6054})

        assert resp.output_status == OUTPUT_EMPTY_CONTENT
        assert resp.total_tokens == 6054
        assert resp.usage_from_api is True
        assert resp.token_source == "api"

    def test_empty_content_is_not_an_exception(self):
        """
        EMPTY_CONTENT **不是**异常：调用确实成功了，钱确实花了。

        把它变成异常会让「失败了」和「成功了但没内容」看起来一样 ——
        而那正是本 Bug 的成因。
        """
        resp = _parse({"content": "", "reasoning_content": "r"})
        assert isinstance(resp, LLMResponse)


# ================================================================
# 架构边界：契约住在 llm/，不在 agents/
# ================================================================
class TestContractLivesInLLMLayer:
    def test_agents_do_not_branch_on_output_status_of_specific_providers(self):
        """§三：Brain 里不许出现具体 Provider 名。"""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        known = {"mock", "deepseek", "doubao", "grok",
                 "bailian_qwen", "bailian_glm"}
        for name in ("brain.py", "base_agent.py"):
            tree = ast.parse((root / "agents" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                for comparator in node.comparators:
                    if (isinstance(comparator, ast.Constant)
                            and isinstance(comparator.value, str)):
                        assert comparator.value.lower() not in known, (
                            f"{name} 里出现了和 {comparator.value!r} 的比较")

    def test_openai_compatible_never_backfills_from_reasoning(self):
        """
        🔒 源码级守卫：`llm/` 里**不存在**把 reasoning 赋给 content 的写法。

        AST 扫描 `content = <reasoning 相关>` 这种赋值。
        反向测试见下一条。
        """
        from pathlib import Path

        offenders = _scan_reasoning_backfill(
            Path(__file__).resolve().parent.parent / "llm")
        assert offenders == [], (
            "出现了 reasoning → content 的回填（§五 明令禁止）：\n  "
            + "\n  ".join(offenders))

    def test_reverse_reasoning_backfill_guard_fires(self, tmp_path):
        """**反向测试**：把被禁止的写法真的写进一个文件，守卫必须变红。"""
        evil = tmp_path / "llm"
        evil.mkdir()
        (evil / "bad_provider.py").write_text(
            "def parse(message):\n"
            "    content = message.get('content')\n"
            "    reasoning_content = message.get('reasoning_content')\n"
            "    if not content:\n"
            "        content = reasoning_content\n"
            "    return content\n",
            encoding="utf-8")

        offenders = _scan_reasoning_backfill(evil)
        assert offenders, "守卫认不出被禁止的回填 —— 它是假绿的"


def _scan_reasoning_backfill(root) -> list:
    """
    扫一棵源码树，找 `content = <名字里带 reasoning 的东西>` 这类赋值。

    🔒 按 **AST 赋值节点**比对，不做子串匹配 ——
       本项目真实发生过 `agent_directory` 含 `agent_d`、
       `seen_tracker` 含 `ack` 这类误报。
    """
    import ast

    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:                                  # AnnAssign 无值
                continue
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if "content" not in names:
                continue
            used = [n.id for n in ast.walk(value) if isinstance(n, ast.Name)]
            used += [n.attr for n in ast.walk(value)
                     if isinstance(n, ast.Attribute)]
            if any("reasoning" in name.lower() for name in used):
                offenders.append(f"{path.name}:{node.lineno} content ← reasoning")
    return offenders


# ================================================================
# §七：EMPTY_CONTENT 不许在 Cognition 里悄悄成功
# ================================================================
class TestEmptyContentDoesNotSilentlySucceedInCognition:
    """
    生产路径：`LLMResponse → Brain → Decision → Action`。

    🔒 关键风险：一次「API 成功但 content 为空」的调用，
       在 Cognition 里会变成 `Decision(kind="reply", reasoning="")` ——
       与「Agent 主动决定不作答」一模一样。

    ✅ 修法**不是**新增 Runtime / Stage 状态（§七 禁止扩张），
       而是让 `CognitiveRunResult` 如实带上契约状态。
    """

    @staticmethod
    def _agent(response):
        class _A:
            agent_id = "novel_world_builder"

            def generate(self, messages=None, **kw):
                return response

        return _A()

    def test_empty_content_produces_no_action(self):
        """空内容 → 不产生 Action（不允许凭空造出一个生产动作）。"""
        from actions.run import run_cognition
        from llm.base import LLMResponse

        empty = LLMResponse(content="", provider="deepseek",
                            model="deepseek-flash", output_tokens=4348,
                            reasoning="很长的推理")

        result = run_cognition(self._agent(empty), messages="...")

        assert result.has_action is False
        assert result.action_intent is None
        assert result.action_result is None
        assert result.reflection_proposals == []

    def test_empty_content_is_visible_on_the_result(self):
        """关键：结果上必须**看得见**它是 EMPTY_CONTENT。"""
        from actions.run import run_cognition
        from llm.base import LLMResponse

        empty = LLMResponse(content="", provider="deepseek",
                            model="deepseek-flash", output_tokens=4348,
                            reasoning="很长的推理")

        result = run_cognition(self._agent(empty), messages="...")

        assert result.output_status == OUTPUT_EMPTY_CONTENT
        assert result.is_empty_output is True
        assert result.to_dict()["output_status"] == OUTPUT_EMPTY_CONTENT

    def test_valid_content_is_not_flagged(self):
        from actions.run import run_cognition
        from llm.base import LLMResponse

        ok = LLMResponse(content="普通回复", provider="deepseek",
                         model="deepseek-chat")

        result = run_cognition(self._agent(ok), messages="...")

        assert result.output_status == OUTPUT_VALID_CONTENT
        assert result.is_empty_output is False

    def test_unknown_contract_is_not_treated_as_success_or_failure(self):
        """
        🔒 鸭子类型取不到 `output_status`（比如一个不认识契约的假 Agent）时，
           必须是 `None` = **「不知道」**，不能默认成 VALID 也不能默认成 EMPTY。
        """
        from actions.run import run_cognition

        class _Bare:
            agent_id = "x"
            content = "hello"

            def generate(self, messages=None, **kw):
                class _R:
                    content = "hello"
                return _R()

        result = run_cognition(_Bare(), messages="...")

        assert result.output_status is None
        assert result.is_empty_output is False

    def test_reverse_the_cognition_marker_fires(self):
        """
        **反向测试**：如果 `run_cognition` 吞掉了契约状态
        （比如忘了透传），上面那条断言必须能红。
        """
        from actions.intent import CognitiveRunResult

        swallowed = CognitiveRunResult()          # 未透传的形状
        assert swallowed.output_status is None

        # 一个「正确透传」的结果必须能被区分出来
        reported = CognitiveRunResult(output_status=OUTPUT_EMPTY_CONTENT)
        assert reported.is_empty_output is True
        assert swallowed.is_empty_output != reported.is_empty_output

    def test_actions_layer_does_not_import_llm(self):
        """🔒 `actions/` 是叶子层 —— 不许为了拿常量而 import `llm/`。"""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "actions"
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for m in modules:
                    assert not m.startswith("llm"), (
                        f"actions/{path.name} 导入了 {m} —— 叶子层不该依赖 llm")


# ================================================================
# §八：EMPTY_CONTENT **仍然**有经济成本 —— 不许冲销
# ================================================================
class TestEmptyContentStillCosts:
    """
    🔒 本轮最重要的一条经济语义：

        「没有 production output」 **不等于** 「没有经济成本」。

    正确语义：
        LLM call → 真实消耗成本 → EMPTY_CONTENT → Agent 没有获得有效内容

    **禁止**为了让实验数据好看而冲销这笔成本。
    """

    def _recorder(self):
        from llm.cost import CostCalculator
        from llm.recorder import LLMCallRecorder
        from storage.database import create_memory_database

        db = create_memory_database()
        db.initialize()
        charged = []
        rec = LLMCallRecorder(
            db=db,
            tokens_per_ai_coin=10000,
            cost_calculator=CostCalculator.from_config({}),
            on_cost=lambda agent_id, cost, call_id, row: charged.append(
                (agent_id, cost)),
        )
        return db, rec, charged

    def test_empty_content_call_is_recorded_with_full_tokens(self):
        db, rec, charged = self._recorder()
        resp = LLMResponse(content="", provider="deepseek",
                           model="deepseek-flash",
                           input_tokens=2273, output_tokens=3781,
                           total_tokens=6054, token_source="api",
                           reasoning="很长的推理")

        rec.record("novel_world_builder", resp)

        row = db.fetchall("SELECT * FROM llm_calls")[0]
        assert row["total_tokens"] == 6054
        assert row["output_tokens"] == 3781
        assert row["success"] == 1          # 调用**确实**成功了
        assert row["token_source"] == "api"

    def test_empty_content_call_still_charges(self):
        db, rec, charged = self._recorder()
        resp = LLMResponse(content="", provider="deepseek",
                           model="deepseek-flash",
                           input_tokens=2273, output_tokens=3781,
                           total_tokens=6054, token_source="api")

        rec.record("novel_world_builder", resp)

        assert charged, "EMPTY_CONTENT 的调用没有被计费 —— 成本被冲销了"
        assert charged[0][1] > 0

    def test_cost_does_not_depend_on_content(self):
        """
        🔒 结构性证据：同样的 token、不同的 content，成本必须一样。

        如果哪天有人把成本算法改成「看 content 决定收不收费」，
        这条测试会红。
        """
        from llm.cost import CostCalculator

        calc = CostCalculator.from_config({})
        empty = LLMResponse(content="", provider="deepseek",
                            model="deepseek-flash", input_tokens=100,
                            output_tokens=900, total_tokens=1000,
                            token_source="api")
        full = LLMResponse(content="x" * 5000, provider="deepseek",
                           model="deepseek-flash", input_tokens=100,
                           output_tokens=900, total_tokens=1000,
                           token_source="api")

        assert calc.cost_of(empty)[:2] == calc.cost_of(full)[:2]

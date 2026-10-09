"""
🔴 **一次决策可以给出多个动作**（2026-10-10）

【用户的需求】
    「完成写作的**同时**发消息给下一个人。」

【为什么以前做不到】
    不是 API 不支持 —— `messages` 本来就支持多条。
    是**生产信封只有单数**（`envelope.get("action")`），
    再加上 prompt 里写着「一轮一个动作……想都做就分两轮」。
    ⇒ Agent 只能把它拆成两次决策，而「下一次」需要有人唤醒它
      ⇒ **拆完就断在那里**。

【改了什么】
    · `Decision.intents`（0..N）+ `intent` 保留为**兼容属性**（第一个）
    · `CognitiveRunResult.action_intents` / `action_results`（0..N）
      + `action_intent` / `action_result` 保留为兼容属性
    · `decision_from_text` 接受 `actions`（列表）**或** `action`（单数）
    · `run_cognition` 按顺序**逐个执行**，一个失败不阻止后面的
    · `BaseAgent.act` 每个动作**各记一条** Experience
    · 实验的 `_turn` 逐个投影 —— `turn["write"]` 与 `turn["message"]`
      **可以同时出现**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import (                                          # noqa: E402
    ActionDispatcher, ActionResult, ActionIntent, CognitiveRunResult,
    Decision, build_intent,
)
from actions.parse import decision_from_text                   # noqa: E402


class _EchoExecutor:
    """把 intent 原样回成一个成功结果 —— 只记录**执行顺序**。"""

    def __init__(self, types, log):
        self.action_types = frozenset(types)
        self.log = log

    def execute(self, intent):
        self.log.append(intent.action_type)
        return ActionResult(
            success=True, output={"path": intent.parameters.get("path")},
            error=None, action_id=intent.intent_id,
            action_type=intent.action_type, metadata={"code": None})


# ---- 解析层 ----
def test_single_action_still_works():
    """🔒 **向后兼容**：老信封（`action` 单数）必须继续能用。"""
    d = decision_from_text("a", json.dumps(
        {"decision": "act", "reasoning": "r",
         "action": {"type": "workspace_write", "parameters": {"path": "x.md"}}}))
    assert d.kind == "act" and len(d.intents) == 1
    assert d.intent.action_type == "workspace_write"      # 兼容属性


def test_multiple_actions_are_parsed_in_order():
    """🎯 这就是「写完正文 + 通知下一个人」。"""
    d = decision_from_text("a", json.dumps(
        {"decision": "act", "reasoning": "r", "actions": [
            {"type": "workspace_write", "parameters": {"path": "x.md"}},
            {"type": "message_send", "parameters": {"to": "agent_b"}},
        ]}))
    assert d.kind == "act"
    assert [i.action_type for i in d.intents] == [
        "workspace_write", "message_send"]
    assert d.dropped_actions == 0


def test_a_broken_entry_is_skipped_but_counted():
    """🔒 跳过写坏的那个，但**不静默** —— `dropped_actions` 如实记数。"""
    d = decision_from_text("a", json.dumps(
        {"decision": "act", "reasoning": "r", "actions": [
            {"type": ""},
            {"type": "message_send", "parameters": {"to": "b"}},
        ]}))
    assert [i.action_type for i in d.intents] == ["message_send"]
    assert d.dropped_actions == 1


def test_all_broken_degrades_to_reply():
    """一个都不成 ⇒ 和今天一样降级为普通回复，**不崩**。"""
    d = decision_from_text("a", json.dumps(
        {"decision": "act", "reasoning": "r", "actions": [{"type": ""}]}))
    assert d.kind == "reply" and d.intents == []


# ---- 执行层 ----
def test_run_cognition_executes_all_in_order(monkeypatch):
    """
    🔴 每个动作**独立执行、独立留结果** —— 一个失败**不阻止**后面的。
       理由：它们都是 Agent **自己声明的**决定，我们不替它取舍。
    """
    import actions.run as run_mod

    class _Resp:
        content = json.dumps({"decision": "act", "reasoning": "r", "actions": [
            {"type": "workspace_write", "parameters": {"path": "x.md"}},
            {"type": "message_send", "parameters": {"to": "agent_b"}},
        ]})
        output_status = "VALID_CONTENT"
        cognitive_status = "COMPLETE"
        finish_reason = "stop"

    class _Agent:
        agent_id = "agent_a"

        def generate(self, messages=None, **kw):
            return _Resp()

    log = []
    d = ActionDispatcher([
        _EchoExecutor(["workspace_write"], log),
        _EchoExecutor(["message_send"], log)])
    r = run_mod.run_cognition(_Agent(), messages="x", dispatcher=d)

    assert log == ["workspace_write", "message_send"], "执行顺序不对"
    assert len(r.action_intents) == 2 and len(r.action_results) == 2
    assert all(x.success for x in r.action_results)
    # 兼容属性仍指向**第一个**
    assert r.action_intent.action_type == "workspace_write"


def test_truncated_keeps_intents_but_executes_nothing(monkeypatch):
    """截断时不执行 —— 但**意图照样保留**（那是真实发生的事实）。"""
    import actions.run as run_mod

    class _Resp:
        content = json.dumps({"decision": "act", "reasoning": "r", "actions": [
            {"type": "workspace_write", "parameters": {"path": "x.md"}},
            {"type": "message_send", "parameters": {"to": "b"}},
        ]})
        output_status = "VALID_CONTENT"
        cognitive_status = "TRUNCATED"
        finish_reason = "length"

    class _Agent:
        agent_id = "agent_a"

        def generate(self, messages=None, **kw):
            return _Resp()

    log = []
    d = ActionDispatcher([_EchoExecutor(["workspace_write", "message_send"], log)])
    r = run_mod.run_cognition(_Agent(), messages="x", dispatcher=d)
    assert log == [], "截断了还执行"
    assert len(r.action_intents) == 2
    assert r.action_results == [None, None]


# ---- 结构守卫 ----
def test_the_envelope_instruction_documents_the_list_form():
    """Prompt 用的格式说明必须提 `actions` 列表 —— 否则 Agent 不知道能写多个。"""
    from actions.parse import ENVELOPE_INSTRUCTION

    assert '"actions"' in ENVELOPE_INSTRUCTION
    assert "more than one" in ENVELOPE_INSTRUCTION
    # 🔒 单数仍然被接受（向后兼容），要在说明里写出来
    assert "single form" in ENVELOPE_INSTRUCTION


def test_the_prompt_no_longer_says_split_into_two_rounds():
    """
    🔴 「一轮一个动作……想都做就分两轮」**现在是错的** ——
       它正是在教 Agent 把「写产物 + 通知」拆成两次决策，
       而那两次之间的「下一次」需要有人唤醒它。
    """
    from experiments.heterogeneous_production import production as P

    assert "想都做就分两轮" not in P.PROMPT
    assert "多个动作" in P.PROMPT


def test_compatibility_properties_exist():
    """🔒 老调用方只认 `intent` / `action_intent` —— 它们必须还在。"""
    d = Decision(decision_id="d", agent_id="a", kind="act",
                 intents=[ActionIntent(action_type="workspace_write",
                                       agent_id="a", intent_id="i1")])
    assert d.intent is not None and d.is_action
    empty = Decision(decision_id="d", agent_id="a", kind="reply")
    assert empty.intent is None and not empty.is_action
    r = CognitiveRunResult()
    assert r.action_intent is None and r.action_result is None
    assert not r.has_action

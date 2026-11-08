"""
信封解析失败 ≠ Agent 决定只回复
================================

【真实事故（5-Agent 空间站 run 1107-2114）】

生产者 agent_b 连续两次输出了：

    {"decision": "act", "reasoning": "...", "actions": [
        {"type": "run_tool", ...}, {"type": "workspace_read", "paramet

—— JSON 在 payload 中间断掉（provider 返回 finish_reason=stop，
   output_status=VALID_CONTENT，但内容确实不完整）。

旧行为：
    `extract_envelope()` 返回 None → `Decision(kind="reply")`
    → 报告写「Agent 没有行动」→ 看起来像 Agent 不想结项。

真实原因：
    模型**想行动**（reasoning 里明确说要发 SUBMIT），
    但输出不可用。两者必须分开统计。

本文件锁住三件事：
    ① 坏信封 → `envelope_error="unparsable_envelope"`
    ② 普通文本回复 → `envelope_error=""`（不能误报）
    ③ 合法信封 / 只有 reflection → `envelope_error=""`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.parse import (                                    # noqa: E402
    decision_from_text, looks_like_envelope,
)


# 真实事故的**缩短版**：在 payload 中间断开，且没有闭合括号。
TRUNCATED = (
    '{"decision": "act", "reasoning": "协调者指出 SUBMIT 必须由我发出。", '
    '"actions": [{"type": "run_tool", "parameters": {"tool": "pytest"}}, '
    '{"type": "message_send", "parameters": {"to": "agent_e", '
    '"message_type": "SUBMIT", "payload": {"task_id": "task_space_x", '
    '"result": {"result_ref": "backend", "result_typ'
)


class TestMalformedEnvelope:
    def test_truncated_envelope_is_not_silent(self):
        d = decision_from_text("agent_b", TRUNCATED)
        # 兼容：仍然是 reply，不崩、不产生 Intent
        assert d.kind == "reply"
        assert d.intents == []
        # 🔴 但必须**如实记下**解析失败
        assert d.envelope_error == "unparsable_envelope"
        # 原文保留，便于事后定位
        assert d.reasoning == TRUNCATED

    def test_plain_text_reply_is_not_flagged(self):
        d = decision_from_text("agent_a", "我先说明一下当前的分工。")
        assert d.kind == "reply"
        assert d.envelope_error == ""

    def test_reflection_only_envelope_is_not_flagged(self):
        d = decision_from_text("agent_a", json.dumps(
            {"reflections": [{"kind": "strategy", "content": "少读多写"}]},
            ensure_ascii=False))
        assert d.kind == "reply"
        assert d.envelope_error == ""

    def test_example_json_before_envelope_does_not_hide_it(self):
        """模型常把示例 JSON 放在信封前面 —— 不能只看第一个花括号块。"""
        text = (
            '示例参数：{"to": "agent_e"}\n'
            '现在执行：{"decision": "act", "reasoning": "r", '
            '"action": {"type": "workspace_write", '
            '"parameters": {"path": "x.md"}}}')
        d = decision_from_text("agent_a", text)
        assert d.kind == "act"
        assert d.intent.action_type == "workspace_write"
        assert d.envelope_error == ""

    def test_valid_envelope_is_not_flagged(self):
        d = decision_from_text("agent_a", json.dumps({
            "decision": "act",
            "reasoning": "写文件",
            "action": {"type": "workspace_write",
                       "parameters": {"path": "x.md"}},
        }))
        assert d.kind == "act"
        assert d.envelope_error == ""

    def test_all_actions_broken_is_reported(self):
        d = decision_from_text("agent_a", json.dumps({
            "decision": "act",
            "reasoning": "写文件",
            "actions": [{"parameters": {"path": "x.md"}}],   # 缺 type
        }))
        assert d.kind == "reply"
        assert d.dropped_actions == 1
        assert d.envelope_error == "no_valid_actions"

    def test_to_dict_carries_the_error(self):
        d = decision_from_text("agent_b", TRUNCATED)
        assert d.to_dict()["envelope_error"] == "unparsable_envelope"


class TestDetectionIsConservative:
    def test_empty_and_none(self):
        assert looks_like_envelope(None) is False
        assert looks_like_envelope("") is False

    def test_braces_are_a_signal(self):
        assert looks_like_envelope("{not valid json") is True

    def test_keywords_are_a_signal(self):
        assert looks_like_envelope(
            'prefix "actions": [ ...') is True

    def test_plain_chinese_is_not(self):
        assert looks_like_envelope("我会先读契约，然后写 backend。") is False


class TestExperimentPromptShrinksSubmit:
    """结项提示必须给**最小 payload**，避免小模型把大 JSON 写断。"""

    def _prompt(self) -> str:
        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        return src

    def test_minimal_submit_is_the_primary_example(self):
        src = self._prompt()
        assert "最小合法 SUBMIT 只需要 `task_id` + `result`" in src
        # completion_proposal 明确标成可选
        assert "`completion_proposal` 是**可选**的" in src

    def test_full_proposal_is_not_the_first_shape(self):
        src = self._prompt()
        # 旧版把 criteria_status / evidence / outstanding_gaps 全塞进
        # 主示例；现在它们只在「可选」块里出现。
        main = src.split("生产者提交成果", 1)[1].split("可选", 1)[0]
        assert "criteria_status" not in main

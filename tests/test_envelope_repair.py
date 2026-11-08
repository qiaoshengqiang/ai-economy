"""
信封写坏后的**一次**格式修复
================================

【为什么需要】

    实测（5-Agent 空间站 run）：
    doubao lite 在构造嵌套 SUBMIT payload 时把 JSON 写断，
    provider 仍返回 finish_reason=stop / output_status=VALID_CONTENT。
    旧行为把它降级成 reply ⇒ 生产者「看起来不想结项」，
    但它的 reasoning 明明在说「必须由我发 SUBMIT」。

    修复只做一件事：
    再问模型一次，要求它输出**更短、只有一个动作**的合法信封。

    🔒 不替 Agent 决定做什么 —— 只要求「把决定写成可执行的样子」。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.run import run_cognition                        # noqa: E402


TRUNCATED = (
    '{"decision": "act", "reasoning": "我要发 SUBMIT", '
    '"actions": [{"type": "message_send", "parameters": '
    '{"to": "agent_e", "message_type": "SUBMIT", "payload": {"task_'
)


@dataclass
class _Resp:
    content: str
    total_tokens: int = 10
    output_status: str = "VALID_CONTENT"
    cognitive_status: str = "COMPLETE"
    finish_reason: str = "stop"


@dataclass
class _Agent:
    agent_id: str = "agent_b"
    script: List[str] = field(default_factory=list)
    calls: List[Any] = field(default_factory=list)
    _i: int = 0

    def generate(self, messages=None, **kwargs):
        self.calls.append(messages)
        content = (self.script[self._i] if self._i < len(self.script)
                   else self.script[-1])
        self._i += 1
        return _Resp(content=content)


def _valid_envelope() -> str:
    return json.dumps({
        "decision": "act",
        "reasoning": "发 SUBMIT",
        "action": {"type": "message_send", "parameters": {
            "to": "agent_e", "message_type": "SUBMIT",
            "payload": {"task_id": "t1"}}},
    }, ensure_ascii=False)


class TestRepair:
    def test_broken_envelope_is_retried_once(self):
        a = _Agent(script=[TRUNCATED, _valid_envelope()])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            repair_malformed=True)

        assert len(a.calls) == 2, "坏信封必须触发一次修复调用"
        assert run.segments[0].repair_attempted is True
        assert run.segments[0].repair_succeeded is True
        assert [i.action_type for i in run.action_intents] == [
            "message_send"]
        # 两次调用的 token 都要算进这一段
        assert run.segments[0].tokens == 20

    def test_repair_is_not_used_for_plain_reply(self):
        a = _Agent(script=["我先说明一下。"])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            repair_malformed=True)

        assert len(a.calls) == 1
        assert run.segments[0].repair_attempted is False
        assert run.segments[0].repair_succeeded is False

    def test_repair_failure_is_reported_not_hidden(self):
        a = _Agent(script=[TRUNCATED, TRUNCATED])
        run = run_cognition(
            a, [{"role": "user", "content": "go"}],
            repair_malformed=True)

        assert len(a.calls) == 2
        seg = run.segments[0]
        assert seg.repair_attempted is True
        assert seg.repair_succeeded is False
        assert seg.decision.envelope_error == "unparsable_envelope"
        assert run.action_intents == []

    def test_repair_can_be_disabled(self):
        a = _Agent(script=[TRUNCATED, _valid_envelope()])
        run_cognition(
            a, [{"role": "user", "content": "go"}],
            repair_malformed=False)

        assert len(a.calls) == 1

    def test_experiment_turns_repair_on(self):
        """实验必须显式打开 —— 否则这个能力等于没接。"""
        from experiments.heterogeneous_production import production as P

        assert P.REPAIR_MALFORMED is True
        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        assert "repair_malformed=REPAIR_MALFORMED" in src

    def test_repair_prompt_asks_for_a_shorter_envelope(self):
        a = _Agent(script=[TRUNCATED, _valid_envelope()])
        run_cognition(
            a, [{"role": "user", "content": "go"}],
            repair_malformed=True)

        repair_messages = a.calls[1]
        assert repair_messages[0] == {"role": "user", "content": "go"}
        assert repair_messages[-1]["role"] == "user"
        assert "更短" in repair_messages[-1]["content"]
        assert "最多一个动作" in repair_messages[-1]["content"]

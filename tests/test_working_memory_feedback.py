"""
工作记忆维护反馈 —— 只在“理解可能不一致”时给出事实信号
==========================================================

信号来源：

    version_lag        公共记忆已更新，私有记忆为空/更旧
    plan_changed       公共 plan / architecture 晚于私有记忆
    work_failure       动作失败 / run_tool 非零退出
    rework             not_found / already_exists / locked 等返工
    cognition_failure  空产出 / 截断 / 信封修复

🔒 这些只是 prompt 里的**反馈事实**；不拒绝动作、不强制更新。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.heterogeneous_production import production as P  # noqa: E402


@dataclass
class _Item:
    kind: str
    updated_at: str


@dataclass
class _Memory:
    revision: int = 0
    updated_at: str = ""
    items: dict = None

    def __post_init__(self):
        if self.items is None:
            self.items = {}


@dataclass
class _Result:
    success: bool = True
    error: str = ""
    metadata: dict = None
    output: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.output is None:
            self.output = {}


@dataclass
class _Intent:
    action_type: str


@dataclass
class _Segment:
    repair_attempted: bool = False


@dataclass
class _Run:
    action_intents: list
    action_results: list
    output_status: str = "VALID_CONTENT"
    cognitive_status: str = "COMPLETE"
    segments: list = None

    def __post_init__(self):
        if self.segments is None:
            self.segments = []


class TestSignals:
    def test_version_lag(self):
        public = _Memory(revision=3, updated_at="2026-01-02T00:00:00Z",
                         items={"plan": _Item("plan",
                                              "2026-01-02T00:00:00Z")})
        private = _Memory(revision=1, updated_at="2026-01-01T00:00:00Z")
        signals = P._maintenance_signals(public, private)
        kinds = {s["kind"] for s in signals}
        assert "version_lag" in kinds
        assert "plan_changed" in kinds

    def test_no_signal_when_private_is_current(self):
        public = _Memory(revision=3, updated_at="2026-01-02T00:00:00Z",
                         items={"plan": _Item("plan",
                                              "2026-01-02T00:00:00Z")})
        private = _Memory(revision=4, updated_at="2026-01-03T00:00:00Z")
        assert P._maintenance_signals(public, private) == []

    def test_work_failure_and_rework(self):
        run = _Run(
            action_intents=[_Intent("workspace_write"),
                            _Intent("run_tool")],
            action_results=[
                _Result(success=False, error="路径不存在",
                        metadata={"code": "not_found"}),
                _Result(success=True,
                        output={"tool": "pytest", "ok": False,
                                "exit_code": 1}),
            ])
        kinds = {s["kind"] for s in P._maintenance_signals(
            _Memory(), _Memory(), run)}
        assert "rework" in kinds
        assert "work_failure" in kinds

    def test_cognition_failure(self):
        run = _Run(action_intents=[], action_results=[],
                   cognitive_status="TRUNCATED",
                   segments=[_Segment(repair_attempted=True)])
        kinds = {s["kind"] for s in P._maintenance_signals(
            _Memory(), _Memory(), run)}
        assert "cognition_failure" in kinds

    def test_feedback_has_no_forced_instruction(self):
        text = P._feedback_text([P._sig("version_lag", "x")])
        assert "[version_lag]" in text
        assert "由你判断" in text
        assert "必须" not in text
        assert "拒绝" not in text


class TestPromptWiring:
    def test_prompt_contains_feedback_section(self):
        assert "{maintenance_feedback}" in P.PROMPT
        assert "私有工作记忆维护反馈" in P.PROMPT

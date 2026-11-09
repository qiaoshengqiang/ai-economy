"""
DemandRunner 的“动作事实”计算
==============================

这些是**事实派生**，不是墙钟压力：

    · 单 Agent 连续只读 turn 数；
    · 自上次 turn 以来锁从谁变到谁；
    · 给 creator 的其他 Agent 停摆事实。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demand.runner import DemandRunner                        # noqa: E402


def test_read_only_streak_counts_only_read_search_turns():
    assert DemandRunner._next_read_only_streak(
        2, ["workspace_read", "workspace_search"]) == 3
    assert DemandRunner._next_read_only_streak(
        2, ["workspace_read", "public_query"]) == 3


def test_read_only_streak_resets_on_any_structural_or_communication_action():
    for actions in (
        ["workspace_write"],
        ["workspace_edit"],
        ["resource_handoff"],
        ["message_send"],
        ["workspace_plan_update"],
        [],
    ):
        assert DemandRunner._next_read_only_streak(4, actions) == 0


def test_diff_locks_reports_both_new_and_released_locks():
    changes = DemandRunner._diff_locks(
        {"index.html": "agent_c", "README.md": "agent_c"},
        {"index.html": "agent_a", "src": "agent_c"},
    )
    by_path = {item["path"]: item for item in changes}
    assert by_path["index.html"] == {
        "path": "index.html", "from": "agent_c", "to": "agent_a"}
    assert by_path["README.md"] == {
        "path": "README.md", "from": "agent_c", "to": "none"}
    assert by_path["src"] == {
        "path": "src", "from": "none", "to": "agent_c"}


def test_stalled_agents_facts_are_creator_facing_and_action_based():
    fake = SimpleNamespace(
        _read_only_streak={"agent_c": 5, "agent_e": 1},
        _pending_requests={"agent_c": [{}, {}, {}]},
    )
    task = SimpleNamespace(participants=["agent_a", "agent_c", "agent_e"])
    locks = {"index.html": "agent_c", "tests": "agent_e"}

    out = DemandRunner._stalled_agents(
        fake, task, locks, exclude="agent_a")

    assert out == [{
        "agent_id": "agent_c",
        "read_only_streak": 5,
        "held_locks": ["index.html"],
        "pending_requests": 3,
    }]


def test_lock_snapshot_failure_returns_empty_dict():
    class _Broken:
        def lock_snapshot(self, workspace_id):
            raise RuntimeError("boom")

    fake = SimpleNamespace(rt=SimpleNamespace(
        workspace_gateway=_Broken()))
    assert DemandRunner._lock_snapshot(fake, "WS-X") == {}

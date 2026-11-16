"""Front 的记忆：注入 + 可写动作。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from core.private_long_term_memory_store import PrivateLongTermMemoryStore
from core.private_short_term_memory_store import PrivateShortTermMemoryStore
from core.workspace import Workspace
from services.private_long_term_memory_service import (
    PrivateLongTermMemoryService,
)
from services.private_short_term_memory_service import (
    PrivateShortTermMemoryService,
)
from storage.database import create_memory_database
from work.front_actions import build_front_dispatcher, execute_front_actions


def _memory(tmp_path):
    db = create_memory_database()
    short_store = PrivateShortTermMemoryStore(db)
    long_store = PrivateLongTermMemoryStore(db)
    short = PrivateShortTermMemoryService(short_store)
    long = PrivateLongTermMemoryService(long_store, short_store)
    workspace = Workspace(tmp_path / "agent_a", "agent_a")
    dispatcher = build_front_dispatcher(
        workspace_of=lambda _aid: workspace,
        short_term_memory=short,
        long_term_memory=long,
    )
    return db, short, long, dispatcher


def _run(dispatcher, action_type, parameters=None):
    text = execute_front_actions(
        dispatcher,
        [{"type": action_type, "parameters": parameters or {}}],
        agent_id="agent_a",
    )
    return text


def test_front_memory_actions_are_registered(tmp_path):
    db, _short, _long, dispatcher = _memory(tmp_path)
    try:
        assert {
            "short_term_memory_add", "short_term_memory_update",
            "short_term_memory_reinforce",
            "long_term_memory_search", "long_term_memory_read",
            "long_term_memory_consolidate",
        } <= set(dispatcher.action_types)
    finally:
        db.close()


def test_front_can_add_and_search_memory(tmp_path):
    db, short, _long, dispatcher = _memory(tmp_path)
    try:
        receipt = _run(dispatcher, "short_term_memory_add", {
            "summary": "用户偏好简洁回答",
            "notable": "多次要求不要长篇解释",
            "importance": 4,
        })
        assert "short_term_memory_add" in receipt
        assert '"success": true' in receipt
        memory = short.read("agent_a")
        assert len(memory.entries) == 1
        assert memory.entries[0].summary == "用户偏好简洁回答"
        assert memory.entries[0].task_id == ""

        search = _run(dispatcher, "long_term_memory_search", {"query": "简洁"})
        assert "long_term_memory_search" in search
        assert '"success": true' in search
    finally:
        db.close()


def test_front_can_consolidate_memory(tmp_path):
    db, short, long, dispatcher = _memory(tmp_path)
    try:
        _run(dispatcher, "short_term_memory_add", {"summary": "记忆 A"})
        _run(dispatcher, "short_term_memory_add", {"summary": "记忆 B"})
        memory = short.read("agent_a")
        ids = [entry.entry_id for entry in memory.entries]
        assert len(ids) == 2

        receipt = _run(dispatcher, "long_term_memory_consolidate", {
            "expected_revision": memory.revision,
            "summary": "用户偏好：简洁、少解释",
            "events": ["对话"],
            "short_term_entry_ids": ids,
        })
        assert '"success": true' in receipt
        entries = long.all("agent_a")
        assert len(entries) == 1
        assert entries[0].summary == "用户偏好：简洁、少解释"
        assert entries[0].tasks == []
        assert short.read("agent_a").entries == []
    finally:
        db.close()


def test_front_prompt_injects_memory_and_documents_actions():
    from front.runtime import FrontAgentRuntime

    captured = {}

    def think(prompt):
        captured["prompt"] = prompt
        return json.dumps({"intent": "direct_reply", "reply": "好"})

    runtime = FrontAgentRuntime(
        "agent_a", think,
        action_types=(
            "short_term_memory_add", "short_term_memory_update",
            "long_term_memory_search", "long_term_memory_consolidate",
        ),
    )
    runtime.decide_turn(
        "以后回答短一点",
        memory_facts=(
            "短期记忆（最近的经验）：\nrevision = 2\n"
            "- [stm-1] importance=4\n  summary: 用户偏好简洁回答"),
    )
    prompt = captured["prompt"]
    assert "记忆（只读，过去的事实）" in prompt
    assert "用户偏好简洁回答" in prompt
    assert "与当前输入冲突时，以当前输入为准" in prompt
    assert "- short_term_memory_add {summary" in prompt
    assert "- long_term_memory_search {query}" in prompt
    assert "- long_term_memory_consolidate {expected_revision" in prompt
    assert '"type":"short_term_memory_add"' in prompt


def test_front_prompt_omits_empty_memory_section():
    from front.runtime import FrontAgentRuntime

    captured = {}

    def think(prompt):
        captured["prompt"] = prompt
        return json.dumps({"intent": "direct_reply", "reply": "好"})

    runtime = FrontAgentRuntime(
        "agent_a", think, action_types=("artifact_read",))
    runtime.decide_turn("你好", memory_facts="")
    assert "记忆（只读" not in captured["prompt"]
    assert "short_term_memory_add" not in captured["prompt"]


def test_controller_memory_facts_render_for_front(tmp_path):
    from work.controller import AIEOSController

    db, short, long, _dispatcher = _memory(tmp_path)
    runtime = SimpleNamespace(
        directory=SimpleNamespace(all=lambda: [
            SimpleNamespace(agent_id="agent_a")]),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
        private_short_term_memory=short,
        private_long_term_memory=long,
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        short.add("agent_a", summary="用户偏好简洁回答")
        facts = controller._front_memory_facts()
        assert "短期记忆" in facts
        assert "用户偏好简洁回答" in facts
    finally:
        controller.close()
        db.close()


def test_controller_front_runtime_exposes_memory_actions(tmp_path):
    from work.controller import AIEOSController

    db, short, long, _dispatcher = _memory(tmp_path)
    runtime = SimpleNamespace(
        directory=SimpleNamespace(all=lambda: [
            SimpleNamespace(agent_id="agent_a")]),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=None, workspace=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
        private_short_term_memory=short,
        private_long_term_memory=long,
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        front = controller._front_runtime()
        assert "short_term_memory_add" in front.action_types
        assert "long_term_memory_consolidate" in front.action_types
    finally:
        controller.close()
        db.close()


def test_front_prompt_shows_memory_trigger_even_when_empty():
    from front.runtime import FrontAgentRuntime

    captured = {}

    def think(prompt):
        captured["prompt"] = prompt
        return json.dumps({"intent": "direct_reply", "reply": "好"})

    runtime = FrontAgentRuntime(
        "agent_a", think, action_types=("short_term_memory_add",))
    runtime.decide_turn("记住：以后回答简洁", memory_facts="")
    prompt = captured["prompt"]
    assert "（还没有记忆）" in prompt
    assert "必须先把记忆写进" in prompt
    assert "只回复「记住了」不算记住" in prompt

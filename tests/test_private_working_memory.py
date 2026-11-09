"""
Private Working Memory —— 私有工作记忆
========================================

验证：

    ① 层级节点 / kind / content / revision 校验；
    ② 环与“删除有子节点的节点”被拒绝；
    ③ 持久化 + 历史 + finalize 清空当前、保留历史；
    ④ 只有 Task Participant 能访问自己的私有记忆；
    ⑤ Action 的 read/update；
    ⑥ prompt 注入与 action 文档。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.private_working_memory import (                     # noqa: E402
    ACTION_PRIVATE_WORKING_MEMORY_READ,
    ACTION_PRIVATE_WORKING_MEMORY_UPDATE,
    PrivateWorkingMemoryReadExecutor,
    PrivateWorkingMemoryUpdateExecutor,
)
from actions.intent import ActionIntent                         # noqa: E402
from core.money import Money                                    # noqa: E402
from core.private_working_memory import (                       # noqa: E402
    ConcurrentPrivateWorkingMemoryModification,
    PrivateWorkingMemory,
    PrivateWorkingMemoryValidationError,
    apply_update,
)
from core.private_working_memory_store import (                 # noqa: E402
    PrivateWorkingMemoryStore,
)
from core.task import Task                                      # noqa: E402
from core.task_store import TaskStore                           # noqa: E402
from services.private_working_memory_service import (            # noqa: E402
    PrivateWorkingMemoryAccessError,
    PrivateWorkingMemoryService,
)


# ------------------------------------------------------------
# 领域
# ------------------------------------------------------------
class TestDomain:
    def test_create_tree(self):
        mem = apply_update(
            None, agent_id="agent_a", task_id="T-1", expected_revision=0,
            nodes={
                "root": {"kind": "goal", "title": "ship page",
                         "content": {"done": False}},
                "front": {"kind": "branch", "title": "frontend",
                          "content": {}, "parent_id": "root"},
                "todo": {"kind": "task", "title": "build todo list",
                         "content": {}, "parent_id": "front"},
            })
        assert mem.revision == 1
        assert mem.nodes["todo"].parent_id == "front"

    def test_cycle_is_rejected(self):
        mem = apply_update(
            None, agent_id="a", task_id="T", expected_revision=0,
            nodes={
                "a": {"kind": "branch", "title": "A"},
                "b": {"kind": "branch", "title": "B", "parent_id": "a"},
            })
        with pytest.raises(PrivateWorkingMemoryValidationError):
            apply_update(
                mem, agent_id="a", task_id="T", expected_revision=1,
                nodes={"a": {"kind": "branch", "title": "A",
                             "parent_id": "b"}})

    def test_remove_parent_with_children_rejected(self):
        mem = apply_update(
            None, agent_id="a", task_id="T", expected_revision=0,
            nodes={
                "a": {"kind": "branch", "title": "A"},
                "b": {"kind": "task", "title": "B", "parent_id": "a"},
            })
        with pytest.raises(PrivateWorkingMemoryValidationError):
            apply_update(
                mem, agent_id="a", task_id="T", expected_revision=1,
                remove_nodes=["a"])

    def test_invalid_kind_and_conflict(self):
        with pytest.raises(PrivateWorkingMemoryValidationError):
            apply_update(None, agent_id="a", task_id="T",
                         expected_revision=0,
                         nodes={"x": {"kind": "bad", "title": "x"}})
        mem = apply_update(
            None, agent_id="a", task_id="T", expected_revision=0,
            nodes={"x": {"kind": "note", "title": "x"}})
        with pytest.raises(ConcurrentPrivateWorkingMemoryModification):
            apply_update(mem, agent_id="a", task_id="T",
                         expected_revision=0,
                         nodes={"x": {"kind": "note", "title": "y"}})


# ------------------------------------------------------------
# Store / Service
# ------------------------------------------------------------
@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


def _task(store, task_id="T-PWM",
          participants=("agent_a", "agent_b")) -> Task:
    task = Task.create(
        task_id=task_id, creator=participants[0],
        description="private wm task", budget=Money(10),
        workspace_id="WS-PWM", participants=list(participants))
    store.save(task)
    return task


class TestStoreAndService:
    def test_persist_history_and_finalize(self, db, store):
        _task(store)
        mem_store = PrivateWorkingMemoryStore(db)
        mem = mem_store.update(
            "agent_a", "T-PWM", expected_revision=0,
            nodes={"root": {"kind": "goal", "title": "ship"}})
        assert mem.revision == 1
        mem_store.update(
            "agent_a", "T-PWM", expected_revision=1,
            nodes={"branch": {"kind": "branch", "title": "front",
                              "parent_id": "root"}})
        assert mem_store.get("agent_a", "T-PWM").revision == 2
        assert [m.revision for m in mem_store.history("agent_a", "T-PWM")] == [2, 1]

        final = mem_store.finalize("agent_a", "T-PWM")
        assert final.revision == 2
        assert final.nodes["branch"].parent_id == "root"
        assert mem_store.get("agent_a", "T-PWM") is None
        # 历史仍在
        assert [m.revision for m in mem_store.history("agent_a", "T-PWM")] == [2, 1]

    def test_service_participant_boundary(self, db, store):
        _task(store)
        service = PrivateWorkingMemoryService(
            PrivateWorkingMemoryStore(db), store)
        assert service.read("agent_a", "T-PWM").revision == 0
        with pytest.raises(PrivateWorkingMemoryAccessError):
            service.read("agent_c", "T-PWM")

    def test_service_render_tree(self, db, store):
        _task(store)
        service = PrivateWorkingMemoryService(
            PrivateWorkingMemoryStore(db), store)
        service.update(
            "agent_a", "T-PWM", expected_revision=0,
            nodes={
                "root": {"kind": "goal", "title": "ship"},
                "front": {"kind": "branch", "title": "front",
                          "parent_id": "root"},
            })
        text = service.render("agent_a", "T-PWM")
        assert "revision = 1" in text
        assert "[goal] root" in text
        assert "[branch] front" in text


# ------------------------------------------------------------
# Action
# ------------------------------------------------------------
class TestActions:
    def test_read_action(self):
        ex = PrivateWorkingMemoryReadExecutor(
            lambda agent_id: PrivateWorkingMemory(
                agent_id=agent_id, task_id="T", revision=4,
                nodes={}, updated_at="now"))
        result = ex.execute(ActionIntent(
            action_type=ACTION_PRIVATE_WORKING_MEMORY_READ,
            parameters={}, agent_id="agent_a"))
        assert result.success
        assert result.output["revision"] == 4

    def test_update_requires_expected_revision(self):
        ex = PrivateWorkingMemoryUpdateExecutor(lambda *a, **kw: None)
        result = ex.execute(ActionIntent(
            action_type=ACTION_PRIVATE_WORKING_MEMORY_UPDATE,
            parameters={"nodes": {}}, agent_id="agent_a"))
        assert not result.success
        assert "expected_revision" in result.error

    def test_update_action_uses_intent_agent(self):
        seen = {}

        def update(agent_id, *, expected_revision, nodes, remove_nodes):
            seen.update(agent_id=agent_id,
                        expected_revision=expected_revision,
                        nodes=nodes, remove_nodes=remove_nodes)
            return PrivateWorkingMemory(
                agent_id=agent_id, task_id="T",
                revision=expected_revision + 1, nodes={}, updated_at="now")

        ex = PrivateWorkingMemoryUpdateExecutor(update)
        result = ex.execute(ActionIntent(
            action_type=ACTION_PRIVATE_WORKING_MEMORY_UPDATE,
            parameters={"expected_revision": 1,
                        "nodes": {"x": {"kind": "note", "title": "x"}}},
            agent_id="agent_a"))
        assert result.success
        assert result.output["revision"] == 2
        assert seen["agent_id"] == "agent_a"

    def test_unknown_update_is_rejected_but_empty_is_allowed(self):
        ex = PrivateWorkingMemoryUpdateExecutor(
            lambda *a, **kw: PrivateWorkingMemory(
                agent_id="agent_a", task_id="T", revision=1,
                nodes={}, updated_at="now"))
        unknown = ex.execute(ActionIntent(
            action_type=ACTION_PRIVATE_WORKING_MEMORY_UPDATE,
            parameters={"expected_revision": 0, "content": "x"},
            agent_id="agent_a"))
        assert not unknown.success
        assert unknown.metadata["code"] == "unknown_parameters"

        # work checkpoint 明确允许“检查后没有变化”的空更新。
        empty = ex.execute(ActionIntent(
            action_type=ACTION_PRIVATE_WORKING_MEMORY_UPDATE,
            parameters={"expected_revision": 0},
            agent_id="agent_a"))
        assert empty.success


# ------------------------------------------------------------
# Prompt wiring
# ------------------------------------------------------------
class TestPromptWiring:
    def test_prompt_injects_private_memory_and_actions(self):
        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        assert "{private_working_memory}" in src
        assert "private_working_memory_read" in src
        assert "private_working_memory_update" in src
        assert "重大问题" in src
        assert "空更新" in src

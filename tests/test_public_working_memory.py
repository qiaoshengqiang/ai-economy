"""
Public Working Memory —— 公共工作记忆
======================================

验证：

    ① 领域校验：key / kind / value / 禁止复制 Task Core 字段；
    ② 乐观锁：expected_revision 不匹配必须失败；
    ③ 持久化：创建 / 更新 / 历史；
    ④ 参与者边界：非 Task 参与者不能读/写；
    ⑤ Action：read/update 的结构化结果；
    ⑥ prompt 里真的注入了工作记忆与动作说明。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.public_working_memory import (                      # noqa: E402
    ACTION_PUBLIC_WORKING_MEMORY_READ,
    ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
    PublicWorkingMemoryReadExecutor,
    PublicWorkingMemoryUpdateExecutor,
)
from actions.intent import ActionIntent                         # noqa: E402
from core.money import Money                                    # noqa: E402
from core.public_working_memory import (                        # noqa: E402
    ConcurrentWorkingMemoryModification,
    PublicWorkingMemory,
    WorkingMemoryValidationError,
    apply_update,
)
from core.public_working_memory_store import (
    PublicWorkingMemoryStore,
)
from core.task import Task                                      # noqa: E402
from core.task_store import TaskStore                           # noqa: E402
from services.public_working_memory_service import (            # noqa: E402
    PublicWorkingMemoryService,
    WorkingMemoryAccessError,
)


# ------------------------------------------------------------
# 领域
# ------------------------------------------------------------
class TestDomain:
    def test_create_and_update(self):
        mem = apply_update(
            None, task_id="T-1", author="agent_a", expected_revision=0,
            items={"plan": {"kind": "plan", "value": {"steps": ["a", "b"]}}})
        assert mem.revision == 1
        assert mem.items["plan"].kind == "plan"
        assert mem.items["plan"].author == "agent_a"

        mem2 = apply_update(
            mem, task_id="T-1", author="agent_b", expected_revision=1,
            items={"checklist:acceptance": {
                "kind": "checklist",
                "value": [{"id": "c1", "text": "page opens", "done": False}],
            }})
        assert mem2.revision == 2
        assert set(mem2.items) == {"plan", "checklist:acceptance"}

    def test_remove_key(self):
        mem = apply_update(
            None, task_id="T-1", author="a", expected_revision=0,
            items={"note": {"kind": "note", "value": "x"}})
        mem2 = apply_update(
            mem, task_id="T-1", author="a", expected_revision=1,
            remove_keys=["note"])
        assert "note" not in mem2.items

    def test_revision_conflict(self):
        mem = apply_update(
            None, task_id="T-1", author="a", expected_revision=0,
            items={"note": {"kind": "note", "value": "x"}})
        with pytest.raises(ConcurrentWorkingMemoryModification):
            apply_update(mem, task_id="T-1", author="b",
                         expected_revision=0,
                         items={"note": {"kind": "note", "value": "y"}})

    def test_forbidden_task_core_keys(self):
        for key in ("status", "agreed_price", "contractor", "budget"):
            with pytest.raises(WorkingMemoryValidationError):
                apply_update(
                    None, task_id="T-1", author="a", expected_revision=0,
                    items={key: {"kind": "note", "value": "x"}})

    def test_invalid_kind_and_large_value(self):
        with pytest.raises(WorkingMemoryValidationError):
            apply_update(None, task_id="T", author="a", expected_revision=0,
                         items={"x": {"kind": "unknown", "value": 1}})
        with pytest.raises(WorkingMemoryValidationError):
            apply_update(None, task_id="T", author="a", expected_revision=0,
                         items={"x": {"kind": "note", "value": "x" * 5000}})

    def test_hierarchy_parent_key(self):
        mem = apply_update(
            None, task_id="T-1", author="a", expected_revision=0,
            items={
                "plan": {"kind": "plan", "value": {}},
                "train": {"kind": "plan", "value": {},
                          "parent_key": "plan"},
            })
        assert mem.items["train"].parent_key == "plan"

    def test_hierarchy_rejects_missing_parent_and_cycles(self):
        with pytest.raises(WorkingMemoryValidationError, match="parent_key"):
            apply_update(
                None, task_id="T", author="a", expected_revision=0,
                items={"x": {"kind": "plan", "value": {},
                             "parent_key": "missing"}})
        with pytest.raises(WorkingMemoryValidationError, match="环"):
            apply_update(
                None, task_id="T", author="a", expected_revision=0,
                items={
                    "a": {"kind": "plan", "value": {},
                          "parent_key": "b"},
                    "b": {"kind": "plan", "value": {},
                          "parent_key": "a"},
                })

    def test_remove_parent_with_children_is_rejected(self):
        mem = apply_update(
            None, task_id="T", author="a", expected_revision=0,
            items={
                "plan": {"kind": "plan", "value": {}},
                "train": {"kind": "plan", "value": {},
                          "parent_key": "plan"},
            })
        with pytest.raises(WorkingMemoryValidationError, match="子条目"):
            apply_update(
                mem, task_id="T", author="a", expected_revision=1,
                remove_keys=["plan"])


# ------------------------------------------------------------
# 持久化 / 服务
# ------------------------------------------------------------
@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


def _task(store, task_id="T-WM", participants=("agent_a", "agent_b")) -> Task:
    task = Task.create(
        task_id=task_id,
        creator=participants[0],
        description="working memory task",
        budget=Money(10),
        workspace_id="WS-WM",
        participants=list(participants),
    )
    store.save(task)
    return task


class TestStoreAndService:
    def test_persist_and_history(self, db, store):
        _task(store)
        mem_store = PublicWorkingMemoryStore(db)
        mem = mem_store.update(
            "T-WM", author="agent_a", expected_revision=0,
            items={"plan": {"kind": "plan", "value": {"n": 1}}})
        assert mem.revision == 1
        mem2 = mem_store.update(
            "T-WM", author="agent_b", expected_revision=1,
            items={"progress": {"kind": "progress", "value": "half"}})
        assert mem2.revision == 2
        loaded = mem_store.get("T-WM")
        assert loaded.revision == 2
        assert set(loaded.items) == {"plan", "progress"}
        history = mem_store.history("T-WM", limit=10)
        assert [m.revision for m in history] == [2, 1]

    def test_service_participant_boundary(self, db, store):
        _task(store)
        service = PublicWorkingMemoryService(
            PublicWorkingMemoryStore(db), store)
        assert service.read("T-WM", "agent_a").revision == 0
        with pytest.raises(WorkingMemoryAccessError):
            service.read("T-WM", "agent_c")
        with pytest.raises(WorkingMemoryAccessError):
            service.update("T-WM", "agent_c", expected_revision=0,
                           items={"note": {"kind": "note", "value": "x"}})

    def test_service_render(self, db, store):
        _task(store)
        service = PublicWorkingMemoryService(
            PublicWorkingMemoryStore(db), store)
        service.update(
            "T-WM", "agent_a", expected_revision=0,
            items={"plan": {"kind": "plan", "value": {"steps": ["x"]}}})
        text = service.render("T-WM", "agent_b")
        assert "revision = 1" in text
        assert "plan" in text and "agent_a" in text
        assert "self-report" in text

    def test_evidence_must_point_to_a_real_artifact(self, db, store):
        from types import SimpleNamespace

        class _Gateway:
            def read(self, workspace_id, path, agent_id):
                ok = path == "index.html"
                return SimpleNamespace(
                    success=ok,
                    result={"revision": 2} if ok else None,
                    error=None if ok else {"message": "not found"})

        _task(store)
        service = PublicWorkingMemoryService(
            PublicWorkingMemoryStore(db), store, gateway=_Gateway())

        with pytest.raises(WorkingMemoryValidationError, match="evidence"):
            service.update(
                "T-WM", "agent_a", expected_revision=0,
                items={"done": {
                    "kind": "progress",
                    "value": "index.html done",
                    "evidence": {"path": "missing.html", "revision": 1},
                }})

        # evidence 里的未知字段（拼写错误）必须失败，不能静默忽略。
        with pytest.raises(WorkingMemoryValidationError, match="不认识的字段"):
            service.update(
                "T-WM", "agent_a", expected_revision=0,
                items={"done": {
                    "kind": "progress",
                    "value": "index.html done",
                    "evidence": {"path": "index.html", "revisoin": 2},
                }})

        mem = service.update(
            "T-WM", "agent_a", expected_revision=0,
            items={"done": {
                "kind": "progress",
                "value": "index.html done",
                "evidence": {"path": "index.html", "revision": 2},
            }})
        assert mem.items["done"].evidence["path"] == "index.html"
        text = service.render("T-WM", "agent_b")
        assert "evidence: index.html @R2 (verified)" in text


# ------------------------------------------------------------
# Action
# ------------------------------------------------------------
class TestActions:
    def test_read_action(self):
        ex = PublicWorkingMemoryReadExecutor(
            lambda agent_id: PublicWorkingMemory(
                task_id="T", revision=3, items={}, updated_at="now"))
        result = ex.execute(ActionIntent(
            action_type=ACTION_PUBLIC_WORKING_MEMORY_READ,
            parameters={}, agent_id="agent_a"))
        assert result.success
        assert result.output["revision"] == 3

    def test_update_requires_expected_revision(self):
        ex = PublicWorkingMemoryUpdateExecutor(lambda *a, **kw: None)
        result = ex.execute(ActionIntent(
            action_type=ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
            parameters={"items": {}}, agent_id="agent_a"))
        assert not result.success
        assert "expected_revision" in result.error

    def test_update_action(self):
        seen = {}

        def update(agent_id, *, expected_revision, items, remove_keys):
            seen.update(agent_id=agent_id,
                        expected_revision=expected_revision,
                        items=items, remove_keys=remove_keys)
            return PublicWorkingMemory(task_id="T", revision=expected_revision + 1,
                                       items={}, updated_at="now")

        ex = PublicWorkingMemoryUpdateExecutor(update)
        result = ex.execute(ActionIntent(
            action_type=ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
            parameters={"expected_revision": 2,
                        "items": {"plan": {"kind": "plan", "value": {}}}},
            agent_id="agent_a"))
        assert result.success
        assert result.output["revision"] == 3
        assert seen["agent_id"] == "agent_a"
        assert seen["expected_revision"] == 2

    def test_content_is_stored_as_a_note_not_silently_ignored(self):
        seen = {}

        def update(agent_id, *, expected_revision, items, remove_keys):
            seen["items"] = items
            return PublicWorkingMemory(
                task_id="T", revision=expected_revision + 1,
                items={}, updated_at="now")

        ex = PublicWorkingMemoryUpdateExecutor(update)
        result = ex.execute(ActionIntent(
            action_type=ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
            parameters={"expected_revision": 0, "content": "# 协作板"},
            agent_id="agent_a"))
        assert result.success
        assert seen["items"] == {
            "note": {"kind": "note", "value": "# 协作板"}}

    def test_unknown_and_empty_updates_are_rejected(self):
        ex = PublicWorkingMemoryUpdateExecutor(lambda *a, **kw: None)
        unknown = ex.execute(ActionIntent(
            action_type=ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
            parameters={"expected_revision": 0, "board": "x"},
            agent_id="agent_a"))
        assert not unknown.success
        assert unknown.metadata["code"] == "unknown_parameters"

        empty = ex.execute(ActionIntent(
            action_type=ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
            parameters={"expected_revision": 0},
            agent_id="agent_a"))
        assert not empty.success
        assert empty.metadata["code"] == "empty_update"


# ------------------------------------------------------------
# Prompt wiring
# ------------------------------------------------------------
class TestPromptWiring:
    def test_prompt_injects_working_memory_and_actions(self):
        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        assert "{working_memory}" in src
        assert "public_working_memory_read" in src
        assert "public_working_memory_update" in src
        assert "expected_revision" in src

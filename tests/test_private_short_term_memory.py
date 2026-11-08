"""
Private Short-Term Memory —— 私有短期记忆
==========================================

验证：

    ① 固定容量 / 超过一半压缩 / digest 进入长期记忆；
    ② 任务 final snapshot → 短期条目（summary/architecture/notable）；
    ③ Agent 反馈修正 / 删除条目；
    ④ Action read/update；
    ⑤ prompt 注入。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.intent import ActionIntent                         # noqa: E402
from actions.private_short_term_memory import (                  # noqa: E402
    ACTION_SHORT_TERM_MEMORY_READ,
    ACTION_SHORT_TERM_MEMORY_UPDATE,
    ShortTermMemoryReadExecutor,
    ShortTermMemoryUpdateExecutor,
)
from core.private_short_term_memory import (                     # noqa: E402
    CAPACITY,
    COMPACT_THRESHOLD,
    KEEP_AFTER_COMPACT,
    PrivateShortTermMemory,
    add_entry,
    make_entry,
)
from core.private_short_term_memory_store import (               # noqa: E402
    PrivateShortTermMemoryStore,
)
from services.private_short_term_memory_service import (         # noqa: E402
    PrivateShortTermMemoryService,
)


class TestDomain:
    def test_constants(self):
        assert CAPACITY == 8
        assert COMPACT_THRESHOLD == 4
        assert KEEP_AFTER_COMPACT == 4

    def test_compaction_after_half(self):
        mem = None
        digest = None
        for i in range(5):
            entry = make_entry(
                task_id=f"T{i}", summary=f"task {i}", importance=3,
                now=f"2026-01-0{i + 1}T00:00:00Z")
            mem, digest = add_entry(
                mem, entry, agent_id="agent_a",
                now=f"2026-01-0{i + 1}T00:00:00Z")
        assert len(mem.entries) == KEEP_AFTER_COMPACT
        assert digest is not None
        assert digest.entry_id.startswith("stm-digest-")
        assert "历史任务摘要" in digest.summary

    def test_no_compaction_below_threshold(self):
        mem = None
        for i in range(4):
            mem, digest = add_entry(
                mem,
                make_entry(task_id=f"T{i}", summary=f"task {i}",
                           now="2026-01-01T00:00:00Z"),
                agent_id="a", now="2026-01-01T00:00:00Z")
        assert len(mem.entries) == 4
        assert digest is None


class TestStoreAndService:
    def test_add_get_and_correct(self, db):
        store = PrivateShortTermMemoryStore(db)
        service = PrivateShortTermMemoryService(store)
        entry = service.add_from_final(
            "agent_a", "T-1",
            {"nodes": {
                "goal": {"kind": "goal", "title": "ship page",
                         "content": {}},
                "front": {"kind": "branch", "title": "frontend",
                          "content": {}},
                "bug": {"kind": "issue", "title": "localStorage broken",
                        "content": {"severity": "high"}},
            }})
        mem = store.get("agent_a")
        assert mem.revision == 1
        assert mem.entries[0].task_id == "T-1"
        assert "ship page" in mem.entries[0].summary
        assert "branch:frontend" in mem.entries[0].architecture
        assert "localStorage broken" in mem.entries[0].notable

        updated = service.update(
            "agent_a", expected_revision=1, entry_id=entry.entry_id,
            summary="corrected summary")
        assert updated.entries[0].summary == "corrected summary"

        removed = service.update(
            "agent_a", expected_revision=2, entry_id=entry.entry_id,
            remove=True)
        assert removed.entries == []

    def test_compaction_promotes_digest_to_long_term(self, db):
        store = PrivateShortTermMemoryStore(db)
        service = PrivateShortTermMemoryService(store)
        for i in range(5):
            service.add_from_final(
                "agent_a", f"T{i}",
                {"nodes": {"goal": {"kind": "goal",
                                    "title": f"task {i}",
                                    "content": {}}}})
        mem = store.get("agent_a")
        assert len(mem.entries) == KEEP_AFTER_COMPACT
        long_term = store.long_term_entries("agent_a")
        assert len(long_term) == 1
        assert long_term[0]["entry_id"].startswith("stm-digest-")

    def test_render(self, db):
        service = PrivateShortTermMemoryService(
            PrivateShortTermMemoryStore(db))
        service.add_from_final("agent_a", "T-1",
                               {"nodes": {"goal": {
                                   "kind": "goal", "title": "ship",
                                   "content": {}}}})
        text = service.render("agent_a")
        assert "revision = 1" in text
        assert "ship" in text


class TestActions:
    def test_read_action(self):
        ex = ShortTermMemoryReadExecutor(
            lambda agent_id: PrivateShortTermMemory(
                agent_id=agent_id, revision=2, entries=[], updated_at="now"))
        result = ex.execute(ActionIntent(
            action_type=ACTION_SHORT_TERM_MEMORY_READ,
            parameters={}, agent_id="agent_a"))
        assert result.success
        assert result.output["revision"] == 2

    def test_update_requires_revision_and_entry(self):
        ex = ShortTermMemoryUpdateExecutor(lambda *a, **kw: None)
        r1 = ex.execute(ActionIntent(
            action_type=ACTION_SHORT_TERM_MEMORY_UPDATE,
            parameters={"entry_id": "x"}, agent_id="agent_a"))
        assert not r1.success and "expected_revision" in r1.error
        r2 = ex.execute(ActionIntent(
            action_type=ACTION_SHORT_TERM_MEMORY_UPDATE,
            parameters={"expected_revision": 1}, agent_id="agent_a"))
        assert not r2.success and "entry_id" in r2.error


class TestPromptWiring:
    def test_prompt_contains_short_term_section(self):
        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        assert "{short_term_memory}" in src
        assert "short_term_memory_read" in src
        assert "short_term_memory_update" in src
        assert "固定容量 8" in src

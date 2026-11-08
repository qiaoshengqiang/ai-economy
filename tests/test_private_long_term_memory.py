"""
Private Long-Term Memory + 权重反馈
=====================================

验证：

    ① 长期条目 / 权重增加 / 未使用下降；
    ② Agent consolidation：短期条目 → 长期摘要 + archive；
    ③ long_term_memory_read 能拿回具体短期存档；
    ④ top-N 只读高权重，低权重数量可见；
    ⑤ 短期记忆 reinforce（use_count / importance）；
    ⑥ Actions / prompt wiring。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.intent import ActionIntent                         # noqa: E402
from actions.private_long_term_memory import (                  # noqa: E402
    ACTION_LONG_TERM_MEMORY_CONSOLIDATE,
    LongTermMemoryConsolidateExecutor,
)
from core.private_long_term_memory import (                     # noqa: E402
    LongTermEntry,
    decay,
    make_entry,
    mark_used,
)
from core.private_long_term_memory_store import (               # noqa: E402
    PrivateLongTermMemoryStore,
)
from core.private_short_term_memory_store import (              # noqa: E402
    PrivateShortTermMemoryStore,
)
from services.private_long_term_memory_service import (         # noqa: E402
    PrivateLongTermMemoryService,
)
from services.private_short_term_memory_service import (        # noqa: E402
    PrivateShortTermMemoryService,
)


class TestWeights:
    def test_mark_used_and_decay(self):
        e = make_entry(summary="debug async bug", tasks=["T1"],
                       events=["race condition"], weight=1.0)
        used = mark_used(e, "T2")
        assert used.weight == 1.5 and used.use_count == 1
        assert used.last_used_task_id == "T2"
        # 另一个任务里没用 → 下降
        d = decay(used, "T3")
        assert abs(d.weight - 1.3) < 1e-9
        # 当前任务里用过 → 不再下降
        same = decay(used, "T2")
        assert same.weight == used.weight


class TestService:
    def _seed_short_term(self, db, n=5):
        stm = PrivateShortTermMemoryService(PrivateShortTermMemoryStore(db))
        for i in range(n):
            stm.add_from_final(
                "agent_a", f"T{i}",
                {"nodes": {"goal": {"kind": "goal",
                                    "title": f"task {i}"}}})
        return stm

    def test_consolidate_archives_short_term(self, db):
        stm = self._seed_short_term(db, 5)
        current = stm.read("agent_a")
        ids = [e.entry_id for e in current.entries[:2]]
        ltm = PrivateLongTermMemoryService(
            PrivateLongTermMemoryStore(db),
            PrivateShortTermMemoryStore(db))

        entry, remaining = ltm.consolidate(
            "agent_a", task_id="T5", expected_revision=current.revision,
            summary="多个 duration/parse 任务：先写契约再实现",
            tasks=["T0", "T1"], events=["早期缺少契约导致返工"],
            short_term_entry_ids=ids)

        assert entry.weight >= 1.0
        assert len(entry.short_term_entry_ids) == 2
        assert len(remaining.entries) == 3
        archived = stm.store.archived_entries(
            "agent_a", long_term_entry_id=entry.entry_id)
        assert {x["entry_id"] for x in archived} == set(ids)
        data = ltm.read("agent_a", entry.entry_id)
        assert len(data["short_term_entries"]) == 2

    def test_top_and_low_weight_count(self, db):
        store = PrivateLongTermMemoryStore(db)
        for i, weight in enumerate([0.5, 3.0, 1.2]):
            store.upsert("agent_a", make_entry(
                summary=f"memory {i}", weight=weight,
                entry_id=f"ltm-{i}"))
        ltm = PrivateLongTermMemoryService(
            store, PrivateShortTermMemoryStore(db))
        top = ltm.top("agent_a", limit=2)
        assert [e.entry_id for e in top] == ["ltm-1", "ltm-2"]
        text = ltm.render_top("agent_a", limit=2)
        assert "还有 1 条低权重" in text

    def test_mark_used_changes_weight(self, db):
        store = PrivateLongTermMemoryStore(db)
        store.upsert("agent_a", make_entry(
            summary="m", weight=1.0, entry_id="ltm-x"))
        ltm = PrivateLongTermMemoryService(
            store, PrivateShortTermMemoryStore(db))
        updated = ltm.mark_used("agent_a", ["ltm-x"], "T1")
        assert updated[0].weight == 1.5
        decayed = ltm.decay_unused("agent_a", "T2")
        assert decayed[0].weight < 1.5

    def test_short_term_reinforce(self, db):
        stm = PrivateShortTermMemoryService(PrivateShortTermMemoryStore(db))
        stm.add_from_final("agent_a", "T1",
                           {"nodes": {"goal": {"kind": "goal",
                                               "title": "x"}}})
        current = stm.read("agent_a")
        eid = current.entries[0].entry_id
        updated = stm.reinforce(
            "agent_a", expected_revision=current.revision,
            entry_id=eid, reason="直接解决了当前任务")
        assert updated.entries[0].use_count == 1
        assert updated.entries[0].importance == 4
        assert "直接解决" in updated.entries[0].helpful


class TestAction:
    def test_consolidate_action(self):
        def consolidate(agent_id, **kw):
            return (LongTermEntry(entry_id="ltm-1", summary="s"),
                    type("M", (), {"to_dict": lambda self: {"revision": 2}})())

        ex = LongTermMemoryConsolidateExecutor(consolidate)
        result = ex.execute(ActionIntent(
            action_type=ACTION_LONG_TERM_MEMORY_CONSOLIDATE,
            parameters={"expected_revision": 1, "summary": "s",
                        "short_term_entry_ids": ["a"]},
            agent_id="agent_a"))
        assert result.success
        assert result.output["entry"]["entry_id"] == "ltm-1"


class TestPromptWiring:
    def test_prompt_contains_long_term_and_actions(self):
        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        assert "{long_term_memory}" in src
        assert "long_term_memory_search" in src
        assert "long_term_memory_read" in src
        assert "long_term_memory_mark_used" in src
        assert "long_term_memory_consolidate" in src
        assert "短期记忆条目数" in src

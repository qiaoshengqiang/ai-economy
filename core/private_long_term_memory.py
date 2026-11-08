"""
Private Long-Term Memory —— 私有长期记忆
==========================================

【它是什么】

    对短期记忆的**更高层抽象**：
        · 做过哪些事（tasks）
        · 哪些事件印象深刻（events）
        · 对应的具体短期记忆存档（short_term_entry_ids）

【权重】

    一条长期记忆被后续任务使用 → weight 增加；
    完全没用上 → weight 下降。

    每任务只注入 top-N 高权重；低权重条目仍然存在，
    但 Agent 知道“有 N 条没有读取”，可以主动 search/read。

【抽象是 Agent 的工作】

    短期记忆超过一半时，不自动 digest；
    系统只给“需要清理”的信号 + 候选 entry_ids，
    Agent 调用 `long_term_memory_consolidate` 写 summary/events，
    并把选中的短期条目归档。

【与短期记忆的关系】

    LongTermEntry.short_term_entry_ids → 指向
    `agent_short_term_memory_archive` 中的具体短期记忆；
    `long_term_memory_read` 会把存档一起返回。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

MAX_WEIGHT = 10.0
MIN_WEIGHT = 0.1
DEFAULT_WEIGHT = 1.0
USE_BONUS = 0.5
UNUSED_PENALTY = 0.2
TOP_LIMIT = 3


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class LongTermMemoryError(Exception):
    """长期记忆错误基类。"""


class LongTermMemoryValidationError(LongTermMemoryError):
    """条目内容不合法。"""


@dataclass(frozen=True)
class LongTermEntry:
    entry_id: str
    summary: str
    tasks: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    short_term_entry_ids: List[str] = field(default_factory=list)
    weight: float = DEFAULT_WEIGHT
    use_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_used_task_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "summary": self.summary,
            "tasks": list(self.tasks),
            "events": list(self.events),
            "short_term_entry_ids": list(self.short_term_entry_ids),
            "weight": self.weight,
            "use_count": self.use_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_task_id": self.last_used_task_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LongTermEntry":
        return cls(
            entry_id=str(raw.get("entry_id") or ""),
            summary=str(raw.get("summary") or ""),
            tasks=[str(x) for x in (raw.get("tasks") or [])],
            events=[str(x) for x in (raw.get("events") or [])],
            short_term_entry_ids=[
                str(x) for x in (raw.get("short_term_entry_ids") or [])],
            weight=float(raw.get("weight") or DEFAULT_WEIGHT),
            use_count=int(raw.get("use_count") or 0),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            last_used_task_id=str(raw.get("last_used_task_id") or ""),
        )


def _clamp(weight: float) -> float:
    return max(MIN_WEIGHT, min(MAX_WEIGHT, float(weight)))


def make_entry(
    *,
    summary: str,
    tasks: Optional[Iterable[str]] = None,
    events: Optional[Iterable[str]] = None,
    short_term_entry_ids: Optional[Iterable[str]] = None,
    weight: float = DEFAULT_WEIGHT,
    entry_id: Optional[str] = None,
    now: Optional[str] = None,
) -> LongTermEntry:
    now = now or utc_now_iso()
    summary = str(summary or "").strip()
    if not summary:
        raise LongTermMemoryValidationError("summary 不能为空")
    if len(summary) > 3000:
        raise LongTermMemoryValidationError("summary 太长（上限 3000）")
    return LongTermEntry(
        entry_id=entry_id or f"ltm-{uuid.uuid4().hex[:12]}",
        summary=summary,
        tasks=[str(x) for x in (tasks or [])][:50],
        events=[str(x) for x in (events or [])][:50],
        short_term_entry_ids=[str(x) for x in (short_term_entry_ids or [])][:50],
        weight=_clamp(weight),
        created_at=now,
        updated_at=now,
    )


def mark_used(entry: LongTermEntry, task_id: str,
              now: Optional[str] = None) -> LongTermEntry:
    now = now or utc_now_iso()
    return LongTermEntry(
        entry_id=entry.entry_id, summary=entry.summary,
        tasks=entry.tasks, events=entry.events,
        short_term_entry_ids=entry.short_term_entry_ids,
        weight=_clamp(entry.weight + USE_BONUS),
        use_count=entry.use_count + 1,
        created_at=entry.created_at, updated_at=now,
        last_used_task_id=str(task_id or ""),
    )


def decay(entry: LongTermEntry, task_id: str,
          now: Optional[str] = None) -> LongTermEntry:
    """如果本轮没有使用，权重下降。"""
    if entry.last_used_task_id == str(task_id or ""):
        return entry
    now = now or utc_now_iso()
    return LongTermEntry(
        entry_id=entry.entry_id, summary=entry.summary,
        tasks=entry.tasks, events=entry.events,
        short_term_entry_ids=entry.short_term_entry_ids,
        weight=_clamp(entry.weight - UNUSED_PENALTY),
        use_count=entry.use_count,
        created_at=entry.created_at, updated_at=now,
        last_used_task_id=entry.last_used_task_id,
    )


def matches(entry: LongTermEntry, query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return True
    hay = " ".join([entry.summary] + entry.tasks + entry.events).lower()
    return q in hay


__all__ = [
    "MAX_WEIGHT", "MIN_WEIGHT", "DEFAULT_WEIGHT", "USE_BONUS",
    "UNUSED_PENALTY", "TOP_LIMIT",
    "LongTermMemoryError", "LongTermMemoryValidationError",
    "LongTermEntry", "make_entry", "mark_used", "decay", "matches",
]

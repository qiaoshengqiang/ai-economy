"""
Private Short-Term Memory —— 单个 Agent 的跨任务短期记忆
==========================================================

【它是什么】

    Agent 最近做过哪些任务、实现过什么架构/结构、哪些现象值得重视。
    它是对**工作 + 私有工作记忆**的抽象总结，跨 Task 保存。

【与 Private Working Memory 的区别】

    Private Working Memory = 当前任务的操作结构（任务结束清空当前行）
    Private Short-Term     = 已完成任务的抽象摘要（跨任务）

【固定容量】

    CAPACITY = 8
    COMPACT_THRESHOLD = 4      （超过一半就清理）
    KEEP_AFTER_COMPACT = 4

    超过阈值时：
        · 把较旧的条目压成一条 digest（更抽象、重要）；
        · digest 交给 Long-Term Memory（本版只做存储 stub）；
        · 短期记忆只保留较新的 KEEP_AFTER_COMPACT 条。

【反馈】

    Agent 可以在后续任务里发现短期记忆不正确时用
    `short_term_memory_update` 修正/删除条目。
    这是 Agent 的维护，不是系统自动判定。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

CAPACITY = 8
COMPACT_THRESHOLD = CAPACITY // 2       # 4：超过 4 条就触发
KEEP_AFTER_COMPACT = CAPACITY // 2      # 4：清理后保留 4 条

MAX_SUMMARY_CHARS = 1200
MAX_DETAIL_CHARS = 2000


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ShortTermMemoryError(Exception):
    """短期记忆错误的基类。"""


class ShortTermMemoryValidationError(ShortTermMemoryError):
    """条目内容不合法。"""


@dataclass(frozen=True)
class ShortTermEntry:
    entry_id: str
    task_id: str
    summary: str
    architecture: str = ""
    notable: str = ""
    importance: int = 3
    #: 这条短期记忆在后续任务中被 Agent 认为“帮助很大”的次数。
    use_count: int = 0
    #: 最近一次“帮助很大”的原因（Agent 自己写）。
    helpful: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "task_id": self.task_id,
            "summary": self.summary,
            "architecture": self.architecture,
            "notable": self.notable,
            "importance": self.importance,
            "use_count": self.use_count,
            "helpful": self.helpful,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ShortTermEntry":
        return cls(
            entry_id=str(raw.get("entry_id") or ""),
            task_id=str(raw.get("task_id") or ""),
            summary=str(raw.get("summary") or ""),
            architecture=str(raw.get("architecture") or ""),
            notable=str(raw.get("notable") or ""),
            importance=int(raw.get("importance") or 3),
            use_count=int(raw.get("use_count") or 0),
            helpful=str(raw.get("helpful") or ""),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


@dataclass(frozen=True)
class PrivateShortTermMemory:
    agent_id: str
    revision: int
    entries: List[ShortTermEntry] = field(default_factory=list)
    updated_at: str = ""

    @property
    def needs_compaction(self) -> bool:
        return len(self.entries) > COMPACT_THRESHOLD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "revision": self.revision,
            "entries": [e.to_dict() for e in self.entries],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, agent_id: str, raw: Mapping[str, Any]
                  ) -> "PrivateShortTermMemory":
        return cls(
            agent_id=str(raw.get("agent_id") or agent_id),
            revision=int(raw.get("revision") or 1),
            entries=[ShortTermEntry.from_dict(e)
                     for e in (raw.get("entries") or [])
                     if isinstance(e, Mapping)],
            updated_at=str(raw.get("updated_at") or ""),
        )


def _check_text(value: Any, limit: int, name: str) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ShortTermMemoryValidationError(
            f"{name} 太长：{len(text)} 字符，上限 {limit}")
    return text


def make_entry(
    *,
    task_id: str,
    summary: str,
    architecture: str = "",
    notable: str = "",
    importance: int = 3,
    entry_id: Optional[str] = None,
    now: Optional[str] = None,
) -> ShortTermEntry:
    now = now or utc_now_iso()
    summary = _check_text(summary, MAX_SUMMARY_CHARS, "summary")
    if not summary:
        raise ShortTermMemoryValidationError("summary 不能为空")
    architecture = _check_text(architecture, MAX_DETAIL_CHARS,
                               "architecture")
    notable = _check_text(notable, MAX_DETAIL_CHARS, "notable")
    importance = int(importance)
    if importance < 1 or importance > 5:
        raise ShortTermMemoryValidationError(
            f"importance 必须在 1..5，收到 {importance!r}")
    return ShortTermEntry(
        entry_id=entry_id or f"stm-{uuid.uuid4().hex[:12]}",
        task_id=str(task_id or ""),
        summary=summary,
        architecture=architecture,
        notable=notable,
        importance=importance,
        created_at=now,
        updated_at=now,
    )


def add_entry(
    memory: Optional[PrivateShortTermMemory],
    entry: ShortTermEntry,
    *,
    agent_id: str,
    now: Optional[str] = None,
) -> PrivateShortTermMemory:
    """
    追加/替换一个任务条目。

    🔒 **不自动压缩**：超过一半只设置 `needs_compaction` 信号；
       抽象成长期记忆是 Agent 的工作（long_term_memory_consolidate）。
    """
    now = now or utc_now_iso()
    current = list(memory.entries if memory else [])
    # 同一个 task_id 幂等：替换旧条目
    if entry.task_id:
        current = [e for e in current if e.task_id != entry.task_id]
    current.append(entry)
    return PrivateShortTermMemory(
        agent_id=agent_id,
        revision=(memory.revision if memory else 0) + 1,
        entries=current,
        updated_at=now,
    )


def update_entry(
    memory: Optional[PrivateShortTermMemory],
    *,
    agent_id: str,
    entry_id: str,
    summary: Optional[str] = None,
    architecture: Optional[str] = None,
    notable: Optional[str] = None,
    importance: Optional[int] = None,
    remove: bool = False,
    now: Optional[str] = None,
) -> PrivateShortTermMemory:
    """修正/删除一个条目（反馈维护）。"""
    if memory is None:
        raise ShortTermMemoryValidationError("还没有短期记忆条目")
    now = now or utc_now_iso()
    found = False
    entries: List[ShortTermEntry] = []
    for e in memory.entries:
        if e.entry_id != entry_id:
            entries.append(e)
            continue
        found = True
        if remove:
            continue
        entries.append(ShortTermEntry(
            entry_id=e.entry_id,
            task_id=e.task_id,
            summary=(_check_text(summary, MAX_SUMMARY_CHARS, "summary")
                     if summary is not None else e.summary),
            architecture=(_check_text(architecture, MAX_DETAIL_CHARS,
                                      "architecture")
                          if architecture is not None else e.architecture),
            notable=(_check_text(notable, MAX_DETAIL_CHARS, "notable")
                     if notable is not None else e.notable),
            importance=(int(importance) if importance is not None
                        else e.importance),
            created_at=e.created_at,
            updated_at=now,
        ))
    if not found:
        raise ShortTermMemoryValidationError(
            f"找不到 short-term entry {entry_id!r}")
    return PrivateShortTermMemory(
        agent_id=agent_id, revision=memory.revision + 1,
        entries=entries, updated_at=now)


def reinforce_entry(
    memory: Optional[PrivateShortTermMemory],
    *,
    agent_id: str,
    entry_id: str,
    reason: str = "",
    now: Optional[str] = None,
) -> PrivateShortTermMemory:
    """
    Agent 反馈：这条短期记忆对当前任务帮助很大。

    · use_count += 1
    · importance = min(5, importance + 1)
    · helpful = reason
    """
    if memory is None:
        raise ShortTermMemoryValidationError("还没有短期记忆条目")
    now = now or utc_now_iso()
    found = False
    entries: List[ShortTermEntry] = []
    for e in memory.entries:
        if e.entry_id != entry_id:
            entries.append(e)
            continue
        found = True
        entries.append(ShortTermEntry(
            entry_id=e.entry_id, task_id=e.task_id,
            summary=e.summary, architecture=e.architecture,
            notable=e.notable,
            importance=min(5, e.importance + 1),
            use_count=e.use_count + 1,
            helpful=_check_text(reason, 500, "helpful"),
            created_at=e.created_at, updated_at=now))
    if not found:
        raise ShortTermMemoryValidationError(
            f"找不到 short-term entry {entry_id!r}")
    return PrivateShortTermMemory(
        agent_id=agent_id, revision=memory.revision + 1,
        entries=entries, updated_at=now)


def render(memory: PrivateShortTermMemory) -> str:
    lines = [f"revision = {memory.revision}"]
    if not memory.entries:
        lines.append("（还没有短期记忆）")
        return "\n".join(lines)
    for e in sorted(memory.entries, key=lambda x: x.created_at, reverse=True):
        lines.append(f"- [{e.entry_id}] task={e.task_id or '—'} "
                     f"importance={e.importance} use_count={e.use_count}\n"
                     f"  summary: {e.summary}")
        if e.architecture:
            lines.append(f"  architecture: {e.architecture}")
        if e.notable:
            lines.append(f"  notable: {e.notable}")
    return "\n".join(lines)


__all__ = [
    "CAPACITY", "COMPACT_THRESHOLD", "KEEP_AFTER_COMPACT",
    "ShortTermMemoryError", "ShortTermMemoryValidationError",
    "ShortTermEntry", "PrivateShortTermMemory",
    "make_entry", "add_entry", "update_entry", "reinforce_entry", "render",
]

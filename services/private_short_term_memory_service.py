"""
PrivateShortTermMemoryService —— 私有短期记忆应用层
=====================================================

    · 读取 / 渲染（任务开始时加载一次到内存）
    · 任务完成时从私有工作记忆 final snapshot 生成摘要条目
    · Agent 反馈修正（后续任务发现内容不正确时）

🔒 短期记忆跨 Task；只有 Agent 自己能读写。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from core.private_short_term_memory import (
    PrivateShortTermMemory,
    ShortTermEntry,
    make_entry,
    render,
)


class PrivateShortTermMemoryService:
    def __init__(self, store: Any):
        self.store = store

    # ------------------------------------------------------------
    def read(self, agent_id: str) -> PrivateShortTermMemory:
        memory = self.store.get(agent_id)
        if memory is not None:
            return memory
        return PrivateShortTermMemory(
            agent_id=agent_id, revision=0, entries=[], updated_at="")

    def render(self, agent_id: str) -> str:
        return render(self.read(agent_id))

    # ------------------------------------------------------------
    def add_from_final(self, agent_id: str, task_id: str,
                       snapshot: Optional[Mapping[str, Any]]) -> ShortTermEntry:
        """
        任务完成时，把私有工作记忆的 final snapshot 抽象成短期记忆条目。

        🔒 不复制节点内容；只保留 goal / architecture / notable。
        """
        nodes = {}
        if isinstance(snapshot, Mapping):
            nodes = snapshot.get("nodes") or {}

        goals: List[str] = []
        architecture: List[str] = []
        notable: List[str] = []
        for node_id, raw in (nodes or {}).items():
            if not isinstance(raw, Mapping):
                continue
            kind = str(raw.get("kind") or "")
            title = str(raw.get("title") or "").strip()
            content = raw.get("content")
            if kind == "goal":
                goals.append(title or node_id)
            elif kind in ("branch", "task"):
                architecture.append(f"{kind}:{title or node_id}")
            elif kind in ("finding", "issue", "note"):
                body = json.dumps(content, ensure_ascii=False,
                                  sort_keys=True) if content is not None else ""
                if len(body) > 300:
                    body = body[:300] + " …"
                notable.append(f"{kind}:{title or node_id}"
                               + (f" {body}" if body else ""))

        summary = (f"任务 {task_id} 完成；目标："
                   + (" | ".join(goals) if goals else "（未记录 goal）"))
        entry = make_entry(
            task_id=task_id,
            summary=summary[:1200],
            architecture=" | ".join(architecture)[:2000],
            notable=" | ".join(notable)[:2000],
            importance=4 if notable else 3,
        )
        self.store.add(agent_id, entry)
        return entry

    # ------------------------------------------------------------
    def update(self, agent_id: str, *, expected_revision: int,
               entry_id: str, summary: Optional[str] = None,
               architecture: Optional[str] = None,
               notable: Optional[str] = None,
               importance: Optional[int] = None,
               remove: bool = False) -> PrivateShortTermMemory:
        return self.store.update(
            agent_id, expected_revision=expected_revision, entry_id=entry_id,
            summary=summary, architecture=architecture, notable=notable,
            importance=importance, remove=remove)


__all__ = ["PrivateShortTermMemoryService"]

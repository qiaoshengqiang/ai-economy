"""
PublicWorkingMemoryService —— 公共工作记忆的应用层
====================================================

职责：

    · 参与者校验（只有 Task Participant 能读/写）
    · 读取当前版本 / 渲染成 prompt 片段
    · 写入（乐观锁交给 store）

🔒 它不是 Task Core，也不是 Shared Context：
    它不判断任务是否完成，不保存 status / price / contractor。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from core.public_working_memory import PublicWorkingMemory


class WorkingMemoryAccessError(Exception):
    """不是这个 Task 的参与者。"""


class PublicWorkingMemoryService:
    def __init__(self, store: Any, tasks: Any):
        self.store = store
        self.tasks = tasks

    # ------------------------------------------------------------
    def _task(self, task_id: str) -> Any:
        task = self.tasks.get(task_id)
        if task is None:
            raise WorkingMemoryAccessError(
                f"Task {task_id!r} 不存在，无法访问公共工作记忆。")
        return task

    def _require_participant(self, task_id: str, agent_id: str) -> Any:
        task = self._task(task_id)
        if not task.is_participant(agent_id):
            raise WorkingMemoryAccessError(
                f"Agent {agent_id!r} 不是 Task {task_id!r} 的参与方，"
                "不能读/写公共工作记忆。")
        return task

    # ------------------------------------------------------------
    def read(self, task_id: str, agent_id: str) -> PublicWorkingMemory:
        self._require_participant(task_id, agent_id)
        memory = self.store.get(task_id)
        if memory is not None:
            return memory
        return PublicWorkingMemory(
            task_id=task_id, revision=0, items={}, updated_at="")

    # ------------------------------------------------------------
    def update(
        self,
        task_id: str,
        agent_id: str,
        *,
        expected_revision: int,
        items: Optional[Mapping[str, Mapping[str, Any]]] = None,
        remove_keys: Optional[List[str]] = None,
    ) -> PublicWorkingMemory:
        self._require_participant(task_id, agent_id)
        return self.store.update(
            task_id,
            author=agent_id,
            expected_revision=int(expected_revision),
            items=items,
            remove_keys=remove_keys,
        )

    # ------------------------------------------------------------
    def render(self, task_id: str, agent_id: str) -> str:
        """
        渲染成 prompt 里的事实块。

        🔒 只呈现条目内容与 revision；不写「你应该更新」。
        """
        memory = self.read(task_id, agent_id)
        lines = [
            f"revision = {memory.revision}",
        ]
        if not memory.items:
            lines.append("（还没有条目）")
            return "\n".join(lines)

        children: Dict[Optional[str], List[str]] = {}
        for key, item in memory.items.items():
            children.setdefault(item.parent_key, []).append(key)
        for key in children:
            children[key].sort()

        def walk(parent_key: Optional[str], depth: int) -> None:
            indent = "  " * depth
            for key in children.get(parent_key, []):
                item = memory.items[key]
                value = json.dumps(
                    item.value, ensure_ascii=False, sort_keys=True)
                if len(value) > 1200:
                    value = value[:1200] + " …（已截断）"
                lines.append(
                    f"{indent}[{item.kind}] {key}  by {item.author} "
                    f"at {item.updated_at}\n{indent}  {value}")
                walk(key, depth + 1)

        walk(None, 0)
        return "\n".join(lines)


__all__ = ["PublicWorkingMemoryService", "WorkingMemoryAccessError"]

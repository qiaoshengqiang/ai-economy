"""
PrivateWorkingMemoryService —— 私有工作记忆应用层
==================================================

职责：

    · 参与者校验（Agent 必须参与这个 Task）
    · 渲染成树（给 decision prompt）
    · 更新 / finalize（任务结项清理）

🔒 私有：读/写只使用 intent.agent_id；没有“读别人的私有记忆”的 API。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.private_working_memory import PrivateWorkingMemory


class PrivateWorkingMemoryAccessError(Exception):
    """Agent 不是这个 Task 的参与者。"""


class PrivateWorkingMemoryService:
    def __init__(self, store: Any, tasks: Any):
        self.store = store
        self.tasks = tasks

    def _require_participant(self, agent_id: str, task_id: str) -> Any:
        task = self.tasks.get(task_id)
        if task is None:
            raise PrivateWorkingMemoryAccessError(
                f"Task {task_id!r} 不存在，无法访问私有工作记忆。")
        if not task.is_participant(agent_id):
            raise PrivateWorkingMemoryAccessError(
                f"Agent {agent_id!r} 不是 Task {task_id!r} 的参与方，"
                "不能访问它的私有工作记忆。")
        return task

    # ------------------------------------------------------------
    def read(self, agent_id: str, task_id: str) -> PrivateWorkingMemory:
        self._require_participant(agent_id, task_id)
        memory = self.store.get(agent_id, task_id)
        if memory is not None:
            return memory
        return PrivateWorkingMemory(
            agent_id=agent_id, task_id=task_id, revision=0,
            nodes={}, updated_at="")

    # ------------------------------------------------------------
    def update(
        self,
        agent_id: str,
        task_id: str,
        *,
        expected_revision: int,
        nodes: Optional[Mapping[str, Mapping[str, Any]]] = None,
        remove_nodes: Optional[List[str]] = None,
    ) -> PrivateWorkingMemory:
        self._require_participant(agent_id, task_id)
        return self.store.update(
            agent_id, task_id,
            expected_revision=int(expected_revision),
            nodes=nodes, remove_nodes=remove_nodes)

    # ------------------------------------------------------------
    def render(self, agent_id: str, task_id: str) -> str:
        memory = self.read(agent_id, task_id)
        lines = [f"revision = {memory.revision}"]
        if not memory.nodes:
            lines.append("（还没有节点）")
            return "\n".join(lines)

        children: Dict[Optional[str], List[str]] = {}
        for node_id, node in memory.nodes.items():
            children.setdefault(node.parent_id, []).append(node_id)
        for key in children:
            children[key].sort()

        def walk(parent_id: Optional[str], depth: int) -> None:
            indent = "  " * depth
            for node_id in children.get(parent_id, []):
                node = memory.nodes[node_id]
                body = json.dumps(node.content, ensure_ascii=False,
                                  sort_keys=True)
                if len(body) > 800:
                    body = body[:800] + " …（已截断）"
                lines.append(
                    f"{indent}- [{node.kind}] {node_id}: {node.title}\n"
                    f"{indent}  {body}")
                walk(node_id, depth + 1)

        walk(None, 0)
        return "\n".join(lines)

    # ------------------------------------------------------------
    def finalize(self, task_id: str, agent_ids: Iterable[str]
                 ) -> Dict[str, Any]:
        """
        任务结项：对每个 Agent 返回 final snapshot 并清空当前工作记忆。

        🔒 短期记忆迁移尚未实现；返回值就是它的输入。
        """
        final: Dict[str, Any] = {}
        for agent_id in agent_ids:
            snapshot = self.store.finalize(agent_id, task_id)
            if snapshot is not None:
                final[agent_id] = snapshot.to_dict()
        return final


__all__ = ["PrivateWorkingMemoryService", "PrivateWorkingMemoryAccessError"]

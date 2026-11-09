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

from core.public_working_memory import (
    PublicWorkingMemory,
    WorkingMemoryValidationError,
)


class WorkingMemoryAccessError(Exception):
    """不是这个 Task 的参与者。"""


class PublicWorkingMemoryService:
    def __init__(self, store: Any, tasks: Any, gateway: Any = None):
        self.store = store
        self.tasks = tasks
        self.gateway = gateway

    def attach_gateway(self, gateway: Any) -> None:
        """组合根在 WorkspaceGateway 构造完成后注入（用于 evidence 校验）。"""
        self.gateway = gateway

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
        task = self._require_participant(task_id, agent_id)
        self._validate_evidence(task, agent_id, items or {})
        return self.store.update(
            task_id,
            author=agent_id,
            expected_revision=int(expected_revision),
            items=items,
            remove_keys=remove_keys,
        )

    def _validate_evidence(self, task: Any, agent_id: str,
                           items: Mapping[str, Mapping[str, Any]]) -> None:
        """
        校验 item.evidence 是否指向真实的 Workspace artifact。

        🔒 这是“完成声明”与真实产物之间的唯一硬链接：
           evidence = {"path": "tests/acceptance.spec.js", "revision": 1}
        """
        workspace_id = (
            task.workspace.workspace_id if task.workspace else "")
        for key, raw in items.items():
            if not isinstance(raw, Mapping) or "evidence" not in raw:
                continue
            evidence = raw.get("evidence")
            if evidence is None:
                continue
            if not isinstance(evidence, Mapping):
                raise WorkingMemoryValidationError(
                    f"item {key!r} 的 evidence 必须是对象")
            unknown = sorted(set(evidence) - {"path", "revision"})
            if unknown:
                raise WorkingMemoryValidationError(
                    f"item {key!r} 的 evidence 不认识的字段：{unknown}；"
                    "只支持 path / revision")
            path = evidence.get("path")
            if not isinstance(path, str) or not path.strip():
                raise WorkingMemoryValidationError(
                    f"item {key!r} 的 evidence.path 必须是非空字符串")
            path = path.strip()
            if self.gateway is None or not workspace_id:
                raise WorkingMemoryValidationError(
                    f"item {key!r} 带 evidence，但 evidence 校验器不可用")
            got = self.gateway.read(workspace_id, path, agent_id)
            if not got.success:
                err = got.error or {}
                raise WorkingMemoryValidationError(
                    f"item {key!r} 的 evidence 指向不存在的 artifact："
                    f"{path!r}（{err.get('message') or err.get('code')}）")
            expected_revision = evidence.get("revision")
            if expected_revision is not None:
                try:
                    expected_revision = int(expected_revision)
                except (TypeError, ValueError):
                    raise WorkingMemoryValidationError(
                        f"item {key!r} 的 evidence.revision 必须是整数")
                actual = (got.result or {}).get("revision")
                if actual is None:
                    raise WorkingMemoryValidationError(
                        f"item {key!r} 的 evidence 指向的 artifact "
                        f"没有 revision：{path!r}")
                if int(actual) != expected_revision:
                    raise WorkingMemoryValidationError(
                        f"item {key!r} 的 evidence.revision="
                        f"{expected_revision} 与当前 revision={actual} 不一致")

    # ------------------------------------------------------------
    def render(self, task_id: str, agent_id: str) -> str:
        """
        渲染成 prompt 里的事实块。

        🔒 只呈现条目内容与 revision；不写「你应该更新」。
        """
        memory = self.read(task_id, agent_id)
        lines = [
            f"revision = {memory.revision}",
            "（以下条目是参与者的记录/观点，不是 Domain Fact；"
            "完成声明只有带 evidence 才是已验证事实。）",
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
                if item.evidence:
                    lines.append(
                        f"{indent}  evidence: {item.evidence.get('path')}"
                        + (f" @R{item.evidence.get('revision')}"
                           if item.evidence.get("revision") is not None
                           else "")
                        + " (verified)")
                else:
                    lines.append(
                        f"{indent}  evidence: (none — self-report, not verified)")
                walk(key, depth + 1)

        walk(None, 0)
        return "\n".join(lines)


__all__ = ["PublicWorkingMemoryService", "WorkingMemoryAccessError"]

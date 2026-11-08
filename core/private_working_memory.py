"""
Private Working Memory —— 单个 Agent 的私有工作记忆
=====================================================

【它解决什么】

    Agent 在执行一个具体任务时，需要维护自己的抽象结构：

        · 这个任务的目标 / 子目标
        · 分支（前端 / 后端 / 验收 / 测试…）
        · 当前正在做的节点
        · 重大发现（finding）
        · 重大问题（issue）
        · 临时笔记（note）

    它与 Public Working Memory 的区别：

        Public  = 参与者可见的计划/分工/清单/进度
        Private = **只有自己可见**的抽象工作结构

    它与 Shared Context 的区别：

        Shared Context 是 Proposal → Alignment 后的共同认知；
        Private Working Memory 不需要对齐，也不会自动变成公共事实。

【层级】

    节点有 `parent_id`；根节点 parent_id = None。
    Store 层只保存节点；Service 渲染成树。

【并发】

    每个 (agent_id, task_id) 一个 revision；CAS on revision。
    私有记忆没有跨 Agent 竞争，但仍然防止同一 Agent 的并发写覆盖。

【任务结项】

    `finalize()` 把当前快照留在 revisions（append-only）并删除当前行。
    “迁移到短期记忆”是下一阶段；本次只保留可消费的 final snapshot。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple

FIRST_REVISION = 1

#: 节点类型。
NODE_KINDS = frozenset({
    "goal",      # 目标 / 子目标
    "branch",    # 结构分支
    "task",      # 当前正在做的具体工作
    "finding",   # 重大发现
    "issue",     # 重大问题 / 风险
    "note",      # 临时笔记
})

#: node_id 语法：小写开头，允许字母数字与 `_.:-`，最长 64。
_NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")

MAX_NODE_CHARS = 4000
MAX_TOTAL_CHARS = 32000
MAX_TITLE_CHARS = 200


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ============================================================
# 错误
# ============================================================
class PrivateWorkingMemoryError(Exception):
    """私有工作记忆错误的基类。"""


class PrivateWorkingMemoryValidationError(PrivateWorkingMemoryError):
    """节点 / 结构不合法。"""


class ConcurrentPrivateWorkingMemoryModification(PrivateWorkingMemoryError):
    def __init__(self, agent_id: str, task_id: str,
                 expected_revision: int, actual_revision: Optional[int]):
        self.agent_id = agent_id
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        suffix = (f"（当前 revision={actual_revision}）"
                  if actual_revision is not None else "")
        super().__init__(
            f"{agent_id} 在 Task {task_id!r} 的私有工作记忆已被更新："
            f"你基于 revision={expected_revision} 提交{suffix}。")


# ============================================================
# 节点 / 记忆
# ============================================================
@dataclass(frozen=True)
class PrivateWorkingNode:
    node_id: str
    kind: str
    title: str
    content: Any = None
    parent_id: Optional[str] = None
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "parent_id": self.parent_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PrivateWorkingNode":
        return cls(
            node_id=str(raw.get("node_id") or ""),
            kind=str(raw.get("kind") or ""),
            title=str(raw.get("title") or ""),
            content=raw.get("content"),
            parent_id=(str(raw["parent_id"])
                       if raw.get("parent_id") else None),
            updated_at=str(raw.get("updated_at") or ""),
        )


@dataclass(frozen=True)
class PrivateWorkingMemory:
    agent_id: str
    task_id: str
    revision: int
    nodes: Dict[str, PrivateWorkingNode] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, agent_id: str, task_id: str,
                  raw: Mapping[str, Any]) -> "PrivateWorkingMemory":
        nodes = {}
        for node_id, value in (raw.get("nodes") or {}).items():
            if isinstance(value, Mapping):
                value = dict(value)
                value.setdefault("node_id", node_id)
                nodes[str(node_id)] = PrivateWorkingNode.from_dict(value)
        return cls(
            agent_id=str(raw.get("agent_id") or agent_id),
            task_id=str(raw.get("task_id") or task_id),
            revision=int(raw.get("revision") or FIRST_REVISION),
            nodes=nodes,
            updated_at=str(raw.get("updated_at") or ""),
        )


# ============================================================
# 校验
# ============================================================
def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise PrivateWorkingMemoryValidationError(
            f"content 不能序列化成 JSON：{exc}") from exc


def validate_node_id(node_id: str) -> str:
    node_id = str(node_id or "").strip()
    if not _NODE_ID_RE.match(node_id):
        raise PrivateWorkingMemoryValidationError(
            f"node_id 不合法：{node_id!r}；"
            "要求小写开头，只含字母数字与 `_.:-`，最长 64。")
    return node_id


def _validate_node(node_id: str, raw: Mapping[str, Any], *,
                   now: str) -> PrivateWorkingNode:
    if not isinstance(raw, Mapping):
        raise PrivateWorkingMemoryValidationError(
            f"节点 {node_id!r} 必须是对象")
    node_id = validate_node_id(node_id)
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in NODE_KINDS:
        raise PrivateWorkingMemoryValidationError(
            f"节点 {node_id!r} 的 kind 不合法：{kind!r}；"
            f"只支持 {sorted(NODE_KINDS)}")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise PrivateWorkingMemoryValidationError(
            f"节点 {node_id!r} 需要 title")
    if len(title) > MAX_TITLE_CHARS:
        raise PrivateWorkingMemoryValidationError(
            f"节点 {node_id!r} 的 title 太长：{len(title)} 字符")
    content = raw.get("content")
    body = _dump(content)
    if len(body) > MAX_NODE_CHARS:
        raise PrivateWorkingMemoryValidationError(
            f"节点 {node_id!r} 的 content 太大：{len(body)} 字符，"
            f"上限 {MAX_NODE_CHARS}")
    parent = raw.get("parent_id")
    parent_id = str(parent).strip() if parent else None
    if parent_id is not None:
        validate_node_id(parent_id)
        if parent_id == node_id:
            raise PrivateWorkingMemoryValidationError(
                f"节点 {node_id!r} 不能以自己为 parent")
    return PrivateWorkingNode(
        node_id=node_id, kind=kind, title=title, content=content,
        parent_id=parent_id, updated_at=now)


def _would_cycle(node_id: str, parent_id: Optional[str],
                 nodes: Mapping[str, PrivateWorkingNode]) -> bool:
    seen: Set[str] = set()
    cur = parent_id
    while cur:
        if cur == node_id:
            return True
        if cur in seen:
            return True
        seen.add(cur)
        node = nodes.get(cur)
        cur = node.parent_id if node else None
    return False


def _check_total(nodes: Iterable[PrivateWorkingNode]) -> None:
    total = sum(len(_dump(n.content)) + len(n.title) for n in nodes)
    if total > MAX_TOTAL_CHARS:
        raise PrivateWorkingMemoryValidationError(
            f"私有工作记忆总大小 {total} 字符，上限 {MAX_TOTAL_CHARS}。")


# ============================================================
# 更新
# ============================================================
def apply_update(
    current: Optional[PrivateWorkingMemory],
    *,
    agent_id: str,
    task_id: str,
    expected_revision: int,
    nodes: Optional[Mapping[str, Mapping[str, Any]]] = None,
    remove_nodes: Optional[Iterable[str]] = None,
    now: Optional[str] = None,
) -> PrivateWorkingMemory:
    actual = current.revision if current else 0
    if int(expected_revision) != actual:
        raise ConcurrentPrivateWorkingMemoryModification(
            agent_id, task_id, int(expected_revision), actual)

    now = now or utc_now_iso()
    next_nodes: Dict[str, PrivateWorkingNode] = dict(
        current.nodes if current else {})

    for node_id, raw in (nodes or {}).items():
        node = _validate_node(node_id, raw, now=now)
        next_nodes[node.node_id] = node

    remove = {str(x) for x in (remove_nodes or [])}
    if remove:
        children = [n for n in next_nodes.values()
                    if n.parent_id in remove and n.node_id not in remove]
        if children:
            raise PrivateWorkingMemoryValidationError(
                "remove_nodes 不能删除还有子节点的节点："
                f"{sorted(n.node_id for n in children)}")
        for node_id in remove:
            next_nodes.pop(node_id, None)

    for node in next_nodes.values():
        if node.parent_id and node.parent_id not in next_nodes:
            raise PrivateWorkingMemoryValidationError(
                f"节点 {node.node_id!r} 的 parent_id={node.parent_id!r} "
                "不存在")
        if _would_cycle(node.node_id, node.parent_id, next_nodes):
            raise PrivateWorkingMemoryValidationError(
                f"节点 {node.node_id!r} 形成了环")

    _check_total(next_nodes.values())

    return PrivateWorkingMemory(
        agent_id=agent_id, task_id=task_id,
        revision=actual + 1, nodes=next_nodes, updated_at=now)


__all__ = [
    "FIRST_REVISION", "NODE_KINDS",
    "MAX_NODE_CHARS", "MAX_TOTAL_CHARS", "MAX_TITLE_CHARS",
    "PrivateWorkingMemoryError", "PrivateWorkingMemoryValidationError",
    "ConcurrentPrivateWorkingMemoryModification",
    "PrivateWorkingNode", "PrivateWorkingMemory",
    "validate_node_id", "apply_update",
]

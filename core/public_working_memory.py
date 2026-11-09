"""
Public Working Memory —— Task 范围内的公共工作记忆
====================================================

【它解决什么】

    Shared Context 回答「团队确认了什么」。
    但 Agent 在工作过程中还需要一个**可写的操作台**：

        · 总计划 / 阶段划分
        · 谁负责什么
        · 完成清单（owner 的最终确认项）
        · 当前进度 / 已知风险
        · 需要共享的抽象信息

    这就是 Public Working Memory：**Task 范围内、参与者可见、可更新**
    的工作记忆。

【它不是什么（三条硬边界）】

    ① 不是 Task Core
       `status` / `agreed_price` / `contractor` / `budget` 仍然只由
       Task Core 权威。这里禁止用这些 key 复制一份可修改的状态。

    ② 不是 Shared Context
       Shared Context 是**已确认的共同认知**（Proposal → Alignment）。
       工作记忆是**可变的操作状态**，不自动成为团队决定。
       要把工作记忆里的结论升级为共识，仍然走 Alignment。

    ③ 不是 Private Working Memory
       这里是公共区；每个 Agent 的私有工作记忆是另一块（本次未实现）。

【为什么用整份快照 + revision，而不是 item 级 diff】

    工作记忆是**小对象**（计划/清单/几条进度），不是大文档。
    整份快照让「Revision N 长什么样」是 O(1) 读取；
    revision 历史 append-only，方便审计。
    代价是少量存储冗余 —— 可接受。

【并发】

    与 Shared Context / TaskStore 同一个 CAS 模式：

        UPDATE ... WHERE task_id=? AND revision=?
        影响 0 行 → ConcurrentWorkingMemoryModification

    不自动 merge / retry / rebase。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: 一个 Task 的公共工作记忆，revision 从 1 开始。
FIRST_REVISION = 1

#: item 的 kind。🔒 这是**语法分类**，不是行为指令。
ITEM_KINDS = frozenset({
    "plan",        # 总计划 / 阶段
    "assignment",  # 分工 / 责任
    "checklist",   # 完成清单 / owner 确认项
    "progress",    # 当前进度
    "note",        # 需要共享的抽象信息
})

#: 禁止用这些 key 复制 Task Core 的权威字段。
#:
#: 🔒 工作记忆可以记录「我们的完成清单」，
#:    但不能记录「Task.status = X」这种第二份可修改事实。
FORBIDDEN_KEYS = frozenset({
    "status", "task_status", "agreed_price", "contractor", "budget",
})

#: key 的语法：小写开头，允许字母数字与 `_.:-`，最长 64。
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")

#: 单条 value 的 JSON 字符上限 / 整个 memory 的上限。
MAX_ITEM_CHARS = 4000
MAX_TOTAL_CHARS = 32000


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
class WorkingMemoryError(Exception):
    """公共工作记忆相关错误的基类。"""


class WorkingMemoryValidationError(WorkingMemoryError):
    """item / key / value 不合法。"""


class WorkingMemoryNotFound(WorkingMemoryError):
    """这个 Task 还没有公共工作记忆。"""


class ConcurrentWorkingMemoryModification(WorkingMemoryError):
    """乐观锁冲突：有人在你之前更新了工作记忆。"""

    def __init__(self, task_id: str, expected_revision: int,
                 actual_revision: Optional[int] = None):
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        suffix = (f"（当前 revision={actual_revision}）"
                  if actual_revision is not None else "")
        super().__init__(
            f"Task {task_id!r} 的公共工作记忆已被别人更新："
            f"你基于 revision={expected_revision} 提交{suffix}。"
            "请重新读取后再更新。")


# ============================================================
# Item / Memory
# ============================================================
@dataclass(frozen=True)
class WorkingMemoryItem:
    key: str
    kind: str
    value: Any
    author: str
    updated_at: str
    #: 🔴 可选的父节点 key，用于表达“大架构 → 局部细节”的层级。
    #: None = 根条目；旧数据没有该字段时默认为 None。
    parent_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "value": self.value,
            "author": self.author,
            "updated_at": self.updated_at,
            "parent_key": self.parent_key,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkingMemoryItem":
        return cls(
            key=str(raw.get("key") or ""),
            kind=str(raw.get("kind") or ""),
            value=raw.get("value"),
            author=str(raw.get("author") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            parent_key=(str(raw["parent_key"])
                        if raw.get("parent_key") else None),
        )


@dataclass(frozen=True)
class PublicWorkingMemory:
    task_id: str
    revision: int
    items: Dict[str, WorkingMemoryItem] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "items": {k: v.to_dict() for k, v in self.items.items()},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, task_id: str, raw: Mapping[str, Any]
                  ) -> "PublicWorkingMemory":
        items = {}
        for key, value in (raw.get("items") or {}).items():
            if isinstance(value, Mapping):
                value = dict(value)
                value.setdefault("key", key)
                items[str(key)] = WorkingMemoryItem.from_dict(value)
        return cls(
            task_id=str(raw.get("task_id") or task_id),
            revision=int(raw.get("revision") or FIRST_REVISION),
            items=items,
            updated_at=str(raw.get("updated_at") or ""),
        )


# ============================================================
# 校验
# ============================================================
def validate_key(key: str) -> str:
    key = str(key or "").strip()
    if not key:
        raise WorkingMemoryValidationError("working memory key 不能为空")
    if key.lower() in FORBIDDEN_KEYS:
        raise WorkingMemoryValidationError(
            f"working memory 不能用 key={key!r} 复制 Task Core 的权威字段；"
            "Task 状态/价格/承接方/预算只属于 Task Core。")
    if not _KEY_RE.match(key):
        raise WorkingMemoryValidationError(
            f"working memory key 不合法：{key!r}；"
            "要求小写开头，只含字母数字与 `_.:-`，最长 64。")
    return key


def _dump_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise WorkingMemoryValidationError(
            f"working memory value 不能序列化成 JSON：{exc}") from exc


def validate_item(key: str, kind: str, value: Any) -> Tuple[str, str, str]:
    key = validate_key(key)
    kind = str(kind or "").strip().lower()
    if kind not in ITEM_KINDS:
        raise WorkingMemoryValidationError(
            f"working memory kind 不合法：{kind!r}；"
            f"只支持 {sorted(ITEM_KINDS)}")
    body = _dump_value(value)
    if len(body) > MAX_ITEM_CHARS:
        raise WorkingMemoryValidationError(
            f"working memory item {key!r} 太大：{len(body)} 字符，"
            f"上限 {MAX_ITEM_CHARS}。请只存抽象信息，不要存大段正文。")
    return key, kind, body


def _validate_hierarchy(items: Mapping[str, WorkingMemoryItem]) -> None:
    """parent_key 必须存在，且不能形成环。"""
    for item in items.values():
        if item.parent_key is None:
            continue
        if item.parent_key == item.key:
            raise WorkingMemoryValidationError(
                f"working memory item {item.key!r} 不能以自己为 parent_key")
        if item.parent_key not in items:
            raise WorkingMemoryValidationError(
                f"working memory item {item.key!r} 的 parent_key="
                f"{item.parent_key!r} 不存在")
        seen = {item.key}
        current = item.parent_key
        while current is not None:
            if current in seen:
                raise WorkingMemoryValidationError(
                    f"working memory 层级出现环：{sorted(seen)} → {current!r}")
            seen.add(current)
            parent = items.get(current)
            current = parent.parent_key if parent is not None else None


def _check_total(items: Iterable[WorkingMemoryItem]) -> None:
    total = sum(len(_dump_value(i.value)) for i in items)
    if total > MAX_TOTAL_CHARS:
        raise WorkingMemoryValidationError(
            f"public working memory 总大小 {total} 字符，"
            f"上限 {MAX_TOTAL_CHARS}。请删减/合并条目。")


# ============================================================
# 更新
# ============================================================
def apply_update(
    current: Optional[PublicWorkingMemory],
    *,
    task_id: str,
    author: str,
    expected_revision: int,
    items: Optional[Mapping[str, Mapping[str, Any]]] = None,
    remove_keys: Optional[Iterable[str]] = None,
    now: Optional[str] = None,
) -> PublicWorkingMemory:
    """
    生成**下一版** PublicWorkingMemory（纯函数，不碰数据库）。

    `items` 形状：
        {"plan": {"kind": "plan", "value": {...}},
         "checklist:acceptance": {"kind": "checklist", "value": [...]}}
    """
    actual_revision = current.revision if current else 0
    if int(expected_revision) != actual_revision:
        raise ConcurrentWorkingMemoryModification(
            task_id, int(expected_revision), actual_revision)

    now = now or utc_now_iso()
    next_items: Dict[str, WorkingMemoryItem] = dict(
        current.items if current else {})

    for key, raw in (items or {}).items():
        if not isinstance(raw, Mapping):
            raise WorkingMemoryValidationError(
                f"working memory item {key!r} 必须是对象 "
                "（含 kind / value）")
        key2, kind, _ = validate_item(key, raw.get("kind"), raw.get("value"))
        # 🔒 `parent_key` 缺省时保留已有层级；显式 null 才是“移到根”。
        if "parent_key" in raw:
            parent_raw = raw.get("parent_key")
            parent_key = (validate_key(parent_raw)
                          if parent_raw else None)
        else:
            existing = next_items.get(key2)
            parent_key = existing.parent_key if existing else None
        next_items[key2] = WorkingMemoryItem(
            key=key2, kind=kind, value=raw.get("value"),
            author=str(author), updated_at=now, parent_key=parent_key)

    remove_set = {str(key) for key in (remove_keys or [])}
    for key in remove_set:
        children = [item.key for item in next_items.values()
                    if item.parent_key == key and item.key not in remove_set]
        if children:
            raise WorkingMemoryValidationError(
                f"不能删除 {key!r}：它还有子条目 {sorted(children)}；"
                "请先删除/移动子条目。")
    for key in remove_set:
        next_items.pop(key, None)

    _validate_hierarchy(next_items)
    _check_total(next_items.values())

    return PublicWorkingMemory(
        task_id=task_id,
        revision=actual_revision + 1,
        items=next_items,
        updated_at=now,
    )


__all__ = [
    "FIRST_REVISION", "ITEM_KINDS", "FORBIDDEN_KEYS",
    "MAX_ITEM_CHARS", "MAX_TOTAL_CHARS",
    "WorkingMemoryError", "WorkingMemoryValidationError",
    "WorkingMemoryNotFound", "ConcurrentWorkingMemoryModification",
    "WorkingMemoryItem", "PublicWorkingMemory",
    "validate_key", "validate_item", "apply_update",
]

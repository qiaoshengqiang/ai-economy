"""
Audit 与 Workspace Event —— **两个不同的问题**
============================================================

【🔒 必须严格区分（任务书 §30 / §31）】

    Audit 回答： **谁做了什么？结果如何？**
        · 包含**失败的尝试**（EDIT_ATTEMPT / result = FAILED）
        · 需要**尽可能长期保存**
        · 是「操作记录」

    Workspace Event 回答： **Workspace 状态发生了什么变化？**
        · 只记录**成功的状态变化**
        · 是「协作基础设施」—— 未来 Runtime / Communication 层可以订阅

【🔒 两条容易写错的】

    ① **失败的 edit 不产生 RESOURCE_UPDATED Event。**
       它只产生一条 Audit（EDIT_ATTEMPT / FAILED）。

    ② **Event ≠ 向所有 Agent 发 Message。**
       `RESOURCE_UPDATED` **不会**自动广播。
       是否通知别人，由 **Agent 自己判断**（ADR-0028）。
"""

from __future__ import annotations

from enum import Enum


class AuditAction(str, Enum):
    """
    Audit 的动作名。

    第一版覆盖 Gateway 的正式操作 + 它们的**失败尝试**。
    """

    LIST = "LIST"
    READ = "READ"

    CREATE = "CREATE"
    EDIT = "EDIT"
    DELETE = "DELETE"
    RENAME = "RENAME"
    MOVE = "MOVE"

    HANDOFF = "HANDOFF"
    RELEASE = "RELEASE"

    # Governance
    FORCE_UNLOCK = "FORCE_UNLOCK"

    # ---- 失败尝试 ----
    # 【为什么单独一套】
    #   任务书 §20 明确要求失败操作**也可以**有 Audit。
    #   用 `<ACTION>_ATTEMPT` 而不是复用成功动作名，
    #   这样「有多少次编辑尝试失败」可以直接聚合出来。
    CREATE_ATTEMPT = "CREATE_ATTEMPT"
    EDIT_ATTEMPT = "EDIT_ATTEMPT"
    DELETE_ATTEMPT = "DELETE_ATTEMPT"
    RENAME_ATTEMPT = "RENAME_ATTEMPT"
    MOVE_ATTEMPT = "MOVE_ATTEMPT"
    HANDOFF_ATTEMPT = "HANDOFF_ATTEMPT"
    RELEASE_ATTEMPT = "RELEASE_ATTEMPT"


class AuditResult(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class WorkspaceEventType(str, Enum):
    """
    **只记录成功的状态变化。**

    🔒 没有 `*_FAILED` 事件 —— 失败属于 Audit，不属于 Event。
    """

    RESOURCE_CREATED = "RESOURCE_CREATED"
    RESOURCE_UPDATED = "RESOURCE_UPDATED"
    RESOURCE_DELETED = "RESOURCE_DELETED"
    RESOURCE_RENAMED = "RESOURCE_RENAMED"
    RESOURCE_MOVED = "RESOURCE_MOVED"

    RESPONSIBILITY_HANDOFF = "RESPONSIBILITY_HANDOFF"
    RESPONSIBILITY_RELEASED = "RESPONSIBILITY_RELEASED"

    GOVERNANCE_FORCE_UNLOCK = "GOVERNANCE_FORCE_UNLOCK"


#: Audit 动作 → 对应的「失败尝试」动作
ATTEMPT_OF: dict[AuditAction, AuditAction] = {
    AuditAction.CREATE: AuditAction.CREATE_ATTEMPT,
    AuditAction.EDIT: AuditAction.EDIT_ATTEMPT,
    AuditAction.DELETE: AuditAction.DELETE_ATTEMPT,
    AuditAction.RENAME: AuditAction.RENAME_ATTEMPT,
    AuditAction.MOVE: AuditAction.MOVE_ATTEMPT,
    AuditAction.HANDOFF: AuditAction.HANDOFF_ATTEMPT,
    AuditAction.RELEASE: AuditAction.RELEASE_ATTEMPT,
}


def attempt_action(action: AuditAction) -> AuditAction:
    """拿到一个动作的「失败尝试」版本（没有对应项时原样返回）。"""
    return ATTEMPT_OF.get(action, action)

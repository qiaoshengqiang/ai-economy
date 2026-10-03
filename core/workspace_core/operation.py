"""
OperationResult 与结构化错误码（Step 6 的前置）
============================================================

【为什么必须结构化（任务书 §28）】

    🔒 **不要让 Agent 必须解析自然语言错误。**

    失败时 Agent 需要知道的是：
        · 是版本过期（可以重新 read 再改）
        · 还是锁在别人手里（需要走 Handoff）
        · 还是自己根本不是 Participant（做什么都没用）

    这三件事的**正确应对完全不同**，所以必须是机器可读的 code。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    """
    第一版的错误码集合（任务书 §28 点名 + 实现中必须补的几个）。

    ⚠️ 新增错误码时应保持「Agent 能据此决定下一步」这个标准，
       不要为了描述细节而无限膨胀。
    """

    # ---- 任务书 §28 点名 ----
    STALE_REVISION = "STALE_REVISION"
    LOCKED_BY_OTHER_AGENT = "LOCKED_BY_OTHER_AGENT"
    NOT_TASK_PARTICIPANT = "NOT_TASK_PARTICIPANT"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    OUTSIDE_WORKSPACE = "OUTSIDE_WORKSPACE"
    INVALID_PARENT = "INVALID_PARENT"
    RESOURCE_BOUNDARY_VIOLATION = "RESOURCE_BOUNDARY_VIOLATION"

    # ---- 实现中必须补的（否则语义无法表达）----
    NOT_EFFECTIVE_LOCK_HOLDER = "NOT_EFFECTIVE_LOCK_HOLDER"
    NOT_CONTROLLER = "NOT_CONTROLLER"
    INVALID_RESOURCE_TYPE = "INVALID_RESOURCE_TYPE"
    NAME_CONFLICT = "NAME_CONFLICT"
    RESOURCE_DELETED = "RESOURCE_DELETED"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    GOVERNANCE_REQUIRED = "GOVERNANCE_REQUIRED"


def new_operation_id() -> str:
    """每次 Gateway 调用一个 operation_id —— Audit 与 Event 用它串联。"""
    return f"OP-{uuid.uuid4().hex[:12]}"


@dataclass
class OperationResult:
    """
    Gateway 的**统一返回格式**（任务书 §28）。

    成功：
        success = True
        result  = {"resource_id": ..., "revision": ...}
    失败：
        success = False
        error   = {"code": "STALE_REVISION", "current_revision": 18, ...}

    🔒 **只返回适合该错误的信息** —— 不把所有字段都塞进去。
    """

    operation_id: str = field(default_factory=new_operation_id)
    success: bool = False
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    references: dict[str, Any] = field(default_factory=dict)

    # ----------------------------------------------------------
    # 构造
    # ----------------------------------------------------------
    @classmethod
    def ok(
        cls,
        *,
        operation_id: str,
        result: Optional[dict[str, Any]] = None,
        references: Optional[dict[str, Any]] = None,
    ) -> "OperationResult":
        return cls(
            operation_id=operation_id,
            success=True,
            result=result or {},
            error=None,
            references=references or {},
        )

    @classmethod
    def fail(
        cls,
        *,
        operation_id: str,
        code: ErrorCode,
        message: str = "",
        references: Optional[dict[str, Any]] = None,
        **details: Any,
    ) -> "OperationResult":
        payload: dict[str, Any] = {"code": code.value, "message": message}
        payload.update({k: v for k, v in details.items() if v is not None})
        return cls(
            operation_id=operation_id,
            success=False,
            result=None,
            error=payload,
            references=references or {},
        )

    # ----------------------------------------------------------
    # 便捷读取
    # ----------------------------------------------------------
    @property
    def error_code(self) -> Optional[str]:
        return self.error.get("code") if self.error else None

    def to_dict(self) -> dict[str, Any]:
        """
        转成普通 dict —— **用于跨进程 / 序列化边界**。

        ⚠️ 这里**不会**出现任何宿主机物理路径。
           Agent 只能看到 workspace_id / resource_id / 逻辑 path。
        """
        return {
            "operation_id": self.operation_id,
            "success": self.success,
            "result": self.result,
            "error": self.error,
            "references": self.references,
        }

    def __repr__(self) -> str:  # pragma: no cover
        if self.success:
            return f"OperationResult(ok, {self.operation_id}, {self.result})"
        return f"OperationResult(fail, {self.operation_id}, {self.error_code})"

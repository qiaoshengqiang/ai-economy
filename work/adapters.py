"""
Work Action Adapters —— 只做参数归一，不改变动作语义
=====================================================

🔒 `actions/workspace_write.py` 里**不能**出现 create/replace/append
   这类默认模式字面量（有守卫盯着），因为 Executor 不应该替 Agent
   选 mode。但 Work 层需要容忍模型常见的参数误写：

    target     → path
    mode       → write_mode
    缺 write_mode → create
    overwrite  → replace

这些归一放在 Work 适配器里，**不改** actions 叶子层，也不改
Demand 的既有行为。
"""

from __future__ import annotations

from dataclasses import replace

from actions import (
    ACTION_WORKSPACE_READ,
    ACTION_WORKSPACE_WRITE,
    WorkspaceReadExecutor,
    WorkspaceWriteExecutor,
)
from actions.intent import ActionResult, ActionIntent


class WorkWorkspaceReadExecutor:
    """workspace_read 的 Work 参数适配。"""

    action_types = frozenset({ACTION_WORKSPACE_READ})

    def __init__(self, inner: WorkspaceReadExecutor) -> None:
        self.inner = inner

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        if "path" not in params and "target" in params:
            params["path"] = params.pop("target")
        # 模型常把起始行写成 0；行号从 1 开始，直接归一，
        # 不让一次可避免的参数错误烧掉一个 turn。
        if "offset" in params:
            try:
                if int(params["offset"]) == 0:
                    params["offset"] = 1
            except (TypeError, ValueError):
                pass
        return self.inner.execute(replace(intent, parameters=params))


class WorkWorkspaceWriteExecutor:
    """workspace_write 的 Work 参数适配。"""

    action_types = frozenset({ACTION_WORKSPACE_WRITE})

    def __init__(self, inner: WorkspaceWriteExecutor) -> None:
        self.inner = inner

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        if "path" not in params and "target" in params:
            params["path"] = params.pop("target")
        if "write_mode" not in params and "mode" in params:
            params["write_mode"] = params.pop("mode")
        if not params.get("write_mode"):
            params["write_mode"] = "create"
        mode = str(params.get("write_mode") or "").strip().lower()
        if mode == "overwrite":
            params["write_mode"] = "replace"
        result = self.inner.execute(replace(intent, parameters=params))
        if (not result.success
                and (result.metadata or {}).get("code") == "not_found"
                and mode == "replace"):
            message = str(result.error or "")
            if "create" not in message:
                message += "；新文件请使用 write_mode='create'"
            return replace(result, error=message)
        return result

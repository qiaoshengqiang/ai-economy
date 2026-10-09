"""
资源结构 / 责任归属的三个执行器 —— 建目录 · 交接 · 释放
==========================================================

    ActionIntent(resource_create | resource_handoff | resource_release)
        → 对应执行器
        → 注入的 fn（由组合根提供）
        → ActionResult

【🔒 与 Workspace 内容读写的分工】

    `workspace_write` / `workspace_read`  管**内容**
    本模块三个                             管**结构与责任**

    两组都只经过 Workspace Gateway，权限 / 锁 / 审计照旧。

【🔒 边界（与其它执行器一致）】

① **只执行，不决策**
     ❌ 不替 Agent 选交给谁、不替它决定要不要建目录
     ❌ 失败不重试、不换路径、不换接收方
② **不 import `services/`** —— 只拿注入的 fn（叶子层）
③ **失败也是 ActionResult**，不是异常
④ 🔴 **发起方只取自 `intent.agent_id`**（运行时填的），
     绝不可从 `parameters` 读 —— 否则 Agent 能**替别人**交接责任。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_RESOURCE_CREATE = "resource_create"
ACTION_RESOURCE_HANDOFF = "resource_handoff"
ACTION_RESOURCE_RELEASE = "resource_release"


class _ResourceBase:
    """三个执行器共用的部分：注入 fn + 结构化失败 + 结果翻译。"""

    action_types: frozenset = frozenset()
    OUTPUT_FIELDS: tuple = ("path",)

    def __init__(self, fn: Callable[..., Any]) -> None:
        if fn is None or not callable(fn):
            raise ValueError(
                f"{type(self).__name__} 需要一个可调用的 fn（由组合根注入）")
        self.fn = fn

    # ------------------------------------------------------------
    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, path: str = "", **extra: Any) -> ActionResult:
        metadata: dict = {"code": code}
        if path:
            metadata["path"] = path
        metadata.update(extra)
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata=metadata)

    def _from_dict(self, intent: ActionIntent, raw: Any,
                   path: str) -> ActionResult:
        if not isinstance(raw, dict):
            return self._fail(intent,
                              f"fn 返回了 {type(raw).__name__}，期望 dict",
                              code="invalid_result", path=path)
        if raw.get("ok"):
            out = {k: raw[k] for k in self.OUTPUT_FIELDS if k in raw}
            return ActionResult(success=True, output=out, error=None,
                                action_id=intent.intent_id,
                                action_type=intent.action_type,
                                metadata={"code": None})
        return self._fail(intent, str(raw.get("message") or "操作失败"),
                          code=str(raw.get("code") or "unknown"),
                          path=str(raw.get("path") or path),
                          gateway_code=raw.get("gateway_code"))

    def _call(self, intent: ActionIntent, path: str, *args: Any,
              **kwargs: Any) -> ActionResult:
        try:
            raw = self.fn(*args, **kwargs)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error", path=path)
        return self._from_dict(intent, raw, path)


class ResourceCreateExecutor(_ResourceBase):
    """`resource_create` —— 在 Task Workspace 里建一个**目录**。"""

    action_types = frozenset({ACTION_RESOURCE_CREATE})
    OUTPUT_FIELDS = ("path", "type")

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "")
        return self._call(intent, path,
                          intent.agent_id, path)


class ResourceHandoffExecutor(_ResourceBase):
    """`resource_handoff` —— 把一份资源的责任**明确交给**另一个 Agent。"""

    action_types = frozenset({ACTION_RESOURCE_HANDOFF})
    OUTPUT_FIELDS = ("path", "to")

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "")
        return self._call(intent, path,
                          intent.agent_id, path, params.get("to"),
                          reason=str(params.get("reason") or ""),
                          task_id=params.get("task_id"))


class ResourceReleaseExecutor(_ResourceBase):
    """`resource_release` —— 放掉自己在那份资源上的**独立**责任。"""

    action_types = frozenset({ACTION_RESOURCE_RELEASE})
    OUTPUT_FIELDS = ("path",)

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "")
        return self._call(intent, path,
                          intent.agent_id, path)

"""
私有 Workspace 的三个执行器 —— 读 / 写 / 列
================================================

    ActionIntent(private_write | private_read | private_list)
        → 对应执行器
        → 注入的 fn（由组合根提供）
        → ActionResult

【🔴 与 Task Workspace 执行器的**结构性差异**：注入方式】

    Task Workspace：注入一个**固定**的 `gateway` + `workspace_id` ——
                    那个 workspace 在构造时就定了。

    私有 Workspace：必须**按 `intent.agent_id` 路由** ——
                    因为每个 Agent 有**自己**的空间。
                    所以注入的是一个 **resolver**：

                        workspace_of(agent_id) -> Workspace | None

    ⚠️ 这个差异是**实质的**，不是风格问题：它正对应
       「私有空间按 Agent 归属，Task 空间按 Task 归属」。

【🔒 边界（与其它执行器一致）】

① **只执行，不决策**
     ❌ 不替 Agent 挑写什么/读什么
     ❌ 失败不重试、不换路径
② **不 import `services/`** —— 只拿注入的 fn（叶子层）
③ **失败也是 ActionResult**，不是异常
④ 🔴 **`agent_id` 只取自 `intent.agent_id`**（运行时填的），
    绝不从 `parameters` 读 —— 否则 Agent 可以读写**别人**的私有空间。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from actions.intent import ActionResult, ActionIntent

ACTION_PRIVATE_WRITE = "private_write"
ACTION_PRIVATE_READ = "private_read"
ACTION_PRIVATE_LIST = "private_list"


class _PrivateBase:
    """三个执行器共用的部分：按 agent 拿到空间 + 结构化失败。"""

    def __init__(self, fn: Callable[..., Any], *,
                 workspace_of: Callable[[str], Any]) -> None:
        if fn is None or not callable(fn):
            raise ValueError(f"{type(self).__name__} 需要一个可调用的 fn")
        if workspace_of is None or not callable(workspace_of):
            raise ValueError(
                f"{type(self).__name__} 需要一个 workspace_of(agent_id) "
                f"resolver —— 私有空间是**每个 Agent 一份**的")
        self.fn = fn
        self.workspace_of = workspace_of

    def _resolve(self, intent: ActionIntent):
        """按 intent.agent_id 取那个 Agent 自己的空间。"""
        return self.workspace_of(str(getattr(intent, "agent_id", "") or ""))

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

    def _run(self, intent: ActionIntent, path: str = "") -> ActionResult:
        ws = self._resolve(intent)
        if ws is None:
            return self._fail(intent,
                              "没有这个 Agent，或它没有私有空间",
                              code="no_private_workspace", path=path)
        try:
            raw = self.fn(ws, path) if path else self.fn(ws)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error", path=path)
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
        return self._fail(intent, str(raw.get("message") or "失败"),
                          code=str(raw.get("code") or "unknown"),
                          path=str(raw.get("path") or path),
                          available=raw.get("available"))


class PrivateWriteExecutor(_PrivateBase):
    """`private_write` —— 写自己的私有文件。"""

    action_types = frozenset({ACTION_PRIVATE_WRITE})
    OUTPUT_FIELDS = ("path", "chars")

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "")
        ws = self._resolve(intent)
        if ws is None:
            return self._fail(intent, "没有这个 Agent，或它没有私有空间",
                              code="no_private_workspace", path=path)
        try:
            raw = self.fn(ws, path, params.get("content"))
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error", path=path)
        return self._finish(intent, raw, path)

    def _finish(self, intent, raw, path):
        if not isinstance(raw, dict):
            return self._fail(intent, "fn 返回的不是 dict",
                              code="invalid_result", path=path)
        if raw.get("ok"):
            out = {k: raw[k] for k in self.OUTPUT_FIELDS if k in raw}
            return ActionResult(success=True, output=out, error=None,
                                action_id=intent.intent_id,
                                action_type=intent.action_type,
                                metadata={"code": None})
        return self._fail(intent, str(raw.get("message") or "写入失败"),
                          code=str(raw.get("code") or "unknown"),
                          path=str(raw.get("path") or path))


class PrivateReadExecutor(_PrivateBase):
    """`private_read` —— 读自己的私有文件。"""

    action_types = frozenset({ACTION_PRIVATE_READ})
    OUTPUT_FIELDS = ("path", "chars", "content")

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        return self._run(intent, str(params.get("path") or ""))


class PrivateListExecutor(_PrivateBase):
    """`private_list` —— 列出自己私有空间里的文件。"""

    action_types = frozenset({ACTION_PRIVATE_LIST})
    OUTPUT_FIELDS = ("paths", "count")

    def execute(self, intent: ActionIntent) -> ActionResult:
        return self._run(intent)

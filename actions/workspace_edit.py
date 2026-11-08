"""
WorkspaceEditExecutor —— 用 old_string → new_string 局部修改
==============================================================

    ActionIntent(workspace_edit)
        → WorkspaceEditExecutor
        → 注入的 edit_fn（services.artifacts.edit_artifact）
        → ActionResult

【🔒 边界】

① 只执行，不决策：不替 Agent 找 old_string、不做模糊匹配、不重试
② 不 import services/core：只拿注入的 edit_fn（actions/ 是叶子层）
③ 修改仍走 Workspace Gateway（锁 / revision / audit 不变）
④ 失败也是 ActionResult，不是异常

【V1 语义】

- old_string 必须**唯一命中**；0 个或多个都失败；
- replace_all=true 才允许多个；
- 严格字符串匹配，不做正则 / 大小写 / 空白归一化。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_WORKSPACE_EDIT = "workspace_edit"

_OUTPUT_FIELDS = (
    "path", "mode", "revision", "previous_revision",
    "previous_chars", "chars", "matched", "replaced",
)


class WorkspaceEditExecutor:
    action_types = frozenset({ACTION_WORKSPACE_EDIT})

    def __init__(self, edit_fn: Callable[..., Any]) -> None:
        if edit_fn is None or not callable(edit_fn):
            raise ValueError(
                "WorkspaceEditExecutor 需要一个可调用的 edit_fn —— "
                "它由组合根注入（例如 services.artifacts.edit_artifact）")
        self.edit_fn = edit_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "")
        old_string = params.get("old_string")
        new_string = params.get("new_string")
        replace_all = bool(params.get("replace_all", False))
        expected_revision = params.get("expected_revision")

        if not isinstance(old_string, str) or not old_string:
            return self._fail(
                intent, "old_string 必须是非空字符串",
                code="bad_argument", path=path)
        if not isinstance(new_string, str):
            return self._fail(
                intent, "new_string 必须是字符串",
                code="bad_argument", path=path)

        try:
            raw = self.edit_fn(
                intent.agent_id, path, old_string, new_string,
                replace_all=replace_all,
                expected_revision=expected_revision,
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error", path=path)

        if not isinstance(raw, dict):
            return self._fail(
                intent,
                f"edit_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_edit_result", path=path)
        if raw.get("ok"):
            output = {k: raw[k] for k in _OUTPUT_FIELDS if k in raw}
            return ActionResult(
                success=True, output=output, error=None,
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": None})
        return self._fail(
            intent, str(raw.get("message") or "修改失败"),
            code=str(raw.get("code") or "unknown"),
            path=path, **{k: raw[k] for k in ("matched", "revision")
                          if k in raw})

    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, **extra: Any) -> ActionResult:
        metadata = {"code": code}
        metadata.update(extra)
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata=metadata)

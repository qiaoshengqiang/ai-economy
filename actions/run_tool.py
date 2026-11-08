"""
RunToolExecutor —— 把受控工具执行接进 Action Seam
====================================================

    ActionIntent(run_tool) → RunToolExecutor
        → 注入的 run_fn（由组合根提供）
        → ToolRunner / 公共工具库
        → ActionResult

【🔒 边界】

① 只执行，不决策
   ❌ 不替 Agent 选工具 / 不补参数 / 不重试
② 不 import `tool_runtime/` / `services/` / `subprocess`
   `actions/` 是叶子层；执行能力由组合根注入
③ 失败也是 ActionResult
④ `agent_id` 只取自 intent（运行时填的）

【🟡 语义】

    `success=True` 表示**工具真的跑起来了**；
    测试是否通过看 `output["ok"]` / `output["exit_code"]`。
    这样 pytest 失败也能把 stdout/stderr 回喂给 Agent，
    而不是把「测试失败」误当成「动作没执行」。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from actions.intent import ActionResult, ActionIntent

ACTION_RUN_TOOL = "run_tool"


class RunToolExecutor:
    """执行 `action_type = "run_tool"` 的 ActionIntent。"""

    action_types = frozenset({ACTION_RUN_TOOL})

    def __init__(self, run_fn: Callable[..., Any]) -> None:
        if run_fn is None or not callable(run_fn):
            raise ValueError("RunToolExecutor 需要一个可调用的 run_fn")
        self.run_fn = run_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params: Dict[str, Any] = dict(getattr(intent, "parameters", None) or {})
        tool = str(params.get("tool") or "").strip()
        args = params.get("args")
        source = str(params.get("source") or "task")
        path = str(params.get("path") or "")
        timeout_seconds = params.get("timeout_seconds", 60)

        if not tool:
            return self._fail(intent, "tool 不能为空", code="bad_tool")
        if args is not None and not isinstance(args, (list, tuple)):
            return self._fail(intent, "args 必须是字符串列表", code="bad_args")
        if isinstance(args, (list, tuple)):
            args = list(args)

        try:
            raw = self.run_fn(
                intent.agent_id,
                tool=tool,
                args=args,
                source=source,
                path=path,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error")

        if not isinstance(raw, dict):
            return self._fail(
                intent, f"run_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_result")

        if raw.get("ran") is True:
            code = raw.get("code") or (
                None if raw.get("ok") else "tool_exit_nonzero")
            return ActionResult(
                success=True,
                output=raw,
                error=None,
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": code},
            )

        return self._fail(
            intent,
            str(raw.get("message") or "工具没有运行"),
            code=str(raw.get("code") or "tool_not_run"),
            tool=tool, source=source, path=path)

    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, **extra: Any) -> ActionResult:
        metadata: Dict[str, Any] = {"code": code}
        metadata.update(extra)
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata=metadata)

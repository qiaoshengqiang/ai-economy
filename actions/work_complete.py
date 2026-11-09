"""
WorkCompleteExecutor —— Work 层「我完成了」
=============================================

Solo Agent 用这个动作结束当前 Iteration：

    work_complete(result_ref, summary, ...)
        → 注入的 complete_fn
        → WorkLifecycle.complete_solo()
        → SUBMIT / VERIFY / COMPLETE（按当前状态补齐）

🔒 这不是 Life 层的消息。Work 层不发送 `COMPLETE` 消息；
   它直接调用 Task 生命周期。

🔒 Executor 仍然只执行、不决策，也不 import services/core。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_WORK_COMPLETE = "work_complete"

_OUTPUT_FIELDS = ("status", "iteration", "already_completed", "summary")


class WorkCompleteExecutor:
    """执行 `action_type = "work_complete"`。"""

    action_types = frozenset({ACTION_WORK_COMPLETE})

    def __init__(self, complete_fn: Callable[..., Any]) -> None:
        if complete_fn is None or not callable(complete_fn):
            raise ValueError(
                "WorkCompleteExecutor 需要一个可调用的 complete_fn —— "
                "它由 Work runtime 注入")
        self.complete_fn = complete_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        result_ref = str(params.get("result_ref") or "").strip()
        if not result_ref:
            return self._fail(
                intent,
                "work_complete 需要 result_ref（成果的逻辑名，不是路径/URL）",
                code="missing_result_ref")

        try:
            raw = self.complete_fn(
                actor=intent.agent_id,
                result_ref=result_ref,
                summary=str(params.get("summary") or ""),
                result_type=str(params.get("result_type") or "artifact"),
                metadata=params.get("metadata") or {},
                feedback=str(params.get("feedback") or ""),
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="complete_failed")

        if not isinstance(raw, dict) or not raw.get("ok"):
            message = (
                str(raw.get("message")) if isinstance(raw, dict) else
                f"complete_fn 返回了 {type(raw).__name__}")
            return self._fail(
                intent, message or "完成失败",
                code=str((raw or {}).get("code") or "complete_rejected")
                if isinstance(raw, dict) else "invalid_result")

        out = {k: raw[k] for k in _OUTPUT_FIELDS if k in raw}
        return ActionResult(
            success=True, output=out, error=None,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None})

    def _fail(self, intent: ActionIntent, message: str, *,
              code: str) -> ActionResult:
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": code})

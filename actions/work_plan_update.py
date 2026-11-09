"""
WorkPlanUpdateExecutor —— Leader 运行中重新计划
================================================

Leader 在两轮执行之间调用：

    work_plan_update(
        expected_revision=1,
        operations=[...],
        qa=...,
        reason="...",
    )

🔒 只有 Leader 能调用；Executor 只把参数交给注入的 fn，
   不自己决定改什么、也不改 Task 状态。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_WORK_PLAN_UPDATE = "work_plan_update"

_OUTPUT_FIELDS = ("revision", "previous_revision", "applied", "summary")


class WorkPlanUpdateExecutor:
    action_types = frozenset({ACTION_WORK_PLAN_UPDATE})

    def __init__(self, update_fn: Callable[..., Any]) -> None:
        if update_fn is None or not callable(update_fn):
            raise ValueError(
                "WorkPlanUpdateExecutor 需要可调用的 update_fn（由 runtime 注入）")
        self.update_fn = update_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        expected = params.get("expected_revision")
        if not isinstance(expected, int) or isinstance(expected, bool):
            return self._fail(
                intent, "work_plan_update 需要整数 expected_revision",
                code="bad_expected_revision")
        operations = params.get("operations")
        if not isinstance(operations, list) or not operations:
            return self._fail(
                intent, "work_plan_update 需要非空 operations 列表",
                code="bad_operations")
        try:
            raw = self.update_fn(
                agent_id=intent.agent_id,
                expected_revision=expected,
                operations=operations,
                qa=params.get("qa"),
                qa_provided="qa" in params,
                reason=str(params.get("reason") or ""),
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict) or not raw.get("ok"):
            return self._fail(
                intent,
                str((raw or {}).get("message") or "计划更新被拒绝")
                if isinstance(raw, dict) else "计划更新返回无效",
                code=str((raw or {}).get("code") or "plan_update_rejected")
                if isinstance(raw, dict) else "invalid_result")
        output = {
            key: raw[key] for key in _OUTPUT_FIELDS if key in raw
        }
        return ActionResult(
            success=True, output=output, error=None,
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

"""
WorkspacePlanCheckpointExecutor —— 记录阶段检查结果
=====================================================

    ActionIntent(workspace_plan_checkpoint)
        → 注入的 record_fn（由 Demand 组合根提供）
        → ActionResult

【为什么需要它】

`workspace_plan.phases` 可以声明开发顺序与依赖，但系统**不应该**硬编码
“每阶段跑 DOM/import + smoke”——那太针对 Web 任务。

本动作只做通用的一件事：

    Agent 用什么方式检查（run_tool / workspace_read / 人工检查都可以），
    然后把“这个 phase 的检查是否通过 + 证据/备注”记录到 run。

🔒 它记录的是 **Agent 的阶段检查结果**，不是 Task 验收，也不是
   Shared Context 结论；系统不替 Agent 判断检查是否充分。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_WORKSPACE_PLAN_CHECKPOINT = "workspace_plan_checkpoint"


class WorkspacePlanCheckpointExecutor:
    action_types = frozenset({ACTION_WORKSPACE_PLAN_CHECKPOINT})

    _ALLOWED_PARAMS = frozenset({
        "phase", "passed", "evidence", "note",
    })

    def __init__(self, record_fn: Callable[..., Any]) -> None:
        if record_fn is None or not callable(record_fn):
            raise ValueError(
                "WorkspacePlanCheckpointExecutor 需要一个可调用的 record_fn "
                "—— 它由组合根注入")
        self.record_fn = record_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        unknown = sorted(set(params) - self._ALLOWED_PARAMS)
        if unknown:
            return self._fail(
                intent,
                f"workspace_plan_checkpoint 不认识的参数：{unknown}；"
                "只支持 phase / passed / evidence / note",
                code="unknown_parameters", unknown=unknown)
        phase = str(params.get("phase") or "").strip()
        if not phase:
            return self._fail(
                intent, "workspace_plan_checkpoint 需要 phase",
                code="missing_phase")
        passed = params.get("passed")
        if not isinstance(passed, bool):
            return self._fail(
                intent, "workspace_plan_checkpoint 的 passed 必须是布尔值",
                code="invalid_passed")
        evidence = params.get("evidence")
        if evidence is not None and not isinstance(evidence, dict):
            return self._fail(
                intent, "evidence 必须是对象或省略",
                code="invalid_evidence")
        try:
            raw = self.record_fn(
                intent.agent_id,
                phase=phase,
                passed=passed,
                evidence=evidence,
                note=str(params.get("note") or ""),
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error")
        if not isinstance(raw, dict):
            return self._fail(
                intent,
                f"record_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_result")
        if not raw.get("ok"):
            return self._fail(
                intent,
                str(raw.get("message") or "阶段检查记录失败"),
                code=str(raw.get("code") or "unknown"))
        return ActionResult(
            success=True,
            output={"checkpoint": raw.get("checkpoint")},
            error=None,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )

    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, **extra: Any) -> ActionResult:
        metadata = {"code": code}
        metadata.update(extra)
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata=metadata)

"""
WorkspacePlanUpdateExecutor —— Creator 更新交付计划
====================================================

    ActionIntent(workspace_plan_update)
        → 注入的 update_fn（由 Demand 组合根提供）
        → ActionResult

【为什么需要它】

真实 run `demand_cc61c0b5`：Creator 把交付从“index.html + src/*”缩减为
“index.html + README.md”，但 `run.workspace_plan` 没有更新。于是
“计划完成度”每轮仍显示 `src/* ❌ missing`，和 Creator 的新指令冲突。

🔒 计划是 Creator 的责任计划，不是 Task Core 状态，也不是工作记忆。
   本执行器只负责把 Creator 的显式计划变更交给组合根校验/持久化。

【边界】

① 只执行，不决策：不替 Agent 改 plan、不推断 scope；
② 不 import services/：只拿注入的 update_fn（actions/ 是叶子层）；
③ 失败也是 ActionResult，不是异常。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_WORKSPACE_PLAN_UPDATE = "workspace_plan_update"


class WorkspacePlanUpdateExecutor:
    action_types = frozenset({ACTION_WORKSPACE_PLAN_UPDATE})

    _ALLOWED_PARAMS = frozenset({"plan", "reason"})

    def __init__(self, update_fn: Callable[..., Any]) -> None:
        if update_fn is None or not callable(update_fn):
            raise ValueError(
                "WorkspacePlanUpdateExecutor 需要一个可调用的 update_fn "
                "—— 它由组合根注入")
        self.update_fn = update_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        unknown = sorted(set(params) - self._ALLOWED_PARAMS)
        if unknown:
            return self._fail(
                intent,
                f"workspace_plan_update 不认识的参数：{unknown}；"
                "只支持 plan / reason",
                code="unknown_parameters", unknown=unknown)
        plan = params.get("plan")
        if not isinstance(plan, dict):
            return self._fail(
                intent,
                "workspace_plan_update 需要 plan 对象："
                "{root_owner, directories, files}",
                code="missing_plan")
        try:
            raw = self.update_fn(
                intent.agent_id,
                plan=plan,
                reason=str(params.get("reason") or ""),
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error")
        if not isinstance(raw, dict):
            return self._fail(
                intent,
                f"update_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_result")
        if not raw.get("ok"):
            return self._fail(
                intent,
                str(raw.get("message") or "计划更新失败"),
                code=str(raw.get("code") or "unknown"))
        return ActionResult(
            success=True,
            output={"workspace_plan": raw.get("workspace_plan")},
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

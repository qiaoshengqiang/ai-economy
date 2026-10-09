"""
指派验收责任的执行器 —— `assign_acceptance`
=============================================

    ActionIntent(assign_acceptance)
        → AssignAcceptanceExecutor
        → 注入的 fn（由组合根绑定 alignment + task_id）
        → ActionResult

【🔴 它补的是哪一个缺口】

`task_execution_service.py` 自己写着「谁负责验收由 **Agent 自己**
通过现有的 **ASSIGN** 能力决定」—— 但 ASSIGN **没有消息通道**，
于是真实实验里只能由 runner 代做。
没有它，Agent 能发 SUBMIT，却**没有人能被授权 VERIFY**
（`acceptors_of()` 返回空集，fail-closed）。

🔒 所以这是**把设计已声明的能力接上**，与 `private_*` 同一类。

【🔒 边界（与其它执行器一致）】

① **只执行，不决策** —— 不替 Agent 选谁验收、不重试、不换人
② **不 import `services/` `core/`** —— 只拿注入的 fn（叶子层）
③ **失败也是 ActionResult**，不是异常
④ 🔴 **发起方只取自 `intent.agent_id`**（运行时填的），
     绝不可从 `parameters` 读 —— 否则 Agent 能**冒充别人**去指派。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

#: 这个执行器认领的 action_type。
ACTION_ASSIGN_ACCEPTANCE = "assign_acceptance"

_OUTPUT_FIELDS = ("task_id", "to", "responsibility", "change_id")


class AssignAcceptanceExecutor:
    """
    执行 `action_type = "assign_acceptance"` 的 ActionIntent。

    `parameters`：
        {"to": "接收方的 agent_id", "reason": "可选"}

    🔒 `alignment` 与 `task_id` **由组合根绑定** —— Agent 只说要给谁，
       它既看不到 alignment 服务，也无法指定别的 task。
    """

    action_types = frozenset({ACTION_ASSIGN_ACCEPTANCE})

    def __init__(self, assign_fn: Callable[..., Any]) -> None:
        if assign_fn is None or not callable(assign_fn):
            raise ValueError(
                "AssignAcceptanceExecutor 需要一个可调用的 assign_fn —— "
                "它由组合根注入（例如 assignment_ops.assign_acceptance）")
        self.assign_fn = assign_fn

    # ------------------------------------------------------------
    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        to = str(params.get("to") or "")
        try:
            # 🔴 发起方**只**来自 intent
            raw = self.assign_fn(by=intent.agent_id, to=to,
                                 reason=str(params.get("reason") or ""))
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error", to=to)

        if not isinstance(raw, dict):
            return self._fail(intent,
                              f"assign_fn 返回了 {type(raw).__name__}，期望 dict",
                              code="invalid_result", to=to)
        if raw.get("ok"):
            out = {k: raw[k] for k in _OUTPUT_FIELDS if k in raw}
            return ActionResult(
                success=True, output=out, error=None,
                action_id=intent.intent_id, action_type=intent.action_type,
                metadata={"code": None})
        return self._fail(intent, str(raw.get("message") or "指派失败"),
                          code=str(raw.get("code") or "unknown"),
                          to=str(raw.get("to") or to))

    # ------------------------------------------------------------
    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, to: str) -> ActionResult:
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": code, "to": to})

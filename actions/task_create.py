"""
TaskCreateExecutor —— Agent 自主创建 Root Task 的动作
========================================================

    ActionIntent(task_create)
        → TaskCreateExecutor
        → 注入的 create_fn（由组合根绑定 DemandRun / Runtime）
        → ActionResult

【🔒 边界】

① **只执行，不决策** —— 不替 Agent 选参与者、不补 description、不重试
② **不 import services/core** —— 只拿注入的 create_fn（actions/ 是叶子层）
③ **creator 只取自 `intent.agent_id`** —— 不能让 Agent 冒充别人建 Task
④ 失败也是 ActionResult，不是异常

【V1 范围】

- 只创建 Root Task；`parent_task_id` 留给后续 Child Task。
- participants 由创建者**直接指定**（V1 TEMPORARY）；
  显式邀请/接受是 V1.1。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_TASK_CREATE = "task_create"

_OUTPUT_FIELDS = (
    "task_id", "workspace_id", "description", "participants",
    "producer", "acceptance_assignee", "status",
)


class TaskCreateExecutor:
    """执行 `action_type = "task_create"` 的 ActionIntent。"""

    action_types = frozenset({ACTION_TASK_CREATE})

    def __init__(self, create_fn: Callable[..., Any]) -> None:
        if create_fn is None or not callable(create_fn):
            raise ValueError(
                "TaskCreateExecutor 需要一个可调用的 create_fn —— "
                "它由组合根注入")
        self.create_fn = create_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        try:
            raw = self.create_fn(
                creator=intent.agent_id,
                description=params.get("description"),
                participants=params.get("participants"),
                acceptance_criteria=params.get("acceptance_criteria"),
                producer=params.get("producer"),
                acceptance_assignee=params.get("acceptance_assignee"),
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")

        if not isinstance(raw, dict):
            return self._fail(
                intent,
                f"create_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_result")
        if raw.get("ok"):
            out = {k: raw[k] for k in _OUTPUT_FIELDS if k in raw}
            return ActionResult(
                success=True, output=out, error=None,
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": None})
        return self._fail(
            intent,
            str(raw.get("message") or "创建任务失败"),
            code=str(raw.get("code") or "unknown"),
            task_id=raw.get("task_id"))

    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, **extra: Any) -> ActionResult:
        metadata = {"code": code}
        metadata.update(extra)
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata=metadata)

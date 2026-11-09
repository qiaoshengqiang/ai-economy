"""
Work Item Actions —— 团队成员标记自己的分工状态
=================================================

    work_item_done     我负责的 item 完成了
    work_item_blocked  我负责的 item 被阻塞了（没有消息机制）
    work_qa_verdict    QA 对整体交付给出结论

🔒 这些动作**不改变 Task 状态**：

    · item DONE 只表示“这个分工完成”，不是 Task COMPLETED；
    · 只有 Leader 的 `work_complete` 才能结束 Task；
    · QA verdict 只是 Work runtime 的输入，不是 Task Core 的验收记录。

真正的 Task 生命周期仍由 `WorkLifecycle` 写入 `task_executions`。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_WORK_ITEM_DONE = "work_item_done"
ACTION_WORK_ITEM_BLOCKED = "work_item_blocked"
ACTION_WORK_QA_VERDICT = "work_qa_verdict"

_OUTPUT_FIELDS = (
    "item_id", "status", "summary", "paths", "passed", "feedback",
)


class _Base:
    action_types: frozenset = frozenset()
    _allowed: frozenset = frozenset()

    def __init__(self, fn: Callable[..., Any]) -> None:
        if fn is None or not callable(fn):
            raise ValueError(
                f"{type(self).__name__} 需要可调用的 fn（由 Work runtime 注入）")
        self.fn = fn

    def _params(self, intent: ActionIntent):
        params = dict(getattr(intent, "parameters", None) or {})
        unknown = sorted(set(params) - set(self._allowed))
        if unknown:
            return params, self._fail(
                intent,
                f"{intent.action_type} 不认识的参数：{unknown}；"
                f"只支持 {sorted(self._allowed)}",
                code="unknown_parameters")
        return params, None

    def _ok(self, intent: ActionIntent, raw: Any) -> ActionResult:
        output = {
            key: raw[key] for key in _OUTPUT_FIELDS
            if isinstance(raw, dict) and key in raw
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


class WorkItemDoneExecutor(_Base):
    action_types = frozenset({ACTION_WORK_ITEM_DONE})
    _allowed = frozenset({"item_id", "summary", "paths"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        item_id = str(params.get("item_id") or "").strip()
        if not item_id:
            return self._fail(intent, "work_item_done 需要 item_id",
                              code="missing_item_id")
        try:
            raw = self.fn(
                agent_id=intent.agent_id,
                item_id=item_id,
                summary=str(params.get("summary") or ""),
                paths=params.get("paths") or [],
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict) or not raw.get("ok"):
            return self._fail(
                intent,
                str((raw or {}).get("message") or "work_item_done 被拒绝")
                if isinstance(raw, dict) else "work_item_done 返回无效",
                code=str((raw or {}).get("code") or "item_done_rejected")
                if isinstance(raw, dict) else "invalid_result")
        return self._ok(intent, raw)


class WorkItemBlockedExecutor(_Base):
    action_types = frozenset({ACTION_WORK_ITEM_BLOCKED})
    _allowed = frozenset({"item_id", "reason"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        item_id = str(params.get("item_id") or "").strip()
        reason = str(params.get("reason") or "").strip()
        if not item_id:
            return self._fail(intent, "work_item_blocked 需要 item_id",
                              code="missing_item_id")
        if not reason:
            return self._fail(intent, "work_item_blocked 需要 reason",
                              code="missing_reason")
        try:
            raw = self.fn(
                agent_id=intent.agent_id,
                item_id=item_id,
                reason=reason,
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict) or not raw.get("ok"):
            return self._fail(
                intent,
                str((raw or {}).get("message") or "work_item_blocked 被拒绝")
                if isinstance(raw, dict) else "work_item_blocked 返回无效",
                code=str((raw or {}).get("code") or "item_blocked_rejected")
                if isinstance(raw, dict) else "invalid_result")
        return self._ok(intent, raw)


class WorkQaVerdictExecutor(_Base):
    action_types = frozenset({ACTION_WORK_QA_VERDICT})
    _allowed = frozenset({"passed", "feedback"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        passed = params.get("passed")
        if not isinstance(passed, bool):
            return self._fail(intent, "work_qa_verdict.passed 必须是布尔值",
                              code="bad_passed")
        feedback = str(params.get("feedback") or "").strip()
        if not passed and not feedback:
            return self._fail(
                intent, "QA 不通过时必须给出 feedback",
                code="missing_feedback")
        try:
            raw = self.fn(
                agent_id=intent.agent_id,
                passed=passed,
                feedback=feedback,
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict) or not raw.get("ok"):
            return self._fail(
                intent,
                str((raw or {}).get("message") or "work_qa_verdict 被拒绝")
                if isinstance(raw, dict) else "work_qa_verdict 返回无效",
                code=str((raw or {}).get("code") or "qa_verdict_rejected")
                if isinstance(raw, dict) else "invalid_result")
        return self._ok(intent, raw)

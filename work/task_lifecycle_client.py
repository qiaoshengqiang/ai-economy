"""Task 生命周期的消息客户端。

Phase 3：

    START / SUBMIT / VERIFY / COMPLETE
      → CommunicationManager（写 Journal）
      → TaskService handler（Task Core 状态变更）

同步控制面用 `run_coro` 发送消息；handler 直接调用 TaskService，
不依赖邮箱消费循环。消息 ID 同时进入 `task_executions`，所以
生命周期记录可以回溯到真实 Message。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from communication import MessageType
from work.comm_bridge import run_coro

logger = logging.getLogger(__name__)


class TaskLifecycleError(Exception):
    """生命周期消息发送或 Task handler 处理失败。"""


class TaskLifecycleClient:
    def __init__(
        self,
        communication: Any,
        task_service: Any,
        *,
        service_agent: str = "task_service",
    ) -> None:
        self.communication = communication
        self.task_service = task_service
        self.service_agent = str(service_agent or "task_service")

    def _send_and_apply(
        self,
        message_type: str,
        handler_name: str,
        *,
        actor: str,
        task_id: str,
        payload: Dict[str, Any],
    ) -> Any:
        if self.communication is None or self.task_service is None:
            raise TaskLifecycleError("Task 生命周期消息未接线")

        try:
            message = run_coro(self.communication.send(
                actor,
                self.service_agent,
                message_type=message_type,
                payload={"task_id": task_id, **payload},
                trace_id=str(task_id),
                conversation_id=f"conv_task_{task_id}",
            ))
        except Exception as exc:                              # noqa: BLE001
            raise TaskLifecycleError(
                f"{message_type} 消息发送失败：{exc}") from exc

        handler = getattr(self.task_service, handler_name, None)
        if not callable(handler):
            raise TaskLifecycleError(
                f"TaskService 没有 handler：{handler_name}")
        result = handler(message)
        if not getattr(result, "ok", False):
            errors = list(getattr(result, "errors", ()) or ())
            raise TaskLifecycleError(
                "; ".join(str(item) for item in errors)
                or f"{message_type} 被 Task Core 拒绝")
        recorder = getattr(self.communication, "recorder", None)
        if recorder is not None:
            try:
                recorder.mark_processed(message, self.service_agent)
            except Exception:                                 # noqa: BLE001
                logger.debug("标记生命周期消息已处理失败", exc_info=True)
        return message

    def start(self, task_id: str, actor: str) -> Any:
        return self._send_and_apply(
            MessageType.START, "handle_start",
            actor=actor, task_id=task_id, payload={})

    def submit(self, task_id: str, actor: str, result: Any) -> Any:
        return self._send_and_apply(
            MessageType.SUBMIT, "handle_submit",
            actor=actor, task_id=task_id,
            payload={"result": result.to_dict()})

    def verify(
        self, task_id: str, actor: str, *,
        passed: bool = True, feedback: str = "",
    ) -> Any:
        return self._send_and_apply(
            MessageType.VERIFY, "handle_verify",
            actor=actor, task_id=task_id,
            payload={"passed": bool(passed), "feedback": str(feedback)})

    def complete(self, task_id: str, actor: str, summary: str = "") -> Any:
        return self._send_and_apply(
            MessageType.COMPLETE, "handle_complete",
            actor=actor, task_id=task_id,
            payload={"summary": str(summary or "")})

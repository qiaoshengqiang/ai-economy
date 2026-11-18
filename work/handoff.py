"""Front → Worker 的 handoff 消息。

Phase 2 只做消息通道：

    Front --TASK_OFFER--> Worker
    Worker --ACCEPT/REJECT--> Front

Task 仍由 Worker 侧通过 Task Core 创建；消息不承载 Task 状态。
Pi 的 inbox/outbox 属于 Phase 4；当前 Worker 侧 handoff 由 Runtime
适配器代表执行。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from communication import MessageType
from work.comm_bridge import run_coro

logger = logging.getLogger(__name__)

#: handoff 消息的 payload.kind。它们不是 TaskCore 的 TASK_OFFER/ACCEPT/REJECT；
#: Task 由 Worker 侧创建，Front 在 offer 时没有 task_id。
HANDOFF_MESSAGE_KINDS = frozenset({
    "handoff_offer",
    "handoff_accept",
    "handoff_reject",
})


def is_handoff_message(message: Any) -> bool:
    """这条消息是不是 Front → Worker 的 handoff 通知，而不是 TaskCore 协议？"""
    payload = getattr(message, "payload", None) or {}
    if not isinstance(payload, dict):
        return False
    return str(payload.get("kind") or "") in HANDOFF_MESSAGE_KINDS


class HandoffRuntime:
    """一次 Front → Worker handoff 的消息运行时。"""

    def __init__(
        self,
        communication: Any,
        *,
        front_agent: str,
        worker: str,
        job: Any,
    ) -> None:
        self.communication = communication
        self.front_agent = str(front_agent)
        self.worker = str(worker)
        self.job = job
        self.conversation_id = f"conv_handoff_{job.job_id}"
        self.trace_id = str(job.job_id)
        self.offer_message = None

    def _send(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str,
        payload: Dict[str, Any],
        *,
        reply_to: str = "",
    ) -> Any:
        if self.communication is None:
            return None
        try:
            return run_coro(self.communication.send(
                from_agent,
                to_agent,
                message_type=message_type,
                payload=payload,
                reply_to=reply_to or None,
                trace_id=self.trace_id,
                conversation_id=self.conversation_id,
            ))
        except Exception as exc:                              # noqa: BLE001
            logger.warning("handoff 消息发送失败：%s", exc)
            return None

    def offer(self, decision: Any) -> Any:
        brief = getattr(decision, "brief", None)
        task_text = str(
            getattr(brief, "task_text", "")
            or getattr(brief, "human_text", "")
            or getattr(brief, "goal", "")
        ).strip()
        self.offer_message = self._send(
            self.front_agent,
            self.worker,
            MessageType.TASK_OFFER,
            {
                "kind": "handoff_offer",
                "job_id": str(self.job.job_id),
                "task_text": task_text,
                "role": str(getattr(decision, "role", "") or ""),
                "work_profile": str(
                    getattr(decision, "work_profile", "") or ""),
                "workdir": str(getattr(self.job, "workdir", "") or ""),
            },
        )
        return self.offer_message

    def accept(self, task_id: str) -> Any:
        if self.offer_message is None:
            return None
        offer_id = (
            str(getattr(self.offer_message, "message_id", "") or ""))
        return self._send(
            self.worker,
            self.front_agent,
            MessageType.ACCEPT,
            {
                "kind": "handoff_accept",
                "job_id": str(self.job.job_id),
                "task_id": str(task_id or ""),
                "offer_message_id": offer_id,
            },
            reply_to=offer_id,
        )

    def reject(self, error: str) -> Any:
        if self.offer_message is None:
            return None
        offer_id = (
            str(getattr(self.offer_message, "message_id", "") or ""))
        return self._send(
            self.worker,
            self.front_agent,
            MessageType.REJECT,
            {
                "kind": "handoff_reject",
                "job_id": str(self.job.job_id),
                "offer_message_id": offer_id,
                "error": str(error or ""),
            },
            reply_to=offer_id,
        )

    def negotiate(
        self,
        decision: Any,
        create_task: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """发送 offer → 由 Worker 侧创建 Task → 发送 accept/reject。"""
        self.offer(decision)
        try:
            created = create_task()
        except Exception as exc:                              # noqa: BLE001
            created = {
                "ok": False,
                "code": "task_create_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
        if created.get("ok"):
            self.accept(str(created.get("task_id") or ""))
        else:
            self.reject(
                str(created.get("message") or "Task 创建失败"))
        return created

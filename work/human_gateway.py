"""Human Gateway —— UI 人类消息的通信端点。

它**不是 Agent**：

- 不进入 Agent Directory / Availability；
- 不持有钱包；
- 不创建 Task；
- 只把 UI 的人类输入转成 Message，把 Front 回复写回 Journal。

Phase 1 只做本机单 Human；多用户 / 跨机器后续再扩展。
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict, Optional

from communication import Message, MessageType


class HumanGateway:
    """一个只负责收发人类消息的技术端点。"""

    def __init__(
        self,
        communication: Any,
        *,
        human_id: str = "local",
        agent_id: str = "human_gateway",
    ) -> None:
        self.communication = communication
        self.human_id = str(human_id or "local").strip() or "local"
        self.agent_id = str(agent_id or "human_gateway").strip()
        self.name = "Human Gateway"
        self._lock = threading.RLock()
        self._registered = False

    def handle_message(self, message: Message) -> None:
        """通信层要求本地端点有处理函数；UI 从 Journal 读取，不在这里消费。"""
        return None

    @staticmethod
    def _run(coro: Any) -> Any:
        """在同步 UI 线程里执行 CommunicationManager 的协程。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        # 调用方本身在事件循环里：换一个线程 + 新 loop 执行，避免嵌套。
        result = []
        error = []

        def _target() -> None:
            try:
                result.append(asyncio.run(coro))
            except Exception as exc:                          # noqa: BLE001
                error.append(exc)

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0] if result else None

    def ensure_registered(self) -> None:
        with self._lock:
            if self._registered:
                return
            if not self.communication.registry.exists(self.agent_id):
                self.communication.register(
                    self,
                    capabilities=(),
                    metadata={"actor_kind": "human_gateway"},
                )
            self._registered = True

    def conversation_id(self, front_agent: str) -> str:
        return f"conv_{self.human_id}_{str(front_agent).strip()}"

    def send_human_message(
        self,
        text: str,
        *,
        to_agent: str,
        job_id: str = "",
        conversation_id: str = "",
    ) -> str:
        """Human → Front；返回 message_id。"""
        self.ensure_registered()
        with self._lock:
            message = self._run(self.communication.send(
                self.agent_id,
                to_agent,
                text=str(text),
                message_type=MessageType.TEXT,
                payload={
                    "kind": "human_message",
                    "human_id": self.human_id,
                    "job_id": str(job_id or ""),
                },
                trace_id=str(job_id or ""),
                conversation_id=(
                    conversation_id
                    or self.conversation_id(to_agent)),
            ))
        return message.message_id

    def send_front_reply(
        self,
        text: str,
        *,
        front_agent: str,
        job_id: str = "",
        task_id: str = "",
        reply_to: str = "",
        conversation_id: str = "",
    ) -> str:
        """Front → Human；返回 message_id。"""
        self.ensure_registered()
        with self._lock:
            message = self._run(self.communication.send(
                front_agent,
                self.agent_id,
                text=str(text),
                message_type=MessageType.TEXT,
                payload={
                    "kind": "front_reply",
                    "human_id": self.human_id,
                    "job_id": str(job_id or ""),
                    "task_id": str(task_id or ""),
                },
                reply_to=str(reply_to or "") or None,
                trace_id=str(job_id or ""),
                conversation_id=(
                    conversation_id
                    or self.conversation_id(front_agent)),
            ))
        return message.message_id

    def journal_messages(
        self,
        *,
        front_agent: str,
        limit: int = 200,
    ) -> list[Dict[str, Any]]:
        """从 CommunicationRecorder 读取 Human/Front 对话。"""
        recorder = getattr(self.communication, "recorder", None)
        if recorder is None:
            return []
        rows = recorder.conversation(
            self.conversation_id(front_agent))[-max(1, int(limit)):]
        out: list[Dict[str, Any]] = []
        for row in rows:
            payload = row.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
            if not isinstance(payload, dict):
                continue
            kind = str(payload.get("kind") or "")
            if kind == "human_message":
                role = "human"
            elif kind == "front_reply":
                role = "front"
            else:
                continue
            out.append({
                "role": role,
                "text": str(payload.get("text") or ""),
                "job_id": str(payload.get("job_id") or ""),
                "task_id": str(payload.get("task_id") or ""),
                "message_id": str(row.get("message_id") or ""),
                "at": str(row.get("created_at") or ""),
                "human_id": str(payload.get("human_id") or ""),
            })
        return out

"""Life 层 Inbox —— 空闲 Agent 的可选自动回复。

Phase 5 V1 只做一件事：

    reply_mode = auto 的空闲 Agent
      → 读取 pending 的 agent_message
      → 用 Brain 生成一段简短回复
      → 通过 CommunicationManager 发回
      → mark_processed_id

🔒 默认 reply_mode = never；不会自动回复。
🔒 只处理 payload.kind = "agent_message"，不处理 Human / Task / handoff。
🔒 只在 Agent IDLE 时处理；WORKING 时消息留在 pending。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

from communication import MessageType
from communication.inbox import InboxPolicy
from core.identity import format_agent_identity, format_agent_label
from work.comm_bridge import run_coro

logger = logging.getLogger(__name__)


class LifeInboxService:
    """空闲 Agent 的生活层收件箱处理器。"""

    def __init__(
        self,
        runtime: Any,
        availability: Any,
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self.rt = runtime
        self.availability = availability
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._policies: Dict[str, InboxPolicy] = {}
        self._reply_counts: Dict[Tuple[str, str], int] = {}
        self._failures: Dict[Tuple[str, str], int] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------
    def configure(self, policies: Mapping[str, InboxPolicy]) -> None:
        with self._lock:
            self._policies = {
                str(agent_id): policy
                for agent_id, policy in dict(policies or {}).items()
                if policy.reply_mode == "auto"
            }

    def sync(self, policies: Mapping[str, InboxPolicy]) -> None:
        self.configure(policies)
        with self._lock:
            has_auto = bool(self._policies)
        if has_auto:
            self.start()
        else:
            self.stop()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="life-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _thread_main(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception:                                 # noqa: BLE001
                logger.warning("Life inbox 处理失败", exc_info=True)
            self._stop.wait(self.poll_seconds)

    # ------------------------------------------------------------
    @staticmethod
    def _payload(row: Mapping[str, Any]) -> Dict[str, Any]:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _is_agent_message(row: Mapping[str, Any]) -> bool:
        return str(
            LifeInboxService._payload(row).get("kind") or "") == "agent_message"

    def _recorder(self) -> Any:
        communication = getattr(self.rt, "communication", None)
        return getattr(communication, "recorder", None)

    def _agent(self, agent_id: str) -> Any:
        economy = getattr(self.rt, "economy", None)
        if economy is None:
            return None
        try:
            return economy.agent(agent_id)
        except Exception:                                     # noqa: BLE001
            return None

    def _conversation_id(self, row: Mapping[str, Any]) -> str:
        value = str(row.get("conversation_id") or "").strip()
        if value:
            return value
        return (
            f"conv_life_{row.get('to_agent')}_{row.get('from_agent')}")

    def _agent_label(self, agent_id: str) -> str:
        agent = self._agent(agent_id)
        name = str(getattr(agent, "name", "") or "")
        return format_agent_label(name, agent_id)

    def _reply(self, agent_id: str, row: Mapping[str, Any]) -> str:
        agent = self._agent(agent_id)
        if agent is None:
            return ""
        payload = self._payload(row)
        text = str(payload.get("text") or "").strip()
        if not text:
            return ""
        name = str(getattr(agent, "name", "") or "")
        prompt = (
            "你收到一条来自另一个 Agent 的生活层消息。\n"
            "这不是 Task，不要求你创建工作、修改文件或声称完成了工作。\n\n"
            f"来自：{self._agent_label(str(row.get('from_agent') or ''))}\n"
            f"消息：{text}\n\n"
            "请用简短、自然的中文回复。只输出回复正文，不要解释规则。"
        )
        try:
            return str(agent.think(
                prompt,
                system=(
                    format_agent_identity(name, agent_id) + "\n\n"
                    "你是 AIEOS 中的一个独立 Agent。"
                    "这是一条生活层通信，不是任务执行。"
                    "你可以简短回复，也可以不回复；不要创建 Task，"
                    "不要声称完成了没有做的工作。"
                ),
                call_purpose="utility",
                cognitive=False,
                max_tokens=256,
            ) or "").strip()
        except Exception:                                     # noqa: BLE001
            logger.warning(
                "Life inbox LLM 回复失败：%s", agent_id, exc_info=True)
            return ""

    def _send_reply(
        self, agent_id: str, row: Mapping[str, Any], reply: str,
    ) -> bool:
        communication = getattr(self.rt, "communication", None)
        if communication is None:
            return False
        conversation_id = self._conversation_id(row)
        try:
            run_coro(communication.send(
                agent_id,
                str(row.get("from_agent") or ""),
                text=reply,
                message_type=MessageType.TEXT,
                payload={
                    "kind": "agent_message",
                    "text": reply,
                    "life": True,
                },
                reply_to=str(row.get("message_id") or "") or None,
                trace_id=str(row.get("trace_id") or ""),
                conversation_id=conversation_id,
            ))
            return True
        except Exception:                                     # noqa: BLE001
            logger.warning(
                "Life inbox 发送回复失败：%s", agent_id, exc_info=True)
            return False

    def process_once(self) -> int:
        """扫描一次；返回本次成功回复的条数。"""
        recorder = self._recorder()
        if recorder is None:
            return 0
        with self._lock:
            policies = dict(self._policies)
        replied = 0
        for agent_id, policy in policies.items():
            if not self.availability.is_idle(agent_id):
                continue
            try:
                rows = recorder.list_pending(agent_id, limit=10)
            except Exception:                                 # noqa: BLE001
                continue
            for row in rows:
                if not self._is_agent_message(row):
                    continue
                message_id = str(row.get("message_id") or "")
                conversation = self._conversation_id(row)
                key = (agent_id, conversation)
                if self._reply_counts.get(key, 0) >= (
                        policy.max_auto_replies_per_conversation):
                    recorder.mark_ignored(
                        message_id, reason="auto_reply_limit")
                    continue
                reply = self._reply(agent_id, row)
                if not reply:
                    failure_key = (agent_id, message_id)
                    self._failures[failure_key] = (
                        self._failures.get(failure_key, 0) + 1)
                    if self._failures[failure_key] >= 3:
                        recorder.mark_ignored(
                            message_id, reason="auto_reply_failed")
                    continue
                if not self._send_reply(agent_id, row, reply):
                    failure_key = (agent_id, message_id)
                    self._failures[failure_key] = (
                        self._failures.get(failure_key, 0) + 1)
                    continue
                recorder.mark_processed_id(message_id, agent_id)
                self._reply_counts[key] = self._reply_counts.get(key, 0) + 1
                replied += 1
                usage = getattr(self._agent(agent_id), "last_usage", None) or {}
                cost = float(usage.get("cost_ai") or 0)
                if (policy.max_cost_per_message > 0
                        and cost > policy.max_cost_per_message):
                    logger.warning(
                        "Life inbox 单条成本超限：%s cost=%.6f limit=%.6f",
                        agent_id, cost, policy.max_cost_per_message)
        return replied


__all__ = ["LifeInboxService"]

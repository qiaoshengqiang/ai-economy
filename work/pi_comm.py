"""Pi 的通信桥：Inbox / Outbox。

Pi 是外部 CLI 进程，不能直接调用 CommunicationManager。

Inbox:
    其他 Agent → CommunicationManager → 本桥 handler
      → .aieos/pi-inbox.md
      → Pi 在下一轮读取

Outbox:
    Pi 写 .aieos/pi-outbox/*.json
      → 本桥读取并校验
      → CommunicationManager.send
      → 对方 Agent 的 inbox
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from communication import MessageType
from core.identity import format_agent_label
from work.comm_bridge import run_coro

logger = logging.getLogger(__name__)


#: Pi Inbox 只承载 Agent 可读的普通文本消息。
#: Task 生命周期 / 协商 / ERROR 是协议事实，不应作为“用户消息”
#: 进入 Pi 的 prompt，更不能以空 text 唤醒 Life。
PI_INBOX_MESSAGE_TYPES = frozenset({"TEXT"})


class PiInboxBridge:
    """把发给 Pi Worker 的消息落成文件，供 Pi 在 turn 边界读取。"""

    def __init__(
        self,
        communication: Any,
        *,
        agent_id: str,
        aieos_dir: Optional[Path] = None,
        inbox_dir: Optional[Path] = None,
    ) -> None:
        self.communication = communication
        self.agent_id = str(agent_id or "").strip()
        # 持久模式使用 agents/<id>/inbox；任务内旧模式使用 .aieos。
        root = inbox_dir if inbox_dir is not None else aieos_dir
        if root is None:
            raise ValueError("PiInboxBridge 需要 aieos_dir 或 inbox_dir")
        self.root = Path(root)
        self.inbox_md = self.root / "pi-inbox.md"
        self.inbox_jsonl = self.root / "pi-inbox.jsonl"
        self.archive_md = self.root / "pi-inbox-archive.md"
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._previous_handler: Any = None

    @property
    def directory(self) -> Path:
        return self.root

    def start(self) -> bool:
        if self.communication is None or not self.agent_id:
            return False
        registry = getattr(self.communication, "registry", None)
        if registry is not None and not registry.exists(self.agent_id):
            logger.warning("Pi inbox：%s 未接入通信层", self.agent_id)
            return False
        if self._running:
            return True
        self.root.mkdir(parents=True, exist_ok=True)
        self._previous_handler = self.communication.replace_handler(
            self.agent_id, self.capture)
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main, name="pi-inbox", daemon=True)
        self._thread.start()
        return True

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._poll())
        finally:
            loop.close()

    async def _poll(self) -> None:
        while self._running:
            try:
                await self.communication.receive(
                    self.agent_id, timeout=0.2)
            except asyncio.CancelledError:
                return
            except Exception:                                 # noqa: BLE001
                logger.warning(
                    "Pi inbox 收取失败", exc_info=True)
                await asyncio.sleep(0.2)

    def _sender_label(self, agent_id: str) -> str:
        """消息头显示名；技术上仍保留 `from_agent` 原值。"""
        display = ""
        registry = getattr(self.communication, "registry", None)
        if registry is not None:
            try:
                info = registry.find(agent_id)
                if info is not None:
                    display = str(getattr(info, "display_name", "") or "")
            except Exception:                                 # noqa: BLE001
                display = ""
        return format_agent_label(display, agent_id)

    def capture(self, message: Any) -> None:
        """CommunicationManager handler：把消息写入 Pi 可读的文件。"""
        message_type = str(
            getattr(message, "message_type", "") or "").upper()
        if message_type not in PI_INBOX_MESSAGE_TYPES:
            return None
        payload = getattr(message, "payload", None) or {}
        if not isinstance(payload, dict):
            payload = {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        from_agent = str(getattr(message, "from_agent", "") or "")
        record = {
            "message_id": str(getattr(message, "message_id", "") or ""),
            "from_agent": from_agent,
            "from_display_name": self._sender_label(from_agent),
            "to_agent": str(getattr(message, "to_agent", "") or ""),
            "message_type": str(getattr(message, "message_type", "") or ""),
            "conversation_id": str(
                getattr(message, "conversation_id", "") or ""),
            "task_id": str(payload.get("task_id") or ""),
            "kind": str(payload.get("kind") or ""),
            "text": text,
            "created_at": str(getattr(message, "created_at", "") or ""),
        }
        block = (
            f"### {record['message_id']} · {record['from_display_name']} "
            f"· {record['message_type']}\n{text}\n\n")
        with self._lock:
            with self.inbox_jsonl.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            with self.inbox_md.open("a", encoding="utf-8") as handle:
                handle.write(block)
        return None

    def drain_for_prompt(self) -> str:
        """取走当前 inbox 文本，交给下一次 Pi prompt。"""
        with self._lock:
            if not self.inbox_md.is_file():
                return ""
            text = self.inbox_md.read_text(encoding="utf-8").strip()
            if not text:
                self.inbox_md.unlink(missing_ok=True)
                return ""
            with self.archive_md.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n\n")
            self.inbox_md.unlink(missing_ok=True)
        return text

    def peek_for_prompt(self) -> str:
        """只读取当前 inbox，不消费。

        Life Turn 必须先 peek → 执行 → 成功后再 consume；
        失败时消息仍留在 inbox，不会因为一次 LLM 失败而丢失。
        """
        with self._lock:
            if not self.inbox_md.is_file():
                return ""
            return self.inbox_md.read_text(encoding="utf-8").strip()

    def consume_text(self, expected: str) -> bool:
        """消费已经成功处理过的一段 inbox；保留处理期间新到的消息。"""
        target = str(expected or "")
        if not target.strip():
            return False
        with self._lock:
            if not self.inbox_md.is_file():
                return False
            current = self.inbox_md.read_text(encoding="utf-8")
            if not current.startswith(target):
                return False
            remainder = current[len(target):].lstrip("\n")
            with self.archive_md.open("a", encoding="utf-8") as handle:
                handle.write(target.rstrip() + "\n\n")
            if remainder.strip():
                self.inbox_md.write_text(
                    remainder.rstrip() + "\n", encoding="utf-8")
            else:
                self.inbox_md.unlink(missing_ok=True)
        return True

    def stop(self) -> None:
        self._running = False
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(
                    lambda: None)
            except Exception:                                 # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.communication is not None:
            try:
                self.communication.restore_handler(
                    self.agent_id, self._previous_handler)
            except Exception:                                 # noqa: BLE001
                pass
            self._previous_handler = None
        self._loop = None


class PiOutboxBridge:
    """读取 Pi 写出的 JSON 文件，转成真正的 Agent Message。"""

    def __init__(
        self,
        communication: Any,
        *,
        actor: str,
        task_id: str,
        aieos_dir: Path,
    ) -> None:
        self.communication = communication
        self.actor = str(actor or "").strip()
        self.task_id = str(task_id or "").strip()
        self.root = Path(aieos_dir)
        self.pending = self.root / "pi-outbox"
        self.sent = self.pending / "sent"
        self.failed = self.pending / "failed"
        self._lock = threading.RLock()

    def has_pending(self) -> bool:
        return self.pending.is_dir() and any(self.pending.glob("*.json"))

    def drain_and_send(self) -> int:
        if self.communication is None or not self.pending.is_dir():
            return 0
        sent = 0
        with self._lock:
            for path in sorted(self.pending.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    self._send(payload)
                except Exception as exc:                      # noqa: BLE001
                    self.failed.mkdir(parents=True, exist_ok=True)
                    target = self.failed / path.name
                    try:
                        path.replace(target)
                    except OSError:
                        pass
                    logger.warning(
                        "Pi outbox 消息失败 %s：%s", path.name, exc)
                    continue
                self.sent.mkdir(parents=True, exist_ok=True)
                try:
                    path.replace(self.sent / path.name)
                except OSError:
                    pass
                sent += 1
        return sent

    def _send(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("outbox 内容必须是 JSON 对象")
        to_agent = str(payload.get("to") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not to_agent or not text:
            raise ValueError("outbox 需要 to 和 text")
        if to_agent == self.actor:
            raise ValueError("不能发给自己")
        task_id = str(payload.get("task_id") or self.task_id).strip()
        conversation_id = str(
            payload.get("conversation_id") or "").strip()
        if not conversation_id:
            conversation_id = (
                f"conv_{task_id}_{self.actor}_{to_agent}")
        run_coro(self.communication.send(
            self.actor,
            to_agent,
            text=text,
            message_type=MessageType.TEXT,
            payload={
                "kind": "agent_message",
                "task_id": task_id,
                "text": text,
            },
            trace_id=task_id,
            conversation_id=conversation_id,
        ))

"""Life Wake Service —— 把消息 / 闹钟变成一次 Life Turn。

它负责**唤醒决策**，不负责认知：

    Message / Alarm
        ↓
    空闲？限流？预算？会话轮数？
        ↓
    LifeTurnRuntime（一次小预算 Brain 调用）
        ↓
    reply / message_send / alarm_* / no_op
        ↓
    Activity Record（agent_wakeup_runs）

🔒 忙时不打断：Agent WORKING 时消息留在 Inbox，等空闲后再唤醒。
🔒 闹钟忙时按现有 Wakeup 语义 `skipped_busy`，不补发。
🔒 这里不创建 Task、不修改 Task 状态、不写 Task Workspace。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from core.alarm import WakeupEvent, WakeupResult
from core.identity import utc_now_iso
from work.life_policy import LifePolicy
from work.life_turn import (
    LifeMessage, LifeStimulus, LifeTurnResult, LifeTurnRuntime,
    extract_message_ids,
)

logger = logging.getLogger(__name__)

#: 只把普通文本当成 Life stimulus。
LIFE_MESSAGE_TYPES = frozenset({"TEXT"})

#: 系统发送者不触发 Life Turn。
SYSTEM_SENDERS = frozenset({"system", "human_gateway"})


def _parse_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _iso_ago(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=max(0, int(seconds)))
    ).isoformat()


def _is_auto_life_message(row: Mapping[str, Any]) -> bool:
    payload = _parse_payload(row.get("payload"))
    return bool(payload.get("life") and payload.get("auto"))


@dataclass
class _StimulusPlan:
    stimulus: LifeStimulus
    consume: Optional[Callable[[], Any]] = None


class LifeWakeService:
    """消息 / 闹钟唤醒协调器。"""

    def __init__(
        self,
        *,
        policy_fn: Callable[[str], LifePolicy],
        turn_runtime: LifeTurnRuntime,
        availability: Any,
        agent_ids: Iterable[str],
        pi_agents: Iterable[str],
        pi_bridge_fn: Callable[[str], Any],
        pending_messages_fn: Callable[[str], List[Dict[str, Any]]],
        message_lookup_fn: Callable[[str], Optional[Dict[str, Any]]],
        mark_processed_fn: Callable[[str, List[str]], Any],
        conversation_fn: Callable[[str], List[Dict[str, Any]]],
        alarm_store: Any = None,
        disabled_fn: Optional[Callable[[str], bool]] = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.policy_fn = policy_fn
        self.turn_runtime = turn_runtime
        self.availability = availability
        self.agent_ids = tuple(str(x) for x in agent_ids)
        self.pi_agents = frozenset(str(x) for x in pi_agents)
        self.pi_bridge_fn = pi_bridge_fn
        self.pending_messages_fn = pending_messages_fn
        self.message_lookup_fn = message_lookup_fn
        self.mark_processed_fn = mark_processed_fn
        self.conversation_fn = conversation_fn
        self.alarm_store = alarm_store
        self.disabled_fn = disabled_fn or (lambda _aid: False)
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._last_turn_at: Dict[str, float] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}
        self._throttled_logged: Dict[str, float] = {}

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="life-wake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _signal(self) -> None:
        self._wake.set()

    def _thread_main(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:                                 # noqa: BLE001
                logger.warning("Life wake 扫描失败", exc_info=True)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    # ------------------------------------------------------------
    # 消息扫描
    # ------------------------------------------------------------
    def scan_once(self) -> int:
        """扫描所有 life-enabled Agent；返回启动的 Life Turn 数。"""
        started = 0
        now = time.monotonic()
        for agent_id in self.agent_ids:
            policy = self.policy_fn(agent_id)
            if not policy.enabled or not policy.wake_on_message:
                continue
            if self.disabled_fn(agent_id):
                continue
            if now < self._cooldown_until.get(agent_id, 0.0):
                continue
            if not self.availability.is_idle(agent_id):
                continue
            min_interval = float(policy.min_interval_seconds)
            if now - self._last_turn_at.get(agent_id, 0.0) < min_interval:
                continue
            plan = self._message_plan(agent_id)
            if plan is None:
                continue
            if self._message_rate_limited(agent_id, policy):
                self._cooldown_until[agent_id] = now + 60.0
                self._log_throttled(agent_id, "max_turns_per_hour")
                continue
            if self._conversation_limit_reached(
                    agent_id, plan.stimulus, policy):
                if plan.consume is not None:
                    try:
                        plan.consume()
                    except Exception:                         # noqa: BLE001
                        logger.warning(
                            "Life conversation limit consume 失败：%s",
                            agent_id, exc_info=True)
                self._record_message_run(
                    plan.stimulus, outcome="throttled",
                    summary="conversation_turn_limit")
                self._cooldown_until[agent_id] = now + 60.0
                self._log_throttled(agent_id, "conversation_turn_limit")
                continue
            if self._run_plan(
                    agent_id, policy, plan, source="message") is not None:
                started += 1
        return started

    def _message_plan(self, agent_id: str) -> Optional[_StimulusPlan]:
        if agent_id in self.pi_agents:
            return self._pi_message_plan(agent_id)
        return self._builtin_message_plan(agent_id)

    def _pi_message_plan(self, agent_id: str) -> Optional[_StimulusPlan]:
        bridge = self.pi_bridge_fn(agent_id)
        if bridge is None:
            return None
        text = str(bridge.peek_for_prompt() or "").strip()
        if not text:
            return None
        message_ids = extract_message_ids(text)
        messages: List[LifeMessage] = []
        for message_id in message_ids:
            row = self.message_lookup_fn(message_id)
            if row is None:
                continue
            message = self._row_to_message(row)
            if not message.text:
                continue
            if message.message_type not in LIFE_MESSAGE_TYPES:
                continue
            if message.from_agent == agent_id:
                continue
            if message.from_agent in SYSTEM_SENDERS:
                continue
            messages.append(message)
        if not messages:
            # 空 text / 非 TEXT 的历史 inbox 不唤醒 Life；
            # 直接消费掉，避免每秒重复扫描同一个空块。
            try:
                bridge.consume_text(text)
            except Exception:                                 # noqa: BLE001
                pass
            return None
        conversation = ""
        if len(messages) == 1:
            conversation = messages[0].conversation_id
        stimulus = LifeStimulus(
            agent_id=agent_id,
            wake_id=f"msgwake_{uuid.uuid4().hex[:12]}",
            kind="message",
            conversation_id=conversation,
            messages=messages,
            inbox_text=text,
        )
        return _StimulusPlan(
            stimulus=stimulus,
            consume=lambda: bridge.consume_text(text),
        )

    def _builtin_message_plan(
        self, agent_id: str,
    ) -> Optional[_StimulusPlan]:
        rows = list(self.pending_messages_fn(agent_id) or [])
        messages: List[LifeMessage] = []
        for row in rows:
            message = self._row_to_message(row)
            if not message.text:
                continue
            if message.message_type not in LIFE_MESSAGE_TYPES:
                continue
            if message.from_agent == agent_id:
                continue
            if message.from_agent in SYSTEM_SENDERS:
                continue
            messages.append(message)
        if not messages:
            return None
        conversation = ""
        if len({m.conversation_id for m in messages}) == 1:
            conversation = messages[0].conversation_id
        stimulus = LifeStimulus(
            agent_id=agent_id,
            wake_id=f"msgwake_{uuid.uuid4().hex[:12]}",
            kind="message",
            conversation_id=conversation,
            messages=messages,
        )
        message_ids = [m.message_id for m in messages if m.message_id]
        return _StimulusPlan(
            stimulus=stimulus,
            consume=lambda: self.mark_processed_fn(agent_id, message_ids),
        )

    # ------------------------------------------------------------
    # 闹钟
    # ------------------------------------------------------------
    def run_alarm(self, event: WakeupEvent) -> WakeupResult:
        agent_id = str(event.agent_id or "").strip()
        policy = self.policy_fn(agent_id)
        if not policy.enabled:
            return WakeupResult(
                outcome="rejected", summary="life_disabled")
        if not policy.wake_on_alarm:
            return WakeupResult(
                outcome="rejected", summary="wake_on_alarm_disabled")
        if self.disabled_fn(agent_id):
            return WakeupResult(
                outcome="rejected", summary="agent_disabled")
        if not self.availability.is_idle(agent_id):
            return WakeupResult(
                outcome="skipped_busy", summary="agent_working")
        plan = self._alarm_plan(event)
        result = self._run_plan(agent_id, policy, plan, source="alarm")
        if result is None:
            return WakeupResult(
                outcome="skipped_busy", summary="life_lock_busy")
        return WakeupResult(
            outcome=self._wake_outcome(result),
            summary=result.summary or result.outcome,
            detail=result.to_detail(),
        )

    def _alarm_plan(self, event: WakeupEvent) -> _StimulusPlan:
        pending = self._message_plan(event.agent_id)
        messages = list(pending.stimulus.messages) if pending else []
        conversation = (
            str(event.conversation_id or "").strip()
            or (messages[0].conversation_id if len(messages) == 1 else ""))
        stimulus = LifeStimulus(
            agent_id=event.agent_id,
            wake_id=event.wakeup_id,
            kind="alarm" if not messages else "mixed",
            reason=event.reason,
            instruction=event.instruction,
            created_by=event.created_by,
            scheduled_for=event.scheduled_for,
            task_ids=list(event.task_ids),
            conversation_id=conversation,
            messages=messages,
            inbox_text=pending.stimulus.inbox_text if pending else "",
        )
        return _StimulusPlan(
            stimulus=stimulus,
            consume=pending.consume if pending else None,
        )

    # ------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------
    def _run_plan(
        self,
        agent_id: str,
        policy: LifePolicy,
        plan: _StimulusPlan,
        *,
        source: str,
    ) -> Optional[LifeTurnResult]:
        if not self.availability.try_acquire(agent_id):
            return None
        try:
            result = self.turn_runtime.run(plan.stimulus, policy)
            self._last_turn_at[agent_id] = time.monotonic()
            # 已经产生任何成功的副作用（发出消息 / 建了闹钟）就消费；
            # 否则重试会重复执行同一个动作。
            acted = any(
                bool(item.get("ok"))
                for item in (result.actions or [])
                if isinstance(item, Mapping))
            if (result.outcome in ("replied", "no_op") or acted) and plan.consume:
                try:
                    plan.consume()
                except Exception as exc:                      # noqa: BLE001
                    result.errors.append(
                        f"consume_failed:{type(exc).__name__}: {exc}")
            if result.outcome == "failed":
                self._failures[agent_id] = (
                    self._failures.get(agent_id, 0) + 1)
                backoff = min(300, 15 * self._failures[agent_id])
                self._cooldown_until[agent_id] = (
                    time.monotonic() + backoff)
            else:
                self._failures[agent_id] = 0
            if source == "message":
                self._record_message_run(
                    plan.stimulus,
                    outcome=self._wake_outcome(result),
                    summary=result.summary or result.outcome,
                    detail=result.to_detail(),
                )
            if (policy.max_cost_per_turn > 0
                    and result.cost_ai > policy.max_cost_per_turn):
                logger.warning(
                    "Life Turn 成本超限：agent=%s cost=%.6f limit=%.6f",
                    agent_id, result.cost_ai, policy.max_cost_per_turn)
            logger.info(
                "Life Turn：agent=%s source=%s outcome=%s "
                "messages=%d actions=%d tokens=%s cost=%.6f",
                agent_id, source, result.outcome,
                len(plan.stimulus.messages), len(result.actions),
                result.tokens, result.cost_ai)
            return result
        finally:
            self.availability.release(agent_id)

    @staticmethod
    def _wake_outcome(result: LifeTurnResult) -> str:
        if result.outcome == "replied":
            return "replied"
        if result.outcome == "no_op":
            return "no_notify"
        return "failed"

    def _record_message_run(
        self,
        stimulus: LifeStimulus,
        *,
        outcome: str,
        summary: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.alarm_store is None:
            return
        try:
            run_id = self.alarm_store.begin_run(
                wakeup_id=stimulus.wake_id,
                agent_id=stimulus.agent_id,
                scheduled_for=utc_now_iso(),
                kind="message",
            )
            if run_id is None:
                return
            payload = dict(detail or {})
            payload.update({
                "message_ids": [
                    item.message_id for item in stimulus.messages
                    if item.message_id
                ],
                "conversation_id": stimulus.conversation_id,
            })
            self.alarm_store.finish_run(
                run_id, outcome=outcome, summary=summary,
                detail=payload)
        except Exception:                                     # noqa: BLE001
            logger.warning(
                "Life message Activity 写入失败：%s",
                stimulus.agent_id, exc_info=True)

    # ------------------------------------------------------------
    # 限流 / 转换
    # ------------------------------------------------------------
    def _message_rate_limited(
        self, agent_id: str, policy: LifePolicy,
    ) -> bool:
        if self.alarm_store is None:
            return False
        try:
            count = self.alarm_store.count_runs(
                agent_id=agent_id, since=_iso_ago(3600))
        except Exception:                                     # noqa: BLE001
            return False
        return count >= policy.max_turns_per_hour

    def _conversation_limit_reached(
        self,
        agent_id: str,
        stimulus: LifeStimulus,
        policy: LifePolicy,
    ) -> bool:
        conversation = str(stimulus.conversation_id or "").strip()
        if not conversation:
            return False
        try:
            rows = self.conversation_fn(conversation) or []
        except Exception:                                     # noqa: BLE001
            return False
        auto_count = sum(1 for row in rows if _is_auto_life_message(row))
        return auto_count >= policy.max_conversation_turns

    def _log_throttled(self, agent_id: str, reason: str) -> None:
        now = time.monotonic()
        if now - self._throttled_logged.get(agent_id, 0.0) < 60.0:
            return
        self._throttled_logged[agent_id] = now
        logger.info(
            "Life wake 暂停：agent=%s reason=%s", agent_id, reason)

    @staticmethod
    def _row_to_message(row: Mapping[str, Any]) -> LifeMessage:
        payload = _parse_payload(row.get("payload"))
        text = str(payload.get("text") or "").strip()
        return LifeMessage(
            message_id=str(row.get("message_id") or ""),
            from_agent=str(row.get("from_agent") or ""),
            text=text,
            message_type=str(row.get("message_type") or "TEXT").upper(),
            conversation_id=str(row.get("conversation_id") or ""),
            trace_id=str(row.get("trace_id") or ""),
            created_at=str(row.get("created_at") or ""),
            payload=payload,
        )


class LifeAlarmWakeupHandler:
    """AlarmService 的 Worker handler：把闹钟交给 LifeWakeService。"""

    def __init__(self, service: LifeWakeService) -> None:
        self.service = service

    def can_wake(self, agent_id: str) -> bool:
        policy = self.service.policy_fn(agent_id)
        return bool(policy.enabled and policy.wake_on_alarm)

    def wake(self, event: WakeupEvent) -> WakeupResult:
        return self.service.run_alarm(event)


__all__ = ["LifeAlarmWakeupHandler", "LifeWakeService"]

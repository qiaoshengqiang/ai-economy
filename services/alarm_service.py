"""Agent Wakeup V1 —— AlarmService / WakeupDispatcher。

🔒 这里只做：
   · 等到 next_fire_at；
   · 发 WakeupEvent；
   · 写 Activity Record。

不调用 LLM、不创建 Task、不写 communication_messages。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from core.alarm import Alarm, WakeupEvent, WakeupResult
from core.alarm_store import AlarmStore
from core.identity import utc_now_iso

logger = logging.getLogger(__name__)

MAX_TASKS_PER_ALARM = 5


class WakeupHandler(Protocol):
    def can_wake(self, agent_id: str) -> bool: ...
    def wake(self, event: WakeupEvent) -> WakeupResult: ...


class WakeupDispatcher:
    """按 agent_id 把 WakeupEvent 交给对应 handler。"""

    def __init__(self) -> None:
        self._handlers: Dict[str, WakeupHandler] = {}
        self._lock = threading.RLock()

    def register(self, agent_id: str, handler: WakeupHandler) -> None:
        with self._lock:
            self._handlers[str(agent_id)] = handler

    def handler_for(self, agent_id: str) -> Optional[WakeupHandler]:
        with self._lock:
            return self._handlers.get(str(agent_id))

    def dispatch(self, event: WakeupEvent) -> WakeupResult:
        handler = self.handler_for(event.agent_id)
        if handler is None:
            return WakeupResult(
                outcome="failed",
                summary=f"no_wakeup_handler:{event.agent_id}")
        return handler.wake(event)


def _parse_iso(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class AlarmService:
    """一个进程内的 deadline-driven 闹钟调度器。"""

    def __init__(
        self,
        store: AlarmStore,
        dispatcher: WakeupDispatcher,
        *,
        min_interval_seconds: int = 60,
        max_active_per_agent: int = 5,
        task_status_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        guard_max_per_agent: int = 0,
        guard_max_total: int = 0,
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.min_interval_seconds = max(1, int(min_interval_seconds))
        self.max_active_per_agent = max(1, int(max_active_per_agent))
        self.task_status_fn = task_status_fn
        self.guard_max_per_agent = max(0, int(guard_max_per_agent))
        self.guard_max_total = max(0, int(guard_max_total))
        self._guard_per_agent: Dict[str, int] = {}
        self._guard_total = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.store.recover_running(now=utc_now_iso())
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="alarm-scheduler", daemon=True)
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

    # ------------------------------------------------------------
    # Agent API
    # ------------------------------------------------------------
    def create(
        self,
        *,
        agent_id: str,
        reason: str,
        instruction: str,
        task_ids: Any = (),
        schedule: str = "once",
        interval_seconds: Any = None,
        delay_seconds: Any = None,
        at: str = "",
        max_runs: Any = None,
        expires_at: str = "",
        expires_in_seconds: Any = None,
        dedupe_key: str = "",
        source_message_id: str = "",
        conversation_id: str = "",
    ) -> Dict[str, Any]:
        key = str(agent_id or "").strip()
        if not key:
            raise ValueError("alarm_create 缺少 agent_id")
        text_reason = str(reason or "").strip()
        text_instruction = str(instruction or "").strip()
        if not text_reason:
            raise ValueError("alarm_create 需要 reason")
        if not text_instruction:
            raise ValueError("alarm_create 需要 instruction")

        tasks = self._normalize_tasks(task_ids)
        kind = str(schedule or "").strip().lower()
        # Agent 只给 interval_seconds 时，语义已经明确是周期闹钟。
        # 这里做归一化，避免模型为了猜 schedule 多花一轮调用。
        if kind in ("interval", "recurring", "repeat", "periodic"):
            kind = "every"
        elif kind in ("delay", "after"):
            kind = "once"
        if not kind:
            kind = "every" if interval_seconds not in (None, "") else "once"
        if kind not in ("once", "every"):
            raise ValueError(
                "schedule 只能是 once / every（可省略："
                "给 interval_seconds 即为 every）")

        now = datetime.now(timezone.utc)
        interval: Optional[int] = None
        next_at: Optional[datetime] = None
        if kind == "every":
            try:
                interval = int(interval_seconds)
            except (TypeError, ValueError):
                raise ValueError(
                    "every 需要 interval_seconds（秒，最小 "
                    f"{self.min_interval_seconds}）")
            if interval < self.min_interval_seconds:
                raise ValueError(
                    f"interval_seconds 不能小于 {self.min_interval_seconds}")
            next_at = now + timedelta(seconds=interval)
        else:
            if at:
                next_at = _parse_iso(at)
                if next_at is None or next_at <= now:
                    raise ValueError("at 必须是未来的 UTC 时间")
            else:
                try:
                    delay = int(delay_seconds)
                except (TypeError, ValueError):
                    raise ValueError(
                        "once 需要 delay_seconds 或 at；"
                        "周期触发请传 interval_seconds")
                if delay <= 0:
                    raise ValueError("delay_seconds 必须 > 0")
                next_at = now + timedelta(seconds=delay)

        max_count: Optional[int] = None
        if max_runs not in (None, "", 0):
            try:
                max_count = int(max_runs)
            except (TypeError, ValueError):
                raise ValueError("max_runs 必须是整数")
            if max_count <= 0:
                raise ValueError("max_runs 必须 > 0")

        expires = _parse_iso(expires_at)
        if expires is None and expires_in_seconds not in (None, ""):
            try:
                expires = now + timedelta(seconds=int(expires_in_seconds))
            except (TypeError, ValueError):
                raise ValueError("expires_in_seconds 必须是整数")
        if expires is not None and expires <= now:
            raise ValueError("expires_at 必须是未来时间")

        dedupe = str(dedupe_key or "").strip()
        if dedupe:
            existing = self.store.find_dedupe(key, dedupe)
            if existing is not None:
                return existing.to_dict()

        if self.store.count_active(key) >= self.max_active_per_agent:
            raise ValueError(
                f"Agent {key} 的活动闹钟已达到上限 "
                f"{self.max_active_per_agent}")

        alarm = self.store.create(
            agent_id=key,
            reason=text_reason,
            instruction=text_instruction,
            task_ids=tasks,
            schedule_kind=kind,
            next_fire_at=_iso(next_at),
            interval_seconds=interval,
            max_runs=max_count,
            expires_at=_iso(expires) if expires is not None else "",
            dedupe_key=dedupe,
            source_message_id=str(source_message_id or ""),
            conversation_id=str(conversation_id or ""),
        )
        self._signal()
        return alarm.to_dict()

    def list(self, agent_id: str, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.store.list_for_agent(
                agent_id, include_inactive=include_inactive)
        ]

    def cancel(self, agent_id: str, alarm_id: str) -> bool:
        ok = self.store.cancel(agent_id, alarm_id)
        if ok:
            self._signal()
        return ok

    def recent_runs(self, agent_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        return self.store.recent_runs(agent_id, limit=limit)

    def run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        """单条 Activity 的完整记录（含原始模型输出）。"""
        return self.store.get_run(run_id)

    def count_runs(self, agent_id: str = "", *, since: str = "") -> int:
        return self.store.count_runs(agent_id=agent_id, since=since)

    # ------------------------------------------------------------
    # 调度循环
    # ------------------------------------------------------------
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.expire_due(utc_now_iso())
                alarm = self.store.claim_due(utc_now_iso())
                if alarm is not None:
                    self._dispatch(alarm)
                    continue
            except Exception:                                 # noqa: BLE001
                logger.warning("闹钟调度 tick 失败", exc_info=True)

            # 先清信号，再计算 deadline；这样 create/cancel 在计算期间
            # 发出的 signal 不会被清掉。
            with self._lock:
                self._wake.clear()
            timeout = self._wait_timeout()
            self._wake.wait(timeout=timeout)

    def _wait_timeout(self) -> Optional[float]:
        due = self.store.next_due_at()
        if not due:
            return None
        target = _parse_iso(due)
        if target is None:
            return None
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, min(delta, 60.0 * 60.0))

    def _normalize_tasks(self, value: Any) -> List[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("task_ids 必须是数组")
        out: List[str] = []
        for item in value:
            key = str(item or "").strip()
            if key and key not in out:
                out.append(key)
        if len(out) > MAX_TASKS_PER_ALARM:
            raise ValueError(f"task_ids 最多 {MAX_TASKS_PER_ALARM} 个")
        return out

    def _task_snapshot(self, task_ids: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for task_id in task_ids:
            item: Dict[str, Any] = {"task_id": task_id, "found": False,
                                    "status": ""}
            if self.task_status_fn is not None:
                try:
                    value = self.task_status_fn(task_id)
                except Exception:                             # noqa: BLE001
                    value = None
                if isinstance(value, dict):
                    item.update(value)
                    item["task_id"] = task_id
            out.append(item)
        return out

    def _guard_blocked(self, agent_id: str) -> bool:
        if self.guard_max_per_agent:
            if self._guard_per_agent.get(agent_id, 0) >= self.guard_max_per_agent:
                return True
        if self.guard_max_total:
            if self._guard_total >= self.guard_max_total:
                return True
        return False

    def _dispatch(self, alarm: Alarm) -> None:
        now = utc_now_iso()
        scheduled_for = alarm.next_fire_at
        run_id = self.store.begin_run(
            wakeup_id=alarm.alarm_id,
            agent_id=alarm.agent_id,
            scheduled_for=scheduled_for,
            kind="alarm",
        )
        if run_id is None:
            # 唯一约束说明这个到期时刻已经有过 run；不再重复唤醒，
            # 按正常节奏推进到下一次（或完成）。
            self._reschedule(alarm, now=now)
            return

        if self._guard_blocked(alarm.agent_id):
            self.store.pause(alarm.agent_id, "guard_limit")
            self.store.reschedule(
                alarm.alarm_id, status="PAUSED",
                next_fire_at=alarm.next_fire_at,
                last_fired_at=now,
                fire_count=alarm.fire_count,
                last_error="guard_limit",
            )
            self.store.finish_run(
                run_id, outcome="guard_limit",
                summary="WakeupGuard 上限，已暂停闹钟")
            return

        self._guard_total += 1
        self._guard_per_agent[alarm.agent_id] = (
            self._guard_per_agent.get(alarm.agent_id, 0) + 1)

        event = WakeupEvent.from_alarm(
            alarm, tasks=self._task_snapshot(alarm.task_ids))
        try:
            result = self.dispatcher.dispatch(event)
        except Exception as exc:                              # noqa: BLE001
            logger.warning(
                "Wakeup handler 失败：%s", alarm.agent_id, exc_info=True)
            result = WakeupResult(
                outcome="failed",
                summary=f"{type(exc).__name__}: {exc}")
        detail: Dict[str, Any] = {
            "alarm_id": alarm.alarm_id,
            "task_ids": list(alarm.task_ids),
            "fire_count": alarm.fire_count + 1,
        }
        # handler 返回的原始证据（每次模型输出）原样并入。
        detail.update(dict(getattr(result, "detail", None) or {}))
        self.store.finish_run(
            run_id,
            outcome=result.outcome,
            summary=result.summary,
            job_id=result.job_id,
            detail=detail,
        )
        self._reschedule(alarm, now=utc_now_iso())

    def _reschedule(
        self, alarm: Alarm, *, now: str, status: Optional[str] = None,
    ) -> None:
        fired = alarm.fire_count + 1
        next_at: Optional[datetime] = None
        final_status = status or "ACTIVE"
        if alarm.schedule_kind == "every" and alarm.interval_seconds:
            next_at = _parse_iso(now) or datetime.now(timezone.utc)
            next_at = next_at + timedelta(seconds=alarm.interval_seconds)
            if alarm.expires_at:
                expires = _parse_iso(alarm.expires_at)
                if expires is not None and next_at >= expires:
                    final_status = "COMPLETED"
        else:
            final_status = "COMPLETED"

        if alarm.max_runs is not None and fired >= alarm.max_runs:
            final_status = "COMPLETED"

        self.store.reschedule(
            alarm.alarm_id,
            status=final_status,
            next_fire_at=(
                _iso(next_at) if next_at is not None
                else alarm.next_fire_at),
            last_fired_at=now,
            fire_count=fired,
            last_error="" if final_status != "COMPLETED" else "completed",
        )


__all__ = [
    "AlarmService", "WakeupDispatcher", "WakeupHandler",
    "MAX_TASKS_PER_ALARM",
]

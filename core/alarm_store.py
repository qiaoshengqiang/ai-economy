"""Agent Wakeup V1 —— 闹钟与 Activity 的持久化。

🔒 这里只做调度状态 / 审计事实的存取：
   · 不调用 LLM；
   · 不创建 Task；
   · 不写 communication_messages。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from core.alarm import Alarm
from core.identity import utc_now_iso
from storage.models import TABLE_AGENT_ALARMS, TABLE_AGENT_WAKEUP_RUNS


class AlarmStore:
    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------
    # 写：创建 / 取消 / 暂停
    # ------------------------------------------------------------
    def create(
        self,
        *,
        agent_id: str,
        reason: str,
        instruction: str,
        task_ids: List[str],
        schedule_kind: str,
        next_fire_at: str,
        interval_seconds: Optional[int] = None,
        max_runs: Optional[int] = None,
        expires_at: str = "",
        dedupe_key: str = "",
        source_message_id: str = "",
        conversation_id: str = "",
    ) -> Alarm:
        now = utc_now_iso()
        alarm_id = f"alarm_{uuid.uuid4().hex[:12]}"
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_AGENT_ALARMS}
                    (alarm_id, agent_id, status, reason, instruction,
                     task_ids_json, schedule_kind, interval_seconds,
                     next_fire_at, last_fired_at, fire_count, max_runs,
                     expires_at, dedupe_key, last_error, created_by,
                     source_message_id, conversation_id, created_at,
                     updated_at)
                VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?,
                        ?, '', ?, ?, ?, ?, ?)
                """,
                (
                    alarm_id, str(agent_id), str(reason),
                    str(instruction), json.dumps(list(task_ids)),
                    str(schedule_kind), interval_seconds, str(next_fire_at),
                    max_runs, str(expires_at or ""), str(dedupe_key or ""),
                    str(agent_id), str(source_message_id or ""),
                    str(conversation_id or ""), now, now,
                ),
            )
        return self.get(alarm_id)  # type: ignore[return-value]

    def cancel(self, agent_id: str, alarm_id: str) -> bool:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE {TABLE_AGENT_ALARMS}
                   SET status = 'CANCELLED', updated_at = ?
                 WHERE alarm_id = ? AND agent_id = ?
                   AND status IN ('ACTIVE', 'RUNNING', 'PAUSED')
                """,
                (now, str(alarm_id), str(agent_id)),
            )
            return cur.rowcount > 0

    def pause(self, agent_id: str, reason: str = "") -> int:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE {TABLE_AGENT_ALARMS}
                   SET status = 'PAUSED', last_error = ?, updated_at = ?
                 WHERE agent_id = ? AND status = 'ACTIVE'
                """,
                (str(reason or "paused"), now, str(agent_id)),
            )
            return max(0, int(cur.rowcount or 0))

    def pause_all(self, reason: str = "") -> int:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE {TABLE_AGENT_ALARMS}
                   SET status = 'PAUSED', last_error = ?, updated_at = ?
                 WHERE status = 'ACTIVE'
                """,
                (str(reason or "paused"), now),
            )
            return max(0, int(cur.rowcount or 0))

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------
    def get(self, alarm_id: str) -> Optional[Alarm]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_AGENT_ALARMS} WHERE alarm_id = ?",
            (str(alarm_id),),
        )
        return Alarm.from_row(row) if row is not None else None

    def find_dedupe(self, agent_id: str, dedupe_key: str) -> Optional[Alarm]:
        key = str(dedupe_key or "").strip()
        if not key:
            return None
        row = self.db.fetchone(
            f"""
            SELECT * FROM {TABLE_AGENT_ALARMS}
             WHERE agent_id = ? AND dedupe_key = ?
               AND status IN ('ACTIVE', 'RUNNING', 'PAUSED')
             ORDER BY created_at DESC LIMIT 1
            """,
            (str(agent_id), key),
        )
        return Alarm.from_row(row) if row is not None else None

    def list_for_agent(
        self, agent_id: str, *, include_inactive: bool = False,
    ) -> List[Alarm]:
        where = "" if include_inactive else (
            " AND status IN ('ACTIVE', 'RUNNING', 'PAUSED')")
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_AGENT_ALARMS}
             WHERE agent_id = ?{where}
             ORDER BY next_fire_at ASC, created_at ASC
            """,
            (str(agent_id),),
        )
        return [Alarm.from_row(row) for row in rows]

    def count_active(self, agent_id: str) -> int:
        row = self.db.fetchone(
            f"""
            SELECT COUNT(*) AS n FROM {TABLE_AGENT_ALARMS}
             WHERE agent_id = ? AND status IN ('ACTIVE', 'RUNNING', 'PAUSED')
            """,
            (str(agent_id),),
        )
        return int(row["n"] or 0) if row is not None else 0

    def expire_due(self, now: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE {TABLE_AGENT_ALARMS}
                   SET status = 'COMPLETED', last_error = 'expired',
                       updated_at = ?
                 WHERE status = 'ACTIVE'
                   AND expires_at IS NOT NULL
                   AND expires_at != ''
                   AND expires_at <= ?
                """,
                (utc_now_iso(), str(now)),
            )
            return max(0, int(cur.rowcount or 0))

    def next_due_at(self) -> Optional[str]:
        row = self.db.fetchone(
            f"""
            SELECT MIN(next_fire_at) AS due FROM {TABLE_AGENT_ALARMS}
             WHERE status = 'ACTIVE'
            """
        )
        if row is None:
            return None
        value = str(row["due"] or "").strip()
        return value or None

    # ------------------------------------------------------------
    # 调度 claim
    # ------------------------------------------------------------
    def claim_due(self, now: str) -> Optional[Alarm]:
        row = self.db.fetchone(
            f"""
            SELECT * FROM {TABLE_AGENT_ALARMS}
             WHERE status = 'ACTIVE' AND next_fire_at <= ?
             ORDER BY next_fire_at ASC, created_at ASC
             LIMIT 1
            """,
            (str(now),),
        )
        if row is None:
            return None
        alarm = Alarm.from_row(row)
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE {TABLE_AGENT_ALARMS}
                   SET status = 'RUNNING', updated_at = ?
                 WHERE alarm_id = ? AND status = 'ACTIVE'
                   AND next_fire_at = ?
                """,
                (utc_now_iso(), alarm.alarm_id, alarm.next_fire_at),
            )
            if cur.rowcount != 1:
                return None
        alarm.status = "RUNNING"
        return alarm

    def reschedule(
        self,
        alarm_id: str,
        *,
        status: str,
        next_fire_at: str,
        last_fired_at: str,
        fire_count: int,
        last_error: str = "",
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                UPDATE {TABLE_AGENT_ALARMS}
                   SET status = ?, next_fire_at = ?, last_fired_at = ?,
                       fire_count = ?, last_error = ?, updated_at = ?
                 WHERE alarm_id = ? AND status != 'CANCELLED'
                """,
                (
                    str(status), str(next_fire_at), str(last_fired_at),
                    int(fire_count), str(last_error or ""), utc_now_iso(),
                    str(alarm_id),
                ),
            )

    def recover_running(self, *, now: str) -> int:
        """进程重启后，把遗留 RUNNING 放回可调度状态。"""
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE {TABLE_AGENT_ALARMS}
                   SET status = 'ACTIVE', next_fire_at = ?,
                       last_error = 'runtime_restart', updated_at = ?
                 WHERE status = 'RUNNING'
                """,
                (str(now), utc_now_iso()),
            )
            conn.execute(
                f"""
                UPDATE {TABLE_AGENT_WAKEUP_RUNS}
                   SET outcome = 'failed', finished_at = ?,
                       summary = COALESCE(summary, 'runtime_restart')
                 WHERE outcome = 'running'
                """,
                (str(now),),
            )
            return max(0, int(cur.rowcount or 0))

    # ------------------------------------------------------------
    # Activity Record
    # ------------------------------------------------------------
    def begin_run(
        self, *, wakeup_id: str, agent_id: str, scheduled_for: str,
        kind: str = "alarm",
    ) -> Optional[str]:
        run_id = f"wake_{uuid.uuid4().hex[:12]}"
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    f"""
                    INSERT INTO {TABLE_AGENT_WAKEUP_RUNS}
                        (run_id, wakeup_id, agent_id, kind, scheduled_for,
                         started_at, finished_at, outcome, summary, job_id,
                         detail_json)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, 'running', NULL, NULL, '{{}}')
                    """,
                    (
                        run_id, str(wakeup_id), str(agent_id), str(kind),
                        str(scheduled_for), utc_now_iso(),
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        summary: str = "",
        job_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                UPDATE {TABLE_AGENT_WAKEUP_RUNS}
                   SET finished_at = ?, outcome = ?, summary = ?,
                       job_id = ?, detail_json = ?
                 WHERE run_id = ?
                """,
                (
                    utc_now_iso(), str(outcome), str(summary or ""),
                    str(job_id or ""),
                    json.dumps(dict(detail or {}), ensure_ascii=False),
                    str(run_id),
                ),
            )

    def recent_runs(
        self, agent_id: str, *, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_AGENT_WAKEUP_RUNS}
             WHERE agent_id = ?
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (str(agent_id), max(1, min(200, int(limit)))),
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            # 列表只给摘要：原始模型输出只在打开详情时取一次，
            # UI 每 1.2 秒轮询，不能把 detail_json 一起传。
            item = dict(row)
            raw_detail = item.pop("detail_json", None)
            task_ids: List[str] = []
            alarm_id = ""
            if raw_detail:
                try:
                    parsed = json.loads(raw_detail)
                except (TypeError, ValueError):
                    parsed = {}
                if isinstance(parsed, dict):
                    task_ids = [
                        str(value)
                        for value in (parsed.get("task_ids") or ())
                        if str(value or "").strip()
                    ]
                    alarm_id = str(parsed.get("alarm_id") or "")
            item["task_ids"] = task_ids
            item["alarm_id"] = alarm_id
            out.append(item)
        return out

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetchone(
            f"""
            SELECT * FROM {TABLE_AGENT_WAKEUP_RUNS}
             WHERE run_id = ?
            """,
            (str(run_id),),
        )
        if row is None:
            return None
        item = dict(row)
        try:
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
        except (TypeError, ValueError):
            item["detail"] = {}
        return item

    def count_runs(self, *, agent_id: str = "", since: str = "") -> int:
        clauses = []
        params: List[Any] = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(str(agent_id))
        if since:
            clauses.append("started_at >= ?")
            params.append(str(since))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM {TABLE_AGENT_WAKEUP_RUNS}{where}",
            tuple(params),
        )
        return int(row["n"] or 0) if row is not None else 0


__all__ = ["AlarmStore"]

"""
Production Stage V1 —— 持久化（§五）
============================================================

    Stage 必须是**持久对象**。Agent / Runtime 重启后仍可恢复。
    ❌ 不能只活在 Python object / Runtime session / LLM context 里。

🔒 建表由本模块自带（与 selfmodel / experience 一致），
   对既有存储层**零侵入**。
🔒 本模块**只做 persistence / lifecycle**（§三十二）。
   **没有** StageEngine / WorkflowEngine / StagePlanner ——
   「怎么干」由 Agent 决定。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso
from stage.models import (
    STATUS_ACTIVE, STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED,
    STATUS_PENDING, STATUS_WAITING, VALID_FEEDBACK_SOURCES, VALID_STATUSES,
    VALID_WAITING_FOR, Checkpoint, Feedback, Stage, new_checkpoint_id,
    new_feedback_id, new_stage_id,
)

TABLE_STAGES = "production_stages"
TABLE_CHECKPOINTS = "stage_checkpoints"
TABLE_FEEDBACK = "stage_feedback"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_STAGES} (
    stage_id           TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    goal               TEXT NOT NULL,
    deliverables       TEXT NOT NULL DEFAULT '[]',
    status             TEXT NOT NULL,
    waiting_for        TEXT NOT NULL DEFAULT '',
    current_checkpoint TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_task ON {TABLE_STAGES}(task_id, status);

CREATE TABLE IF NOT EXISTS {TABLE_CHECKPOINTS} (
    checkpoint_id TEXT PRIMARY KEY,
    stage_id      TEXT NOT NULL,
    workspace_id  TEXT NOT NULL DEFAULT '',
    revision      TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    progress      TEXT NOT NULL DEFAULT '{{}}',
    metadata      TEXT NOT NULL DEFAULT '{{}}',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ckpt_stage
    ON {TABLE_CHECKPOINTS}(stage_id, created_at);

CREATE TABLE IF NOT EXISTS {TABLE_FEEDBACK} (
    feedback_id TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    task_id     TEXT NOT NULL DEFAULT '',
    stage_id    TEXT NOT NULL DEFAULT '',
    agent_id    TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{{}}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fb_stage ON {TABLE_FEEDBACK}(stage_id);
"""


class StageError(Exception):
    """Stage 层面的错误。"""


def _j(raw: Any, default: Any) -> Any:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, type(default)) else default
    except (TypeError, ValueError):
        return default


def _row_stage(r: Any) -> Stage:
    return Stage(stage_id=r["stage_id"], task_id=r["task_id"],
                 goal=r["goal"],
                 deliverables=_j(r["deliverables"], []),
                 status=r["status"], waiting_for=r["waiting_for"] or "",
                 current_checkpoint=r["current_checkpoint"] or "",
                 created_at=r["created_at"], updated_at=r["updated_at"])


def _row_ckpt(r: Any) -> Checkpoint:
    return Checkpoint(checkpoint_id=r["checkpoint_id"],
                      stage_id=r["stage_id"],
                      workspace_id=r["workspace_id"] or "",
                      revision=r["revision"] or "", reason=r["reason"] or "",
                      progress=_j(r["progress"], {}),
                      metadata=_j(r["metadata"], {}),
                      created_at=r["created_at"])


def _row_fb(r: Any) -> Feedback:
    return Feedback(feedback_id=r["feedback_id"], content=r["content"],
                    task_id=r["task_id"] or "", stage_id=r["stage_id"] or "",
                    agent_id=r["agent_id"] or "", source=r["source"],
                    metadata=_j(r["metadata"], {}),
                    created_at=r["created_at"])


class StageStore:
    """
    Stage / Checkpoint / Feedback 的持久化。

    🔒 **只处理 persistence / lifecycle。** 不做「怎么干」的编排。
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.db.transaction() as conn:
            conn.executescript(_SCHEMA)

    # ---------------- Stage ------------------------------------------
    def create_stage(self, task_id: str, goal: str, *,
                     deliverables: Optional[List[Dict[str, Any]]] = None,
                     status: str = STATUS_PENDING,
                     stage_id: Optional[str] = None) -> Stage:
        if not task_id:
            raise StageError("task_id 不能为空")
        if not goal or not str(goal).strip():
            raise StageError("goal 不能为空")
        if status not in VALID_STATUSES:
            raise StageError(f"未知 status {status!r}")
        now = utc_now_iso()
        s = Stage(stage_id=stage_id or new_stage_id(), task_id=task_id,
                  goal=str(goal).strip(),
                  deliverables=list(deliverables or []),
                  status=status, created_at=now, updated_at=now)
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_STAGES} (stage_id, task_id, goal, "
                "deliverables, status, waiting_for, current_checkpoint, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (s.stage_id, s.task_id, s.goal,
                 json.dumps(s.deliverables, ensure_ascii=False),
                 s.status, "", "", now, now))
        return s

    def get_stage(self, stage_id: str) -> Optional[Stage]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_STAGES} WHERE stage_id=?", (stage_id,))
        return _row_stage(row) if row else None

    def list_stages(self, task_id: str) -> List[Stage]:
        rows = self.db.fetchall(
            f"SELECT * FROM {TABLE_STAGES} WHERE task_id=? "
            "ORDER BY created_at", (task_id,))
        return [_row_stage(r) for r in rows]

    def current_stage(self, task_id: str) -> Optional[Stage]:
        """Task 当前**未结束**的 Stage（§八：Stage 是 Task 的细视角）。"""
        for s in self.list_stages(task_id):
            if s.is_open:
                return s
        return None

    def set_status(self, stage_id: str, status: str, *,
                   waiting_for: str = "") -> Stage:
        if status not in VALID_STATUSES:
            raise StageError(f"未知 status {status!r}")
        if waiting_for and waiting_for not in VALID_WAITING_FOR:
            raise StageError(f"未知 waiting_for {waiting_for!r}")
        if status != STATUS_WAITING:
            waiting_for = ""      # 只有 WAITING 才有等待原因
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {TABLE_STAGES} SET status=?, waiting_for=?, "
                "updated_at=? WHERE stage_id=?",
                (status, waiting_for, utc_now_iso(), stage_id))
        got = self.get_stage(stage_id)
        if got is None:
            raise StageError(f"stage {stage_id!r} 不存在")
        return got

    def activate(self, stage_id: str) -> Stage:
        return self.set_status(stage_id, STATUS_ACTIVE)

    def wait(self, stage_id: str, waiting_for: str) -> Stage:
        """
        Stage → WAITING（§九 / §二十二）。

        🔴 **原因放进 `waiting_for`，不放进状态名。**
        """
        return self.set_status(stage_id, STATUS_WAITING,
                               waiting_for=waiting_for)

    def resume(self, stage_id: str) -> Stage:
        """WAITING → ACTIVE（Wakeup 之后，§二十二）。"""
        return self.set_status(stage_id, STATUS_ACTIVE)

    def complete(self, stage_id: str) -> Stage:
        return self.set_status(stage_id, STATUS_COMPLETED)

    def fail(self, stage_id: str) -> Stage:
        return self.set_status(stage_id, STATUS_FAILED)

    def cancel(self, stage_id: str) -> Stage:
        return self.set_status(stage_id, STATUS_CANCELLED)

    # ---------------- Checkpoint（§十九–§二十一）--------------------
    def save_checkpoint(self, stage_id: str, *, reason: str = "",
                        workspace_id: str = "", revision: str = "",
                        progress: Optional[Dict[str, Any]] = None,
                        metadata: Optional[Dict[str, Any]] = None,
                        checkpoint_id: Optional[str] = None) -> Checkpoint:
        """
        🔴 **不复制 Workspace** —— 只记录 `workspace_id` + `revision`。
        """
        if self.get_stage(stage_id) is None:
            raise StageError(f"stage {stage_id!r} 不存在")
        ck = Checkpoint(
            checkpoint_id=checkpoint_id or new_checkpoint_id(),
            stage_id=stage_id, workspace_id=workspace_id or "",
            revision=str(revision or ""), reason=reason or "",
            progress=dict(progress or {}),
            metadata=dict(metadata or {}), created_at=utc_now_iso())
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_CHECKPOINTS} (checkpoint_id, stage_id, "
                "workspace_id, revision, reason, progress, metadata, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ck.checkpoint_id, ck.stage_id, ck.workspace_id,
                 ck.revision, ck.reason,
                 json.dumps(ck.progress, ensure_ascii=False),
                 json.dumps(ck.metadata, ensure_ascii=False),
                 ck.created_at))
            conn.execute(
                f"UPDATE {TABLE_STAGES} SET current_checkpoint=?, "
                "updated_at=? WHERE stage_id=?",
                (ck.checkpoint_id, ck.created_at, stage_id))
        return ck

    def get_checkpoint(self, checkpoint_id: str) -> Optional[Checkpoint]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_CHECKPOINTS} WHERE checkpoint_id=?",
            (checkpoint_id,))
        return _row_ckpt(row) if row else None

    def latest_checkpoint(self, stage_id: str) -> Optional[Checkpoint]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_CHECKPOINTS} WHERE stage_id=? "
            "ORDER BY created_at DESC LIMIT 1", (stage_id,))
        return _row_ckpt(row) if row else None

    # ---------------- Feedback（§二十四 / §二十五）------------------
    def add_feedback(self, content: str, *, task_id: str = "",
                     stage_id: str = "", agent_id: str = "",
                     source: str = "human",
                     metadata: Optional[Dict[str, Any]] = None,
                     feedback_id: Optional[str] = None) -> Feedback:
        """
        🔴 **Feedback 只落库，绝不碰 Self Model**（§二十五）。
           它要经过 Agent Cognition → Experience/Reflection 才可能影响认知。
        """
        if source not in VALID_FEEDBACK_SOURCES:
            raise StageError(
                f"未知 source {source!r}；合法：{sorted(VALID_FEEDBACK_SOURCES)}")
        if not content or not str(content).strip():
            raise StageError("feedback content 不能为空")
        fb = Feedback(feedback_id=feedback_id or new_feedback_id(),
                      content=str(content).strip(), task_id=task_id or "",
                      stage_id=stage_id or "", agent_id=agent_id or "",
                      source=source, metadata=dict(metadata or {}),
                      created_at=utc_now_iso())
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_FEEDBACK} (feedback_id, content, task_id,"
                " stage_id, agent_id, source, metadata, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fb.feedback_id, fb.content, fb.task_id, fb.stage_id,
                 fb.agent_id, fb.source,
                 json.dumps(fb.metadata, ensure_ascii=False), fb.created_at))
        return fb

    def list_feedback(self, *, stage_id: str = "",
                      task_id: str = "") -> List[Feedback]:
        if stage_id:
            rows = self.db.fetchall(
                f"SELECT * FROM {TABLE_FEEDBACK} WHERE stage_id=? "
                "ORDER BY created_at", (stage_id,))
        elif task_id:
            rows = self.db.fetchall(
                f"SELECT * FROM {TABLE_FEEDBACK} WHERE task_id=? "
                "ORDER BY created_at", (task_id,))
        else:
            rows = self.db.fetchall(f"SELECT * FROM {TABLE_FEEDBACK} "
                                    "ORDER BY created_at")
        return [_row_fb(r) for r in rows]

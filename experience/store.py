"""
Experience 持久化（§二十三）
============================================================

    ActionResult → Experience → **persisted**（重启后仍在）

🔒 本阶段**不做**复杂 Experience Retrieval（§二十三）。
   只保证「记下来、活得下去、能按 agent 列出来」。

🔴 **本模块不做任何解读**：
   它只存「发生了什么」。**不**降 confidence、**不**改 Strategy、
   **不**推断「所以以后应该…」。那些必须经过 Agent Reflection + 受控写入。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso
from experience.models import (
    SOURCE_ACTION_RESULT, VALID_SOURCES, Experience, new_experience_id,
)

TABLE_EXPERIENCES = "experiences"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_EXPERIENCES} (
    experience_id TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    source        TEXT NOT NULL,
    event_ref     TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '{{}}',
    feedback      TEXT NOT NULL DEFAULT '',
    scope         TEXT NOT NULL DEFAULT '{{}}',
    metadata      TEXT NOT NULL DEFAULT '{{}}',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_agent
    ON {TABLE_EXPERIENCES}(agent_id, created_at);
"""


def _experience_compatible(scope: Any, ctx: Dict[str, Any]) -> bool:
    """
    硬兼容：**只**把 world / domain 的不一致当作排除理由。

    🔒 与 selfmodel 的 Scope 同一条原则：「优先看到」不是「只看到」。
       situation 不一致的经历仍然可被检索（只是排序靠后）。
    """
    scope = scope or {}
    for key in ("world", "domain"):
        want = ctx.get(key)
        got = scope.get(key)
        if want is None or got is None:
            continue
        if str(got).strip().lower() != str(want).strip().lower():
            return False
    return True


def _neg(value: Any) -> str:
    """时间倒序用的反转字符串。"""
    return "".join(chr(0x10FFFF - ord(c)) for c in str(value or ""))


class ExperienceError(Exception):
    """Experience 层面的错误。"""


def _row_to_experience(row: Any) -> Experience:
    def _load(raw):
        try:
            v = json.loads(raw) if isinstance(raw, str) else raw
            return v if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            return {}
    return Experience(
        experience_id=row["experience_id"], agent_id=row["agent_id"],
        source=row["source"], event_ref=row["event_ref"] or "",
        result=_load(row["result"]), feedback=row["feedback"] or "",
        scope=_load(row["scope"]), metadata=_load(row["metadata"]),
        created_at=row["created_at"],
    )


class ExperienceStore:
    """
    Experience 的持久化。**只存取，不解读。**
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.db.transaction() as conn:
            conn.executescript(_SCHEMA)

    def record(
        self,
        agent_id: str,
        source: str,
        *,
        result: Optional[Dict[str, Any]] = None,
        event_ref: str = "",
        feedback: str = "",
        scope: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        experience_id: Optional[str] = None,
    ) -> Experience:
        """记一次经历。"""
        if not agent_id:
            raise ExperienceError("agent_id 不能为空")
        if source not in VALID_SOURCES:
            raise ExperienceError(
                f"未知 source {source!r}；合法取值：{sorted(VALID_SOURCES)}")

        exp = Experience(
            experience_id=experience_id or new_experience_id(),
            agent_id=agent_id, source=source, event_ref=event_ref or "",
            result=dict(result or {}), feedback=feedback or "",
            scope=dict(scope or {}), metadata=dict(metadata or {}),
            created_at=utc_now_iso(),
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_EXPERIENCES} "
                "(experience_id, agent_id, source, event_ref, result, "
                " feedback, scope, metadata, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (exp.experience_id, exp.agent_id, exp.source, exp.event_ref,
                 json.dumps(exp.result, ensure_ascii=False),
                 exp.feedback,
                 json.dumps(exp.scope, ensure_ascii=False),
                 json.dumps(exp.metadata, ensure_ascii=False),
                 exp.created_at),
            )
        return exp

    def record_action_result(
        self,
        agent_id: str,
        intent: Any,
        action_result: Any,
        *,
        scope: Optional[Dict[str, Any]] = None,
    ) -> Experience:
        """
        **§五：ActionResult → Experience。**

        🔒 这里只做**翻译**（把结果搬进 Experience），
           不做任何解读 —— 不降 confidence、不改 Strategy。

        🔴 M1：额外把 Executor **真的给出**的机器可读错误码
           （`ActionResult.metadata["code"]`）原样带上 —— 它是
           「为什么失败」的结构化事实，旧实现把它丢在内存里。

           ⚠️ **没有就不写这个键** —— 不补 `None`、不猜一个默认码。
              这样没有 code 的旧记录形状**一个字节都不变**。
        """
        result: Dict[str, Any] = {
            "success": bool(getattr(action_result, "success", False)),
            "output": getattr(action_result, "output", None),
            "error": getattr(action_result, "error", None),
            "action_type": getattr(intent, "action_type", ""),
            "parameters": dict(getattr(intent, "parameters", {}) or {}),
        }
        code = (dict(getattr(action_result, "metadata", None) or {})
                .get("code"))
        if code:
            result["code"] = code

        return self.record(
            agent_id,
            SOURCE_ACTION_RESULT,
            event_ref=getattr(intent, "intent_id", "") or "",
            result=result,
            scope=scope,
            metadata={"intent_id": getattr(intent, "intent_id", "")},
        )

    def retrieve_relevant(
        self,
        agent_id: str,
        *,
        world: Optional[str] = None,
        domain: Optional[str] = None,
        situation: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 5,
    ) -> List[Experience]:
        """
        **选择**相关的历史经历（§四 / §五）。

        🔒 它只负责：「哪些历史经历值得被 Agent 看到」。
           它**不**负责：「这个经历是否应该导致 Strategy 修改」（§六）。

           例如这里**没有**、也**不许有**：
               if failure_count > 3: 触发 abandon_strategy
           —— 那就是 Rule。

        §二十五：**跨 Agent 隔离** —— 只返回 `agent_id` 自己的经历。
        §二十六：Top-K 有限选择，**不**做 embedding / 摘要引擎。

        排序：情境匹配度 → 时间倒序（近期优先）。
        情境字段为空时不筛该维度（`world/domain/situation` 都可为 None）。
        """
        sql = f"SELECT * FROM {TABLE_EXPERIENCES} WHERE agent_id=?"
        params: List[Any] = [agent_id]
        if source:
            sql += " AND source=?"
            params.append(source)
        rows = self.db.fetchall(sql, tuple(params))
        items = [_row_to_experience(r) for r in rows]

        ctx = {"world": world, "domain": domain, "situation": situation}
        if any(v is not None for v in ctx.values()):
            items = [e for e in items
                     if _experience_compatible(e.scope, ctx)]

        def relevance(e: Experience) -> int:
            score = 0
            for key, want in ctx.items():
                got = (e.scope or {}).get(key)
                if want is None or got is None:
                    continue
                if str(got).strip().lower() == str(want).strip().lower():
                    score += 1
            return score

        items.sort(key=lambda e: (-relevance(e), _neg(e.created_at)))
        return items[:max(0, int(limit))]

    def get(self, experience_id: str) -> Optional[Experience]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_EXPERIENCES} WHERE experience_id=?",
            (experience_id,))
        return _row_to_experience(row) if row else None

    def list(self, agent_id: str, *, limit: int = 50) -> List[Experience]:
        rows = self.db.fetchall(
            f"SELECT * FROM {TABLE_EXPERIENCES} WHERE agent_id=? "
            "ORDER BY created_at DESC LIMIT ?", (agent_id, int(limit)))
        return [_row_to_experience(r) for r in rows]

    def count(self, agent_id: Optional[str] = None) -> int:
        if agent_id:
            row = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_EXPERIENCES} "
                "WHERE agent_id=?", (agent_id,))
        else:
            row = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_EXPERIENCES}")
        return int(row["n"] if row else 0)

"""
DelegationStore —— 委托的持久化（唯一边界）
========================================================

沿用 `stage/store.py` 的做法：模块自带 schema，构造时 `ensure_schema()`。

【🔒 表里没有 balance 列（§十一）】

    余额只能来自 Ledger。这里存的是**委托关系与状态**，
    不是经济事实本身。实际的钱只在 `ledger_transactions` 里。
"""

from __future__ import annotations

import sqlite3
from typing import Any, List, Optional

from core.identity import utc_now_iso
from delegation.models import (
    STATUS_OFFERED, DelegationOffer, new_offer_id,
)

TABLE_DELEGATIONS = "delegation_offers"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_DELEGATIONS} (
    offer_id             TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,

    requester_agent_id   TEXT NOT NULL,
    provider_agent_id    TEXT NOT NULL,

    description          TEXT NOT NULL DEFAULT '',
    price_aic            REAL,
    currency             TEXT NOT NULL DEFAULT 'AIC',

    status               TEXT NOT NULL,

    deliverable_ref      TEXT NOT NULL DEFAULT '',
    rejected_by          TEXT NOT NULL DEFAULT '',
    reject_reason        TEXT NOT NULL DEFAULT '',

    -- 🔒 结算的唯一凭据是 Ledger 交易号。
    --    这里**只存引用**，不存金额副本 —— 金额以账本为准。
    settled_tx_id        TEXT NOT NULL DEFAULT '',

    created_at           TEXT NOT NULL,
    accepted_at          TEXT NOT NULL DEFAULT '',
    completed_at         TEXT NOT NULL DEFAULT '',
    settled_at           TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_delegation_task
    ON {TABLE_DELEGATIONS} (task_id);
CREATE INDEX IF NOT EXISTS idx_delegation_requester
    ON {TABLE_DELEGATIONS} (requester_agent_id);
CREATE INDEX IF NOT EXISTS idx_delegation_provider
    ON {TABLE_DELEGATIONS} (provider_agent_id);
CREATE INDEX IF NOT EXISTS idx_delegation_status
    ON {TABLE_DELEGATIONS} (status);
"""


def _row_to_offer(row: sqlite3.Row) -> DelegationOffer:
    return DelegationOffer(
        offer_id=row["offer_id"], task_id=row["task_id"],
        requester_agent_id=row["requester_agent_id"],
        provider_agent_id=row["provider_agent_id"],
        description=row["description"],
        price_aic=row["price_aic"], currency=row["currency"],
        status=row["status"], deliverable_ref=row["deliverable_ref"],
        rejected_by=row["rejected_by"], reject_reason=row["reject_reason"],
        settled_tx_id=row["settled_tx_id"], created_at=row["created_at"],
        accepted_at=row["accepted_at"], completed_at=row["completed_at"],
        settled_at=row["settled_at"],
    )


class DelegationStore:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.db.transaction() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------
    def create(
        self,
        *,
        task_id: str,
        requester_agent_id: str,
        provider_agent_id: str,
        description: str = "",
        price_aic: Optional[float] = None,
        currency: str = "AIC",
        offer_id: Optional[str] = None,
    ) -> DelegationOffer:
        offer_id = offer_id or new_offer_id()
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_DELEGATIONS} ("
                "  offer_id, task_id, requester_agent_id, provider_agent_id,"
                "  description, price_aic, currency, status, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (offer_id, task_id, requester_agent_id, provider_agent_id,
                 description, price_aic, currency, STATUS_OFFERED, now),
            )
        got = self.get(offer_id)
        assert got is not None
        return got

    def get(self, offer_id: str) -> Optional[DelegationOffer]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_DELEGATIONS} WHERE offer_id=?", (offer_id,))
        return _row_to_offer(row) if row else None

    def list_offers(
        self,
        *,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[DelegationOffer]:
        sql = f"SELECT * FROM {TABLE_DELEGATIONS} WHERE 1=1"
        params: list = []
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if agent_id:
            sql += " AND (requester_agent_id = ? OR provider_agent_id = ?)"
            params += [agent_id, agent_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at LIMIT ?"
        params.append(int(limit))
        return [_row_to_offer(r) for r in self.db.fetchall(sql, tuple(params))]

    def update(self, offer_id: str, **fields: Any) -> DelegationOffer:
        """
        更新若干列。**空 fields 会被拒绝** ——

        🔒 一次「什么都没改」的 update 会返回一个看起来成功的对象，
           那正是本项目反复栽的 silent no-op。
        """
        allowed = {
            "price_aic", "status", "deliverable_ref", "rejected_by",
            "reject_reason", "settled_tx_id", "accepted_at", "completed_at",
            "settled_at", "description", "currency",
        }
        unknown = sorted(k for k in fields if k not in allowed)
        if unknown:
            raise ValueError(f"不可更新的列：{unknown}")
        if not fields:
            raise ValueError("update() 没有给任何字段")

        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {TABLE_DELEGATIONS} SET {sets} WHERE offer_id = ?",
                (*fields.values(), offer_id),
            )
        got = self.get(offer_id)
        if got is None:
            raise ValueError(f"offer {offer_id!r} 不存在")
        return got

    def count(self, *, status: Optional[str] = None) -> int:
        if status:
            row = self.db.fetchone(
                f"SELECT COUNT(*) n FROM {TABLE_DELEGATIONS} WHERE status=?",
                (status,))
        else:
            row = self.db.fetchone(
                f"SELECT COUNT(*) n FROM {TABLE_DELEGATIONS}")
        return int(row["n"]) if row else 0

"""
Task Store —— Task 的持久化
============================================================

【为什么持久化和 Task 分开两个文件】

    因为这样架构守卫才能**证明** Task Core 是纯的：

        core/task.py         → 不 import storage，不 import sqlite
                               （测试可以直接读源码检查这一点）
        core/task_store.py   → 只有这里碰数据库

    如果两者在一个文件里，「Task Core 不依赖数据库」
    就只是一句无法验证的话。

【存什么】

    tasks 表        任务主体的**当前状态**
    task_offers 表  报价历史（append-only）

【为什么不建 workspace_members / workspace_permissions / workspace_files】

    因为本步骤不实现 Workspace 权限。
    建了空表只会让人以为系统已经有那些能力。
    等真正做 Workspace Manager 时再建。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.identity import new_message_id, utc_now_iso
from core.logging_setup import get_logger
from core.money import Money
from core.task import Offer, Task, TaskStatus
from storage.database import Database
from storage.models import TABLE_TASK_OFFERS, TABLE_TASKS

logger = get_logger(__name__)


class TaskStore:
    """
    Task 的读写。

    用法：
        store = TaskStore(db)

        task = Task.create(creator="agent_a", budget=Money(100))
        store.save(task)

        loaded = store.get(task.task_id)
        all_tasks = store.list_tasks()
        store.record_offer(task.task_id, OfferType.TASK_OFFER, Money(100))

    【注意】save() 会把 Task 的**当前状态**写进去（UPSERT）。
    Task 对象本身仍然是内存里的事实来源；数据库是它的快照。
    """

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------
    # 写
    # ------------------------------------------------------------
    def save(self, task: Task) -> Task:
        """
        保存 / 更新一个任务。

        用 UPSERT —— 因为同一个任务会被反复保存（每次状态变化）。
        主键是 task_id，所以不会出现重复行。
        """
        if not isinstance(task, Task):
            raise TypeError(f"save() 需要 Task，收到 {type(task).__name__}")

        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_TASKS} (
                    task_id, current_trace_id, creator, contractor,
                    participants, status, description, workspace_id,
                    budget_amount, budget_currency,
                    agreed_price_amount, agreed_price_currency,
                    acceptance, expires_at,
                    created_at, updated_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    current_trace_id      = excluded.current_trace_id,
                    contractor            = excluded.contractor,
                    participants          = excluded.participants,
                    status                = excluded.status,
                    description           = excluded.description,
                    workspace_id          = excluded.workspace_id,
                    budget_amount         = excluded.budget_amount,
                    budget_currency       = excluded.budget_currency,
                    agreed_price_amount   = excluded.agreed_price_amount,
                    agreed_price_currency = excluded.agreed_price_currency,
                    acceptance            = excluded.acceptance,
                    expires_at            = excluded.expires_at,
                    updated_at            = excluded.updated_at,
                    note                  = excluded.note
                """,
                (
                    task.task_id,
                    task.current_trace_id,
                    task.creator,
                    task.contractor,
                    json.dumps(task.participants, ensure_ascii=False),
                    task.status,
                    task.description,
                    task.workspace_id,
                    task.budget.amount if task.budget else None,
                    task.budget.currency if task.budget else None,
                    task.agreed_price.amount if task.agreed_price else None,
                    task.agreed_price.currency if task.agreed_price else None,
                    (
                        json.dumps(task.acceptance, ensure_ascii=False)
                        if task.acceptance else None
                    ),
                    task.expires_at,
                    task.created_at,
                    task.updated_at,
                    task.note,
                ),
            )

            # 报价历史也一起同步（幂等：offer_id 是主键）
            for offer in task.offers:
                self._upsert_offer(conn, task.task_id, offer)

        logger.debug("已保存任务 %s（状态 %s）", task.task_id, task.status)
        return task

    def _upsert_offer(self, conn, task_id: str, offer: Offer) -> None:
        conn.execute(
            f"""
            INSERT INTO {TABLE_TASK_OFFERS} (
                offer_id, task_id, offer_type, amount, currency,
                message_id, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_id) DO NOTHING
            """,
            (
                offer.offer_id,
                task_id,
                offer.offer_type,
                offer.amount.amount,
                offer.amount.currency,
                offer.message_id,
                offer.note,
                offer.created_at,
            ),
        )

    def record_offer(
        self,
        task_id: str,
        offer_type: str,
        amount: Money,
        message_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Offer:
        """
        只记一条报价，不碰任务主体。

        【为什么会需要单独这个方法】
            因为「收到一个报价」和「任务状态变化」是两件事。
            有些报价只是记录（比如对方还了个价但你还没决定），
            这时不该动任务状态。

            如果要「记录并改状态」，请用 task.record_offer() + store.save(task)。
        """
        self.require(task_id)

        offer = Offer(
            offer_type=offer_type,
            amount=amount,
            message_id=message_id,
            note=note,
        )

        with self.db.transaction() as conn:
            self._upsert_offer(conn, task_id, offer)

        return offer

    def update_trace(self, task_id: str, trace_id: str) -> Task:
        """
        换掉当前 trace（比如任务失败后重跑，开始新的一次执行）。

        【注意】这里只动 current_trace_id，不动 task_id ——
        这正是两者分离的用途：同一个任务可以有多次执行，
        每次一个新 trace，但任务身份不变。
        """
        task = self.require(task_id)
        task.current_trace_id = trace_id
        task.updated_at = utc_now_iso()
        return self.save(task)

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------
    def get(self, task_id: str) -> Optional[Task]:
        """按 ID 取任务。不存在返回 None。"""
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_TASKS} WHERE task_id = ?", (str(task_id),)
        )
        if row is None:
            return None
        return self._row_to_task(row)

    def require(self, task_id: str) -> Task:
        """按 ID 取任务。不存在抛 KeyError（明确比 None 好排查）。"""
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"找不到任务：{task_id}")
        return task

    def exists(self, task_id: str) -> bool:
        row = self.db.fetchone(
            f"SELECT 1 FROM {TABLE_TASKS} WHERE task_id = ?", (str(task_id),)
        )
        return row is not None

    def list_tasks(
        self,
        status: Optional[str] = None,
        creator: Optional[str] = None,
        contractor: Optional[str] = None,
        workspace_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Task]:
        """按条件列出任务。"""
        conditions = []
        params: list = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if creator:
            conditions.append("creator = ?")
            params.append(creator)
        if contractor:
            conditions.append("contractor = ?")
            params.append(contractor)
        if workspace_id:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(int(limit))

        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_TASKS}
            {where}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [self._row_to_task(row) for row in rows]

    def offers_for(self, task_id: str) -> List[Offer]:
        """取一个任务的完整报价历史（按时间正序）。"""
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_TASK_OFFERS}
             WHERE task_id = ?
             ORDER BY created_at ASC, rowid ASC
            """,
            (str(task_id),),
        )
        return [
            Offer(
                offer_id=row["offer_id"],
                offer_type=row["offer_type"],
                amount=Money(row["amount"], row["currency"]),
                message_id=row["message_id"],
                note=row["note"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def count(self, status: Optional[str] = None) -> int:
        if status:
            row = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_TASKS} WHERE status = ?",
                (status,),
            )
        else:
            row = self.db.fetchone(f"SELECT COUNT(*) AS n FROM {TABLE_TASKS}")
        return int(row["n"]) if row else 0

    def count_offers(self) -> int:
        row = self.db.fetchone(f"SELECT COUNT(*) AS n FROM {TABLE_TASK_OFFERS}")
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------
    def _row_to_task(self, row) -> Task:
        """
        数据库行 → Task 对象。

        【注意】这里用 Task(...) 而不是 Task.create() ——
        因为这是「恢复一个已存在的任务」，它的状态不是 CREATED。
        所以必须能指定 status。这是唯一允许指定初始状态的路径。
        """
        return Task(
            task_id=row["task_id"],
            creator=row["creator"],
            description=row["description"] or "",
            status=row["status"],
            current_trace_id=row["current_trace_id"],
            workspace_id=row["workspace_id"],
            budget=self._money_from(row["budget_amount"], row["budget_currency"]),
            agreed_price=self._money_from(
                row["agreed_price_amount"], row["agreed_price_currency"]
            ),
            contractor=row["contractor"],
            participants=self._json_or(row["participants"], []),
            acceptance=self._json_or(row["acceptance"], None),
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            offers=self.offers_for(row["task_id"]),
            note=row["note"],
        )

    @staticmethod
    def _money_from(amount, currency) -> Optional[Money]:
        """金额和货币必须同时存在，否则视为没有。"""
        if amount is None or currency is None:
            return None
        return Money(amount, currency)

    @staticmethod
    def _json_or(raw, fallback):
        if raw is None:
            return fallback
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return fallback

    # ------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """按状态汇总。"""
        rows = self.db.fetchall(
            f"SELECT status, COUNT(*) AS n FROM {TABLE_TASKS} GROUP BY status"
        )
        by_status = {row["status"]: int(row["n"]) for row in rows}

        return {
            "tasks": self.count(),
            "offers": self.count_offers(),
            "by_status": by_status,
            "workspaces": len({
                row["workspace_id"]
                for row in self.db.fetchall(
                    f"SELECT DISTINCT workspace_id FROM {TABLE_TASKS} "
                    f"WHERE workspace_id IS NOT NULL"
                )
            }),
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "store": type(self).__name__,
            "tasks": self.count(),
            "offers": self.count_offers(),
        }

    def __repr__(self) -> str:
        return f"TaskStore(tasks={self.count()}, offers={self.count_offers()})"

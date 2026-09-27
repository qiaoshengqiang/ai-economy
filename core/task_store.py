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
from core.task_agreement import TaskAgreementRecord
from core.task_execution import (
    ExecutionEvent,
    TaskExecutionRecord,
    TaskResultRef,
    TaskVerification,
)
from storage.database import Database
from storage.models import (
    TABLE_TASK_AGREEMENTS,
    TABLE_TASK_EXECUTIONS,
    TABLE_TASK_OFFERS,
    TABLE_TASKS,
)

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
                    acceptance, required_capabilities, expires_at,
                    created_at, updated_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    required_capabilities = excluded.required_capabilities,
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
                    json.dumps(
                        task.required_capabilities.to_list(), ensure_ascii=False
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
                message_id, reply_to, from_agent, trace_id, conversation_id,
                note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(offer_id) DO NOTHING
            """,
            (
                offer.offer_id,
                task_id,
                offer.offer_type,
                offer.amount.amount,
                offer.amount.currency,
                offer.message_id,
                offer.reply_to,
                offer.from_agent,
                offer.trace_id,
                offer.conversation_id,
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
        reply_to: Optional[str] = None,
        from_agent: Optional[str] = None,
        trace_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
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
            reply_to=reply_to,
            from_agent=from_agent,
            trace_id=trace_id,
            conversation_id=conversation_id,
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
                reply_to=row["reply_to"],
                from_agent=row["from_agent"],
                trace_id=row["trace_id"],
                conversation_id=row["conversation_id"],
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

    # ------------------------------------------------------------
    # 成交凭证（Step 4C）
    # ------------------------------------------------------------
    def record_agreement(
        self, agreement: TaskAgreementRecord
    ) -> TaskAgreementRecord:
        """
        记一份成交凭证。

        【append-only】
            和账本一样只 INSERT，不改写 —— 历史不允许被改写。
            同一个任务理论上可以有多份（先谈成、取消、再谈成），
            所以不做唯一约束，需要时取最新的一份。

        【重复写入是幂等的】
            agreement_id 是主键，重复写同一条不会产生第二行。
        """
        if not isinstance(agreement, TaskAgreementRecord):
            raise TypeError(
                f"record_agreement 需要 TaskAgreementRecord，"
                f"收到 {type(agreement).__name__}"
            )

        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_TASK_AGREEMENTS} (
                    agreement_id, task_id, initiator, contractor,
                    price_amount, price_currency,
                    accepted_offer_message_id, trace_id, conversation_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agreement_id) DO NOTHING
                """,
                (
                    agreement.agreement_id,
                    agreement.task_id,
                    agreement.initiator,
                    agreement.contractor,
                    agreement.agreed_price.amount,
                    agreement.agreed_price.currency,
                    agreement.accepted_offer_message_id,
                    agreement.trace_id,
                    agreement.conversation_id,
                    agreement.created_at,
                ),
            )

        logger.debug(
            "已记录成交凭证 %s（%s @ %s）",
            agreement.agreement_id, agreement.task_id, agreement.agreed_price,
        )
        return agreement

    def agreements_for(self, task_id: str) -> List[TaskAgreementRecord]:
        """取一个任务的全部成交凭证（按时间正序）。"""
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_TASK_AGREEMENTS}
             WHERE task_id = ?
             ORDER BY created_at ASC, rowid ASC
            """,
            (str(task_id),),
        )
        return [self._row_to_agreement(row) for row in rows]

    def latest_agreement(self, task_id: str) -> Optional[TaskAgreementRecord]:
        """当前有效的那份成交凭证（最新一份）。"""
        agreements = self.agreements_for(task_id)
        return agreements[-1] if agreements else None

    def count_agreements(self) -> int:
        row = self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM {TABLE_TASK_AGREEMENTS}"
        )
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_agreement(row) -> TaskAgreementRecord:
        return TaskAgreementRecord(
            agreement_id=row["agreement_id"],
            task_id=row["task_id"],
            initiator=row["initiator"],
            contractor=row["contractor"],
            agreed_price=Money(row["price_amount"], row["price_currency"]),
            accepted_offer_message_id=row["accepted_offer_message_id"],
            trace_id=row["trace_id"],
            conversation_id=row["conversation_id"],
            created_at=row["created_at"],
        )

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
            required_capabilities=self._json_or(
                row["required_capabilities"], []
            ),
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
            "agreements": self.count_agreements(),
            "by_status": by_status,
            "workspaces": len({
                row["workspace_id"]
                for row in self.db.fetchall(
                    f"SELECT DISTINCT workspace_id FROM {TABLE_TASKS} "
                    f"WHERE workspace_id IS NOT NULL"
                )
            }),
        }

    # ============================================================
    # 执行事件（Step 4D）
    #
    # 【为什么这组方法在这里，而不是在 TaskExecutionService 里】
    #     因为 TaskStore 是数据库的**唯一**边界。
    #     应用服务只负责业务规则，不该知道有 SQLite 这回事。
    #     有架构守卫盯着：services/ 里不许出现 sqlite3 / INSERT。
    # ============================================================
    def record_execution(
        self, record: TaskExecutionRecord
    ) -> TaskExecutionRecord:
        """
        记一条执行事件。

        【append-only】
            只 INSERT，不改写 —— 执行历史不允许被改写。

        【幂等是结构性的，不是靠 if 判断】
            message_id 是主键，重复写同一条会撞主键被忽略。
            所以「同一条 SUBMIT 送达两次」在**数据库层面**
            就不可能产生两条记录 ——
            不依赖应用层是否记得先做幂等检查。
            （应用层仍然会先检查，那是为了给出友好的幂等响应。）
        """
        if not isinstance(record, TaskExecutionRecord):
            raise TypeError(
                f"record_execution 需要 TaskExecutionRecord，"
                f"收到 {type(record).__name__}"
            )

        result = record.result
        verification = record.verification

        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_TASK_EXECUTIONS} (
                    message_id, task_id, event_type,
                    result_type, result_ref, result_summary, result_metadata,
                    passed, feedback,
                    reply_to, from_agent, trace_id, conversation_id,
                    status_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (
                    record.message_id,
                    record.task_id,
                    record.event_type,
                    result.result_type if result else None,
                    result.result_ref if result else None,
                    result.summary if result else None,
                    (
                        json.dumps(dict(result.metadata), ensure_ascii=False)
                        if result and result.metadata else None
                    ),
                    (
                        None if verification is None
                        else (1 if verification.passed else 0)
                    ),
                    verification.feedback if verification else None,
                    record.reply_to,
                    record.from_agent,
                    record.trace_id,
                    record.conversation_id,
                    record.status_after,
                    record.created_at,
                ),
            )

        logger.debug(
            "记录执行事件：%s %s（message=%s）",
            record.event_type, record.task_id, record.message_id,
        )
        return record

    def executions_for(self, task_id: str) -> List[TaskExecutionRecord]:
        """
        这个任务的全部执行事件，按时间顺序。

        【为什么返回**全部**而不是「最新一条」】
            返工流程下会有多组 SUBMIT / VERIFY。
            「上一轮为什么不通过」正是承接方最需要的信息 ——
            只给最新一条就把它丢了。
        """
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {TABLE_TASK_EXECUTIONS}
                WHERE task_id = ?
                ORDER BY created_at, rowid
                """,
                (task_id,),
            ).fetchall()

        return [self._row_to_execution(row) for row in rows]

    def find_execution_by_message(
        self, message_id: str
    ) -> Optional[TaskExecutionRecord]:
        """
        按 message_id 找执行事件 —— 这就是幂等检查的依据。

        【注意它和 Task.find_offer_by_message 的区别】
            报价历史在 Task 对象里也能按 message_id 查，
            而执行事件直接在库里查（Task 上不存执行记录）。
            原因见 storage/models.py 里 task_executions 的注释。
        """
        if not message_id:
            return None

        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {TABLE_TASK_EXECUTIONS}
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()

        return self._row_to_execution(row) if row else None

    def submissions_for(self, task_id: str) -> List[TaskExecutionRecord]:
        """这个任务的全部提交，按时间顺序。"""
        return [
            record for record in self.executions_for(task_id)
            if record.event_type == ExecutionEvent.SUBMIT
        ]

    def verifications_for(self, task_id: str) -> List[TaskExecutionRecord]:
        """这个任务的全部验收结论，按时间顺序。"""
        return [
            record for record in self.executions_for(task_id)
            if record.event_type == ExecutionEvent.VERIFY
        ]

    def latest_submission(
        self, task_id: str
    ) -> Optional[TaskExecutionRecord]:
        """最近一次提交。"""
        submissions = self.submissions_for(task_id)
        return submissions[-1] if submissions else None

    def latest_verification(
        self, task_id: str
    ) -> Optional[TaskExecutionRecord]:
        """
        最近一次验收结论。

        【它有实际用途】
            承接到一条 SUBMIT 时，发起方往往想知道
            「上一轮是不是因为某个原因被打回过」——
            直接查这个比翻整条历史方便。
        """
        verifications = self.verifications_for(task_id)
        return verifications[-1] if verifications else None

    def attempt_count(self, task_id: str) -> int:
        """
        这是第几轮执行。

        【为什么是**算出来**的，而不是存在字段里】
            如果存一个 attempt 字段，它就可能和实际事件数漂移
            （某条记录写失败、或者有人手工改了库）。
            数 SUBMIT 的条数永远不会和事实不一致。

            而且这样也就不用发明 revision_id / round_id ——
            「第几轮」是历史的函数，不是一个新的身份。
        """
        return len(self.submissions_for(task_id))

    def count_executions(self, task_id: Optional[str] = None) -> int:
        """执行事件总数（可限定某个任务）。"""
        with self.db.transaction() as conn:
            if task_id is None:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {TABLE_TASK_EXECUTIONS}"
                ).fetchone()
            else:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS n FROM {TABLE_TASK_EXECUTIONS}
                    WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_execution(row: Any) -> TaskExecutionRecord:
        """数据库行 → TaskExecutionRecord。"""
        raw_metadata = row["result_metadata"]
        metadata = json.loads(raw_metadata) if raw_metadata else {}

        result = None
        if row["result_ref"] is not None:
            result = TaskResultRef(
                result_type=row["result_type"] or "artifact",
                result_ref=row["result_ref"],
                summary=row["result_summary"] or "",
                metadata=metadata,
            )

        verification = None
        if row["passed"] is not None:
            verification = TaskVerification(
                passed=bool(row["passed"]),
                feedback=row["feedback"] or "",
            )

        return TaskExecutionRecord(
            message_id=row["message_id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            result=result,
            verification=verification,
            reply_to=row["reply_to"],
            from_agent=row["from_agent"],
            trace_id=row["trace_id"],
            conversation_id=row["conversation_id"],
            created_at=row["created_at"],
            status_after=row["status_after"],
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "store": type(self).__name__,
            "tasks": self.count(),
            "offers": self.count_offers(),
        }

    def __repr__(self) -> str:
        return f"TaskStore(tasks={self.count()}, offers={self.count_offers()})"

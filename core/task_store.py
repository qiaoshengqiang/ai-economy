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


# ============================================================
# 并发冲突
# ============================================================
class ConcurrentTaskModification(Exception):
    """
    保存一个 Task 时发现它已经被别人改过了。

    【它不是 Task 的一种状态】
        并发冲突是 persistence / application 层的问题，
        不是任务生命周期的一个阶段。

        所以：
            ❌ 不新增 CONFLICT 状态
            ❌ 不新增 PROCESSING 状态
            ❌ 不改状态机
            ✅ 抛一个明确的异常，让调用方知道「你手上这份过期了」

    【为什么必须抛，不能静默覆盖】
        静默覆盖的后果是：
            A 读到 task（v5），改成 SUBMITTED
            B 读到 task（v5），改成 VERIFIED
            B 先写成功；A 后写 → 把 VERIFIED 覆盖回 SUBMITTED
            两边都以为自己成功了，没人发现数据已经错了。

        这种 bug **不会报错**，只会让状态悄悄变错 ——
        所以宁可让调用方拿到一个异常，也不要让它静默发生。

    【本阶段不自动重试】
        默认**不**自动重新读取 + 重放。
        原因：状态变化是有语义的，自动重放可能产生
        重复的状态变化、重复的消息、或者替 Agent 做了决定。

        发现冲突是本阶段的目标；自动解决冲突不是。

        （但这里把两边的 version 和状态都带上，
          方便调用方或日志判断到底发生了什么。）
    """

    def __init__(
        self,
        task_id: str,
        expected_version: int,
        actual_version: Optional[int] = None,
        detail: str = "",
    ):
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version

        message = (
            f"任务 {task_id} 已被并发修改："
            f"我手上是 version={expected_version}"
        )
        if actual_version is not None:
            message += f"，数据库里已经是 version={actual_version}"
        message += (
            f"。\n（这份 Task 已经过期了。为了避免静默覆盖别人的修改，"
            f"保存被拒绝。请重新读取最新版本再操作。）"
        )
        if detail:
            message += f"\n{detail}"

        super().__init__(message)


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
    def save(self, task: Task, *, force: bool = False) -> Task:
        """
        保存 / 更新一个任务。**带乐观锁**。

        ============================================================
        【Step 4D.1 改了什么】
        ============================================================

            原来是**无条件 UPSERT**：

                INSERT ... ON CONFLICT(task_id) DO UPDATE SET ...

            谁后写谁覆盖，而且两边都以为自己成功了。
            这种 bug 不报错，只让数据悄悄变错。

            现在分两条路：

                新任务（数据库里没有）
                    → INSERT，version = 1

                已存在的任务
                    → UPDATE ... WHERE task_id=? AND version=?
                      影响行数 1 → 成功，version 推进
                      影响行数 0 → 有人先改了 → 抛 ConcurrentTaskModification

        【为什么要先分新老，而不是写一条 SQL 搞定】

            UPSERT 的 UPDATE 分支**没法表达**「version 不匹配就失败」——
            它要么更新、要么什么都不做，而「什么都不做」和
            「任务不存在」在 UPSERT 里是同一个结果，分不出来。

            所以显式分两步，让「新增」和「更新」各自语义清楚。
            代价是多一次 SELECT（在同一个事务里）。

        【force=True 是什么】

            给迁移、测试、以及「我确实要覆盖」的场景留的口子。
            它必须显式传 —— 默认行为永远是「冲突就报错」。

        【注意：这里是事务安全的】

            SELECT + UPDATE + 读回 version 全在**同一个事务**里。
            而且 db.transaction() 是可重入的，
            所以调用方可以把 save() 和别的事（比如记执行事件）
            包在同一个事务里 —— 这正是 4D.1 的第三个目标。
        """
        if not isinstance(task, Task):
            raise TypeError(f"save() 需要 Task，收到 {type(task).__name__}")

        with self.db.transaction() as conn:
            existing = conn.execute(
                f"SELECT version FROM {TABLE_TASKS} WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()

            if existing is None:
                # ---- 新任务：直接插入，版本从 1 开始 ----
                #
                # 为什么不是 0？
                #     0 表示「还没被保存过」。
                #     保存成功之后就应该是 1 —— 这样
                #     「数据库里的 version」永远等于「保存过几次」。
                new_version = 1
                self._insert_task(conn, task, new_version)
            else:
                current = int(existing["version"])

                if not force and current != task.version:
                    # ---- 冲突：绝不静默覆盖 ----
                    raise ConcurrentTaskModification(
                        task_id=task.task_id,
                        expected_version=task.version,
                        actual_version=current,
                        detail=(
                            f"（我手上的状态是 {task.status}，"
                            f"没有写入任何东西。）"
                        ),
                    )

                new_version = current + 1
                # force=True 时**不做**版本比对 ——
                # 调用方明确说了「我就是要覆盖」。
                # 否则会走进一个荒谬的状态：跳过了检查，
                # 却在 UPDATE 的 WHERE 里被拦下来。
                self._update_task(
                    conn, task, new_version, check_version=not force
                )

            # 报价历史也一起同步（幂等：offer_id 是主键）
            for offer in task.offers:
                self._upsert_offer(conn, task.task_id, offer)

            # 保存成功才把手里这份的版本推进到新值 ——
            # 这样同一个 Task 对象可以连续 save() 多次。
            task.version = new_version

        logger.debug(
            "已保存任务 %s（状态 %s，version %s）",
            task.task_id, task.status, task.version,
        )
        return task

    # ------------------------------------------------------------
    # save() 的两条具体路径
    # ------------------------------------------------------------
    def _insert_task(self, conn, task: Task, version: int) -> None:
        """插入一个全新的任务。"""
        conn.execute(
            f"""
            INSERT INTO {TABLE_TASKS} (
                task_id, current_trace_id, creator, contractor,
                participants, status, description, workspace_id,
                budget_amount, budget_currency,
                agreed_price_amount, agreed_price_currency,
                acceptance, required_capabilities, expires_at,
                created_at, updated_at, note, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*self._task_columns(task), version),
        )

    def _update_task(
        self, conn, task: Task, version: int, check_version: bool = True
    ) -> None:
        """
        更新一个已存在的任务，条件里带上 version（compare-and-set）。

        【WHERE version = ? 是这里最关键的一行】
            它让「读到的版本」和「写回时的版本」必须一致。
            影响行数为 0 时说明有人在这中间改过了。

        【check_version=False 是什么】
            给 save(force=True) 用的 —— 跳过版本条件，
            但仍然按照「数据库里的当前版本 + 1」来推进版本号，
            所以覆盖之后版本号不会倒退。
        """
        where = "WHERE task_id = ? AND version = ?" if check_version else "WHERE task_id = ?"
        params_tail = (
            (task.task_id, task.version) if check_version else (task.task_id,)
        )

        cursor = conn.execute(
            f"""
            UPDATE {TABLE_TASKS} SET
                current_trace_id      = ?,
                contractor            = ?,
                participants          = ?,
                status                = ?,
                description           = ?,
                workspace_id          = ?,
                budget_amount         = ?,
                budget_currency       = ?,
                agreed_price_amount   = ?,
                agreed_price_currency = ?,
                acceptance            = ?,
                required_capabilities = ?,
                expires_at            = ?,
                updated_at            = ?,
                note                  = ?,
                version               = ?
            {where}
            """,
            (
                task.current_trace_id,
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
                json.dumps(task.required_capabilities.to_list(), ensure_ascii=False),
                task.expires_at,
                task.updated_at,
                task.note,
                version,
                *params_tail,
            ),
        )

        if cursor.rowcount == 0:
            # 理论上到这里 current 已经校验过了，不该发生。
            # 但两个进程真的同时 UPDATE 时，第二个会在这里拿到 0 行 ——
            # 所以这里再兜一次，绝不静默放过。
            raise ConcurrentTaskModification(
                task_id=task.task_id,
                expected_version=task.version,
            )

    @staticmethod
    def _task_columns(task: Task) -> tuple:
        """INSERT 用的列值（顺序和 _insert_task 的 SQL 一致）。"""
        return (
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
            json.dumps(task.required_capabilities.to_list(), ensure_ascii=False),
            task.expires_at,
            task.created_at,
            task.updated_at,
            task.note,
        )

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
            # 【必须把 version 读出来 —— 否则乐观锁形同虚设】
            #
            #     乐观锁的前提是「读到的版本」和「写回时的版本」一致。
            #     如果读的时候不带 version（永远是 0），
            #     那么任何一次 get() → 改 → save() 都会被判成冲突 ——
            #     包括完全没有并发的情况。
            #
            #     （这个坑实现时就撞上了：save() 的 compare-and-set 写完之后，
            #       所有「读-改-写」都开始报冲突。原因就是这里没读 version。）
            #
            # 用 keys() 判断是为了兼容列还没补齐的极端情况 ——
            # migrate() 会给老库加上这一列，默认 0。
            version=row["version"] if "version" in row.keys() else 0,
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
        with self.db.transaction(write=False) as conn:
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

        with self.db.transaction(write=False) as conn:
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
        with self.db.transaction(write=False) as conn:
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

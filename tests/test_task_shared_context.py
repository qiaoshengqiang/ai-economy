"""
Task Shared Context / Alignment 测试（Step 4F）

覆盖规范 §21 要求的全部类别：

    基础       创建 / 读取 context、创建 / 读取 revision
    Change     有效 / 无效提议、Accepted / Rejected
    Authority  参与方可提交、非参与方不可提交
    Source of Truth
               Message 不自动进入 Context
               Private Memory 不自动覆盖 Context
               Task Core 不被 Shared Context 反向修改
    并发       真实多进程，只有一个成功、版本一致、无静默覆盖
    Persistence 写入 → 关闭 → 重开 → 仍在
    Isolation  Task-001 不污染 Task-002
    History    Revision 1/2/3 都可追溯
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from core.task import Task
from core.task_context_store import TaskContextStore, new_change_id
from core.task_shared_context import (
    AssignmentRecord,
    AssignmentStatus,
    AssumptionRecord,
    AssumptionStatus,
    ContextChange,
    ContextChangeStatus,
    ContextChangeType,
    ContextNotFoundError,
    ContextRevision,
    ContextValidationError,
    ConcurrentContextModification,
    DecisionRecord,
    InvalidContextChange,
    NotAParticipantError,
    OpenQuestion,
    QuestionStatus,
    SelfAlignmentError,
    SharedFact,
    TaskSharedContext,
    apply_change,
    utc_now_iso,
)
from core.task_store import TaskStore
from services.task_alignment_service import TaskAlignmentService
from storage.database import Database, create_memory_database
from tests.multiprocess_helpers import context_change_worker, record

A = "agent_a"
B = "agent_b"
C = "agent_c"          # 非参与方
TASK = "TASK-4F-T"


# ============================================================
# fixtures
# ============================================================
@pytest.fixture()
def db():
    database = create_memory_database()
    yield database
    database.close()


@pytest.fixture()
def tasks(db):
    return TaskStore(db)


@pytest.fixture()
def store(db):
    return TaskContextStore(db)


@pytest.fixture()
def service(db, tasks, store):
    return TaskAlignmentService(tasks, store)


@pytest.fixture()
def task(tasks):
    """一个已经建好的 Task，参与方 = A + B。"""
    created = Task.create(task_id=TASK, creator=A, description="CSV parser")
    created.participants = [A, B]
    created.contractor = B
    tasks.save(created)
    return created


@pytest.fixture()
def with_context(service, task):
    """已经建好 Revision 1 的 Task。"""
    return service.create_context(TASK, by=A, objective="实现 parser")


# ============================================================
# 一、基础：Context / Revision
# ============================================================
class TestBasic:
    def test_create_context_makes_revision_1(self, service, task):
        rev = service.create_context(TASK, by=A, objective="实现 parser")

        assert rev.revision == 1
        assert rev.parent_revision is None
        assert rev.context.objective == "实现 parser"

    def test_revision_1_is_persisted_not_implied(self, service, task, store):
        """Revision 1 必须真的进历史表 —— 否则历史会出现空洞。"""
        service.create_context(TASK, by=A)

        assert store.revision(TASK, 1) is not None
        assert store.revision_count(TASK) == 1

    def test_current_points_at_revision_1(self, service, task, store):
        service.create_context(TASK, by=A)

        assert store.current_revision_number(TASK) == 1
        assert service.current(TASK).revision == 1

    def test_cannot_create_twice(self, service, task, with_context):
        with pytest.raises(ContextValidationError, match="已经有"):
            service.create_context(TASK, by=A)

    def test_create_requires_participant(self, service, task):
        with pytest.raises(NotAParticipantError):
            service.create_context(TASK, by=C)

    def test_read_revision_by_number(self, service, task, with_context, store):
        found = store.revision(TASK, 1)

        assert found is not None
        assert found.revision == 1
        assert found.context.task_id == TASK

    def test_missing_revision_returns_none(self, service, task, with_context, store):
        assert store.revision(TASK, 99) is None

    def test_no_context_yet(self, service, task, store):
        assert service.current(TASK) is None
        assert store.current_revision_number(TASK) is None

    def test_require_current_raises_when_absent(self, service, task):
        with pytest.raises(ContextNotFoundError):
            service.require_current(TASK)


# ============================================================
# 二、Change：提议 / 接受 / 拒绝
# ============================================================
class TestChangeLifecycle:
    def test_proposal_does_not_change_context(self, service, with_context, store):
        """提议只是提议。Context 必须一点都不变。"""
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="只用标准库", expected_revision=1, reason="依赖最小化",
        )

        assert change.status == ContextChangeStatus.PROPOSED
        assert store.current_revision_number(TASK) == 1
        assert service.current(TASK).context.constraints == ()

    def test_accept_advances_revision_and_applies(self, service, with_context, store):
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="只用标准库", expected_revision=1, reason="依赖最小化",
        )
        rev = service.accept(TASK, change.change_id, decided_by=B)

        assert rev.revision == 2
        assert rev.parent_revision == 1
        assert rev.change_id == change.change_id
        assert "只用标准库" in rev.context.constraints
        assert store.current_revision_number(TASK) == 2

    def test_reject_does_not_touch_context(self, service, with_context, store):
        """**核心边界**：被拒绝的 Change 不得改变当前有效 Context。"""
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="支持 GBK", expected_revision=1, reason="老系统",
        )
        before = service.current(TASK).context

        decided = service.reject(TASK, change.change_id, decided_by=B,
                                 reason="超出范围")

        after = service.current(TASK).context
        assert decided.status == ContextChangeStatus.REJECTED
        assert store.current_revision_number(TASK) == 1
        assert after.constraints == before.constraints
        assert after.to_dict() == before.to_dict()

    def test_rejected_change_is_kept(self, service, with_context, store):
        """被拒绝的提议要留着 —— 「当时为什么否掉」是有价值的知识。"""
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="支持 GBK", expected_revision=1, reason="老系统",
        )
        service.reject(TASK, change.change_id, decided_by=B, reason="超出范围")

        kept = store.get_change(change.change_id)
        assert kept is not None
        assert kept.status == ContextChangeStatus.REJECTED
        assert kept.decided_reason == "超出范围"

    def test_cannot_decide_twice(self, service, with_context):
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        service.accept(TASK, change.change_id, decided_by=B)

        with pytest.raises(ContextValidationError, match="终态"):
            service.reject(TASK, change.change_id, decided_by=B)

    def test_cannot_accept_after_reject(self, service, with_context):
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        service.reject(TASK, change.change_id, decided_by=B)

        with pytest.raises(ContextValidationError, match="终态"):
            service.accept(TASK, change.change_id, decided_by=B)

    def test_reason_is_required(self, service, with_context):
        """没有理由的变更无法被有效对齐。"""
        with pytest.raises((ContextValidationError, InvalidContextChange)):
            service.propose(
                TASK, proposed_by=A,
                change_type=ContextChangeType.ADD_CONSTRAINT,
                new_value="x", expected_revision=1, reason="   ",
            )

    def test_unknown_change_type_rejected(self, service, with_context):
        with pytest.raises(InvalidContextChange, match="未知"):
            service.propose(
                TASK, proposed_by=A, change_type="DELETE_EVERYTHING",
                new_value="x", expected_revision=1, reason="r",
            )

    def test_bad_payload_shape_fails_at_propose(self, service, with_context):
        """载荷结构错误必须在**提议**时暴露，不能拖到 accept。"""
        with pytest.raises(InvalidContextChange):
            service.propose(
                TASK, proposed_by=A,
                change_type=ContextChangeType.DECIDE,
                new_value={"没有 statement 键": True},
                expected_revision=1, reason="r",
            )

    def test_resolve_unknown_question_rejected(self, service, with_context):
        with pytest.raises(InvalidContextChange, match="找不到"):
            service.propose(
                TASK, proposed_by=A,
                change_type=ContextChangeType.RESOLVE_QUESTION,
                target="q-does-not-exist",
                new_value={"answer": "42"},
                expected_revision=1, reason="r",
            )


# ============================================================
# 三、Authority：参与方边界
# ============================================================
class TestAuthority:
    def test_participant_can_propose(self, service, with_context):
        change = service.propose(
            TASK, proposed_by=B,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        assert change.proposed_by == B

    def test_non_participant_cannot_propose(self, service, with_context):
        """"路人甲"不得改写别人的共同认知。"""
        with pytest.raises(NotAParticipantError):
            service.propose(
                TASK, proposed_by=C,
                change_type=ContextChangeType.ADD_CONSTRAINT,
                new_value="x", expected_revision=1, reason="r",
            )

    def test_non_participant_cannot_decide(self, service, with_context):
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        with pytest.raises(NotAParticipantError):
            service.accept(TASK, change.change_id, decided_by=C)

        with pytest.raises(NotAParticipantError):
            service.reject(TASK, change.change_id, decided_by=C)

    def test_error_message_names_the_agent(self, service, with_context):
        with pytest.raises(NotAParticipantError) as caught:
            service.propose(
                TASK, proposed_by=C,
                change_type=ContextChangeType.ADD_CONSTRAINT,
                new_value="x", expected_revision=1, reason="r",
            )
        assert C in str(caught.value)

    def test_creator_counts_as_participant(self, service, with_context):
        """参与方 = creator / contractor / participants。"""
        change = service.propose(
            TASK, proposed_by=A,       # A 是 creator
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        assert change.proposed_by == A

    def test_message_received_does_not_grant_authority(self, service, with_context):
        """
        收到一条消息**不等于**成为参与方。

        §11：Message 不会自动升级为 Shared Context。
        这里从另一个角度看同一件事：
        也不因为「消息里提到了你」就把你变成参与方。
        """
        from communication.message import Message, MessageType

        Message(
            message_id="m-1", from_agent=A, to_agent=C,
            message_type=MessageType.TEXT, payload={"text": "帮我改一下"},
        )
        # 消息存在了，但 C 依然不是参与方
        with pytest.raises(NotAParticipantError):
            service.propose(
                TASK, proposed_by=C,
                change_type=ContextChangeType.ADD_CONSTRAINT,
                new_value="x", expected_revision=1, reason="r",
            )


# ============================================================
# 四、Source of Truth
# ============================================================
class TestSourceOfTruth:
    def test_decide_cannot_self_align(self, service, with_context):
        """「一个 Agent 自己说了算」不算共同决定。"""
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.DECIDE,
            new_value={"statement": "用方案 B", "reason": "快"},
            expected_revision=1, reason="方案 B 更快",
        )
        with pytest.raises(SelfAlignmentError):
            service.accept(TASK, change.change_id, decided_by=A)

    def test_decide_with_other_aligner_succeeds(self, service, with_context):
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.DECIDE,
            new_value={"statement": "用方案 B", "reason": "快"},
            expected_revision=1, reason="方案 B 更快",
        )
        rev = service.accept(TASK, change.change_id, decided_by=B)

        decision = rev.context.decisions[0]
        assert decision.statement == "用方案 B"
        assert decision.proposed_by == A
        assert decision.accepted_by == (B,)

    def test_declaration_types_may_self_align(self, service, with_context):
        """声明类（ASSUME / FACT / QUESTION / ASSIGN）自己声明自己，合理。"""
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ASSUME,
            new_value={"statement": "编码是 UTF-8"},
            expected_revision=1, reason="常见默认",
        )
        rev = service.accept(TASK, change.change_id, decided_by=A)
        assert rev.context.assumptions[0].held_by == A

    def test_assumption_is_not_a_fact(self, service, with_context):
        """未验证的假设**不能**写成 Fact。"""
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ASSUME,
            new_value={"statement": "外部 API 支持幂等"},
            expected_revision=1, reason="文档没说",
        )
        rev = service.accept(TASK, change.change_id, decided_by=A)

        assumption = rev.context.assumptions[0]
        assert assumption.status == AssumptionStatus.UNVERIFIED
        assert assumption.is_open
        assert rev.context.facts == ()
        assert rev.context.unverified_assumption_count == 1

    def test_assumption_can_be_confirmed_explicitly(self, service, with_context):
        """假设变事实必须是一次**显式**的 CONFIRM。"""
        created = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ASSUME,
            new_value={"statement": "外部 API 支持幂等"},
            expected_revision=1, reason="文档没说",
        )
        rev = service.accept(TASK, created.change_id, decided_by=A)
        assumption_id = rev.context.assumptions[0].assumption_id

        confirm = service.propose(
            TASK, proposed_by=B,
            change_type=ContextChangeType.CONFIRM_ASSUMPTION,
            target=assumption_id,
            new_value={"evidence": "官方文档第 3 节"},
            expected_revision=rev.revision, reason="查到文档了",
        )
        final = service.accept(TASK, confirm.change_id, decided_by=A)

        assert final.context.assumptions[0].status == AssumptionStatus.CONFIRMED
        assert final.context.assumptions[0].resolved_by == A
        assert final.context.assumptions[0].is_open is False

    def test_shared_fact_requires_source(self, service, with_context):
        """Fact 必须带来源，否则它和 Assumption 没区别。"""
        with pytest.raises(InvalidContextChange):
            service.propose(
                TASK, proposed_by=A,
                change_type=ContextChangeType.FACT,
                new_value={"statement": "Python 是 3.13"},   # 没有 source
                expected_revision=1, reason="r",
            )

    def test_shared_context_does_not_modify_task_core(self, service, task,
                                                      with_context, tasks):
        """**核心边界**：Shared Context 不得反向修改 Task Core。"""
        before = tasks.require(TASK).to_dict()

        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.SET_OBJECTIVE,
            new_value="随便改个目标", expected_revision=1, reason="r",
        )
        service.accept(TASK, change.change_id, decided_by=B)

        after = tasks.require(TASK).to_dict()
        assert after == before, "Shared Context 的操作改动了 Task Core"
        assert after["version"] == before["version"]

    def test_context_has_no_status_field(self):
        """
        Context 里**没有** status —— 否则就是第二套生命周期。
        """
        context = TaskSharedContext(context_id="c1", task_id="t1")

        assert not hasattr(context, "status")
        assert not hasattr(context, "state")
        assert not hasattr(context, "agreed_price")
        assert not hasattr(context, "contractor")

    def test_snapshot_references_task_core_live(self, service, task,
                                                with_context, tasks):
        """
        Snapshot 的 domain_state 是**现场读** Task Core 的引用 ——
        Task 状态一变，下一次 snapshot 立刻反映（不是副本）。
        """
        first = service.snapshot(TASK)
        assert first.domain_state["status"] == "CREATED"
        assert first.domain_state_source == "task_core"

        live = tasks.require(TASK)
        live.transition_to("OFFERED")     # 状态机允许 CREATED → OFFERED
        tasks.save(live)

        second = service.snapshot(TASK)
        assert second.domain_state["status"] == "OFFERED"
        # Context 的版本没变 —— 因为 Task 状态变化与 Context 无关
        assert second.revision == first.revision

    def test_snapshot_is_derived_not_independent(self, service, with_context):
        """Snapshot 没有独立写入路径 —— 它只能从 Revision 派生。"""
        snap = service.snapshot(TASK)

        assert snap.revision == service.current(TASK).revision
        assert snap.context.to_dict() == service.current(TASK).context.to_dict()


# ============================================================
# 五、并发（单进程 CAS 语义）
# ============================================================
class TestConcurrency:
    def test_stale_revision_rejected_at_propose(self, service, with_context):
        """基于旧版本提议 → 立刻失败。"""
        change = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        service.accept(TASK, change.change_id, decided_by=B)

        with pytest.raises(ConcurrentContextModification):
            service.propose(
                TASK, proposed_by=A,
                change_type=ContextChangeType.ADD_CONSTRAINT,
                new_value="y", expected_revision=1, reason="r",
            )

    def test_stale_revision_rejected_at_accept(self, service, with_context):
        """提议之后、接受之前别人推进了版本 → 接受失败。"""
        first = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="first", expected_revision=1, reason="r",
        )
        second = service.propose(
            TASK, proposed_by=B,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="second", expected_revision=1, reason="r",
        )

        service.accept(TASK, first.change_id, decided_by=B)

        with pytest.raises(ConcurrentContextModification):
            service.accept(TASK, second.change_id, decided_by=A)

    def test_no_silent_overwrite(self, service, with_context, store):
        """冲突之后，失败者的内容绝对不能出现在 Context 里。"""
        first = service.propose(
            TASK, proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="WINNER", expected_revision=1, reason="r",
        )
        second = service.propose(
            TASK, proposed_by=B,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="LOSER", expected_revision=1, reason="r",
        )

        service.accept(TASK, first.change_id, decided_by=B)
        with pytest.raises(ConcurrentContextModification):
            service.accept(TASK, second.change_id, decided_by=A)

        constraints = service.current(TASK).context.constraints
        assert "WINNER" in constraints
        assert "LOSER" not in constraints
        assert store.current_revision_number(TASK) == 2

    def test_conflict_leaves_revision_count_consistent(self, service,
                                                       with_context, store):
        """
        冲突时不能留下**孤立的历史版本**。

        如果 commit_revision 先 INSERT 再 UPDATE，
        失败者就会在 revisions 表里留下一条从没被 current 指向过的版本。
        """
        first = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="A", expected_revision=1, reason="r",
        )
        second = service.propose(
            TASK, proposed_by=B, change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="B", expected_revision=1, reason="r",
        )

        service.accept(TASK, first.change_id, decided_by=B)
        with pytest.raises(ConcurrentContextModification):
            service.accept(TASK, second.change_id, decided_by=A)

        history = store.history(TASK)
        assert [r.revision for r in history] == [1, 2]
        assert store.current_revision_number(TASK) == len(history)

    def test_second_change_can_succeed_after_reproposing(self, service,
                                                         with_context):
        """失败者基于**新版本**重新提议 → 成功（没有自动重试）。"""
        first = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="A", expected_revision=1, reason="r",
        )
        service.accept(TASK, first.change_id, decided_by=B)

        retry = service.propose(
            TASK, proposed_by=B, change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="B", expected_revision=2, reason="r",
        )
        rev = service.accept(TASK, retry.change_id, decided_by=A)

        assert rev.revision == 3
        assert "B" in rev.context.constraints

    def test_concurrent_decide_only_one_wins(self, tmp_path):
        """
        两个进程同时裁决**同一条** change —— 只有一个成功。

        【为什么必须用真进程】
            「条件 UPDATE 防 TOCTOU」这件事只有**真的并发**才能验证。
            单进程里两次 decide_change 是串行的，
            第二个会正常看到 status 已经不是 PROPOSED ——
            那测的是「顺序调用」，不是「竞争」。

        【为什么用文件库】
            spawn 出来的子进程不能共享内存库（:memory: 是每连接独立的），
            也不能 pickle sqlite3.Connection（Phase 0 的教训）。
            所以走文件路径，子进程自己开连接。
        """
        db_path = str(tmp_path / "decide_race.db")
        database = Database(db_path)
        database.connect()
        database.initialize()
        database.migrate()
        tasks = TaskStore(database)
        created = Task.create(task_id=TASK, creator=A, description="d")
        created.participants = [A, B]
        tasks.save(created)
        service = TaskAlignmentService(tasks, TaskContextStore(database))
        service.create_context(TASK, by=A)

        change = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        database.close()

        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        queue = ctx.Queue()

        procs = [
            ctx.Process(
                target=_reject_worker_entry,
                args=(db_path, TASK, change.change_id, who, barrier, queue),
            )
            for who in (A, B)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)

        results = []
        while not queue.empty():
            results.append(queue.get())

        assert len(results) == 2, results
        oks = [r for r in results if r.get("ok")]
        assert len(oks) == 1, f"应该只有一个裁决成功，实际 {results}"

        # 版本没有因为「裁决」而变化（这是 reject）
        reopened = Database(db_path)
        reopened.connect()
        assert reopened.fetchone(
            "SELECT current_revision AS r FROM task_shared_contexts "
            "WHERE task_id = ?", (TASK,)
        )["r"] == 1
        reopened.close()


def _reject_worker_entry(path, task_id, change_id, agent_id, barrier, queue):
    """spawn 需要一个模块级可 pickle 的入口。"""
    from tests.multiprocess_helpers import context_reject_worker
    context_reject_worker(path, task_id, change_id, agent_id, barrier, queue)


# ============================================================
# 六、真实多进程并发提交
# ============================================================
class TestMultiProcessConcurrency:
    """
    Phase 0 的教训：单进程正确 ≠ 多进程正确。
    所以 Shared Context 的 CAS 必须在**真进程**上验证。
    """

    def _run_two_writers(self, db_path, expected_revision, values):
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        queue = ctx.Queue()

        procs = [
            ctx.Process(
                target=context_change_worker,
                args=(db_path, TASK, agent, expected_revision,
                      value, barrier, queue),
            )
            for agent, value in values
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=90)

        results = []
        while not queue.empty():
            results.append(queue.get())
        return results

    @staticmethod
    def _prepare(db_path, expected_revision=1):
        database = Database(db_path)
        database.connect()
        database.initialize()
        database.migrate()
        tasks = TaskStore(database)
        created = Task.create(task_id=TASK, creator=A, description="d")
        created.participants = [A, B]
        tasks.save(created)
        TaskAlignmentService(tasks, TaskContextStore(database)).create_context(TASK, by=A)
        database.close()

    def test_only_one_writer_succeeds(self, tmp_path):
        """两个真进程基于同一 revision 提交 —— 只有一个成功。"""
        db_path = str(tmp_path / "ctx_race.db")
        self._prepare(db_path)

        results = self._run_two_writers(
            db_path, 1, [(A, "A 的要求"), (B, "B 的要求")]
        )

        assert len(results) == 2, results
        assert not [r for r in results if r.get("step") == "fatal"], results

        succeeded = [r for r in results if r.get("ok")]
        assert len(succeeded) == 1, f"应该恰好一个成功：{results}"

        rejected = [r for r in results if not r.get("ok")]
        assert rejected[0]["error"] == "ConcurrentContextModification", results

    def test_revision_is_consistent_after_race(self, tmp_path):
        """竞争之后：revision == 2，历史连续，没有静默覆盖。"""
        db_path = str(tmp_path / "ctx_consistent.db")
        self._prepare(db_path)

        results = self._run_two_writers(
            db_path, 1, [(A, "A 的要求"), (B, "B 的要求")]
        )
        winner = next(r for r in results if r.get("ok"))
        loser = next(r for r in results if not r.get("ok"))
        loser_value = "B 的要求" if winner["worker"] == A else "A 的要求"
        winner_value = "A 的要求" if winner["worker"] == A else "B 的要求"

        database = Database(db_path)
        database.connect()
        service = TaskAlignmentService(TaskStore(database), TaskContextStore(database))

        assert database.fetchone(
            "SELECT current_revision AS r FROM task_shared_contexts "
            "WHERE task_id = ?", (TASK,)
        )["r"] == 2

        history = [r.revision for r in service.history(TASK)]
        assert history == [1, 2]

        constraints = service.current(TASK).context.constraints
        assert winner_value in constraints, constraints
        assert loser_value not in constraints, (
            f"失败者的内容被静默写入了：{constraints}"
        )
        assert loser["error"] == "ConcurrentContextModification"
        database.close()

    def test_no_row_for_loser_change(self, tmp_path):
        """失败者的 change 必须留在 PROPOSED（没有半成功）。"""
        db_path = str(tmp_path / "ctx_loser.db")
        self._prepare(db_path)

        results = self._run_two_writers(
            db_path, 1, [(A, "A 的要求"), (B, "B 的要求")]
        )
        loser = next(r for r in results if not r.get("ok"))

        # 失败可能发生在两个位置：
        #   提议阶段（revision 已经变了）→ 根本没写进库，这是干净的
        #   接受阶段（提议之后版本才变）→ 库里有，必须留在 PROPOSED
        if loser.get("step") == "propose":
            assert loser["error"] == "ConcurrentContextModification"
            database = Database(db_path)
            database.connect()
            assert database.fetchone(
                "SELECT COUNT(*) AS n FROM task_context_changes "
                "WHERE proposed_by = ?", (loser["worker"],)
            )["n"] == 0, "提议阶段失败的 change 不该留在库里"
            database.close()
        else:
            database = Database(db_path)
            database.connect()
            row = database.fetchone(
                "SELECT status FROM task_context_changes WHERE change_id = ?",
                (loser["change_id"],),
            )
            assert row is not None
            assert row["status"] == "PROPOSED", "失败者不能是半成功状态"
            database.close()


# ============================================================
# 七、Persistence
# ============================================================
class TestPersistence:
    def test_survives_close_and_reopen(self, tmp_path):
        """写入 → 关闭 → 重开 → 还在。"""
        db_path = str(tmp_path / "persist.db")

        database = Database(db_path)
        database.connect()
        database.initialize()
        database.migrate()
        tasks = TaskStore(database)
        created = Task.create(task_id=TASK, creator=A, description="d")
        created.participants = [A, B]
        tasks.save(created)

        service = TaskAlignmentService(tasks, TaskContextStore(database))
        service.create_context(TASK, by=A, objective="持久化测试")
        change = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.DECIDE,
            new_value={"statement": "用方案 B", "reason": "快"},
            expected_revision=1, reason="理由",
        )
        service.accept(TASK, change.change_id, decided_by=B)
        database.close()

        # ---- 重开 ----
        reopened = Database(db_path)
        reopened.connect()
        service2 = TaskAlignmentService(
            TaskStore(reopened), TaskContextStore(reopened)
        )

        current = service2.current(TASK)
        assert current is not None
        assert current.revision == 2
        assert current.context.objective == "持久化测试"
        assert current.context.decisions[0].statement == "用方案 B"

        # 历史也还在
        assert [r.revision for r in service2.history(TASK)] == [1, 2]

        # 被拒绝/待裁决的提议也还在
        assert len(service2.changes(TASK)) == 1
        reopened.close()

    def test_revision_payload_round_trips(self, service, with_context, store):
        """物化全文的序列化必须无损。"""
        change = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.ASSIGN,
            new_value={"agent_id": A, "responsibility": "parser",
                       "notes": "含中文与引号 \"x\""},
            expected_revision=1, reason="分工",
        )
        service.accept(TASK, change.change_id, decided_by=B)

        loaded = store.revision(TASK, 2).context
        original = service.current(TASK).context

        assert loaded.to_dict() == original.to_dict()


# ============================================================
# 八、Isolation
# ============================================================
class TestIsolation:
    def test_two_tasks_do_not_pollute_each_other(self, service, tasks, store):
        """Task-001 的 Context 不得污染 Task-002。"""
        for task_id in ("TASK-001", "TASK-002"):
            created = Task.create(task_id=task_id, creator=A, description="d")
            created.participants = [A, B]
            tasks.save(created)

        service.create_context("TASK-001", by=A, objective="任务一")
        service.create_context("TASK-002", by=A, objective="任务二")

        change = service.propose(
            "TASK-001", proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="只属于任务一", expected_revision=1, reason="r",
        )
        service.accept("TASK-001", change.change_id, decided_by=B)

        one = service.current("TASK-001").context
        two = service.current("TASK-002").context

        assert one.constraints == ("只属于任务一",)
        assert two.constraints == ()
        assert two.objective == "任务二"
        assert store.current_revision_number("TASK-002") == 1
        assert store.current_revision_number("TASK-001") == 2

    def test_change_belongs_to_one_task(self, service, tasks):
        for task_id in ("TASK-001", "TASK-002"):
            created = Task.create(task_id=task_id, creator=A, description="d")
            created.participants = [A, B]
            tasks.save(created)

        service.create_context("TASK-001", by=A)
        service.create_context("TASK-002", by=A)

        change = service.propose(
            "TASK-001", proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )

        # 拿着 TASK-001 的 change 去操作 TASK-002 → 必须拒绝
        with pytest.raises(ContextValidationError, match="不是"):
            service.accept("TASK-002", change.change_id, decided_by=B)

    def test_history_is_per_task(self, service, tasks):
        for task_id in ("TASK-001", "TASK-002"):
            created = Task.create(task_id=task_id, creator=A, description="d")
            created.participants = [A, B]
            tasks.save(created)

        service.create_context("TASK-001", by=A)
        service.create_context("TASK-002", by=A)

        change = service.propose(
            "TASK-001", proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="r",
        )
        service.accept("TASK-001", change.change_id, decided_by=B)

        assert [r.revision for r in service.history("TASK-001")] == [1, 2]
        assert [r.revision for r in service.history("TASK-002")] == [1]


# ============================================================
# 九、Revision History 可追溯
# ============================================================
class TestRevisionHistory:
    def test_all_revisions_traceable(self, service, with_context, store):
        """Revision 1 / 2 / 3 全部可读，链条连续。"""
        c1 = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="c1", expected_revision=1, reason="r1",
        )
        service.accept(TASK, c1.change_id, decided_by=B)

        c2 = service.propose(
            TASK, proposed_by=B, change_type=ContextChangeType.ASSUME,
            new_value={"statement": "s2"}, expected_revision=2, reason="r2",
        )
        service.accept(TASK, c2.change_id, decided_by=A)

        history = store.history(TASK)
        assert [r.revision for r in history] == [1, 2, 3]

        assert history[0].parent_revision is None
        assert history[1].parent_revision == 1
        assert history[2].parent_revision == 2

        # 每一版都能单独读出来
        for rev in (1, 2, 3):
            assert store.revision(TASK, rev).revision == rev

    def test_revision_1_is_not_mutated_by_later_versions(self, service,
                                                         with_context, store):
        """
        **关键**：Revision 1 的内容不能被后续版本改写。

        如果存储层用 UPDATE 覆盖，这里就会读到被污染的数据。
        """
        before = store.revision(TASK, 1).context.to_dict()

        c1 = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.SET_OBJECTIVE,
            new_value="新目标", expected_revision=1, reason="r",
        )
        service.accept(TASK, c1.change_id, decided_by=B)

        after = store.revision(TASK, 1).context.to_dict()
        assert after == before
        assert after["objective"] != "新目标"

    def test_change_reason_recorded_per_revision(self, service, with_context, store):
        c1 = service.propose(
            TASK, proposed_by=A, change_type=ContextChangeType.ADD_CONSTRAINT,
            new_value="x", expected_revision=1, reason="因为依赖最小化",
        )
        service.accept(TASK, c1.change_id, decided_by=B, reason="同意")

        assert store.revision(TASK, 2).change_reason == "同意"
        assert store.revision(TASK, 2).change_id == c1.change_id

    def test_revision_1_has_no_change_id(self, service, with_context, store):
        """初始版本不是由变更产生的。"""
        assert store.revision(TASK, 1).change_id is None


# ============================================================
# 十、领域对象本身
# ============================================================
class TestDomainObjects:
    def test_context_is_frozen(self):
        context = TaskSharedContext(context_id="c", task_id="t")
        with pytest.raises(Exception):
            context.objective = "x"

    def test_revision_mismatch_rejected(self):
        context = TaskSharedContext(context_id="c", task_id="t", revision=2)
        with pytest.raises(ContextValidationError, match="不一致"):
            ContextRevision(task_id="t", revision=3, context=context)

    def test_task_id_mismatch_rejected(self):
        context = TaskSharedContext(context_id="c", task_id="t1", revision=1)
        with pytest.raises(ContextValidationError, match="不一致"):
            ContextRevision(task_id="t2", revision=1, context=context)

    def test_decision_requires_other_aligner(self):
        with pytest.raises(ContextValidationError, match="共同决定"):
            DecisionRecord(
                decision_id="d1", statement="s", proposed_by=A,
                reason="r", accepted_by=(A,),
            )

    def test_decision_with_other_aligner_ok(self):
        record = DecisionRecord(
            decision_id="d1", statement="s", proposed_by=A,
            reason="r", accepted_by=(B,),
        )
        assert record.accepted_by == (B,)

    def test_decision_requires_aligner(self):
        with pytest.raises(ContextValidationError, match="确认人"):
            DecisionRecord(
                decision_id="d1", statement="s", proposed_by=A, reason="r",
            )

    def test_assumption_defaults_to_unverified(self):
        record = AssumptionRecord(
            assumption_id="a1", statement="s", held_by=A,
        )
        assert record.status == AssumptionStatus.UNVERIFIED
        assert record.is_open

    def test_assignment_is_a_record_not_a_scheduler(self):
        """
        AssignmentRecord 只是一条分工记录。

        【这条测试在防什么】
            防「顺手做出 TaskManager / Scheduler」。
            领域对象上不该有任何「派发」「调度」「分配最优」
            之类的方法。
        """
        record = AssignmentRecord(
            assignment_id="as1", agent_id=A, responsibility="parser",
        )
        assert record.status == AssignmentStatus.ACTIVE

        for forbidden in ("assign_to", "dispatch", "schedule", "rebalance",
                          "pick_best", "auto_assign"):
            assert not hasattr(record, forbidden), (
                f"AssignmentRecord 上出现了 {forbidden} —— "
                f"那意味着在往 Scheduler 方向漂移"
            )

    def test_open_question_has_no_answer_by_default(self):
        question = OpenQuestion(question_id="q1", question="?")
        assert question.status == QuestionStatus.OPEN
        assert question.answer is None

    def test_fact_requires_source(self):
        with pytest.raises(ContextValidationError, match="source"):
            SharedFact(fact_id="f1", statement="s", source="  ")

    def test_artifact_ref_rejects_paths(self):
        """artifact_refs 沿用 result_ref 的安全规则。"""
        for bad in ("/etc/passwd", "../secrets", "~/x", "https://e.com/x"):
            with pytest.raises(Exception):
                TaskSharedContext(
                    context_id="c", task_id="t", artifact_refs=(bad,),
                )

    def test_artifact_ref_accepts_logical_names(self):
        context = TaskSharedContext(
            context_id="c", task_id="t",
            artifact_refs=("parser-design-b", "spec-doc-3"),
        )
        assert context.artifact_refs == ("parser-design-b", "spec-doc-3")

    def test_terminology_is_frozen(self):
        context = TaskSharedContext(
            context_id="c", task_id="t", terminology={"CSV": "逗号分隔值"},
        )
        with pytest.raises(TypeError):
            context.terminology["X"] = "Y"

    def test_apply_change_rejects_unaccepted(self):
        context = TaskSharedContext(context_id="c", task_id="t", revision=1)
        change = ContextChange(
            change_id="ch1", task_id="t", proposed_by=A,
            change_type=ContextChangeType.ADD_CONSTRAINT,
            target_revision=1, new_value="x", reason="r",
        )
        assert change.is_proposed

        with pytest.raises(InvalidContextChange, match="ACCEPTED"):
            apply_change(context, change, accepted_by=B, new_revision=2)

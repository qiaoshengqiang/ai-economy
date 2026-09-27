"""
Test: Task Execution —— 执行 / 提交 / 验收 / 完成（Step 4D）
============================================================

【这个文件验证什么】

    把 4A 已有的执行生命周期，通过消息真正接起来：

        ACCEPTED → IN_PROGRESS → SUBMITTED → VERIFIED → COMPLETED
                              ↘ IN_PROGRESS（返工）↗

【最重要的几条断言】

    1. 状态只能按状态机走 —— 跳步（ACCEPTED → SUBMITTED）必须被拒绝
    2. 角色边界不能被越过 ——
       承接方不能自己发 VERIFY 说「我验收自己通过」
    3. 验收不通过必须能说明原因（feedback 落库，可查询）
    4. 四条边界全程不越：
       Wallet / Ledger / LLM / Workspace / Git / 代码执行
    5. 同一条消息重复送达是幂等的
"""

from __future__ import annotations

import pytest

from core.money import Money
from core.task import Task, TaskStatus
from core.task_execution import (
    ExecutionEvent,
    TaskResultRef,
    TaskVerification,
)
from core.task_store import TaskStore

from communication import Message, MessageType

from services import HandlerAction, TaskExecutionService


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


@pytest.fixture
def execution(store) -> TaskExecutionService:
    return TaskExecutionService(store=store)


def accepted_task(store, task_id="T-1", creator="agent_a",
                  contractor="agent_b") -> Task:
    """造一个已经谈成的任务（ACCEPTED）。"""
    task = Task.create(
        task_id=task_id,
        creator=creator,
        description="写个分析程序",
        budget=Money(100),
        workspace_id="WS-001",
        participants=[creator, contractor],
    )
    task.offer()
    task.accept(Money(25), contractor=contractor)
    store.save(task)
    return task


def in_progress_task(store, task_id="T-1") -> Task:
    task = accepted_task(store, task_id)
    task.start()
    store.save(task)
    return task


def submitted_task(store, task_id="T-1") -> Task:
    task = in_progress_task(store, task_id)
    task.submit()
    store.save(task)
    return task


def verified_task(store, task_id="T-1") -> Task:
    task = submitted_task(store, task_id)
    task.verify(passed=True)
    store.save(task)
    return task


def start_message(task_id="T-1", sender="agent_b", recipient="agent_a",
                  reply_to=None, **overrides) -> Message:
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.START,
        payload={"task_id": task_id},
        reply_to=reply_to,
        **overrides,
    )


def submit_message(task_id="T-1", sender="agent_b", recipient="agent_a",
                   result_ref="analysis-v1", reply_to=None,
                   result_type="code", summary="初版",
                   metadata=None, **overrides) -> Message:
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.SUBMIT,
        payload={
            "task_id": task_id,
            "result": {
                "result_type": result_type,
                "result_ref": result_ref,
                "summary": summary,
                "metadata": metadata or {},
            },
        },
        reply_to=reply_to,
        **overrides,
    )


def verify_message(task_id="T-1", passed=True, feedback="", sender="agent_a",
                   recipient="agent_b", reply_to=None, **overrides) -> Message:
    payload = {"task_id": task_id, "passed": passed}
    if feedback:
        payload["feedback"] = feedback
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.VERIFY,
        payload=payload,
        reply_to=reply_to,
        **overrides,
    )


def complete_message(task_id="T-1", sender="agent_a", recipient="agent_b",
                     reply_to=None, **overrides) -> Message:
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.COMPLETE,
        payload={"task_id": task_id},
        reply_to=reply_to,
        **overrides,
    )


def do_start(execution, task, **kwargs):
    return execution.apply_start(task, incoming=start_message(**kwargs))


def do_submit(execution, task, result_ref="analysis-v1", **kwargs):
    result = TaskResultRef(result_ref=result_ref, result_type="code",
                           summary="初版")
    return execution.apply_submit(task, result,
                                  incoming=submit_message(**kwargs))


def do_verify(execution, task, passed=True, feedback="", **kwargs):
    if not passed and not feedback:
        feedback = "有问题需要改"
    verification = TaskVerification(passed=passed, feedback=feedback)
    return execution.apply_verify(task, verification,
                                  incoming=verify_message(passed=passed, **kwargs))


# ============================================================
# 1. START
# ============================================================
class TestStart:
    def test_accepted_to_in_progress(self, execution, store):
        task = accepted_task(store)

        result = execution.apply_start(task, incoming=start_message())

        assert result.ok is True
        assert result.action == HandlerAction.STARTED
        assert store.get("T-1").status == TaskStatus.IN_PROGRESS

    def test_role_must_be_contractor(self, execution, store):
        """发起方不能替承接方宣布开工。"""
        task = accepted_task(store)
        errors = execution.validate_role(
            task, "agent_a", ExecutionEvent.START
        )
        assert errors
        assert "承接方" in errors[0]

    def test_non_participant_rejected(self, execution, store):
        task = accepted_task(store)
        errors = execution.validate_role(
            task, "agent_c", ExecutionEvent.START
        )
        assert errors
        assert "参与方" in errors[0]

    def test_wrong_state_rejected(self, execution, store):
        """已经开工了不能再开工（IN_PROGRESS 不允许 START）。"""
        task = in_progress_task(store)
        errors = execution.validate_state(task, ExecutionEvent.START)
        assert errors
        assert "IN_PROGRESS" in errors[0]

    def test_offered_task_cannot_start(self, execution, store):
        """还没谈成（OFFERED）不能开工。"""
        task = Task.create(task_id="T-O", creator="agent_a",
                           budget=Money(100), participants=["agent_a", "agent_b"])
        task.offer()
        store.save(task)

        errors = execution.validate_state(task, ExecutionEvent.START)
        assert errors

    def test_terminal_task_cannot_start(self, execution, store):
        task = accepted_task(store)
        task.cancel("不做了")
        store.save(task)

        errors = execution.validate_state(task, ExecutionEvent.START)
        assert errors
        assert "终态" in errors[0]

    def test_records_execution_event(self, execution, store):
        task = accepted_task(store)
        message = start_message()

        execution.apply_start(task, incoming=message)

        records = store.executions_for("T-1")
        assert len(records) == 1
        assert records[0].event_type == ExecutionEvent.START
        assert records[0].message_id == message.message_id
        assert records[0].from_agent == "agent_b"
        assert records[0].status_after == TaskStatus.IN_PROGRESS

    def test_state_machine_is_not_duplicated(self, execution, store):
        """执行服务里不许有第二套状态转移表。"""
        import inspect

        source = inspect.getsource(TaskExecutionService)
        # 它只**查询**允许的状态，不自己定义「A → B」
        assert "transition_to" not in source or "TaskStatus." in source
        # 真正改状态的是 Task Core
        assert "def start(self)" not in source


# ============================================================
# 2. SUBMIT
# ============================================================
class TestSubmit:
    def test_in_progress_to_submitted(self, execution, store):
        task = in_progress_task(store)

        result = execution.apply_submit(
            task, TaskResultRef(result_ref="analysis-v1"), incoming=submit_message()
        )

        assert result.ok is True
        assert result.action == HandlerAction.SUBMITTED
        assert store.get("T-1").status == TaskStatus.SUBMITTED

    def test_result_ref_is_required(self, execution, store):
        """
        不能只发「我做好了」。

        验收需要一个**对象** —— 否则发起方不知道该检查什么。
        """
        from services.task_message_mapper import SubmitIntent

        intent = SubmitIntent(task_id="T-1", result=None)
        errors = execution.validate_result(intent)

        assert errors
        assert "result_ref" in errors[0]

    def test_result_ref_is_persisted(self, execution, store):
        task = in_progress_task(store)
        result_ref = TaskResultRef(
            result_ref="analysis-v2", result_type="data",
            summary="月度报表", metadata={"rows": 1200},
        )

        execution.apply_submit(task, result_ref, incoming=submit_message())

        record = store.latest_submission("T-1")
        assert record is not None
        assert record.result.result_ref == "analysis-v2"
        assert record.result.result_type == "data"
        assert record.result.summary == "月度报表"
        assert dict(record.result.metadata) == {"rows": 1200}

    def test_role_must_be_contractor(self, execution, store):
        """发起方不能替承接方提交成果。"""
        task = in_progress_task(store)
        errors = execution.validate_role(
            task, "agent_a", ExecutionEvent.SUBMIT
        )
        assert errors

    def test_cannot_skip_from_accepted(self, execution, store):
        """ACCEPTED → SUBMITTED 是跳步，必须被拒绝。"""
        task = accepted_task(store)
        errors = execution.validate_state(task, ExecutionEvent.SUBMIT)
        assert errors
        assert "不允许" in errors[0]

    def test_cannot_submit_after_verified(self, execution, store):
        """VERIFIED → SUBMITTED 也是跳步。"""
        task = verified_task(store)
        errors = execution.validate_state(task, ExecutionEvent.SUBMIT)
        assert errors

    def test_result_ref_rejects_paths(self):
        """
        【安全边界】result_ref 不许长得像路径。

            如果允许，承接方就能提交一个构造过的名字
            让发起方去读任意文件。
        """
        for bad in ("/etc/passwd", "~/secrets", "../up", "/tmp/x"):
            with pytest.raises(ValueError, match="路径"):
                TaskResultRef(result_ref=bad)

    def test_result_ref_rejects_urls(self):
        with pytest.raises(ValueError, match="URL"):
            TaskResultRef(result_ref="https://evil.example/x")

    def test_result_ref_rejects_empty(self):
        with pytest.raises(ValueError, match="result_ref"):
            TaskResultRef(result_ref="   ")

    def test_result_type_must_be_known(self):
        with pytest.raises(ValueError, match="result_type"):
            TaskResultRef(result_ref="x", result_type="随便写的")

    def test_result_ref_declares_not_permission(self):
        info = TaskResultRef(result_ref="analysis-v1").describe()

        assert info["is_permission"] is False
        assert info["is_filesystem_path"] is False
        assert info["is_url"] is False

    def test_metadata_is_frozen(self):
        ref = TaskResultRef(result_ref="x", metadata={"a": 1})
        with pytest.raises(TypeError):
            ref.metadata["b"] = 2


# ============================================================
# 3. VERIFY
# ============================================================
class TestVerify:
    def test_submitted_to_verified(self, execution, store):
        task = submitted_task(store)

        result = execution.apply_verify(
            task, TaskVerification(passed=True, feedback="通过"),
            incoming=verify_message(passed=True, feedback="通过"),
        )

        assert result.ok is True
        assert result.action == HandlerAction.VERIFIED
        assert store.get("T-1").status == TaskStatus.VERIFIED

    def test_failed_goes_back_to_in_progress(self, execution, store):
        """
        【返工，不是判死】

            交付不合格通常是可以修的。
            直接把任务判死会让一次小错误毁掉整个合同。
        """
        task = submitted_task(store)

        result = execution.apply_verify(
            task,
            TaskVerification(passed=False, feedback="第 3 个函数有 bug"),
            incoming=verify_message(passed=False, feedback="第 3 个函数有 bug"),
        )

        assert result.ok is True
        assert result.action == HandlerAction.REWORK
        assert store.get("T-1").status == TaskStatus.IN_PROGRESS

    def test_role_must_be_initiator(self, execution, store):
        """
        【4D 最关键的角色边界】

            承接方不能自己宣布「我验收自己通过」——
            干活的不能既当运动员又当裁判。
        """
        task = submitted_task(store)
        errors = execution.validate_role(
            task, "agent_b", ExecutionEvent.VERIFY
        )

        assert errors
        assert "发起方" in errors[0]
        assert "运动员" in errors[0] or "裁判" in errors[0]

    def test_non_participant_rejected(self, execution, store):
        task = submitted_task(store)
        errors = execution.validate_role(
            task, "agent_c", ExecutionEvent.VERIFY
        )
        assert errors

    def test_passed_must_be_boolean(self):
        with pytest.raises(ValueError, match="布尔"):
            TaskVerification(passed=1)

    def test_failure_requires_feedback(self):
        """不通过必须说明原因 —— 否则承接方只能盲改。"""
        with pytest.raises(ValueError, match="feedback"):
            TaskVerification(passed=False)

    def test_failure_with_whitespace_feedback_rejected(self):
        with pytest.raises(ValueError, match="feedback"):
            TaskVerification(passed=False, feedback="   ")

    def test_feedback_is_persisted(self, execution, store):
        task = submitted_task(store)
        feedback = "接口返回值错误：第 3 个函数在空输入时抛异常"

        execution.apply_verify(
            task,
            TaskVerification(passed=False, feedback=feedback),
            incoming=verify_message(passed=False, feedback=feedback),
        )

        record = store.latest_verification("T-1")
        assert record is not None
        assert record.verification.passed is False
        assert record.verification.feedback == feedback

    def test_cannot_verify_before_submit(self, execution, store):
        task = in_progress_task(store)
        errors = execution.validate_state(task, ExecutionEvent.VERIFY)
        assert errors

    def test_cannot_verify_accepted_task(self, execution, store):
        task = accepted_task(store)
        errors = execution.validate_state(task, ExecutionEvent.VERIFY)
        assert errors

    def test_verify_does_not_require_llm(self, execution, store):
        """
        验收结论完全由发起方给出 —— 不调用模型。

        这条守卫用源码表达：执行服务不 import llm。
        """
        import ast
        import inspect

        source = inspect.getsource(
            __import__("services.task_execution_service",
                       fromlist=["x"])
        )
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("llm")
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("llm")


# ============================================================
# 4. COMPLETE
# ============================================================
class TestComplete:
    def test_verified_to_completed(self, execution, store):
        task = verified_task(store)

        result = execution.apply_complete(task, incoming=complete_message())

        assert result.ok is True
        assert result.action == HandlerAction.COMPLETED
        assert store.get("T-1").status == TaskStatus.COMPLETED

    def test_role_must_be_initiator(self, execution, store):
        task = verified_task(store)
        errors = execution.validate_role(
            task, "agent_b", ExecutionEvent.COMPLETE
        )
        assert errors

    def test_cannot_complete_before_verified(self, execution, store):
        for make in (accepted_task, in_progress_task, submitted_task):
            task = make(store, task_id=f"T-{make.__name__}")
            errors = execution.validate_state(task, ExecutionEvent.COMPLETE)
            assert errors, f"{make.__name__} 不该允许 COMPLETE"

    def test_completed_is_terminal(self, execution, store):
        task = verified_task(store)
        execution.apply_complete(task, incoming=complete_message())

        for event in ExecutionEvent.all():
            errors = execution.validate_state(store.get("T-1"), event)
            assert errors, f"终态不该允许 {event}"

    def test_does_not_settle(self, execution, store):
        """
        【COMPLETE 不做结算】

            「活干完了」和「钱给了」是两件事。
            结算是一个独立的后续阶段。
        """
        task = verified_task(store)
        result = execution.apply_complete(task, incoming=complete_message())

        assert result.details["payment"] is None
        assert result.details["ledger"] is None

    def test_no_settlement_methods(self):
        for forbidden in ("settle", "pay", "transfer", "escrow",
                          "release", "refund"):
            assert not hasattr(TaskExecutionService, forbidden), (
                f"TaskExecutionService 不该有 {forbidden!r} —— 结算不是 4D 的事"
            )


# ============================================================
# 5. 幂等
# ============================================================
class TestIdempotency:
    def test_duplicate_start_detected(self, execution, store):
        task = accepted_task(store)
        message = start_message()
        execution.apply_start(task, incoming=message)

        assert execution.already_recorded(store.get("T-1"), message) is True

    def test_duplicate_submit_detected(self, execution, store):
        task = in_progress_task(store)
        message = submit_message()
        execution.apply_submit(
            task, TaskResultRef(result_ref="v1"), incoming=message
        )

        assert execution.already_recorded(store.get("T-1"), message) is True

    def test_duplicate_verify_detected(self, execution, store):
        task = submitted_task(store)
        message = verify_message(passed=True, feedback="通过")
        execution.apply_verify(
            task, TaskVerification(passed=True, feedback="通过"), incoming=message
        )

        assert execution.already_recorded(store.get("T-1"), message) is True

    def test_duplicate_does_not_create_second_record(self, execution, store):
        """
        【幂等是结构性的】

            message_id 是 task_executions 的主键，
            重复写入会撞主键被忽略 ——
            不依赖应用层是否记得先判断。
        """
        task = accepted_task(store)
        message = start_message()
        execution.apply_start(task, incoming=message)

        # 绕过 already_recorded 直接再写一次
        from core.task_execution import TaskExecutionRecord

        store.record_execution(TaskExecutionRecord(
            message_id=message.message_id,
            task_id="T-1",
            event_type=ExecutionEvent.START,
            from_agent="agent_b",
            status_after=TaskStatus.IN_PROGRESS,
        ))

        assert store.count_executions("T-1") == 1

    def test_already_processed_result_is_ok(self, execution, store):
        """
        重复投递**不是错误** ——
        重复送达在分布式系统里是常态，正确反应是「认出来，什么都不做」。
        """
        task = accepted_task(store)
        message = start_message()

        result = execution.already_processed(task, message)

        assert result.ok is True
        assert result.action == HandlerAction.ALREADY_PROCESSED
        assert result.reply_type is None

    def test_idempotency_does_not_change_state(self, execution, store):
        task = accepted_task(store)
        message = start_message()
        execution.apply_start(task, incoming=message)
        before = store.get("T-1").status

        execution.already_processed(store.get("T-1"), message)

        assert store.get("T-1").status == before
        assert store.count_executions("T-1") == 1


# ============================================================
# 6. attempt（第几轮）
# ============================================================
class TestAttempt:
    def test_attempt_is_derived_not_stored(self, execution, store):
        """
        「第几轮」是**数出来**的，不是存在字段里 ——
        这样它永远不会和实际事件数漂移。
        """
        task = in_progress_task(store)
        assert store.attempt_count("T-1") == 0

        execution.apply_submit(
            task, TaskResultRef(result_ref="v1"), incoming=submit_message()
        )
        assert store.attempt_count("T-1") == 1

        # 【注意 store.get() 每次都返回新对象】
        #     所以要「读一次 → 改 → 存同一个对象」，
        #     不能 read-modify 之后再 read 一个新的去 save（那会把改动覆盖掉）。
        task = store.get("T-1")
        task.verify(passed=False)
        store.save(task)

        execution.apply_submit(
            task, TaskResultRef(result_ref="v2"),
            incoming=submit_message(result_ref="v2"),
        )
        assert store.attempt_count("T-1") == 2

    def test_attempt_is_not_a_field(self):
        """没有 revision_id / round_id / attempt 这种存出来的字段。"""
        from dataclasses import fields

        from core.task_execution import TaskExecutionRecord

        names = {f.name for f in fields(TaskExecutionRecord)}

        for forbidden in ("attempt", "revision_id", "round_id",
                          "execution_id", "attempt_id"):
            assert forbidden not in names, (
                f"TaskExecutionRecord 里出现了 {forbidden} —— "
                f"「第几轮」应该是推导出来的，不是新的身份"
            )

    def test_history_summary_counts(self, execution, store):
        task = in_progress_task(store)
        execution.apply_submit(
            task, TaskResultRef(result_ref="v1"), incoming=submit_message()
        )
        execution.apply_verify(
            store.get("T-1"),
            TaskVerification(passed=False, feedback="要改"),
            incoming=verify_message(passed=False, feedback="要改"),
        )
        execution.apply_submit(
            store.get("T-1"), TaskResultRef(result_ref="v2"),
            incoming=submit_message(result_ref="v2"),
        )

        summary = execution.history_summary("T-1")

        assert summary["attempts"] == 2
        assert summary["rework_count"] == 1
        assert summary["submissions"] == 2
        assert summary["verifications"] == 1
        # SUBMIT + VERIFY + SUBMIT = 3 条（没有 START）
        assert len(summary["events"]) == 3


# ============================================================
# 7. 执行规则与状态机不漂移
# ============================================================
class TestRulesMatchStateMachine:
    """
    【为什么需要这组测试】

        ALLOWED_STATES（执行服务里）和 TRANSITIONS（Task Core 里）
        是同一件事的两份表达。

        它们一旦漂移，就会出现
        「服务说允许，但 Task Core 抛异常」这种自相矛盾的行为。
        所以必须有测试钉住它们。
    """

    def test_start_matches(self):
        from core.task import TRANSITIONS
        from services.task_execution_service import ALLOWED_STATES

        for state in ALLOWED_STATES[ExecutionEvent.START]:
            assert TaskStatus.IN_PROGRESS in TRANSITIONS[state]

    def test_submit_matches(self):
        from core.task import TRANSITIONS
        from services.task_execution_service import ALLOWED_STATES

        for state in ALLOWED_STATES[ExecutionEvent.SUBMIT]:
            assert TaskStatus.SUBMITTED in TRANSITIONS[state]

    def test_verify_matches(self):
        from core.task import TRANSITIONS
        from services.task_execution_service import ALLOWED_STATES

        for state in ALLOWED_STATES[ExecutionEvent.VERIFY]:
            assert TaskStatus.VERIFIED in TRANSITIONS[state]
            assert TaskStatus.IN_PROGRESS in TRANSITIONS[state]

    def test_complete_matches(self):
        from core.task import TRANSITIONS
        from services.task_execution_service import ALLOWED_STATES

        for state in ALLOWED_STATES[ExecutionEvent.COMPLETE]:
            assert TaskStatus.COMPLETED in TRANSITIONS[state]

    def test_every_event_has_allowed_states(self):
        from services.task_execution_service import ALLOWED_STATES

        for event in ExecutionEvent.all():
            assert event in ALLOWED_STATES, f"{event} 没有声明允许的状态"

    def test_every_event_has_a_required_role(self):
        from services.task_execution_service import REQUIRED_ROLE

        for event in ExecutionEvent.all():
            assert event in REQUIRED_ROLE, f"{event} 没有声明要求的角色"

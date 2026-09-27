"""
Test: 4D 消息处理与完整生命周期（Step 4D）
============================================================

【这个文件验证什么】

    Step 4D 的核心主张是：

        **两个 Agent 之间通过消息推进任务生命周期。**

    所以这里不做「直接调 apply_*」，而是**从 Message 进、从状态出**：

        Message → Registry 路由 → Handler → ExecutionService
                → Task Core → TaskStore

    最后用一条完整链路（含返工）把整条路走通。

【和 test_task_execution.py 的分工】

    test_task_execution.py       业务规则（谁能做、什么状态能做）
    test_execution_handlers.py   消息路由 + 端到端链路 + 边界证明
"""

from __future__ import annotations

import pytest

from core.decision_policy import RuleBasedDecisionPolicy
from core.money import Money
from core.task import Task, TaskStatus
from core.task_store import TaskStore

from communication import Message, MessageType

from services import (
    HandlerAction,
    TaskExecutionService,
    TaskNegotiationService,
    TaskService,
    default_handler_registry,
)


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


@pytest.fixture
def policies():
    return {
        "agent_a": RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=40, walk_away_price=100, max_rounds=2
        ),
        "agent_b": RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=60, walk_away_price=25, max_rounds=2
        ),
    }


@pytest.fixture
def execution(store) -> TaskExecutionService:
    return TaskExecutionService(store=store)


@pytest.fixture
def service(store, policies, execution) -> TaskService:
    """一个完整接线的门面（注册表里 8 种消息）。"""
    negotiation = TaskNegotiationService(store, policies)
    registry = default_handler_registry(negotiation, execution)
    return TaskService(
        store=store, policies=policies, handlers=registry,
        execution=execution,
    )


def msg(sender, recipient, message_type, payload, reply_to=None) -> Message:
    return Message(
        from_agent=sender, to_agent=recipient, message_type=message_type,
        payload=payload, reply_to=reply_to,
    )


def accepted_via_service(service, store, task_id="T-1") -> Message:
    """用消息谈成一个任务，返回最后那条 ACCEPT 消息。"""
    offer = msg("agent_a", "agent_b", MessageType.TASK_OFFER, {
        "task_id": task_id,
        "description": "写个分析程序",
        "budget": {"amount": 100, "currency": "AIC"},
        # workspace_id 只是**逻辑引用** ——
        # 4D 不创建它、不授权它、不读写它对应的任何文件。
        "workspace_id": "WS-001",
    })
    service.handle_message(offer)

    counter = msg("agent_b", "agent_a", MessageType.COUNTER_OFFER, {
        "task_id": task_id,
        "price": {"amount": 25, "currency": "AIC"},
    }, offer.message_id)
    service.handle_message(counter)

    accept = msg("agent_a", "agent_b", MessageType.ACCEPT, {
        "task_id": task_id,
        "price": {"amount": 25, "currency": "AIC"},
    }, counter.message_id)
    service.handle_message(accept)
    return accept


# ============================================================
# 1. Registry（8 种消息）
# ============================================================
class TestRegistry:
    def test_registry_has_eight_types(self, service):
        assert service.handlers.types() == [
            "ACCEPT", "COMPLETE", "COUNTER_OFFER", "REJECT",
            "START", "SUBMIT", "TASK_OFFER", "VERIFY",
        ]

    def test_4c_types_are_not_removed(self, service):
        """加了 4D 不能删掉 4C 的四种。"""
        for message_type in ("TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT"):
            assert message_type in service.handlers

    def test_4d_types_are_registered(self, service):
        for message_type in ("START", "SUBMIT", "VERIFY", "COMPLETE"):
            assert message_type in service.handlers

    def test_routing_goes_through_registry(self, service):
        """
        路由是「查表」而不是「硬编码分支」。

        【为什么看源码而不是调用一个方法】
            如果加一个 self.routing_is_hardcoded() 返回 False，
            那只是「自己声称自己没问题」——
            真正要证明的是**门面里没有 if/elif 分支**。
        """
        import inspect

        source = inspect.getsource(TaskService)

        assert 'if message.message_type ==' not in source
        assert 'elif message.message_type' not in source
        assert 'handlers.require' in source or 'handlers.get' in source

    def test_unregistered_type_still_loud(self, service):
        with pytest.raises(KeyError, match="没有处理"):
            service.handle_message(msg("a", "b", "没注册的类型", {}))

    def test_new_type_can_be_added_without_touching_facade(
        self, store, policies, execution
    ):
        """
        【上一轮重构买来的东西】

            加消息类型不需要改门面 —— 4D 自己就是证据：
            它加了四种类型，TaskService 的路由代码一行没改。
        """
        from services import (
            HandlerResult,
            TaskHandlerRegistry,
            TaskMessageHandler,
            TaskNegotiationService,
        )

        class _Probe(TaskMessageHandler):
            message_type = "SOME_FUTURE_TYPE"

            def handle(self, message):
                return HandlerResult(ok=True, action="probe")

        negotiation = TaskNegotiationService(store, policies)
        registry = default_handler_registry(negotiation, execution)
        registry.register(_Probe(negotiation))

        service = TaskService(store=store, policies=policies, handlers=registry)
        result = service.handle_message(
            msg("a", "b", "SOME_FUTURE_TYPE", {})
        )

        assert result.action == "probe"


# ============================================================
# 2. 通过 Handler 的四种消息
# ============================================================
class TestExecutionHandlers:
    def test_start_handler(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)

        result = service.handle_message(start)

        assert result.ok is True
        assert result.action == HandlerAction.STARTED
        assert store.get("T-1").status == TaskStatus.IN_PROGRESS

    def test_submit_handler(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)

        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1",
            "result": {"result_type": "code", "result_ref": "analysis-v1",
                       "summary": "初版"},
        }, start.message_id)
        result = service.handle_message(submit)

        assert result.ok is True
        assert result.action == HandlerAction.SUBMITTED
        assert store.get("T-1").status == TaskStatus.SUBMITTED

    def test_verify_handler_pass(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "v1"},
        }, start.message_id)
        service.handle_message(submit)

        verify = msg("agent_a", "agent_b", MessageType.VERIFY, {
            "task_id": "T-1", "passed": True, "feedback": "通过",
        }, submit.message_id)
        result = service.handle_message(verify)

        assert result.ok is True
        assert result.action == HandlerAction.VERIFIED
        assert store.get("T-1").status == TaskStatus.VERIFIED

    def test_verify_handler_fail(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "v1"},
        }, start.message_id)
        service.handle_message(submit)

        verify = msg("agent_a", "agent_b", MessageType.VERIFY, {
            "task_id": "T-1", "passed": False, "feedback": "第3个函数错了",
        }, submit.message_id)
        result = service.handle_message(verify)

        assert result.action == HandlerAction.REWORK
        assert store.get("T-1").status == TaskStatus.IN_PROGRESS

    def test_complete_handler(self, service, store):
        task_id = "T-1"
        accept = accepted_via_service(service, store, task_id)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": task_id}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": task_id, "result": {"result_ref": "v1"},
        }, start.message_id)
        service.handle_message(submit)
        verify = msg("agent_a", "agent_b", MessageType.VERIFY, {
            "task_id": task_id, "passed": True,
        }, submit.message_id)
        service.handle_message(verify)

        complete = msg("agent_a", "agent_b", MessageType.COMPLETE,
                       {"task_id": task_id}, verify.message_id)
        result = service.handle_message(complete)

        assert result.ok is True
        assert result.action == HandlerAction.COMPLETED
        assert store.get(task_id).status == TaskStatus.COMPLETED


# ============================================================
# 3. Handler 层的校验（协议错误 → ERROR，不改状态）
# ============================================================
class TestHandlerValidation:
    def test_wrong_role_rejected(self, service, store):
        """
        发起方替承接方发 START —— 必须回 ERROR，且**不改状态**。
        """
        accept = accepted_via_service(service, store)
        start = msg("agent_a", "agent_b", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)

        result = service.handle_message(start)

        assert result.ok is False
        assert result.reply_type == MessageType.ERROR
        assert store.get("T-1").status == TaskStatus.ACCEPTED

    def test_wrong_state_rejected(self, service, store):
        """跳过 START 直接 SUBMIT。"""
        accept = accepted_via_service(service, store)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "v1"},
        }, accept.message_id)

        result = service.handle_message(submit)

        assert result.ok is False
        assert store.get("T-1").status == TaskStatus.ACCEPTED

    def test_unknown_task_rejected(self, service):
        message = msg("agent_b", "agent_a", MessageType.START,
                      {"task_id": "根本没有这个任务"})

        result = service.handle_message(message)

        assert result.ok is False
        assert any("本地没有任务" in e for e in result.errors)

    def test_missing_task_id_rejected(self, service):
        result = service.handle_message(
            msg("agent_b", "agent_a", MessageType.START, {})
        )

        assert result.ok is False
        assert any("task_id" in e for e in result.errors)

    def test_submit_without_result_rejected(self, service, store):
        """
        【不能只发「我做好了」】
            验收需要一个对象。
        """
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)

        submit = msg("agent_b", "agent_a", MessageType.SUBMIT,
                     {"task_id": "T-1"}, start.message_id)
        result = service.handle_message(submit)

        assert result.ok is False
        assert any("result_ref" in e for e in result.errors)
        assert store.get("T-1").status == TaskStatus.IN_PROGRESS

    def test_submit_with_path_result_ref_rejected(self, service, store):
        """安全边界：result_ref 不许是路径。"""
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)

        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "/etc/passwd"},
        }, start.message_id)
        result = service.handle_message(submit)

        assert result.ok is False
        assert any("路径" in e for e in result.errors)

    def test_verify_without_passed_rejected(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "v1"},
        }, start.message_id)
        service.handle_message(submit)

        verify = msg("agent_a", "agent_b", MessageType.VERIFY,
                     {"task_id": "T-1"}, submit.message_id)
        result = service.handle_message(verify)

        assert result.ok is False
        assert any("passed" in e for e in result.errors)

    def test_verify_non_boolean_passed_rejected(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "v1"},
        }, start.message_id)
        service.handle_message(submit)

        verify = msg("agent_a", "agent_b", MessageType.VERIFY,
                     {"task_id": "T-1", "passed": 1}, submit.message_id)
        result = service.handle_message(verify)

        assert result.ok is False
        assert any("布尔" in e for e in result.errors)

    def test_verify_fail_without_feedback_rejected(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "v1"},
        }, start.message_id)
        service.handle_message(submit)

        verify = msg("agent_a", "agent_b", MessageType.VERIFY,
                     {"task_id": "T-1", "passed": False}, submit.message_id)
        result = service.handle_message(verify)

        assert result.ok is False
        assert any("feedback" in e for e in result.errors)

    def test_contractor_cannot_self_verify(self, service, store):
        """
        【4D 最关键的角色边界】

            承接方不能自己发 VERIFY 宣布「我验收自己通过」。
        """
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": "T-1", "result": {"result_ref": "v1"},
        }, start.message_id)
        service.handle_message(submit)

        self_verify = msg("agent_b", "agent_a", MessageType.VERIFY,
                          {"task_id": "T-1", "passed": True},
                          submit.message_id)
        result = service.handle_message(self_verify)

        assert result.ok is False
        assert any("发起方" in e for e in result.errors)
        assert store.get("T-1").status == TaskStatus.SUBMITTED

    def test_protocol_error_does_not_change_status(self, service, store):
        """协议错误 = 任务状态不变。这和「业务拒绝」是两回事。"""
        accept = accepted_via_service(service, store)

        service.handle_message(
            msg("agent_c", "agent_b", MessageType.START,
                {"task_id": "T-1"}, accept.message_id)
        )

        assert store.get("T-1").status == TaskStatus.ACCEPTED


# ============================================================
# 4. 幂等（通过 Handler）
# ============================================================
class TestHandlerIdempotency:
    def test_duplicate_start_is_idempotent(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        first = service.handle_message(start)

        # 同一条消息再送一次（同样的 message_id）
        again = service.handle_message(Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.START,
            payload={"task_id": "T-1"},
            reply_to=accept.message_id,
            message_id=start.message_id,
        ))

        assert first.action == HandlerAction.STARTED
        assert again.ok is True
        assert again.action == HandlerAction.ALREADY_PROCESSED
        assert store.count_executions("T-1") == 1

    def test_duplicate_submit_is_idempotent(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)

        submit = msg("agent_b", "agent_a", MessageType.SUBMIT,
                     {"task_id": "T-1", "result": {"result_ref": "v1"}},
                     start.message_id)
        service.handle_message(submit)

        again = service.handle_message(Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.SUBMIT,
            payload={"task_id": "T-1", "result": {"result_ref": "v1"}},
            reply_to=start.message_id,
            message_id=submit.message_id,
        ))

        assert again.action == HandlerAction.ALREADY_PROCESSED
        assert store.attempt_count("T-1") == 1

    def test_duplicate_verify_is_idempotent(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT,
                     {"task_id": "T-1", "result": {"result_ref": "v1"}},
                     start.message_id)
        service.handle_message(submit)

        verify = msg("agent_a", "agent_b", MessageType.VERIFY,
                     {"task_id": "T-1", "passed": True}, submit.message_id)
        service.handle_message(verify)

        again = service.handle_message(Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.VERIFY,
            payload={"task_id": "T-1", "passed": True},
            reply_to=submit.message_id,
            message_id=verify.message_id,
        ))

        assert again.action == HandlerAction.ALREADY_PROCESSED
        assert store.get("T-1").status == TaskStatus.VERIFIED

    def test_duplicate_does_not_create_second_submission(self, service, store):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)
        submit = msg("agent_b", "agent_a", MessageType.SUBMIT,
                     {"task_id": "T-1", "result": {"result_ref": "v1"}},
                     start.message_id)
        service.handle_message(submit)
        service.handle_message(Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.SUBMIT,
            payload={"task_id": "T-1", "result": {"result_ref": "v1"}},
            reply_to=start.message_id,
            message_id=submit.message_id,
        ))

        assert len(store.submissions_for("T-1")) == 1


# ============================================================
# 5. 完整生命周期（含返工）—— 4D 的核心验收
# ============================================================
class TestFullLifecycle:
    def test_accept_start_submit_verify_fail_rework_verify_pass_complete(
        self, service, store
    ):
        """
        Step 4D 的核心验收 Demo。

            A → TASK_OFFER
            B → COUNTER_OFFER
            A → ACCEPT              → ACCEPTED
            B → START               → IN_PROGRESS
            B → SUBMIT (v1)         → SUBMITTED
            A → VERIFY(passed=False)→ IN_PROGRESS   （返工）
            B → SUBMIT (v2)         → SUBMITTED
            A → VERIFY(passed=True) → VERIFIED
            A → COMPLETE            → COMPLETED
        """
        task_id = "T-4D"

        # ---- 议价 ----
        offer = msg("agent_a", "agent_b", MessageType.TASK_OFFER, {
            "task_id": task_id, "description": "写个分析程序",
            "budget": {"amount": 100, "currency": "AIC"},
        })
        service.handle_message(offer)
        assert store.get(task_id).status == TaskStatus.OFFERED

        counter = msg("agent_b", "agent_a", MessageType.COUNTER_OFFER, {
            "task_id": task_id, "price": {"amount": 25, "currency": "AIC"},
        }, offer.message_id)
        service.handle_message(counter)
        assert store.get(task_id).status == TaskStatus.NEGOTIATING

        accept = msg("agent_a", "agent_b", MessageType.ACCEPT, {
            "task_id": task_id, "price": {"amount": 25, "currency": "AIC"},
        }, counter.message_id)
        service.handle_message(accept)
        assert store.get(task_id).status == TaskStatus.ACCEPTED

        # ---- 执行 ----
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": task_id}, accept.message_id)
        assert service.handle_message(start).ok
        assert store.get(task_id).status == TaskStatus.IN_PROGRESS

        submit1 = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": task_id,
            "result": {"result_type": "code", "result_ref": "analysis-v1",
                       "summary": "初版"},
        }, start.message_id)
        assert service.handle_message(submit1).ok
        assert store.get(task_id).status == TaskStatus.SUBMITTED

        verify1 = msg("agent_a", "agent_b", MessageType.VERIFY, {
            "task_id": task_id, "passed": False,
            "feedback": "第 3 个函数在空输入时抛异常",
        }, submit1.message_id)
        result = service.handle_message(verify1)
        assert result.action == HandlerAction.REWORK
        assert store.get(task_id).status == TaskStatus.IN_PROGRESS

        submit2 = msg("agent_b", "agent_a", MessageType.SUBMIT, {
            "task_id": task_id,
            "result": {"result_type": "code", "result_ref": "analysis-v2",
                       "summary": "已修复空输入"},
        }, verify1.message_id)
        assert service.handle_message(submit2).ok
        assert store.get(task_id).status == TaskStatus.SUBMITTED

        verify2 = msg("agent_a", "agent_b", MessageType.VERIFY, {
            "task_id": task_id, "passed": True, "feedback": "验收通过",
        }, submit2.message_id)
        assert service.handle_message(verify2).action == HandlerAction.VERIFIED
        assert store.get(task_id).status == TaskStatus.VERIFIED

        complete = msg("agent_a", "agent_b", MessageType.COMPLETE,
                       {"task_id": task_id, "summary": "正式结束"},
                       verify2.message_id)
        assert service.handle_message(complete).action == HandlerAction.COMPLETED
        assert store.get(task_id).status == TaskStatus.COMPLETED

        # ---- 事件历史 ----
        events = [r.event_type for r in store.executions_for(task_id)]
        assert events == [
            "START", "SUBMIT", "VERIFY", "SUBMIT", "VERIFY", "COMPLETE"
        ]
        assert store.attempt_count(task_id) == 2

    def test_chain_is_traceable_via_reply_to(self, service, store):
        """
        【协议 §18 的 reply_to 在 4D 里同样成立】

            执行链可以一路回溯：
            COMPLETE → VERIFY → SUBMIT → START → ACCEPT
        """
        task_id = "T-CHAIN"
        accept = accepted_via_service(service, store, task_id)

        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": task_id}, accept.message_id)
        service.handle_message(start)

        submit = msg("agent_b", "agent_a", MessageType.SUBMIT,
                     {"task_id": task_id, "result": {"result_ref": "v1"}},
                     start.message_id)
        service.handle_message(submit)

        verify = msg("agent_a", "agent_b", MessageType.VERIFY,
                     {"task_id": task_id, "passed": True}, submit.message_id)
        service.handle_message(verify)

        complete = msg("agent_a", "agent_b", MessageType.COMPLETE,
                       {"task_id": task_id}, verify.message_id)
        service.handle_message(complete)

        records = {r.event_type: r for r in store.executions_for(task_id)}
        assert records["COMPLETE"].reply_to == records["VERIFY"].message_id
        assert records["VERIFY"].reply_to == records["SUBMIT"].message_id
        assert records["SUBMIT"].reply_to == records["START"].message_id
        assert records["START"].reply_to == accept.message_id

    def test_all_events_share_trace(self, service, store):
        """同一条链上的 trace_id 应该一致（trace 缝合）。"""
        task_id = "T-TRACE"
        accept = accepted_via_service(service, store, task_id)

        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": task_id}, accept.message_id)
        service.handle_message(start)

        task = store.get(task_id)
        assert task.current_trace_id == start.trace_id

    def test_attempts_increase_with_rework(self, service, store):
        task_id = "T-ATTEMPT"
        accept = accepted_via_service(service, store, task_id)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": task_id}, accept.message_id)
        service.handle_message(start)

        previous = start
        for attempt in range(1, 4):
            submit = msg("agent_b", "agent_a", MessageType.SUBMIT, {
                "task_id": task_id,
                "result": {"result_ref": f"v{attempt}"},
            }, previous.message_id)
            service.handle_message(submit)
            assert store.attempt_count(task_id) == attempt

            verify = msg("agent_a", "agent_b", MessageType.VERIFY, {
                "task_id": task_id, "passed": False,
                "feedback": f"第 {attempt} 轮还不行",
            }, submit.message_id)
            service.handle_message(verify)
            previous = verify

        assert store.attempt_count(task_id) == 3
        assert len(store.verifications_for(task_id)) == 3

    def test_old_feedback_survives_new_round(self, service, store):
        """
        【为什么历史必须留在库里】

            第二轮之后，第一轮为什么被打回不应该消失 ——
            那正是承接方返工时要参考的。
        """
        task_id = "T-HISTORY"
        accept = accepted_via_service(service, store, task_id)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": task_id}, accept.message_id)
        service.handle_message(start)

        submit1 = msg("agent_b", "agent_a", MessageType.SUBMIT,
                      {"task_id": task_id, "result": {"result_ref": "v1"}},
                      start.message_id)
        service.handle_message(submit1)
        verify1 = msg("agent_a", "agent_b", MessageType.VERIFY, {
            "task_id": task_id, "passed": False, "feedback": "第一轮的问题",
        }, submit1.message_id)
        service.handle_message(verify1)

        submit2 = msg("agent_b", "agent_a", MessageType.SUBMIT,
                      {"task_id": task_id, "result": {"result_ref": "v2"}},
                      verify1.message_id)
        service.handle_message(submit2)

        verifications = store.verifications_for(task_id)
        assert len(verifications) == 1
        assert verifications[0].verification.feedback == "第一轮的问题"

        # 而且它仍然可查
        assert "第一轮的问题" in store.latest_verification(task_id).verification.feedback


# ============================================================
# 6. 边界：4D 不碰钱 / 不调 LLM / 不建 Workspace
# ============================================================
class TestBoundaries:
    def test_full_lifecycle_moves_no_money(
        self, db, economy, funded_agents
    ):
        """
        【Step 4D 必须证明的事】

            完整执行流程前后，Wallet 和 Ledger 都不变。
            「活干完了」和「钱给了」是两件事。
        """
        store = TaskStore(economy.db)
        execution = TaskExecutionService(store)
        ledger_before = economy.ledger.count()
        balance_a = economy.agent("agent_a").balance
        balance_b = economy.agent("agent_b").balance

        from core.task_execution import TaskResultRef, TaskVerification

        task = Task.create(task_id="T-MONEY", creator="agent_a",
                           description="d", budget=Money(100),
                           participants=["agent_a", "agent_b"])
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        store.save(task)

        def m(sender, recipient, message_type, payload, reply_to=None):
            return Message(from_agent=sender, to_agent=recipient,
                           message_type=message_type, payload=payload,
                           reply_to=reply_to)

        execution.apply_start(store.get("T-MONEY"), incoming=m(
            "agent_b", "agent_a", MessageType.START, {"task_id": "T-MONEY"}))
        execution.apply_submit(store.get("T-MONEY"), TaskResultRef(result_ref="v1"),
                               incoming=m("agent_b", "agent_a", MessageType.SUBMIT,
                                          {"task_id": "T-MONEY"}))
        execution.apply_verify(store.get("T-MONEY"),
                               TaskVerification(passed=False, feedback="改"),
                               incoming=m("agent_a", "agent_b", MessageType.VERIFY,
                                          {"task_id": "T-MONEY"}))
        execution.apply_submit(store.get("T-MONEY"), TaskResultRef(result_ref="v2"),
                               incoming=m("agent_b", "agent_a", MessageType.SUBMIT,
                                          {"task_id": "T-MONEY"}))
        execution.apply_verify(store.get("T-MONEY"),
                               TaskVerification(passed=True, feedback="过"),
                               incoming=m("agent_a", "agent_b", MessageType.VERIFY,
                                          {"task_id": "T-MONEY"}))
        execution.apply_complete(store.get("T-MONEY"), incoming=m(
            "agent_a", "agent_b", MessageType.COMPLETE, {"task_id": "T-MONEY"}))

        assert store.get("T-MONEY").status == TaskStatus.COMPLETED
        assert economy.ledger.count() == ledger_before
        assert economy.agent("agent_a").balance == balance_a
        assert economy.agent("agent_b").balance == balance_b
        assert economy.audit()["balanced"] is True

    def test_no_workspace_created(self, service, store, db):
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)
        service.handle_message(start)

        assert store.get("T-1").workspace_id == "WS-001"   # 只是个引用
        for forbidden in ("workspace_members", "workspace_permissions",
                          "workspace_files"):
            assert forbidden not in db.table_names()

    def test_execution_details_declare_no_side_effects(self, service, store):
        accept = accepted_via_service(service, store)
        result = service.handle_message(
            msg("agent_b", "agent_a", MessageType.START,
                {"task_id": "T-1"}, accept.message_id)
        )

        assert result.details["payment"] is None
        assert result.details["ledger"] is None
        assert result.details["workspace"] is None
        assert result.details["git"] is None
        assert result.details["llm_calls"] == 0
        assert result.details["code_executed"] is False

    def test_execution_service_has_no_code_execution(self):
        for forbidden in ("subprocess", "run", "execute_code", "shell",
                          "sandbox", "docker", "popen"):
            assert not hasattr(TaskExecutionService, forbidden), (
                f"TaskExecutionService 不该有 {forbidden!r} —— 4D 不执行代码"
            )

    def test_start_does_not_reply(self, service, store):
        """
        START / SUBMIT / VERIFY / COMPLETE 都是**单向通知** ——
        对方不需要为它再发一条消息。
        """
        accept = accepted_via_service(service, store)
        start = msg("agent_b", "agent_a", MessageType.START,
                    {"task_id": "T-1"}, accept.message_id)

        result = service.handle_message(start)

        assert result.reply_type is None
        assert result.to_reply(start) is None

"""
Test: 消息处理的持久化事实（Step 4D.1 / 目标 A）
============================================================

【这个文件验证什么】

    4D.1 之前：

        「这条消息处理过了吗」= 进程内存里的一个 OrderedDict

        进程重启 → 忘光 → 同一条消息被再处理一遍。

    4D.1 之后：

        这个问题由**数据库**回答：

            communication_messages.processed_once

        内存 tracker 退化成快路径缓存，丢了也不影响正确性。

【最重要的几组断言】

    · 处理事实跨「进程重启」仍然有效（新 runtime / 新 tracker / 同一个库）
    · 重复消息的业务副作用只发生一次
    · 两个 worker 同时认领同一条消息，只有一个成功
    · 失败**不**被标记成「已处理」
    · processed_once 只增不减（不会被后来的 DUPLICATE 事件抹掉）
"""

from __future__ import annotations

import pytest

from communication import LocalTransport, Message, MessageType
from communication.dedup import ClaimStatus
from communication.recorder import CommunicationRecorder, MessageStatus
from storage.database import Database


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def recorder(db) -> CommunicationRecorder:
    return CommunicationRecorder(db)


def make_message(message_id="msg_1", message_type=MessageType.SUBMIT,
                 from_agent="agent_a", to_agent="agent_b", payload=None):
    return Message(
        from_agent=from_agent, to_agent=to_agent,
        message_type=message_type,
        payload=payload or {"task_id": "T-1"},
        message_id=message_id,
    )


class FakeRuntime:
    """
    模拟一次「进程启动」：全新的 DB 句柄、recorder、communication manager。

    【为什么不真的 kill 进程】
        本阶段要证明的是「去重事实来自持久化数据」，
        而不是「操作系统能重启一个进程」。
        用一个全新的 runtime 对象指向同一个数据库文件，
        就已经证明了这一点 —— 内存里什么都不剩。

        真正 kill 进程反而是更弱的测试（不可重现、慢）。
    """

    def __init__(self, db_path):
        from communication import CommunicationManager

        self.db = Database(str(db_path))
        self.db.initialize()
        self.recorder = CommunicationRecorder(self.db)
        self.transport = LocalTransport()
        self.comm = CommunicationManager(self.transport, recorder=self.recorder)
        self.handled = []

        def handler(message):
            self.handled.append(message.message_id)
            return None

        for message_type in (MessageType.SUBMIT, MessageType.START,
                             MessageType.VERIFY, MessageType.COMPLETE):
            self.comm.register_type_handler(message_type, handler)
        self.comm.register("agent_b")

    def close(self):
        self.db.close()


@pytest.fixture
def runtime_factory(tmp_path):
    """造 runtime 的工厂（每次调用 = 一次新的「进程启动」）。"""
    created = []

    def factory(name="reliability.db"):
        rt = FakeRuntime(tmp_path / name)
        created.append(rt)
        return rt

    yield factory

    for rt in created:
        rt.close()


# ============================================================
# A-1. 认领的基本语义
# ============================================================
class TestClaimBasics:
    def test_first_claim_succeeds(self, recorder):
        claim = recorder.claim_message(make_message(), "worker-1")

        assert claim.status == ClaimStatus.CLAIMED
        assert claim.claimed is True
        assert bool(claim) is True

    def test_second_claim_from_same_worker_is_busy(self, recorder):
        recorder.claim_message(make_message(), "worker-1")
        again = recorder.claim_message(make_message(), "worker-1")

        assert again.status == ClaimStatus.BUSY
        assert again.owner == "worker-1"

    def test_second_claim_from_another_worker_is_busy(self, recorder):
        """两个 worker 撞车 —— 只有一个能处理。"""
        first = recorder.claim_message(make_message(), "worker-1")
        second = recorder.claim_message(make_message(), "worker-2")

        assert first.claimed is True
        assert second.status == ClaimStatus.BUSY
        assert second.owner == "worker-1"

    def test_claim_after_processed_is_duplicate(self, recorder):
        message = make_message()
        recorder.claim_message(message, "worker-1")
        recorder.mark_processed(message, "worker-1")

        claim = recorder.claim_message(message, "worker-2")

        assert claim.status == ClaimStatus.DUPLICATE
        assert claim.duplicate is True

    def test_claim_is_per_message_id(self, recorder):
        """不同 message_id 互不影响。"""
        a = recorder.claim_message(make_message("msg_a"), "w")
        b = recorder.claim_message(make_message("msg_b"), "w")

        assert a.claimed and b.claimed

    def test_claim_result_is_immutable(self, recorder):
        claim = recorder.claim_message(make_message(), "w")

        with pytest.raises(Exception):
            claim.status = ClaimStatus.DUPLICATE


# ============================================================
# A-2. processed_once 是持久化事实
# ============================================================
class TestProcessedIsPersistent:
    def test_initially_not_processed(self, recorder):
        assert recorder.was_processed("msg_1") is False

    def test_mark_processed_persists(self, recorder):
        message = make_message()
        recorder.claim_message(message, "w")

        first = recorder.mark_processed(message, "w")

        assert first is True
        assert recorder.was_processed("msg_1") is True

    def test_mark_processed_is_idempotent(self, recorder):
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_processed(message, "w")

        second = recorder.mark_processed(message, "w")

        assert second is False, "第二次不该被算作「首次成功」"
        assert recorder.was_processed("msg_1") is True

    def test_survives_duplicate_journal_event(self, recorder):
        """
        【这条是本文件最关键的一条】

            status 列会被后来的事件覆盖：

                处理成功      → status = PROCESSED
                又来一条重复  → status = DUPLICATE   ← PROCESSED 被抹掉

            如果去重依赖 status，重复投递反而会把「处理过」这个事实擦掉。
            processed_once 必须只增不减。
        """
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_processed(message, "w")

        # 后来又来了一条重复投递，记一条 DUPLICATE 事件
        recorder.record(message, MessageStatus.DUPLICATE, actor="agent_b")

        state = recorder.processing_state("msg_1")
        assert state["status"] == MessageStatus.DUPLICATE, "status 确实被覆盖了"
        assert recorder.was_processed("msg_1") is True, (
            "但 processed_once 必须还在 —— 否则去重就失效了"
        )

    def test_processed_at_is_recorded(self, recorder):
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_processed(message, "w")

        assert recorder.processing_state("msg_1")["processed_at"] is not None

    def test_count_processed(self, recorder):
        for index in range(3):
            message = make_message(f"msg_{index}")
            recorder.claim_message(message, "w")
            recorder.mark_processed(message, "w")

        assert recorder.count_processed() == 3

    def test_unknown_message_state_is_none(self, recorder):
        assert recorder.processing_state("从来没见过的 id") is None


# ============================================================
# A-3. 失败：记录事实，但不标成完成
# ============================================================
class TestFailureSemantics:
    def test_failure_does_not_mark_processed(self, recorder):
        """
        【失败 ≠ 完成】

            标成完成会让这条消息永远失去被正确处理的机会，
            而且没人会发现。
        """
        message = make_message()
        recorder.claim_message(message, "w")

        recorder.mark_failed(message, "w", "ValueError", "临时错误")

        assert recorder.was_processed("msg_1") is False

    def test_failure_is_recorded(self, recorder):
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_failed(message, "w", "ValueError", "出错了")

        state = recorder.processing_state("msg_1")

        assert state["failure_count"] == 1
        assert state["error_code"] == "ValueError"
        assert "出错了" in state["error_message"]

    def test_failure_releases_claim(self, recorder):
        """
        失败之后不该继续占着认领 ——
        否则这条消息会永远显示「有人正在处理」。
        """
        message = make_message()
        recorder.claim_message(message, "worker-1")
        recorder.mark_failed(message, "worker-1", "E", "boom")

        assert recorder.processing_state("msg_1")["claimed_by"] is None

        # 认领释放了，另一个 worker 可以接手
        again = recorder.claim_message(message, "worker-2")
        assert again.claimed is True

    def test_failure_count_accumulates(self, recorder):
        message = make_message()
        for _ in range(3):
            recorder.claim_message(message, "w")
            recorder.mark_failed(message, "w", "E", "boom")

        assert recorder.processing_state("msg_1")["failure_count"] == 3

    def test_failure_then_success(self, recorder):
        """失败之后成功了 —— 事实要如实反映。"""
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_failed(message, "w", "E", "第一次失败")

        recorder.claim_message(message, "w")
        recorder.mark_processed(message, "w")

        state = recorder.processing_state("msg_1")
        assert recorder.was_processed("msg_1") is True
        assert state["failure_count"] == 1, "失败的历史不该被抹掉"

    def test_failed_message_is_not_duplicate(self, recorder):
        """
        失败过的消息**不是**重复消息 —— 它还没被成功处理过。

        混为一谈的话，一次临时失败就会让这条消息永久作废。
        """
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_failed(message, "w", "E", "boom")

        claim = recorder.claim_message(message, "w")

        assert claim.status == ClaimStatus.CLAIMED
        assert recorder.was_processed("msg_1") is False

    def test_error_message_is_redacted(self, recorder):
        """
        失败原因也要脱敏 —— 不能因为记错误就把 API Key 写进库里。

        （recorder 本来就有脱敏逻辑，这里确认新方法也走了它。）
        """
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_failed(
            message, "w", "AuthError",
            "key is sk-1234567890abcdef1234567890abcdef",
        )

        state = recorder.processing_state("msg_1")
        assert "sk-1234567890abcdef1234567890abcdef" not in (
            state["error_message"] or ""
        )


# ============================================================
# A-4. 三种状态互不混淆
# ============================================================
class TestStatesAreDistinct:
    def test_received_is_not_processed(self, recorder):
        """
        「消息进了 communication_messages 表」
            ≠
        「消息已经成功处理」
        """
        message = make_message()
        recorder.record(message, MessageStatus.RECEIVED, actor="agent_b")

        assert recorder.processing_state("msg_1") is not None, "行存在"
        assert recorder.was_processed("msg_1") is False, "但没处理过"

    def test_duplicate_is_not_a_new_processing(self, recorder):
        message = make_message()
        recorder.claim_message(message, "w")
        recorder.mark_processed(message, "w")
        before = recorder.count_processed()

        recorder.record(message, MessageStatus.DUPLICATE, actor="agent_b")

        assert recorder.count_processed() == before

    def test_failed_and_processed_are_different_facts(self, recorder):
        ok = make_message("msg_ok")
        bad = make_message("msg_bad")

        recorder.claim_message(ok, "w"); recorder.mark_processed(ok, "w")
        recorder.claim_message(bad, "w"); recorder.mark_failed(bad, "w", "E", "x")

        assert recorder.was_processed("msg_ok") is True
        assert recorder.was_processed("msg_bad") is False
        assert recorder.processing_state("msg_bad")["failure_count"] == 1
        assert recorder.processing_state("msg_ok")["failure_count"] == 0


# ============================================================
# A-5. Manager 端到端（有持久层）
# ============================================================
class TestManagerWithPersistence:
    def test_duplicate_processed_once(self, runtime_factory, run_async):
        rt = runtime_factory()

        async def scenario():
            message = make_message("msg_dup")
            await rt.comm.send_message(message)
            await rt.comm.send_message(message)      # 同一条再来一次
            first = await rt.comm.receive("agent_b", timeout=1.0)
            second = await rt.comm.receive("agent_b", timeout=1.0)
            return first, second

        first, second = run_async(scenario())

        assert first.status == "delivered"
        assert second.status == "duplicate"
        assert rt.handled == ["msg_dup"], "业务副作用必须只发生一次"

    def test_three_times_only_one_effect(self, runtime_factory, run_async):
        rt = runtime_factory("three.db")

        async def scenario():
            message = make_message("msg_x3")
            for _ in range(3):
                await rt.comm.send_message(message)
            results = []
            for _ in range(3):
                results.append(await rt.comm.receive("agent_b", timeout=1.0))
            return results

        results = run_async(scenario())

        assert results[0].status == "delivered"
        assert results[1].status == "duplicate"
        assert results[2].status == "duplicate"
        assert len(rt.handled) == 1

    def test_was_processed_exposed_on_manager(self, runtime_factory, run_async):
        rt = runtime_factory("exposed.db")

        async def scenario():
            message = make_message("msg_exposed")
            await rt.comm.send_message(message)
            await rt.comm.receive("agent_b", timeout=1.0)

        run_async(scenario())

        assert rt.comm.was_processed("msg_exposed") is True
        assert rt.comm.was_processed("不存在的") is False

    @pytest.mark.parametrize(
        "message_type",
        [MessageType.START, MessageType.SUBMIT,
         MessageType.VERIFY, MessageType.COMPLETE],
    )
    def test_each_4d_type_is_idempotent(self, runtime_factory, run_async,
                                        message_type):
        """
        4D 的四种消息各自验证一遍幂等。

        （SUBMIT 最重要 —— 重复它意味着交付两次成果。）
        """
        rt = runtime_factory(f"{message_type}.db")

        async def scenario():
            message = make_message(f"msg_{message_type}", message_type)
            await rt.comm.send_message(message)
            await rt.comm.send_message(message)
            return (
                await rt.comm.receive("agent_b", timeout=1.0),
                await rt.comm.receive("agent_b", timeout=1.0),
            )

        first, second = run_async(scenario())

        assert first.status == "delivered"
        assert second.status == "duplicate"
        assert len(rt.handled) == 1

    def test_failed_handler_is_not_marked_processed(self, runtime_factory,
                                                    run_async):
        """
        handler 抛异常 → 消息**不能**被标成「已处理」。
        否则重试的机会就被永久抹掉了。
        """
        rt = runtime_factory("failed.db")

        def boom(message):
            rt.handled.append(message.message_id)
            raise RuntimeError("处理失败了")

        rt.comm.register_type_handler(MessageType.SUBMIT, boom)

        async def scenario():
            message = make_message("msg_fail")
            await rt.comm.send_message(message)
            try:
                await rt.comm.receive("agent_b", timeout=1.0)
            except RuntimeError:
                pass

        run_async(scenario())

        assert rt.comm.was_processed("msg_fail") is False
        state = rt.recorder.processing_state("msg_fail")
        assert state["failure_count"] == 1

    def test_duplicate_event_is_journaled(self, runtime_factory, run_async):
        rt = runtime_factory("journal.db")

        async def scenario():
            message = make_message("msg_j")
            await rt.comm.send_message(message)
            await rt.comm.send_message(message)
            await rt.comm.receive("agent_b", timeout=1.0)
            await rt.comm.receive("agent_b", timeout=1.0)

        run_async(scenario())

        events = [e["status"] for e in rt.recorder.events_for("msg_j")]
        assert MessageStatus.DUPLICATE in events


# ============================================================
# A-6. 重启模拟（本阶段必须新增的一类测试）
# ============================================================
class TestRestartSimulation:
    def test_new_runtime_detects_duplicate(self, runtime_factory, run_async):
        """
        【4D.1 的核心验收】

            Runtime A 处理 X → 落库
            Runtime B（全新进程、全新 tracker、同一个库）收到 X
            → 必须识别为重复

        重点：B 的 tracker 是**空的**，
        所以它只能靠持久化数据判断。
        """
        rt_a = runtime_factory("restart.db")

        async def phase_a():
            message = make_message("msg_restart")
            await rt_a.comm.send_message(message)
            await rt_a.comm.receive("agent_b", timeout=1.0)

        run_async(phase_a())
        rt_a.close()

        # ---- 全新 runtime ----
        rt_b = runtime_factory("restart.db")

        tracker_b = rt_b.comm.seen_tracker("agent_b")
        assert tracker_b is None or tracker_b.size == 0, (
            "新进程的内存 tracker 必须是空的 —— 否则这个测试就没意义了"
        )
        assert rt_b.comm.was_processed("msg_restart") is True, (
            "但它仍然知道这条消息处理过 —— 因为事实在数据库里"
        )

        async def phase_b():
            message = make_message("msg_restart")
            await rt_b.comm.send_message(message)
            return await rt_b.comm.receive("agent_b", timeout=1.0)

        result = run_async(phase_b())

        assert result.status == "duplicate"
        assert rt_b.handled == [], "重启后不该再产生一次业务副作用"

    def test_clearing_tracker_does_not_break_dedup(self, runtime_factory,
                                                   run_async):
        """
        【Guard 2 的行为证明】

            把内存 tracker 清空 —— 去重必须照常工作。
            这说明正确性不依赖它。
        """
        rt = runtime_factory("cleared.db")

        async def scenario():
            message = make_message("msg_clear")
            await rt.comm.send_message(message)
            await rt.comm.receive("agent_b", timeout=1.0)

            # 粗暴地清空内存缓存
            rt.comm.seen_tracker("agent_b").clear()

            await rt.comm.send_message(message)
            return await rt.comm.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        assert result.status == "duplicate"
        assert len(rt.handled) == 1

    def test_fresh_tracker_instance_still_dedups(self, runtime_factory,
                                                 run_async):
        """换一个全新的 tracker 实例（模拟进程重启的内存状态）也一样。"""
        from communication.dedup import SeenMessageTracker

        rt = runtime_factory("fresh_tracker.db")

        async def scenario():
            message = make_message("msg_fresh")
            await rt.comm.send_message(message)
            await rt.comm.receive("agent_b", timeout=1.0)

            # 直接换掉内存 tracker
            rt.comm._seen["agent_b"] = SeenMessageTracker(1024)

            await rt.comm.send_message(message)
            return await rt.comm.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        assert result.status == "duplicate"
        assert len(rt.handled) == 1

    def test_processed_fact_readable_without_manager(self, tmp_path):
        """
        事实在数据库里，所以**不需要 Manager** 就能查到。

        用一个全新的 Database 句柄直接读 ——
        证明它不是某个对象的内部状态。
        """
        db_path = tmp_path / "direct.db"
        rt = FakeRuntime(db_path)

        async def scenario():
            message = make_message("msg_direct")
            await rt.comm.send_message(message)
            await rt.comm.receive("agent_b", timeout=1.0)

        import asyncio
        asyncio.run(scenario())
        rt.close()

        # 全新句柄，直接查库
        db = Database(str(db_path))
        db.initialize()
        reader = CommunicationRecorder(db)

        assert reader.was_processed("msg_direct") is True
        assert reader.processing_state("msg_direct")["processed"] is True
        db.close()


# ============================================================
# A-7. 降级模式（没有持久层）
# ============================================================
class TestDegradedMode:
    """
    没有 recorder 时（纯通信单元测试）退回内存 tracker。

    这是一条**降级路径**，不是设计目标 ——
    它的局限（重启失忆）正是 4D.1 要修的问题本身。
    """

    def test_memory_mode_still_dedups_within_process(self, comm_manager,
                                                     run_async):
        from tests.communication_helpers import FakeAgent

        comm_manager.register(FakeAgent("agent_a"))
        comm_manager.register(FakeAgent("agent_b"))

        async def scenario():
            message = make_message("msg_mem")
            await comm_manager.send_message(message)
            await comm_manager.send_message(message)
            return (
                await comm_manager.receive("agent_b", timeout=1.0),
                await comm_manager.receive("agent_b", timeout=1.0),
            )

        first, second = run_async(scenario())

        assert first.status == "delivered"
        assert second.status == "duplicate"

    def test_memory_mode_loses_fact_on_restart(self, transport, run_async):
        """
        【降级模式的局限 —— 明确测出来，不藏着】

            没有持久层时，重启就失忆。
            这正是为什么**生产路径必须注入 recorder**。
        """
        from communication import CommunicationManager
        from tests.communication_helpers import FakeAgent

        handled = []

        def build():
            comm = CommunicationManager(transport)   # 没有 recorder
            for message_type in (MessageType.SUBMIT,):
                comm.register_type_handler(
                    message_type,
                    lambda m: handled.append(m.message_id) or None,
                )
            comm.register(FakeAgent("agent_b"))
            return comm

        async def scenario():
            message = make_message("msg_lost")

            comm_a = build()
            await comm_a.send_message(message)
            await comm_a.receive("agent_b", timeout=1.0)

            # 「重启」：全新 Manager，同一个 transport
            comm_b = build()
            await comm_b.send_message(message)
            return await comm_b.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        assert result.status == "delivered", (
            "降级模式确实会失忆 —— 这就是 4D.1 要修的问题本身"
        )
        assert len(handled) == 2

"""
Event Wakeup V1 —— 定向测试
============================================================

【🔒 本文件不调用真实 LLM】

    唤醒机制和模型能力是两件事。这里的「处理」都是确定性回调。

【🔒 不用 sleep(1) 判断时序】（任务书 §13）

    全部用 asyncio 的确定性同步：
        · 自旋等状态转移（`await asyncio.sleep(0)`）
        · `asyncio.Event` 作为屏障
    没有一处用「睡一会儿看看好了没」。
"""

from __future__ import annotations

import asyncio

import pytest

from communication.agent_runtime import (
    AgentRuntime,
    RuntimeAlreadyActive,
    RuntimeState,
)
from communication.local_transport import LocalTransport
from communication.manager import CommunicationManager
from communication.wakeup import WakeupReason


# ============================================================
# 脚手架
# ============================================================
class FakeAgent:
    """
    够用的 Agent 形状：`agent_id` + `handle_message`。

    `handle_message` **就是**「Agent 怎么决定处理这条消息」的地方 ——
    Runtime 不该越过它（§6）。
    """

    def __init__(self, agent_id: str, reply: bool = True):
        self.agent_id = agent_id
        self.reply = reply
        self.received: list = []
        self.handler_calls = 0
        self.handler_entered = asyncio.Event()

    async def comm_send(self, comm, to: str, text: str):
        """经 CommunicationManager 发送（不绕过它）。"""
        from communication.message import Message

        # ⚠️ `Message` 没有 `text=` 参数 —— `text` 是 payload 的**派生属性**
        #    （`message.payload["text"]`）。第一版写成了 text=，直接 TypeError。
        return await comm.send_message(Message(
            from_agent=self.agent_id, to_agent=to,
            payload={"text": text},
        ))

    def handle_message(self, message):
        self.handler_calls += 1
        self.received.append(message)
        self.handler_entered.set()
        if self.reply:
            return {"ack": f"Hello {message.from_agent}"}
        return None


def build(*agent_ids: str):
    """建一个真实的 manager + 若干 FakeAgent（不碰 LLM / Task / Economy）。"""
    transport = LocalTransport()
    comm = CommunicationManager(transport=transport)
    agents = {}
    for aid in agent_ids:
        transport.open_mailbox(aid)
        agent = FakeAgent(aid)
        comm.register(agent)
        agents[aid] = agent
    return comm, agents


async def idle():
    """让出控制权，用来做确定性的自旋等待。"""
    await asyncio.sleep(0)


async def wait_for(predicate, limit: int = 5000) -> bool:
    """自旋等到条件成立。返回是否等到（**不用定时器猜**）。"""
    for _ in range(limit):
        if predicate():
            return True
        await idle()
    return False


# ============================================================
# Test 1: B sleeping → A sends → B wakes
# ============================================================
class TestWakeup:
    def test_sleeping_agent_wakes_on_delivery(self):
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            a, b = agents["agent_a"], agents["agent_b"]

            rt_b = AgentRuntime(agent=b, communication=comm)
            task = asyncio.create_task(rt_b.run_forever())

            # 🔒 断言「发送之前 B 确实在睡」—— 这是本测试的核心
            assert await wait_for(lambda: rt_b.state == RuntimeState.WAITING)
            assert rt_b.is_sleeping
            assert rt_b.wakeup_count == 0

            await a.comm_send(comm, "agent_b", "Hello B")

            assert await wait_for(lambda: rt_b.processed_count >= 1)
            assert rt_b.wakeup_count == 1
            assert b.received[0].text == "Hello B"

            # 没有人调用过 receive("agent_b") —— 是信号把它叫醒的
            rt_b.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

    def test_no_message_keeps_agent_sleeping(self):
        """Test 7：没有消息时 Agent 一直睡着，而且**不忙**。"""
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            rt_b = AgentRuntime(agent=agents["agent_b"], communication=comm)
            task = asyncio.create_task(rt_b.run_forever())

            assert await wait_for(lambda: rt_b.state == RuntimeState.WAITING)

            # 确定性地「什么都不发生」一段：让出很多次调度
            for _ in range(500):
                await idle()

            assert rt_b.wakeup_count == 0
            assert rt_b.processed_count == 0
            assert rt_b.is_sleeping

            # ⚠️ 这里**不能**断言 `not rt_b.is_busy` ——
            #    第一版这么写，红了。原因是 `is_busy` 的语义是
            #    「这个 Agent 有一个**活跃的** runtime 循环」，
            #    而「活跃但正在睡」正是它的正常状态
            #    （§10 要防的是**两个**循环，不是「循环在跑」）。
            #
            #    真正要证明的是：它醒着在等，但**没有做任何事**。
            assert rt_b.is_busy, "循环应当活着（只是闲置）"
            assert rt_b.state == RuntimeState.WAITING

            rt_b.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())


# ============================================================
# Test 2: Wakeup 后消息不会丢
# ============================================================
class TestNoMessageLoss:
    def test_message_survives_the_wakeup_window(self):
        """
        🔒 §4/§8：**先入信箱，再发信号**。

        如果顺序反了，会在「信号已发、消息未入」的窗口里
        被 Runtime 看到空信箱 —— 这条消息就丢了。

        这里反复跑同一场景，把这个竞态暴露出来。
        """
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            rt_b = AgentRuntime(agent=agents["agent_b"], communication=comm)
            task = asyncio.create_task(rt_b.run_forever())
            assert await wait_for(lambda: rt_b.state == RuntimeState.WAITING)

            sent = []
            for i in range(20):
                receipt = await agents["agent_a"].comm_send(
                    comm, "agent_b", f"m{i}")
                sent.append(receipt.message_id)

            assert await wait_for(
                lambda: rt_b.processed_count >= len(sent), limit=20000)
            assert rt_b.processed_count == len(sent)

            texts = sorted(m.text for m in agents["agent_b"].received)
            assert texts == sorted(f"m{i}" for i in range(20))

            rt_b.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

    def test_delivery_sets_wakeup_after_enqueue(self):
        """
        🔒 直接盯住那句顺序：投递完之后，信箱里**已经有**消息，
           而且 wakeup 是 SET 的。
        """
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            await agents["agent_a"].comm_send(comm, "agent_b", "x")

            # 消息在信箱里
            assert comm.pending("agent_b") == 1
            # 而且信号已经置位
            assert comm.is_woken("agent_b")

        asyncio.run(scenario())


# ============================================================
# Test 3: 连续三条只触发一个 cycle，但三条都被处理
# ============================================================
class TestWakeupCoalescing:
    def test_three_messages_one_cycle_but_all_processed(self):
        """
        §9：Wakeup 是「有事了」（可合并），
            不是「一条消息一次 Runtime」。
        """
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            a = agents["agent_a"]

            # 先堆三条（此时还没有 Runtime 在跑）
            for i in range(3):
                await a.comm_send(comm, "agent_b", f"m{i}")

            rt_b = AgentRuntime(agent=agents["agent_b"], communication=comm)
            task = asyncio.create_task(rt_b.run_forever())

            assert await wait_for(lambda: rt_b.processed_count >= 3)
            assert rt_b.processed_count == 3, "三条都要被处理"
            assert rt_b.wakeup_count == 1, "只应该醒一次"
            assert agents["agent_b"].handler_calls == 3

            rt_b.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

    def test_wakeup_is_idempotent(self):
        """重复置位不会变成多次唤醒（Event 语义）。"""
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            a = agents["agent_a"]
            await a.comm_send(comm, "agent_b", "1")
            await a.comm_send(comm, "agent_b", "2")
            assert comm.is_woken("agent_b")      # 仍然只是一个 SET
            assert comm.pending("agent_b") == 2

        asyncio.run(scenario())


# ============================================================
# Test 4: 同一 Agent 不会有两个 loop
# ============================================================
class TestSingleRuntimePerAgent:
    def test_second_loop_is_rejected(self):
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            rt_b = AgentRuntime(agent=agents["agent_b"], communication=comm)
            task = asyncio.create_task(rt_b.run_forever())
            assert await wait_for(lambda: rt_b.is_busy)

            # 🔒 第二个循环必须被拒绝，而不是排队
            with pytest.raises(RuntimeAlreadyActive):
                await AgentRuntime(agent=agents["agent_b"],
                                   communication=comm).run_once()

            assert rt_b.is_busy
            rt_b.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

    def test_run_once_also_guards(self):
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            rt = AgentRuntime(agent=agents["agent_b"], communication=comm)
            assert not rt.is_busy
            # 第一次没问题（没消息 → 返回空）
            assert await rt.run_once(timeout=0.01) == []

        asyncio.run(scenario())


# ============================================================
# Test 5 + §15: wakeup 是按 Agent 隔离的
# ============================================================
class TestWakeupIsolation:
    def test_each_agent_has_its_own_signal(self):
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            await agents["agent_a"].comm_send(comm, "agent_b", "x")

            assert comm.is_woken("agent_b")
            assert not comm.is_woken("agent_a"), "A 不该被 B 的消息唤醒"

        asyncio.run(scenario())

    def test_three_agents_only_wake_the_target(self):
        """
        §15：A 的消息只唤醒 A 的收件人，不会顺带叫醒别人。
        """
        async def scenario():
            comm, agents = build("agent_a", "agent_b", "agent_c")

            await agents["agent_a"].comm_send(comm, "agent_b", "for B")

            assert comm.is_woken("agent_b")
            assert not comm.is_woken("agent_c")
            assert comm.pending("agent_c") == 0

            await agents["agent_a"].comm_send(comm, "agent_c", "for C")

            assert comm.is_woken("agent_c")
            assert comm.pending("agent_b") == 1
            assert comm.pending("agent_c") == 1

        asyncio.run(scenario())

    def test_three_runtimes_do_not_cross(self):
        """三个 Runtime 同时跑，每个只处理发给自己的。"""
        async def scenario():
            comm, agents = build("agent_a", "agent_b", "agent_c")

            rts = {aid: AgentRuntime(agent=agents[aid], communication=comm)
                   for aid in ("agent_b", "agent_c")}
            tasks = [asyncio.create_task(rt.run_forever())
                     for rt in rts.values()]
            for rt in rts.values():
                assert await wait_for(lambda rt=rt: rt.is_busy)

            await agents["agent_a"].comm_send(comm, "agent_b", "only B")
            assert await wait_for(
                lambda: rts["agent_b"].processed_count == 1)

            for _ in range(300):
                await idle()
            assert rts["agent_c"].processed_count == 0, "C 不该被唤醒"
            assert rts["agent_c"].wakeup_count == 0

            for rt in rts.values():
                rt.stop()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

        asyncio.run(scenario())


# ============================================================
# Test 6: A → B → A round trip
# ============================================================
class TestRoundTrip:
    def test_a_to_b_and_back_without_any_external_receive(self):
        """
        🔒 全程**没有任何外部代码**调用 `receive("agent_b")` ——
           B 完全靠 wakeup 自己醒过来处理并回复。
        """
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            a, b = agents["agent_a"], agents["agent_b"]

            rt_b = AgentRuntime(agent=b, communication=comm)
            task = asyncio.create_task(rt_b.run_forever())
            assert await wait_for(lambda: rt_b.state == RuntimeState.WAITING)

            await a.comm_send(comm, "agent_b", "Hello B")

            # B 被唤醒、处理、并回了 A
            assert await wait_for(lambda: rt_b.processed_count >= 1)
            assert await wait_for(lambda: comm.pending("agent_a") >= 1)

            inbound = await comm.receive("agent_a", timeout=0.5)
            assert inbound.delivered
            assert inbound.message.payload == {"ack": "Hello agent_a"}

            # 回环：B 的 handler 只被调了一次（不能重复分发）
            assert b.handler_calls == 1

            rt_b.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

    def test_handler_is_called_exactly_once(self):
        """
        🔒 真实抓到的 bug：`manager.receive()` 已经会分发到
           `handle_message`，Runtime **不能**再调一次 ——
           否则同一条消息被处理两遍（真实系统里 = 重复回复 / 重复花钱）。
        """
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            b = agents["agent_b"]
            rt_b = AgentRuntime(agent=b, communication=comm)
            task = asyncio.create_task(rt_b.run_forever())
            assert await wait_for(lambda: rt_b.is_busy)

            await agents["agent_a"].comm_send(comm, "agent_b", "one")
            assert await wait_for(lambda: b.handler_calls >= 1)

            for _ in range(300):
                await idle()

            assert b.handler_calls == 1, (
                f"handler 被调了 {b.handler_calls} 次 —— 重复分发"
            )

            rt_b.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())


# ============================================================
# Wakeup 不是 Message（§3）
# ============================================================
class TestWakeupIsNotAMessage:
    def test_wakeup_does_not_carry_payload(self):
        async def scenario():
            comm, agents = build("agent_a", "agent_b")
            await agents["agent_a"].comm_send(comm, "agent_b", "secret-ish")

            woken = await comm.wait_wakeup("agent_b", timeout=0.05)
            assert woken is True
            # 🔒 唤醒不返回内容 —— 内容只能从信箱取
            assert comm.pending("agent_b") == 1

        asyncio.run(scenario())

    def test_only_message_reason_is_implemented(self):
        """§7：本阶段只实现 MESSAGE，其它类型不许提前实现。"""
        assert WakeupReason.implemented() == (WakeupReason.MESSAGE,)

    def test_wakeup_is_not_a_message_type(self):
        from communication.message import MessageType

        assert "WAKEUP" not in MessageType.known()
        assert "wakeup" not in [t.lower() for t in MessageType.known()]


# ============================================================
# 真实 API 不进 pytest
# ============================================================
class TestNoRealApi:
    def test_file_does_not_reference_real_providers(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords or ():
                    if kw.arg == "provider":
                        v = kw.value
                        if isinstance(v, ast.Constant):
                            pytest.fail(f"不该指定 provider={v.value!r}")

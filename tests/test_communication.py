"""
Test: CommunicationManager（通信编排层）
============================================================

这个文件验证需求里的完整链路：

    Agent A
     ↓
    CommunicationManager
     ↓
    Message
     ↓
    LocalTransport
     ↓
    Agent B          （以及 B → A 的回复）

同时验证几条关键边界：
    · Manager 通过 Transport【抽象】工作，不写死 LocalTransport
    · 假 Agent（不继承 BaseAgent）也能用 → 通信层不依赖 agents 层
    · 真 BaseAgent 也能用            → 集成是通的
    · 重复消息只处理一次
"""

from __future__ import annotations

import pytest

from communication import (
    Address,
    AgentRegistry,
    CommunicationManager,
    LocalTransport,
    Message,
    MessageType,
    ReceiveStatus,
)
from communication.errors import (
    AgentNotFoundError,
    DuplicateMessageError,
    MessageValidationError,
)

from tests.communication_helpers import (
    AsyncFakeAgent,
    FakeAgent,
    RecordingTransport,
)


# ============================================================
# 1. 注册
# ============================================================
class TestRegistration:
    def test_register_agent(self, comm_manager, fake_agents):
        agent_a, _ = fake_agents
        info = comm_manager.register(agent_a, capabilities=["python"])

        assert info.agent_id == "agent_a"
        assert info.is_local is True
        assert "python" in info.capabilities

    def test_registration_opens_mailbox(self, comm_manager, transport, fake_agents):
        agent_a, _ = fake_agents
        comm_manager.register(agent_a)

        assert transport.has_mailbox("agent_a") is True

    def test_registration_records_handler(self, comm_manager, fake_agents):
        agent_a, _ = fake_agents
        comm_manager.register(agent_a)

        assert comm_manager.has_local_handler("agent_a") is True

    def test_agent_without_handler_still_registers(self, comm_manager):
        """
        没有 handle_message 的对象也能接入 —— 只是收到的消息
        会被标记成 no_handler，而不是崩掉。
        """
        class Bare:
            agent_id = "bare"
            name = "Bare"

        info = comm_manager.register(Bare())

        assert info.agent_id == "bare"
        assert comm_manager.has_local_handler("bare") is False

    def test_unregister(self, comm_manager, transport, fake_agents):
        agent_a, _ = fake_agents
        comm_manager.register(agent_a)

        assert comm_manager.unregister("agent_a") is True
        assert comm_manager.registry.exists("agent_a") is False
        assert transport.has_mailbox("agent_a") is False

    def test_registration_uses_transport_address_for(self, fake_agents):
        """
        地址由 Transport 决定，不由 Manager 硬编码。

        如果 Manager 写死了 local://，那么换成 P2P 时
        它就要改 —— 那样抽象就白做了。
        """
        transport = RecordingTransport()
        manager = CommunicationManager(transport)
        agent_a, _ = fake_agents

        info = manager.register(agent_a)

        assert transport.addresses == ["agent_a"]
        assert info.address.scheme == "recording"
        assert str(info.address) == "recording://agent_a"


# ============================================================
# 2. 发送
# ============================================================
class TestSending:
    def test_send_returns_the_message(self, comm_manager, fake_agents):
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        async def scenario():
            return await comm_manager.send("agent_a", "agent_b", text="你好")

        msg = comm_manager and None  # 占位，避免 linter 抱怨
        from tests.communication_helpers import FakeAgent  # noqa: F401

        sent = None

        # 用 run_async 需要夹具，这里换个写法
        import asyncio

        sent = asyncio.run(scenario())

        assert isinstance(sent, Message)
        assert sent.from_agent == "agent_a"
        assert sent.to_agent == "agent_b"
        assert sent.text == "你好"
        assert sent.message_id.startswith("msg_")

    def test_send_from_unregistered_agent_rejected(self, comm_manager, fake_agents):
        """
        没接入通信层的 Agent 不能发消息。

        为什么？否则任何代码都能随便编一个 from_agent ——
        那样「身份」这个概念就形同虚设了。
        """
        _, agent_b = fake_agents
        comm_manager.register(agent_b)

        async def scenario():
            await comm_manager.send("冒充者", "agent_b", text="hi")

        with pytest.raises(AgentNotFoundError, match="发送方"):
            comm_manager  # noqa: B018
            import asyncio
            asyncio.run(scenario())

    def test_send_to_unknown_recipient(self, comm_manager, fake_agents):
        agent_a, _ = fake_agents
        comm_manager.register(agent_a)

        async def scenario():
            await comm_manager.send("agent_a", "agent_z", text="hi")

        with pytest.raises(AgentNotFoundError, match="收件人"):
            import asyncio
            asyncio.run(scenario())

    def test_send_with_payload_and_type(self, comm_manager, fake_agents):
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        async def scenario():
            return await comm_manager.send(
                "agent_a", "agent_b",
                payload={"task_id": "TASK-001", "budget": 100},
                message_type=MessageType.TASK_OFFER,
            )

        import asyncio
        sent = asyncio.run(scenario())

        assert sent.message_type == MessageType.TASK_OFFER
        assert sent.task_id == "TASK-001"

    def test_send_message_with_prebuilt_message(self, comm_manager, fake_agents):
        """允许直接投一条自己构造的消息（测试需要控制 message_id）。"""
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        msg = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_fixed", payload={"text": "指定 ID"},
        )

        async def scenario():
            return await comm_manager.send_message(msg)

        import asyncio
        receipt = asyncio.run(scenario())

        assert receipt.message_id == "msg_fixed"

    def test_send_message_rejects_non_message(self, comm_manager, fake_agents):
        agent_a, _ = fake_agents
        comm_manager.register(agent_a)

        async def scenario():
            await comm_manager.send_message("不是消息")

        with pytest.raises(MessageValidationError):
            import asyncio
            asyncio.run(scenario())


# ============================================================
# 3. 完整链路（用 run_async 夹具，写起来更干净）
# ============================================================
class TestFullRoundTrip:
    def test_a_to_b(self, comm_manager, fake_agents, run_async):
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        async def scenario():
            await comm_manager.send("agent_a", "agent_b", text="Hello Agent B")
            return await comm_manager.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        assert result.status == ReceiveStatus.DELIVERED
        assert result.message.text == "Hello Agent B"
        assert agent_b.message_count == 1

    def test_b_replies_to_a(self, comm_manager, fake_agents, run_async):
        """
        需求里的核心场景：B 能回复 A。

        这里 B 的处理函数返回一个字符串，
        CommunicationManager 自动把它包成回复消息发出去。
        """
        agent_a = FakeAgent("agent_a", "Agent A")
        agent_b = FakeAgent(
            "agent_b", "Agent B",
            reply_with=lambda msg: f"B 收到：{msg.text}",
        )
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        async def scenario():
            sent = await comm_manager.send("agent_a", "agent_b", text="你好")
            at_b = await comm_manager.receive("agent_b", timeout=1.0)
            at_a = await comm_manager.receive("agent_a", timeout=1.0)
            return sent, at_b, at_a

        sent, at_b, at_a = run_async(scenario())

        # B 收到了，并且自动回复了
        assert at_b.delivered is True
        assert at_b.reply is not None
        assert at_b.reply.from_agent == "agent_b"
        assert at_b.reply.to_agent == "agent_a"

        # 回复确实到了 A 的信箱
        assert at_a.delivered is True
        assert at_a.message.text == "B 收到：你好"
        assert at_a.message.reply_to == sent.message_id

    def test_multiple_round_trips(self, comm_manager, run_async):
        """
        多轮往返。

        【注意这里只给 B 装了自动回复】
            如果 A 和 B 都自动回复，就会形成无限往返：
                A 发 → B 回 → A 又回 → B 又回 → ……
            每轮都可能要花钱调模型。

            这个坑正是 handle_message 默认【不回复】的原因，
            下面 test_auto_reply_on_both_sides_ping_pongs 专门演示它。
        """
        a = FakeAgent("agent_a", "A")     # A 不自动回复
        b = FakeAgent("agent_b", "B", reply_with=lambda m: f"B 回：{m.text}")
        comm_manager.register(a)
        comm_manager.register(b)

        async def scenario():
            transcript = []
            for turn in range(3):
                await comm_manager.send("agent_a", "agent_b", text=f"第{turn}问")
                r1 = await comm_manager.receive("agent_b", timeout=1.0)
                r2 = await comm_manager.receive("agent_a", timeout=1.0)
                transcript.append((r1.message.text, r2.message.text))
            return transcript

        transcript = run_async(scenario())

        assert transcript == [
            ("第0问", "B 回：第0问"),
            ("第1问", "B 回：第1问"),
            ("第2问", "B 回：第2问"),
        ]

    def test_auto_reply_on_both_sides_ping_pongs(self, comm_manager, run_async):
        """
        【这个测试是一个警告，不是功能】

        如果两边都装了自动回复，一次发送会触发无限往返。

        这里只跑有限的几轮来演示这个机制 ——
        真实场景里它会一直跑下去，而且每一轮都可能花钱。

        设计上的防护：
            BaseAgent.handle_message 默认【不回复】，
            要回复必须显式 on_message(...)。
            这样「无限循环」必须由人主动打开，而不是默认踩到。
        """
        a = FakeAgent("agent_a", "A", reply_with=lambda m: f"A 回({m.text})")
        b = FakeAgent("agent_b", "B", reply_with=lambda m: f"B 回({m.text})")
        comm_manager.register(a)
        comm_manager.register(b)

        async def scenario():
            await comm_manager.send("agent_a", "agent_b", text="起点")

            # 只跑 4 跳就停 —— 真实情况这里会一直转下去
            trace = []
            for _ in range(4):
                at_b = await comm_manager.receive("agent_b", timeout=1.0)
                if at_b.delivered:
                    trace.append(("B", at_b.message.text))
                at_a = await comm_manager.receive("agent_a", timeout=1.0)
                if at_a.delivered:
                    trace.append(("A", at_a.message.text))
            return trace

        trace = run_async(scenario())

        # 第一跳是原始消息，之后每一跳都是对上一跳的自动回复，
        # 内容不断嵌套加长 —— 这就是失控的样子。
        assert trace[0] == ("B", "起点")
        assert trace[1] == ("A", "B 回(起点)")
        assert trace[2] == ("B", "A 回(B 回(起点))")
        assert trace[3] == ("A", "B 回(A 回(B 回(起点)))")

        # 嵌套层数在递增 = 消息永远不会自然停下
        assert trace[3][1].count("回(") > trace[1][1].count("回(")

    def test_reply_chain_links_are_preserved(self, comm_manager, run_async):
        """回复链的 reply_to 必须指向上一条，形成可追溯的会话。"""
        a = FakeAgent("agent_a", "A")
        b = FakeAgent("agent_b", "B", reply_with=lambda m: "收到")
        comm_manager.register(a)
        comm_manager.register(b)

        async def scenario():
            original = await comm_manager.send("agent_a", "agent_b", text="hi")
            await comm_manager.receive("agent_b", timeout=1.0)
            reply = await comm_manager.receive("agent_a", timeout=1.0)
            return original, reply.message

        original, reply = run_async(scenario())
        assert reply.reply_to == original.message_id


# ============================================================
# 4. 处理函数的各种形态
# ============================================================
class TestHandlers:
    def test_async_handler_is_supported(self, comm_manager, run_async):
        """处理函数可以是 async 的。"""
        a = FakeAgent("agent_a", "A")
        b = AsyncFakeAgent("agent_b", "B", reply_with=lambda m: "异步回复")
        comm_manager.register(a)
        comm_manager.register(b)

        async def scenario():
            await comm_manager.send("agent_a", "agent_b", text="hi")
            at_b = await comm_manager.receive("agent_b", timeout=1.0)
            at_a = await comm_manager.receive("agent_a", timeout=1.0)
            return at_b, at_a

        at_b, at_a = run_async(scenario())

        assert at_b.delivered is True
        assert at_a.message.text == "异步回复"

    def test_dict_reply_becomes_payload(self, comm_manager, run_async):
        a = FakeAgent("agent_a", "A")
        b = FakeAgent("agent_b", "B", reply_with=lambda m: {"status": "ok", "n": 1})
        comm_manager.register(a)
        comm_manager.register(b)

        async def scenario():
            await comm_manager.send("agent_a", "agent_b", text="hi")
            await comm_manager.receive("agent_b", timeout=1.0)
            return await comm_manager.receive("agent_a", timeout=1.0)

        result = run_async(scenario())
        assert result.message.payload["status"] == "ok"

    def test_message_reply_is_passed_through(self, comm_manager, run_async):
        """处理函数直接返回 Message 时原样发出。"""
        a = FakeAgent("agent_a", "A")

        def custom_reply(msg):
            return Message(
                from_agent="agent_b", to_agent="agent_a",
                message_type="CUSTOM", payload={"custom": True},
            )

        b = FakeAgent("agent_b", "B", reply_with=custom_reply)
        comm_manager.register(a)
        comm_manager.register(b)

        async def scenario():
            await comm_manager.send("agent_a", "agent_b", text="hi")
            await comm_manager.receive("agent_b", timeout=1.0)
            return await comm_manager.receive("agent_a", timeout=1.0)

        result = run_async(scenario())
        assert result.message.message_type == "CUSTOM"

    def test_unknown_return_type_is_ignored_not_crashing(
        self, comm_manager, run_async
    ):
        """处理函数返回奇怪的东西时不该崩，只是不回复。"""
        a = FakeAgent("agent_a", "A")
        b = FakeAgent("agent_b", "B", reply_with=lambda m: 12345)
        comm_manager.register(a)
        comm_manager.register(b)

        async def scenario():
            await comm_manager.send("agent_a", "agent_b", text="hi")
            at_b = await comm_manager.receive("agent_b", timeout=1.0)
            at_a = await comm_manager.receive("agent_a", timeout=0.05)
            return at_b, at_a

        at_b, at_a = run_async(scenario())

        assert at_b.delivered is True
        assert at_b.reply is None
        assert at_a.is_timeout is True

    def test_no_handler_gives_no_handler_status(self, comm_manager, run_async):
        class Bare:
            agent_id = "bare"
            name = "Bare"

        a = FakeAgent("agent_a", "A")
        comm_manager.register(a)
        comm_manager.register(Bare())

        async def scenario():
            await comm_manager.send("agent_a", "bare", text="hi")
            return await comm_manager.receive("bare", timeout=1.0)

        result = run_async(scenario())
        assert result.status == ReceiveStatus.NO_HANDLER

    def test_type_handler_takes_precedence(self, comm_manager, run_async):
        """
        按消息类型注册的处理器优先于 Agent 的通用处理器。

        这是 Step 4 接任务系统的口子：
            manager.register_type_handler("TASK_OFFER", task_manager.on_offer)
        加新消息类型不需要改 Agent，也不需要改通信层。
        """
        a = FakeAgent("agent_a", "A")
        b = FakeAgent("agent_b", "B", reply_with=lambda m: "通用处理器回了")
        comm_manager.register(a)
        comm_manager.register(b)

        seen = []

        def special_handler(msg):
            seen.append(msg.message_id)
            return "专用处理器回了"

        comm_manager.register_type_handler("PING", special_handler)

        async def scenario():
            await comm_manager.send(
                "agent_a", "agent_b", message_type="PING", text="ping"
            )
            await comm_manager.receive("agent_b", timeout=1.0)
            return await comm_manager.receive("agent_a", timeout=1.0)

        result = run_async(scenario())

        assert len(seen) == 1
        assert result.message.text == "专用处理器回了"
        # Agent 自己的通用处理器没被调用
        assert b.message_count == 0

    def test_register_type_handler_validates(self, comm_manager):
        with pytest.raises(MessageValidationError):
            comm_manager.register_type_handler("", lambda m: None)

        with pytest.raises(MessageValidationError):
            comm_manager.register_type_handler("PING", "不是函数")


# ============================================================
# 5. 去重
# ============================================================
class TestDeduplication:
    def test_duplicate_message_is_not_processed_twice(
        self, comm_manager, fake_agents, run_async
    ):
        """
        【本步骤的重要安全性质】

        同一条消息（相同 message_id）重复到达时，
        只能被 Agent 处理一次。

        为什么重要？想象 TASK_OFFER 被处理两次：
        Agent B 会接两次同一个任务、交付两份成果、收两次钱。
        这种 bug 在本地测不出来，上线才炸。
        """
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        duplicate = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_same_id", payload={"text": "只应处理一次"},
        )

        async def scenario():
            await comm_manager.send_message(duplicate)
            await comm_manager.send_message(duplicate)   # 同一 ID 再来一次
            first = await comm_manager.receive("agent_b", timeout=1.0)
            second = await comm_manager.receive("agent_b", timeout=1.0)
            return first, second

        first, second = run_async(scenario())

        assert first.status == ReceiveStatus.DELIVERED
        assert second.status == ReceiveStatus.DUPLICATE
        assert agent_b.message_count == 1          # 只处理了一次

    def test_duplicate_can_raise_in_strict_mode(
        self, comm_manager, fake_agents, run_async
    ):
        """严格模式：调试时想知道「到底有没有重复」。"""
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        duplicate = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_strict", payload={"text": "x"},
        )

        async def scenario():
            await comm_manager.send_message(duplicate)
            await comm_manager.send_message(duplicate)
            await comm_manager.receive("agent_b", timeout=1.0)
            return await comm_manager.receive(
                "agent_b", timeout=1.0, strict_dedup=True
            )

        with pytest.raises(DuplicateMessageError):
            run_async(scenario())

    def test_different_messages_are_both_processed(
        self, comm_manager, fake_agents, run_async
    ):
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        async def scenario():
            await comm_manager.send("agent_a", "agent_b", text="第一条")
            await comm_manager.send("agent_a", "agent_b", text="第二条")
            r1 = await comm_manager.receive("agent_b", timeout=1.0)
            r2 = await comm_manager.receive("agent_b", timeout=1.0)
            return r1, r2

        r1, r2 = run_async(scenario())

        assert r1.delivered and r2.delivered
        assert agent_b.message_count == 2

    def test_dedup_is_per_agent(self, comm_manager, run_async):
        """
        去重按【接收方】分开记。

        否则 A 发过的 message_id 会污染 B 的去重表。
        """
        a = FakeAgent("agent_a", "A")
        b = FakeAgent("agent_b", "B")
        c = FakeAgent("agent_c", "C")
        for agent in (a, b, c):
            comm_manager.register(agent)

        async def scenario():
            # Agent A 分别给 B 和 C 发消息（不同 message_id）
            await comm_manager.send("agent_a", "agent_b", text="给B")
            await comm_manager.send("agent_a", "agent_c", text="给C")
            rb = await comm_manager.receive("agent_b", timeout=1.0)
            rc = await comm_manager.receive("agent_c", timeout=1.0)
            return rb, rc

        rb, rc = run_async(scenario())

        assert rb.delivered and rc.delivered

    def test_duplicate_count_in_stats(self, comm_manager, fake_agents, run_async):
        agent_a, agent_b = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register(agent_b)

        dup = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_dup", payload={},
        )

        async def scenario():
            await comm_manager.send_message(dup)
            await comm_manager.send_message(dup)
            await comm_manager.receive("agent_b", timeout=1.0)
            await comm_manager.receive("agent_b", timeout=1.0)

        run_async(scenario())
        assert comm_manager.stats()["duplicates"] == 1


# ============================================================
# 6. 接收状态
# ============================================================
class TestReceiveStatuses:
    def test_timeout_status(self, comm_manager, fake_agents, run_async):
        agent_a, _ = fake_agents
        comm_manager.register(agent_a)

        async def scenario():
            return await comm_manager.receive("agent_a", timeout=0.05)

        result = run_async(scenario())

        assert result.status == ReceiveStatus.TIMEOUT
        assert result.is_timeout is True
        assert result.message is None

    def test_receive_from_unregistered_agent(self, comm_manager, run_async):
        async def scenario():
            return await comm_manager.receive("从没注册过", timeout=0.05)

        with pytest.raises(AgentNotFoundError):
            run_async(scenario())

    def test_misdelivered_message_is_flagged(self, comm_manager, run_async):
        """
        防御性检查：如果信箱里出现了收件人不是自己的消息，
        不能当成正常消息处理。
        """
        a = FakeAgent("agent_a", "A")
        b = FakeAgent("agent_b", "B")
        comm_manager.register(a)
        comm_manager.register(b)

        # 手动往 B 的信箱塞一条收件人写成 C 的消息
        wrong = Message(
            from_agent="agent_a", to_agent="agent_c",
            message_id="msg_wrong", payload={"text": "发错人了"},
        )

        async def scenario():
            await comm_manager.transport.send(
                wrong, comm_manager.transport.address_for("agent_b")
            )
            return await comm_manager.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        assert result.status == ReceiveStatus.NO_HANDLER
        assert "不是 agent_b" in result.detail
        assert b.message_count == 0


# ============================================================
# 7. 不依赖具体 Transport（可替换性证明）
# ============================================================
class TestTransportAgnostic:
    """
    这一组是「Agent 不依赖 LocalTransport」的可执行证明。
    """

    def test_manager_works_with_a_different_transport(self, fake_agents):
        transport = RecordingTransport()
        manager = CommunicationManager(transport)
        agent_a, _ = fake_agents

        manager.register(agent_a)

        async def scenario():
            return await manager.send("agent_a", "agent_a_x", text="hi")

        # agent_a_x 不在注册表里，应该抛「找不到收件人」
        # —— 说明 Manager 走的是抽象接口，而不是自己内部造消息
        with pytest.raises(AgentNotFoundError):
            import asyncio
            asyncio.run(scenario())

    def test_manager_uses_injected_transport_for_send(self, fake_agents):
        transport = RecordingTransport()
        manager = CommunicationManager(transport)
        a, b = fake_agents
        manager.register(a)
        manager.register(b)

        import asyncio

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="hi")

        asyncio.run(scenario())

        assert len(transport.sent) == 1
        message, address = transport.sent[0]
        assert message.text == "hi"
        assert address.agent_id == "agent_b"

    def test_discover_is_consulted_when_registry_misses(self, fake_agents):
        """
        注册表里没有时，Manager 会去问 Transport 的 discover()。

        本地传输返回 None（不做事）；
        未来 P2P 传输会在这里查网络。

        这条测试保证「查找顺序」这个设计是落到实处的 ——
        将来接 P2P 时 Manager 一行都不用改。
        """

        class DiscoveringTransport(RecordingTransport):
            async def discover(self, agent_id):
                self.discover_calls.append(agent_id)
                if agent_id == "remote_agent":
                    return Address(
                        agent_id="remote_agent",
                        scheme="recording",
                        endpoint="remote_agent",
                    )
                return None

        transport = DiscoveringTransport()
        manager = CommunicationManager(transport)
        a, _ = fake_agents
        manager.register(a)

        import asyncio

        async def scenario():
            return await manager.resolve_address("remote_agent")

        address = asyncio.run(scenario())

        assert transport.discover_calls == ["remote_agent"]
        assert address.agent_id == "remote_agent"
        # 发现之后会自动登记成远端条目
        assert manager.registry.lookup("remote_agent").is_local is False

    def test_start_delegates_to_transport(self, fake_agents):
        transport = RecordingTransport()
        manager = CommunicationManager(transport)

        import asyncio
        asyncio.run(manager.start())

        assert transport.started is True

    def test_close_delegates_to_transport(self, fake_agents):
        transport = RecordingTransport()
        manager = CommunicationManager(transport)

        import asyncio
        asyncio.run(manager.close())

        assert transport.closed is True


# ============================================================
# 8. 与真实 BaseAgent 集成
# ============================================================
class TestRealAgentIntegration:
    """
    前面的测试用的是假 Agent，证明通信层不依赖 agents 层。
    这里换成真的 BaseAgent，证明集成是通的。
    """

    def test_two_real_agents_can_talk(self, economy, comm_manager, run_async):
        agent_a = economy.create_agent("agent_a")
        agent_b = economy.create_agent("agent_b")

        comm_manager.register(agent_a, capabilities=agent_a.config.skills)
        comm_manager.register(agent_b, capabilities=agent_b.config.skills)

        agent_a.attach_communication(comm_manager)
        agent_b.attach_communication(comm_manager)

        agent_b.on_message(lambda msg: f"{agent_b.name} 收到了「{msg.text}」")

        async def scenario():
            sent = await agent_a.send("agent_b", text="你好，B")
            at_b = await comm_manager.receive("agent_b", timeout=1.0)
            at_a = await comm_manager.receive("agent_a", timeout=1.0)
            return sent, at_b, at_a

        sent, at_b, at_a = run_async(scenario())

        assert at_b.delivered is True
        assert at_a.message.text == "Agent B 收到了「你好，B」"

        # 消息也进了 Agent 自己的短期记忆
        assert agent_b.received_messages
        assert agent_b.recall("message_received")

    def test_agent_without_communication_cannot_send(self, economy):
        agent = economy.create_agent("agent_a")

        import asyncio

        async def scenario():
            await agent.send("agent_b", text="hi")

        with pytest.raises(RuntimeError, match="通信层"):
            asyncio.run(scenario())

    def test_agent_does_not_reply_by_default(self, economy, comm_manager, run_async):
        """
        默认【不自动回复】。

        为什么？两条 Agent 互相自动回复会形成无限循环，
        每轮循环都可能调模型花钱。
        要回复必须显式设置处理函数。
        """
        agent_a = economy.create_agent("agent_a")
        agent_b = economy.create_agent("agent_b")
        for agent in (agent_a, agent_b):
            comm_manager.register(agent)
            agent.attach_communication(comm_manager)

        async def scenario():
            await agent_a.send("agent_b", text="hi")
            at_b = await comm_manager.receive("agent_b", timeout=1.0)
            at_a = await comm_manager.receive("agent_a", timeout=0.05)
            return at_b, at_a

        at_b, at_a = run_async(scenario())

        assert at_b.delivered is True
        assert at_b.reply is None
        assert at_a.is_timeout is True

    def test_communication_does_not_touch_wallet_or_ledger(
        self, economy, comm_manager, run_async
    ):
        """
        【边界验证】通信不影响经济状态。

        本步骤不实现支付，所以发消息不该动余额，也不该动账本。
        """
        agent_a = economy.create_agent("agent_a")
        agent_b = economy.create_agent("agent_b")
        for agent in (agent_a, agent_b):
            comm_manager.register(agent)
            agent.attach_communication(comm_manager)

        balance_before = {a.agent_id: a.balance for a in economy.all_agents()}
        ledger_before = economy.ledger.count()

        async def scenario():
            for index in range(5):
                await agent_a.send("agent_b", text=f"第 {index} 条")
                await comm_manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        after = {a.agent_id: a.balance for a in economy.all_agents()}
        assert after == balance_before
        assert economy.ledger.count() == ledger_before
        assert economy.audit()["balanced"] is True

    def test_communication_does_not_call_llm(self, economy, comm_manager, run_async):
        """
        【边界验证】通信不调用模型。

        怎么证明？看一眼 llm_calls 表 —— 消息往返之后应该还是 0 条。
        """
        from llm import LLMCallRecorder

        agent_a = economy.create_agent("agent_a")
        agent_b = economy.create_agent("agent_b")
        for agent in (agent_a, agent_b):
            comm_manager.register(agent)
            agent.attach_communication(comm_manager)

        recorder = LLMCallRecorder(economy.db)
        assert recorder.count() == 0

        agent_b.on_message(lambda msg: "机械回复，不需要智力")

        async def scenario():
            await agent_a.send("agent_b", text="hi")
            await comm_manager.receive("agent_b", timeout=1.0)
            await comm_manager.receive("agent_a", timeout=1.0)

        run_async(scenario())

        assert recorder.count() == 0, "通信不该产生任何模型调用"


# ============================================================
# 9. 远端 Agent（为 P2P 预留的能力）
# ============================================================
class TestRemoteAgents:
    def test_register_remote_has_no_local_handler(self, comm_manager):
        """
        远端 Agent 在注册表里有条目，但没有本地处理函数。

        这正是「只存数据」这个设计才能做到的事：
        你没有对方的 Python 对象，却依然能登记它的地址。
        """
        comm_manager.register_remote(
            agent_id="agent_c",
            address=Address(agent_id="agent_c", scheme="p2p", endpoint="12D3KooW"),
            capabilities=["research"],
        )

        info = comm_manager.lookup("agent_c")

        assert info.is_local is False
        assert str(info.address) == "p2p://12D3KooW"
        assert comm_manager.has_local_handler("agent_c") is False

    def test_local_and_remote_coexist(self, comm_manager, fake_agents):
        agent_a, _ = fake_agents
        comm_manager.register(agent_a)
        comm_manager.register_remote(
            agent_id="agent_c",
            address=Address(agent_id="agent_c", scheme="p2p", endpoint="peer1"),
        )

        stats = comm_manager.registry.stats()

        assert stats["local"] == 1
        assert stats["remote"] == 1

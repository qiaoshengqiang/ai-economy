"""
Test: Transport 抽象 + LocalTransport
============================================================

分两部分：

    1. 抽象契约 —— Transport 是不是真的抽象？
       如果 Transport 能被直接实例化，那它就是个假抽象。

    2. 本地实现 —— 收发、超时、找不到人、重复、关闭。
       重点验证「失败时抛的是明确异常，而不是 KeyError」。

另外有一组测试专门盯着「信箱有没有泄漏」——
这是 LocalTransport 最容易被写歪的地方。
"""

from __future__ import annotations

import pytest

from communication import LocalTransport, Message, Address, Transport
from communication.errors import (
    AgentNotFoundError,
    DeliveryError,
    TransportClosedError,
)
from communication.transport import DeliveryReceipt


# ============================================================
# 1. 抽象契约
# ============================================================
class TestTransportContract:
    def test_transport_is_abstract(self):
        """抽象基类不能直接实例化 —— 否则契约形同虚设。"""
        with pytest.raises(TypeError):
            Transport()  # type: ignore[abstract]

    def test_incomplete_subclass_rejected(self):
        """少实现一个抽象方法就不让实例化。"""

        class Incomplete(Transport):
            scheme = "incomplete"

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_local_transport_is_a_transport(self):
        assert issubclass(LocalTransport, Transport)

    def test_local_transport_scheme(self):
        assert LocalTransport.scheme == "local"

    def test_discover_defaults_to_none(self):
        """
        默认的 discover 返回 None。

        这是给未来 P2P 留的口子：
        本地传输不做远端发现，所以直接返回 None。
        """
        async def scenario():
            return await LocalTransport().discover("agent_x")

        # 用一个最小的 run 内联，避免依赖夹具
        import asyncio

        assert asyncio.run(scenario()) is None


# ============================================================
# 2. 信箱管理
# ============================================================
class TestMailboxes:
    def test_open_and_has(self, transport):
        assert transport.has_mailbox("agent_a") is False

        transport.open_mailbox("agent_a")

        assert transport.has_mailbox("agent_a") is True

    def test_open_is_idempotent(self, transport):
        transport.open_mailbox("agent_a")
        transport.open_mailbox("agent_a")

        assert transport.mailboxes == ["agent_a"]

    def test_close_mailbox(self, transport):
        transport.open_mailbox("agent_a")
        transport.close_mailbox("agent_a")

        assert transport.has_mailbox("agent_a") is False

    def test_close_missing_mailbox_is_safe(self, transport):
        transport.close_mailbox("从不存在的")
        assert transport.mailboxes == []

    def test_empty_agent_id_rejected(self, transport):
        with pytest.raises(ValueError):
            transport.open_mailbox("")

    def test_open_after_close_rejected(self, transport, run_async):
        run_async(transport.close())

        with pytest.raises(TransportClosedError):
            transport.open_mailbox("agent_a")


# ============================================================
# 3. 发送与接收
# ============================================================
class TestSendReceive:
    def test_a_to_b(self, transport, run_async):
        """需求里的核心场景：A 能发给 B。"""
        transport.open_mailbox("agent_a")
        transport.open_mailbox("agent_b")

        message = Message(from_agent="agent_a", to_agent="agent_b",
                          payload={"text": "Hello Agent B"})

        async def scenario():
            receipt = await transport.send(message, transport.address_for("agent_b"))
            received = await transport.receive("agent_b", timeout=1.0)
            return receipt, received

        receipt, received = run_async(scenario())

        assert receipt.accepted is True
        assert received is not None
        assert received.message_id == message.message_id
        assert received.text == "Hello Agent B"

    def test_b_to_a(self, transport, run_async):
        """反向也要能通。"""
        transport.open_mailbox("agent_a")
        transport.open_mailbox("agent_b")

        message = Message(from_agent="agent_b", to_agent="agent_a",
                          payload={"text": "Hello Agent A"})

        async def scenario():
            await transport.send(message, transport.address_for("agent_a"))
            return await transport.receive("agent_a", timeout=1.0)

        assert run_async(scenario()).text == "Hello Agent A"

    def test_receipt_contains_routing_info(self, transport, run_async):
        transport.open_mailbox("agent_b")
        message = Message(from_agent="agent_a", to_agent="agent_b")

        async def scenario():
            return await transport.send(
                message, transport.address_for("agent_b")
            )

        receipt = run_async(scenario())

        assert isinstance(receipt, DeliveryReceipt)
        assert receipt.message_id == message.message_id
        assert receipt.scheme == "local"
        assert receipt.to.agent_id == "agent_b"

    def test_multiple_messages_keep_order(self, transport, run_async):
        """多条消息必须按发送顺序到达（FIFO）。"""
        transport.open_mailbox("agent_b")

        async def scenario():
            for index in range(5):
                msg = Message(
                    from_agent="agent_a", to_agent="agent_b",
                    payload={"text": f"第 {index} 条"},
                )
                await transport.send(msg, transport.address_for("agent_b"))

            return [
                (await transport.receive("agent_b", timeout=1.0)).text
                for _ in range(5)
            ]

        texts = run_async(scenario())

        assert texts == [f"第 {i} 条" for i in range(5)]

    def test_pending_count(self, transport, run_async):
        transport.open_mailbox("agent_b")

        async def scenario():
            for _ in range(3):
                await transport.send(
                    Message(from_agent="a", to_agent="agent_b"),
                    transport.address_for("agent_b"),
                )

        run_async(scenario())
        assert transport.pending("agent_b") == 3

    def test_messages_are_isolated_per_mailbox(self, transport, run_async):
        transport.open_mailbox("agent_a")
        transport.open_mailbox("agent_b")

        async def scenario():
            await transport.send(
                Message(from_agent="x", to_agent="agent_b", payload={"text": "给B"}),
                transport.address_for("agent_b"),
            )
            return (
                await transport.receive("agent_b", timeout=1.0),
                await transport.receive("agent_a", timeout=0.05),
            )

        to_b, to_a = run_async(scenario())

        assert to_b.text == "给B"
        assert to_a is None      # A 的信箱是空的


# ============================================================
# 4. 失败路径
# ============================================================
class TestFailureModes:
    def test_send_to_unknown_agent_raises_agent_not_found(self, transport, run_async):
        """
        收件人不存在时必须抛 AgentNotFoundError。

        绝不能是 KeyError —— 那看起来像代码 bug，
        而实际上「对方不在」是分布式环境里的常态。
        """
        message = Message(from_agent="agent_a", to_agent="agent_z")

        async def scenario():
            await transport.send(message, transport.address_for("agent_z"))

        with pytest.raises(AgentNotFoundError):
            run_async(scenario())

    def test_agent_not_found_message_is_helpful(self, transport, run_async):
        transport.open_mailbox("agent_a")
        message = Message(from_agent="agent_a", to_agent="agent_z")

        async def scenario():
            await transport.send(message, transport.address_for("agent_z"))

        with pytest.raises(AgentNotFoundError) as excinfo:
            run_async(scenario())

        text = str(excinfo.value)
        assert "agent_z" in text
        assert "agent_a" in text        # 顺便告诉它有哪些信箱

    def test_receive_from_unknown_mailbox_raises(self, transport, run_async):
        async def scenario():
            return await transport.receive("从没注册过的", timeout=0.05)

        with pytest.raises(AgentNotFoundError):
            run_async(scenario())

    def test_receive_timeout_returns_none(self, transport, run_async):
        """
        超时返回 None，不是异常。

        因为「暂时没有消息」是正常的，不是错误。
        """
        transport.open_mailbox("agent_a")

        async def scenario():
            return await transport.receive("agent_a", timeout=0.05)

        assert run_async(scenario()) is None

    def test_send_after_close_raises(self, transport, run_async):
        transport.open_mailbox("agent_b")
        message = Message(from_agent="agent_a", to_agent="agent_b")

        async def scenario():
            await transport.close()
            await transport.send(message, transport.address_for("agent_b"))

        with pytest.raises(TransportClosedError):
            run_async(scenario())

    def test_send_rejects_non_message(self, transport, run_async):
        transport.open_mailbox("agent_b")

        async def scenario():
            await transport.send("这不是消息", transport.address_for("agent_b"))

        with pytest.raises(DeliveryError):
            run_async(scenario())

    def test_full_mailbox_raises_delivery_error(self, run_async):
        """信箱有上限时，塞满要抛 DeliveryError，而不是静默丢弃。"""
        transport = LocalTransport(max_queue_size=2)
        transport.open_mailbox("agent_b")

        async def scenario():
            for _ in range(3):
                await transport.send(
                    Message(from_agent="a", to_agent="agent_b"),
                    transport.address_for("agent_b"),
                )

        with pytest.raises(DeliveryError):
            run_async(scenario())

        assert transport.drop_count == 1


# ============================================================
# 5. 信箱隔离（本地实现的经典写歪点）
# ============================================================
class TestMailboxIsolation:
    """
    这一组测试盯着「LocalTransport 会不会把 Agent 泄漏出去」。

    如果传输层持有 Agent 对象，就一定会有人写出
        transport.agents["b"].do_something()
    那样通信层就变成了远程控制层，边界彻底消失。
    """

    def test_transport_holds_no_agents(self, transport):
        from tests.communication_helpers import FakeAgent

        agent = FakeAgent("agent_a", "Agent A")
        transport.open_mailbox(agent.agent_id)

        # 传输层内部不应该以任何形式引用这个 Agent
        internals = vars(transport)
        for name, value in internals.items():
            assert value is not agent, f"transport.{name} 里存了 Agent 对象"

        # 队列里不该有 Agent
        for queue in transport._mailboxes.values():
            assert queue.qsize() == 0

    def test_transport_does_not_expose_agents_attribute(self, transport):
        assert not hasattr(transport, "agents")
        assert not hasattr(transport, "get_agent")

    def test_mailbox_dict_is_private(self):
        """信箱字典是下划线开头的私有属性，不构成公开 API。"""
        transport = LocalTransport()
        public = [n for n in dir(transport) if not n.startswith("_")]

        assert "mailboxes" in public          # 只读的名单
        assert "_mailboxes" not in public     # 真正的字典是私有的

    def test_mailboxes_property_returns_names_only(self, transport):
        transport.open_mailbox("agent_a")
        names = transport.mailboxes

        assert names == ["agent_a"]
        assert all(isinstance(n, str) for n in names)


# ============================================================
# 6. 寻址
# ============================================================
class TestAddressing:
    def test_address_for_local(self, transport):
        addr = transport.address_for("agent_b")

        assert addr.scheme == "local"
        assert addr.agent_id == "agent_b"
        assert str(addr) == "local://agent_b"

    def test_address_for_does_not_require_mailbox(self, transport):
        """造地址不需要先注册 —— 地址和信箱是两件事。"""
        addr = transport.address_for("还没注册的")
        assert addr.agent_id == "还没注册的"


# ============================================================
# 7. 统计与自述
# ============================================================
class TestStats:
    def test_counters(self, transport, run_async):
        transport.open_mailbox("agent_a")
        transport.open_mailbox("agent_b")

        async def scenario():
            await transport.send(
                Message(from_agent="agent_a", to_agent="agent_b"),
                transport.address_for("agent_b"),
            )
            await transport.receive("agent_b", timeout=1.0)

        run_async(scenario())

        assert transport.sent_count == 1
        assert transport.received_count == 1
        assert transport.drop_count == 0

    def test_describe(self, transport):
        transport.open_mailbox("agent_a")
        info = transport.describe()

        assert info["transport"] == "LocalTransport"
        assert info["scheme"] == "local"
        assert info["mailboxes"] == ["agent_a"]

    def test_repr_is_readable(self, transport):
        transport.open_mailbox("agent_a")
        assert "LocalTransport" in repr(transport)

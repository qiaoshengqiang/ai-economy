"""
通信层测试的共用工具
============================================================

FakeAgent 刻意【不继承】BaseAgent。

为什么？
    · 通信层不该依赖 agents 层。用假 Agent 测通信层，
      证明了这个边界是真的。
    · 不碰数据库，测试跑得飞快。
    · 真实 BaseAgent 的集成测试在 tests/test_communication.py，
      两边对照，正好验证「鸭子类型」这个设计是成立的。

文件名不匹配 test_*.py，所以 pytest 不会把它当测试收集。
"""

from __future__ import annotations

from typing import Any, List, Optional


class FakeAgent:
    """
    满足通信层需要的最小 Agent。

    通信层只要求它有：
        .agent_id          身份
        .name              显示名（可选）
        .handle_message()  收到消息时调用（可选）

    就这些。没有 Brain、没有 Wallet、没有数据库。
    """

    def __init__(
        self,
        agent_id: str,
        name: Optional[str] = None,
        reply_with: Optional[Any] = None,
        capabilities: Optional[tuple] = None,
    ):
        self.agent_id = agent_id
        self.name = name or agent_id
        self.capabilities = tuple(capabilities or ())

        # 收到过的消息
        self.inbox: List[Any] = []

        # 可以是一个固定回复、一个函数、或者 None
        self.reply_with = reply_with

    # ------------------------------------------------------------
    def handle_message(self, message: Any) -> Any:
        self.inbox.append(message)

        if self.reply_with is None:
            return None
        if callable(self.reply_with):
            return self.reply_with(message)
        return self.reply_with

    # ------------------------------------------------------------
    @property
    def message_count(self) -> int:
        return len(self.inbox)

    def last_message(self) -> Any:
        return self.inbox[-1] if self.inbox else None

    def __repr__(self) -> str:
        return f"FakeAgent({self.agent_id!r}, received={self.message_count})"


class AsyncFakeAgent(FakeAgent):
    """handle_message 是 async 的版本，用来验证通信层两种都支持。"""

    async def handle_message(self, message: Any) -> Any:  # type: ignore[override]
        self.inbox.append(message)
        if callable(self.reply_with):
            return self.reply_with(message)
        return self.reply_with


class RecordingTransport:
    """
    一个「什么都不做，只记录」的 Transport。

    用途：验证 CommunicationManager 确实调用了 Transport 抽象接口，
    而不是偷偷用了 LocalTransport。

    如果 Manager 内部写死了 LocalTransport，这个假传输层就收不到消息，
    测试立刻会发现 —— 这就是「Agent 不依赖具体传输」的可执行证明。
    """

    scheme = "recording"

    def __init__(self):
        self.sent: List[Any] = []
        self.addresses: List[str] = []
        self._mailboxes: List[str] = []
        self.discover_calls: List[str] = []
        self.closed = False
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    def open_mailbox(self, agent_id: str) -> None:
        self._mailboxes.append(agent_id)

    def close_mailbox(self, agent_id: str) -> None:
        if agent_id in self._mailboxes:
            self._mailboxes.remove(agent_id)

    def has_mailbox(self, agent_id: str) -> bool:
        return agent_id in self._mailboxes

    async def send(self, message, to):
        from communication.transport import DeliveryReceipt

        self.sent.append((message, to))
        return DeliveryReceipt(
            message_id=message.message_id,
            to=to,
            scheme=self.scheme,
            detail="被 RecordingTransport 记下了",
        )

    async def receive(self, agent_id: str, timeout=None):
        return None

    def address_for(self, agent_id: str):
        from communication.address import Address

        self.addresses.append(agent_id)
        return Address(agent_id=agent_id, scheme=self.scheme, endpoint=agent_id)

    async def discover(self, agent_id: str):
        self.discover_calls.append(agent_id)
        return None

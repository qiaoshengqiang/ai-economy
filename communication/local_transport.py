"""
LocalTransport —— 同进程内存传输
============================================================

【它是什么】

    在一个 Python 进程内部，把消息从一个 Agent 的信箱
    送到另一个 Agent 的信箱。用 asyncio.Queue 实现。

【它不是什么（很重要）】

    它【不是】直接调用对方的方法：

        agent_b.receive(message)          ❌ 这不是通信，是函数调用
        agent_b.mailbox.put(message)      ❌ 信箱泄漏给了外面
        AGENTS["agent_b"] = agent_b       ❌ 全局字典，未来必然出事

    它是：

        Agent A 的消息 → 序列化友好的 Message → 按地址投进队列
                                                            ↓
                                              Agent B 自己来取

    这条路径和「消息真的经过网线」时【形状完全一样】，
    只是中间那一段从 UDP/TCP 换成了内存队列。
    所以将来把 LocalTransport 换成 P2PTransport 时，
    上面的代码一行都不用改。

【为什么存 Queue 而不是存 Agent】

    队列是「收件箱」这个物理概念；
    Agent 是「会思考的经济主体」。

    如果传输层持有 Agent 对象，就很容易有人图省事写出
    `transport.agents["b"].do_something()` ——
    那样通信层就变成了「远程控制层」，边界彻底消失。

    只存队列，从物理上杜绝这件事。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from communication.address import Address
from communication.errors import (
    AgentNotFoundError,
    DeliveryError,
    TransportClosedError,
)
from communication.message import Message
from communication.transport import DeliveryReceipt, Transport

logger = get_logger(__name__)


class LocalTransport(Transport):
    """
    同进程内的内存传输。

    用法：
        transport = LocalTransport()
        transport.open_mailbox("agent_a")
        transport.open_mailbox("agent_b")

        await transport.send(message, Address.for_local("agent_b"))
        message = await transport.receive("agent_b", timeout=1.0)
    """

    scheme = "local"

    def __init__(self, max_queue_size: int = 0):
        """
        参数：
            max_queue_size: 每个信箱最多积压多少条。
                            0 表示不限（默认）—— 本地环境不需要背压。
                            将来接网络时这个值会变得重要。
        """
        self.max_queue_size = int(max_queue_size)

        # 信箱：agent_id → 队列。注意存的是队列，不是 Agent。
        self._mailboxes: Dict[str, "asyncio.Queue"] = {}

        self._closed = False

        # 统计，方便调试和测试
        self.sent_count = 0
        self.received_count = 0
        self.drop_count = 0

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    async def close(self) -> None:
        """
        关闭传输层。

        这里刻意【不清空】已有队列 ——
        关闭时队列里还留着的消息应该能被取走，
        直接丢弃会造成难以排查的「消息凭空消失」。
        """
        self._closed = True
        logger.debug("LocalTransport 已关闭（保留 %d 个信箱）", len(self._mailboxes))

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------
    # 收件箱
    # ------------------------------------------------------------
    def open_mailbox(self, agent_id: str) -> None:
        key = str(agent_id).strip()
        if not key:
            raise ValueError("agent_id 不能为空")

        if self._closed:
            raise TransportClosedError("传输层已关闭，无法开设新信箱")

        if key not in self._mailboxes:
            self._mailboxes[key] = asyncio.Queue(maxsize=self.max_queue_size)
            logger.debug("已开设信箱：%s", key)

    def close_mailbox(self, agent_id: str) -> None:
        self._mailboxes.pop(str(agent_id).strip(), None)

    def has_mailbox(self, agent_id: str) -> bool:
        return str(agent_id).strip() in self._mailboxes

    @property
    def mailboxes(self) -> List[str]:
        """当前有哪些信箱（用于调试输出，不暴露队列本身）。"""
        return sorted(self._mailboxes)

    # ------------------------------------------------------------
    # 寻址
    # ------------------------------------------------------------
    def address_for(self, agent_id: str) -> Address:
        """本进程内的地址就是 local://<agent_id>。"""
        return Address.for_local(str(agent_id).strip())

    # ------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------
    async def send(self, message: Message, to: Address) -> DeliveryReceipt:
        """
        把消息放进目标 Agent 的信箱。

        【为什么这里要重新校验地址】
            因为 send 可以被直接调用，绕过 CommunicationManager。
            传输层不能假设「上层一定校验过了」。
        """
        if self._closed:
            raise TransportClosedError("传输层已关闭，无法发送消息")

        if not isinstance(message, Message):
            raise DeliveryError(
                f"send 需要 Message，收到 {type(message).__name__}"
            )

        target = str(to.agent_id).strip()
        queue = self._mailboxes.get(target)

        if queue is None:
            # 关键：抛出【明确】的异常，而不是让 KeyError 冒出去
            raise AgentNotFoundError(
                f"本地没有 {target} 的信箱，消息无法投递。\n"
                f"当前已开设的信箱：{self.mailboxes or '（无）'}\n"
                f"（如果它应该在别的节点上，那是未来 P2PTransport 的工作 —— "
                f"本地传输只能送到本进程。）"
            )

        try:
            queue.put_nowait(message)
        except asyncio.QueueFull as exc:
            self.drop_count += 1
            raise DeliveryError(
                f"{target} 的信箱已满（上限 {self.max_queue_size}），消息被丢弃"
            ) from exc

        self.sent_count += 1

        return DeliveryReceipt(
            message_id=message.message_id,
            to=to,
            scheme=self.scheme,
            accepted=True,
            detail=f"已放入 {target} 的信箱",
        )

    # ------------------------------------------------------------
    # 接收
    # ------------------------------------------------------------
    async def receive(
        self, agent_id: str, timeout: Optional[float] = None
    ) -> Optional[Message]:
        """
        从信箱取一条消息。

        返回 None 表示超时（正常的「暂时没有」），不是错误。
        """
        target = str(agent_id).strip()
        queue = self._mailboxes.get(target)

        if queue is None:
            raise AgentNotFoundError(
                f"本地没有 {target} 的信箱。\n"
                f"当前已开设的信箱：{self.mailboxes or '（无）'}"
            )

        try:
            if timeout is None:
                message = await queue.get()
            else:
                message = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        self.received_count += 1
        return message

    # ------------------------------------------------------------
    # 调试辅助
    # ------------------------------------------------------------
    def pending(self, agent_id: str) -> int:
        """某个信箱里还积压多少条（测试用）。"""
        queue = self._mailboxes.get(str(agent_id).strip())
        return queue.qsize() if queue is not None else 0

    def describe(self) -> Dict[str, Any]:
        info = super().describe()
        info.update(
            {
                "mailboxes": self.mailboxes,
                "sent": self.sent_count,
                "received": self.received_count,
                "dropped": self.drop_count,
                "closed": self._closed,
                "max_queue_size": self.max_queue_size,
            }
        )
        return info

    def __repr__(self) -> str:
        return (
            f"LocalTransport(mailboxes={self.mailboxes}, "
            f"sent={self.sent_count}, received={self.received_count})"
        )

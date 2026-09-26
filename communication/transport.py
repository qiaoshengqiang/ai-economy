"""
Transport —— 传输层抽象接口
============================================================

【这个文件是 Step 3 的核心】

    它定义了「怎么把一条 Message 送到一个 Address」这件事，
    但【不定义】具体怎么送。

    当前只有一个实现：LocalTransport（同进程内存队列）。
    未来会有：P2PTransport / WebSocketTransport / QUICTransport ...

【为什么 Agent 绝不能依赖 LocalTransport】

    如果 Agent 里写的是：

        self.transport = LocalTransport()        ❌

    那么将来换成 P2P 时，就要改 Agent ——
    而 Agent 是经济主体，它不该因为「网络怎么实现」而改动。

    正确的关系是：

        Agent → CommunicationManager → Transport（抽象）
                                            ↑
                                    具体实现由组合根注入

    Agent 只知道「我能给人发消息」，不知道消息是怎么过去的。
    这和 Step 2 里「Agent 不知道模型是哪家」是同一个道理。

【接口为什么是 async】

    这不是为了炫技，而是因为未来的传输天然是异步的：

        · 网络 I/O 会阻塞，sync 版本必须靠线程绕
        · P2P 连接需要握手、重连、超时，天然是 await
        · 现在写成 async，将来加 P2PTransport 时
          上下所有代码都不用改

    代价：本地这个「纯内存」实现看起来有点小题大做 ——
    这是为了未来付的一点点提前量。
    项目其余部分（Economy / LLM）保持同步，不受影响。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from core.identity import utc_now_iso
from communication.address import Address
from communication.errors import TransportError
from communication.message import Message


# ============================================================
# 投递回执
# ============================================================
@dataclass(frozen=True)
class DeliveryReceipt:
    """
    一次投递的结果。

    【为什么 send 不返回 None】

        返回 None 意味着「调用方无法区分成功和静默失败」。
        在网络环境下，投递可能：
            · 成功
            · 被接受但还没送达（排队中）
            · 失败

        用一个明确的结构返回，将来接真实网络时不用改签名。
    """

    message_id: str
    to: Address
    scheme: str
    accepted: bool = True
    detail: str = ""
    delivered_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "to": str(self.to),
            "to_agent": self.to.agent_id,
            "scheme": self.scheme,
            "accepted": self.accepted,
            "detail": self.detail,
            "delivered_at": self.delivered_at,
        }

    def __str__(self) -> str:
        mark = "已投递" if self.accepted else "未投递"
        return f"<回执 {self.message_id} → {self.to} {mark}>"


# ============================================================
# 传输抽象
# ============================================================
class Transport(ABC):
    """
    所有传输方式的基类。

    【实现一个新的 Transport 要做的事】
        1. 继承这个类
        2. 设置 scheme（比如 "p2p"）
        3. 实现下面 5 个抽象方法
        4. 在组合根（main.py 的 Runtime）里换掉 LocalTransport

        Agent / CommunicationManager / Message 全都不用改。
    """

    # 这个传输方式对应的地址 scheme，比如 "local" / "p2p" / "ws"
    scheme: str = "base"

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    async def start(self) -> None:
        """
        启动传输层（绑定端口、建立连接池等）。

        默认什么都不做 —— 本地内存传输不需要启动。
        网络传输需要在这里 listen。
        """
        return None

    @abstractmethod
    async def close(self) -> None:
        """关闭传输层，释放资源。"""
        raise NotImplementedError

    # ------------------------------------------------------------
    # 收件箱管理
    # ------------------------------------------------------------
    @abstractmethod
    def open_mailbox(self, agent_id: str) -> None:
        """
        为某个 Agent 准备一个收件箱。

        【注意】这个方法只准备「信箱」，不接触 Agent 对象本身。
            LocalTransport 里存的是 asyncio.Queue，
            不是 Agent 实例 —— 这样信箱不会被 Agent 反过来操作。
        """
        raise NotImplementedError

    @abstractmethod
    def close_mailbox(self, agent_id: str) -> None:
        """注销某个 Agent 的收件箱。"""
        raise NotImplementedError

    @abstractmethod
    def has_mailbox(self, agent_id: str) -> bool:
        """这个 Agent 在本地有没有收件箱。"""
        raise NotImplementedError

    # ------------------------------------------------------------
    # 收发
    # ------------------------------------------------------------
    @abstractmethod
    async def send(self, message: Message, to: Address) -> DeliveryReceipt:
        """
        把消息送到指定地址。

        失败时抛：
            DeliveryError       送不出去
            TransportClosedError 传输层已关闭
        """
        raise NotImplementedError

    @abstractmethod
    async def receive(
        self, agent_id: str, timeout: Optional[float] = None
    ) -> Optional[Message]:
        """
        取一条发给该 Agent 的消息。

        返回 None 表示超时没等到（不是错误，是正常的「暂时没有」）。
        """
        raise NotImplementedError

    # ------------------------------------------------------------
    # 寻址
    # ------------------------------------------------------------
    @abstractmethod
    def address_for(self, agent_id: str) -> Address:
        """
        按本传输方式的格式，为某个 Agent 造一个地址。

        local → local://agent_b
        p2p   → p2p://<查表或缓存的 peer id>
        """
        raise NotImplementedError

    async def discover(self, agent_id: str) -> Optional[Address]:
        """
        远端发现钩子。

        【这是给未来 P2P 留的口子，本阶段不实现】

            本地传输：返回 None（本进程的 Agent 都在 Registry 里，
                      不需要「发现」）

            未来 P2P：在这里查 DHT / 广播询问 / 查引导节点。
                      Registry 里没有的 Agent，通过这里找到地址。

        有了这个钩子，CommunicationManager 查找收件人的逻辑
        从第一天起就是「先查注册表，查不到再问传输层」——
        将来接 P2P 时，Manager 一行都不用改。
        """
        return None

    # ------------------------------------------------------------
    # 自述
    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "transport": type(self).__name__,
            "scheme": self.scheme,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(scheme={self.scheme!r})"


def ensure_open(transport: Transport) -> None:
    """
    小工具：确认传输层还活着。

    写成函数而不是装饰器，是因为装饰器会把 async 方法
    包一层，出错时的堆栈会变得难读。
    """
    if getattr(transport, "_closed", False):
        from communication.errors import TransportClosedError

        raise TransportClosedError(
            f"传输层 {transport.scheme} 已经关闭，不能再收发消息"
        )

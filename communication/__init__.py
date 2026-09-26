"""
Communication —— Agent 之间的通信层
============================================================

【这个包的定位】

    Agent 是经济主体；这个包是它们之间唯一的说话方式。

    Agent
     ↓
    CommunicationManager      造消息 / 查地址 / 校验 / 去重 / 投递
     ↓
    Transport（抽象）
     ↓
    LocalTransport            当前实现：同进程内存
    （未来）P2PTransport       跨机器、去中心化

【两条绝对不能越过的边界】

    1. 不依赖 LLM
       ────────────────────────────────────────────
       通信层不能 import llm/*。
       为什么？
           因为大量消息根本不需要模型：
               PING / PONG          探活
               PAYMENT_RESULT       付款结果
               ACCEPT / REJECT      接单或拒单
           如果通信层依赖 LLM，那么「回一个 PONG」也要加载模型路由，
           而且测试通信时要准备一个假模型 —— 这两件事本该无关。

       正确的方向是单向的：
           Agent 收到消息 → Agent 自己决定 → 需要时才调 Brain

    2. 不依赖 Economy
       ────────────────────────────────────────────
       通信层不能 import core/wallet、core/ledger、core/economy。
       为什么？
           因为「把消息送到」和「解释消息里的钱」是两件事。
               Communication：A 给 B 发了一条 TASK_OFFER
               Economy      ：这条 offer 里的 20 AIC，A 付得起吗
           混在一起的话，以后加一种带钱的消息类型就要改通信层。
           经济语义归 Economy，通信层只搬字节。

    这两条边界由 tests/test_communication_guards.py 强制保证。

【为未来准备好的扩展点（现在都不实现）】

    · Transport.discover()      → P2P 节点发现
    · AgentRegistry.register_remote() → 远端 Agent 目录
    · MessageType 里的 TASK_OFFER / PAYMENT_RESULT 等 → 只有名字
    · Message 的 extensions / protocol_version → 前向兼容
    · Message.payload 里的 task_id / workspace_id → Task Workspace 接口

【通信日志（Step 3.5）】

    recorder.py 是这个包里的观察者，不参与通信：
        Message → Transport → CommunicationRecorder → SQLite

    它记录每条消息的生命周期（CREATED → SENT → … → PROCESSED），
    并支持按 message_id / trace_id / conversation_id 查回整条链路。

    删掉日志表，通信照常工作 —— 这就是「Message ≠ 日志」。
"""

from communication.address import Address
from communication.dedup import SeenMessageTracker
from communication.errors import (
    AgentNotFoundError,
    CommunicationError,
    DeliveryError,
    DuplicateAgentError,
    DuplicateMessageError,
    InvalidAddressError,
    MessageValidationError,
    TransportClosedError,
    TransportError,
)
from communication.local_transport import LocalTransport
from communication.manager import (
    CommunicationManager,
    InboundResult,
    ReceiveStatus,
)
from communication.message import PROTOCOL_VERSION, Message, MessageType
from communication.recorder import (
    REDACTED,
    CommunicationRecorder,
    MessageStatus,
    payload_fingerprint,
    redact,
)
from communication.registry import AgentInfo, AgentRegistry, PresenceStatus
from communication.transport import DeliveryReceipt, Transport

__all__ = [
    # 数据
    "Address",
    "Message",
    "MessageType",
    "PROTOCOL_VERSION",
    "AgentInfo",
    "PresenceStatus",
    "DeliveryReceipt",
    "InboundResult",
    "ReceiveStatus",
    # 抽象
    "Transport",
    # 实现
    "LocalTransport",
    # 目录与编排
    "AgentRegistry",
    "CommunicationManager",
    "SeenMessageTracker",
    # 通信日志
    "CommunicationRecorder",
    "MessageStatus",
    "redact",
    "payload_fingerprint",
    "REDACTED",
    # 异常
    "CommunicationError",
    "AgentNotFoundError",
    "InvalidAddressError",
    "DuplicateAgentError",
    "MessageValidationError",
    "DuplicateMessageError",
    "TransportError",
    "DeliveryError",
    "TransportClosedError",
]

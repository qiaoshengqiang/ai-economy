"""
CommunicationManager —— Agent 之间的通信层
============================================================

【它在整条链路里的位置】

    Agent
     ↓   agent.send(to="agent_b", text="hi")
    CommunicationManager          ← 这个文件
     ↓   造 Message / 查地址 / 校验 / 去重
    Transport（抽象）
     ↓
    LocalTransport                ← 当前实现（未来换 P2PTransport）

【它负责什么】

    · 创建消息（补 message_id、timestamp）
    · 校验消息字段
    · 把 agent_id 解析成 Address
    · 调用 Transport 投递
    · 从 Transport 收消息
    · 去重
    · 把消息交给目标 Agent 处理
    · 如果 Agent 回了东西，把回复也发出去

【它坚决不负责什么】

    ❌ 调用 LLM          → 那是 Brain 的事
    ❌ 碰钱包 / 扣钱      → 那是 Economy 的事
    ❌ 解释经济含义       → TASK_OFFER 里的钱归 Economy 管
    ❌ 读写文件 / 操作 Git

    为什么这条边界这么重要？
        因为通信是「信道」，经济是「规则」。
        如果通信层开始理解「这条 TASK_OFFER 里的报价合不合理」，
        那么以后加一种新消息类型就要改通信层，
        而且通信层会被迫依赖 Economy —— 两个层次就糊在一起了。

        正确的分工：
            Communication  只负责「把消息送到」
            Economy        负责「解释消息里的经济含义」

【本地绑定 vs 注册表数据：为什么分开放】

    registry : agent_id → AgentInfo（纯数据，可序列化）
               ← 本地和远端 Agent 都存在这里，长得一样

    _handlers: agent_id → 处理函数（活的 Python 对象）
               ← 只有【本进程】的 Agent 才有

    分开之后，"远端 Agent" 才可能出现得自然：
        它在 registry 里有条目，但在 _handlers 里没有 ——
        因为它根本不在这个进程里。

    如果混成一个字典，将来就只能存本地对象，
    「跨机器」这件事在数据结构上就被堵死了。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from core.identity import utc_now_iso
from core.logging_setup import get_logger
from communication.address import Address
from communication.dedup import (
    DEFAULT_MEMORY_SIZE,
    ClaimStatus,
    MessageClaim,
    SeenMessageTracker,
)
from communication.errors import (
    AgentNotFoundError,
    DuplicateMessageError,
    MessageValidationError,
)
from communication.message import Message, MessageType
from communication.recorder import CommunicationRecorder, MessageStatus
from communication.registry import AgentInfo, AgentRegistry, PresenceStatus
from communication.transport import DeliveryReceipt, Transport

logger = get_logger(__name__)

# 一个处理函数可以是同步的，也可以是异步的
MessageHandler = Callable[[Message], Any]


# ============================================================
# 收件结果
# ============================================================
class ReceiveStatus:
    """一次 receive() 的结果状态。"""

    DELIVERED = "delivered"    # 成功交给 Agent 处理了
    DUPLICATE = "duplicate"    # 重复消息，已跳过
    TIMEOUT = "timeout"        # 等超时了，没有消息
    NO_HANDLER = "no_handler"  # 收到了，但这个 Agent 没有处理函数


@dataclass
class InboundResult:
    """
    receive() 的返回值。

    【为什么不是直接返回 Message 或 None】
        因为「没收到」和「收到了但是重复」是完全不同的两种情况，
        调用方需要区分它们：
            · 重复 → 什么都不用做（而且不该算异常）
            · 超时 → 可以再等一会儿，或者做别的事
        只返回 None 的话，这两者就分不开了。
    """

    status: str
    message: Optional[Message] = None
    reply: Optional[Message] = None
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.status == ReceiveStatus.DELIVERED

    @property
    def is_duplicate(self) -> bool:
        return self.status == ReceiveStatus.DUPLICATE

    @property
    def is_timeout(self) -> bool:
        return self.status == ReceiveStatus.TIMEOUT

    def __str__(self) -> str:
        base = f"<{self.status}"
        if self.message is not None:
            base += f" {self.message.message_id}"
        if self.reply is not None:
            base += f" 回复={self.reply.message_id}"
        return base + ">"


# ============================================================
# Communication Manager
# ============================================================
class CommunicationManager:
    """
    Agent 之间通信的统一入口。

    用法：
        transport = LocalTransport()
        manager = CommunicationManager(transport)

        manager.register(agent_a, capabilities=["python"])
        manager.register(agent_b, capabilities=["testing"])

        await agent_a.send(to="agent_b", text="Hello")
        result = await manager.receive("agent_b", timeout=1.0)
    """

    def __init__(
        self,
        transport: Transport,
        registry: Optional[AgentRegistry] = None,
        seen_memory_size: int = DEFAULT_MEMORY_SIZE,
        recorder: Optional[CommunicationRecorder] = None,
    ):
        self.transport = transport
        self.registry = registry or AgentRegistry()

        # 通信日志（Step 3.5）。
        #
        # 【为什么是可选注入，而不是在内部 new 一个】
        #     1. 通信层不该知道数据库在哪 —— 那是组合根的事
        #     2. 测试通信时不该被迫准备一个数据库
        #     3. 将来换存储（文件、远端日志服务）只改注入的那一个地方
        #
        # recorder=None 时所有日志调用都是无操作，通信照常工作。
        self.recorder = recorder

        # 本进程内 agent_id → 处理函数。
        # 注意：这里【只放本地 Agent】。
        self._handlers: Dict[str, MessageHandler] = {}

        # 按消息类型注册的处理器（可选的扩展点，见 register_type_handler）
        self._type_handlers: Dict[str, MessageHandler] = {}

        # 去重：每个 Agent 一份
        self._seen: Dict[str, SeenMessageTracker] = {}
        self.seen_memory_size = int(seen_memory_size)

        # 统计
        self.sent_count = 0
        self.delivered_count = 0
        self.duplicate_count = 0
        self.timeout_count = 0

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    async def start(self) -> None:
        """启动底层传输（本地是空操作，网络传输会在这里 listen）。"""
        await self.transport.start()

    async def close(self) -> None:
        await self.transport.close()
        self._handlers.clear()

    # ------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------
    def register(
        self,
        agent: Any,
        capabilities: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
        address: Optional[Address] = None,
        presence: str = PresenceStatus.ONLINE,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> AgentInfo:
        """
        把一个 Agent 接入通信层。

        【参数 agent 是鸭子类型】
            只需要它有 .agent_id；如果有 .name 和 .handle_message 会更好用。
            这里刻意【不做类型检查】要求它必须是 BaseAgent ——
            否则 communication 层就要 import agents 层，
            依赖方向就反了（通信层应该能被独立测试）。

        做三件事：
            1. 往 registry 登记【纯数据】
            2. 在 transport 开一个信箱
            3. 把 alive 的处理函数记到本地绑定表
        """
        agent_id = getattr(agent, "agent_id", None) or str(agent)
        agent_id = str(agent_id).strip()
        if not agent_id:
            raise MessageValidationError("注册的 Agent 缺少 agent_id")

        display_name = str(getattr(agent, "name", "") or agent_id)

        info = AgentInfo(
            agent_id=agent_id,
            address=address or self.transport.address_for(agent_id),
            display_name=display_name,
            capabilities=tuple(capabilities or ()),
            presence=presence,
            is_local=True,
            metadata=dict(metadata or {}),
        )

        self.registry.register(info)
        self.transport.open_mailbox(agent_id)

        handler = getattr(agent, "handle_message", None)
        if callable(handler):
            self._handlers[agent_id] = handler
        else:
            logger.debug("%s 没有 handle_message，收到的消息会被标记为 no_handler", agent_id)

        self._seen[agent_id] = SeenMessageTracker(self.seen_memory_size)

        logger.debug("已接入通信层：%s @ %s", agent_id, info.address)
        return info

    def register_remote(
        self,
        agent_id: str,
        address: Address,
        capabilities: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
        presence: str = PresenceStatus.UNKNOWN,
        node: str = "remote",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> AgentInfo:
        """
        登记一个【远端】Agent —— 只有地址，没有本地处理函数。

        【本步骤不真的用这条路径收发消息】
            它的存在是为了证明数据模型是对的：
            远端 Agent 能作为一条普通记录存在于同一个注册表里，
            只是没有本地信箱。

            将来接上 P2PTransport 之后，往这里登记地址，
            通信就走网络了 —— 而上层代码看不到差别。
        """
        info = AgentInfo(
            agent_id=agent_id,
            address=address,
            display_name=agent_id,
            capabilities=tuple(capabilities or ()),
            presence=presence,
            node=node,
            is_local=False,
            metadata=dict(metadata or {}),
        )
        self.registry.register(info)
        return info

    def unregister(self, agent_id: str) -> bool:
        key = str(agent_id).strip()
        self._handlers.pop(key, None)
        self._seen.pop(key, None)
        self.transport.close_mailbox(key)
        return self.registry.unregister(key)

    # ------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------
    async def send(
        self,
        from_agent: str,
        to: str,
        text: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        message_type: str = MessageType.TEXT,
        reply_to: Optional[str] = None,
        trace_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Message:
        """
        发一条消息。返回实际发出的 Message（含 message_id）。

        trace_id / conversation_id 可以不传（会自动生成）。
        但【一次任务里的所有消息应该传同一个 trace_id】——
        否则日志里它们就是互不相干的孤儿，拼不出完整链路。
        Step 4 的 Task 会把自己的 trace_id 打在所有消息上。

        返回 Message 而不是回执，是因为调用方通常更关心
        「我发出去的那条消息」——尤其是要拿它的 message_id 去等回复。
        投递细节可以通过 send_message() 拿到。
        """
        sender = str(from_agent).strip()
        recipient = str(to).strip()

        # --- 校验发送方：必须是已登记的 Agent ---
        # 为什么必须查？
        #   否则任何代码都能随便编一个 from_agent 发消息 ——
        #   那样「身份」这个概念就失去了意义。
        if not self.registry.exists(sender):
            raise AgentNotFoundError(
                f"发送方 {sender!r} 没有接入通信层，不能发消息。\n"
                f"已登记的 Agent：{self.registry.ids() or '（无）'}"
            )

        body: Dict[str, Any] = dict(payload or {})
        if text is not None:
            body["text"] = text

        message = Message(
            from_agent=sender,
            to_agent=recipient,
            message_type=message_type,
            payload=body,
            reply_to=reply_to,
            **({"trace_id": trace_id} if trace_id else {}),
            **({"conversation_id": conversation_id} if conversation_id else {}),
        )

        await self.send_message(message)
        return message

    async def send_message(self, message: Message) -> DeliveryReceipt:
        """
        把一条已经构造好的 Message 投出去。

        这个方法给「需要完全控制消息内容」的场景用
        （比如测试里指定 message_id 造重复消息）。
        """
        if not isinstance(message, Message):
            raise MessageValidationError(
                f"send_message 需要 Message，收到 {type(message).__name__}"
            )

        address = await self.resolve_address(message.to_agent)

        # --- 日志：消息已构造，即将交给传输层 ---
        self._journal(
            message, MessageStatus.CREATED,
            actor=message.from_agent,
            transport=self.transport.scheme,
            address=address,
            detail="消息已构造，等待投递",
        )

        try:
            receipt = await self.transport.send(message, address)
        except Exception as exc:  # noqa: BLE001
            # 投递失败也要留痕 —— 否则「消息发了但对方没收到」
            # 这种情况在日志里完全看不见。
            self._journal(
                message, MessageStatus.FAILED,
                actor=message.from_agent,
                transport=self.transport.scheme,
                address=address,
                detail="传输层投递失败",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise

        self.sent_count += 1

        # --- 日志：传输层接受 ---
        self._journal(
            message, MessageStatus.SENT,
            actor=message.from_agent,
            transport=self.transport.scheme,
            address=address,
            detail=f"传输层已接受（{self.transport.scheme}）",
        )

        # --- 日志：已送达收件箱 ---
        #
        # 本地传输里 SENT 和 DELIVERED 是同一瞬间发生的。
        # 这里仍然记两条，是为了让状态机的形状和传输方式无关 ——
        # 将来 P2P 传输里「发出」和「对方确认收到」是两件不同的事，
        # 到那时本地和网络的日志才能用同一种方式查询。
        self._journal(
            message, MessageStatus.DELIVERED,
            actor=message.to_agent,
            transport=self.transport.scheme,
            address=address,
            detail=f"已进入 {message.to_agent} 的收件箱",
        )

        logger.debug("发出 %s", message)

        return receipt

    # ------------------------------------------------------------
    # 地址解析
    # ------------------------------------------------------------
    async def resolve_address(self, agent_id: str) -> Address:
        """
        把 agent_id 解析成可以投递的地址。

        【这里的查找顺序是为 P2P 提前设计好的】

            1. 注册表里有 → 直接用它的地址
            2. 注册表里没有 → 问传输层「你能发现它吗」
                               （本地返回 None；未来 P2P 会去查网络）
            3. 还是找不到 → AgentNotFoundError

        第 2 步就是为 P2PTransport 留的口子。
        有了它，将来接网络时这个方法的代码一行都不用改。
        """
        key = str(agent_id).strip()

        info = self.registry.find(key)
        if info is not None:
            return info.address

        discovered = await self.transport.discover(key)
        if discovered is not None:
            # 发现成功 → 登记成远端条目，下次就不用再找了
            self.register_remote(key, discovered)
            logger.info("通过 %s 发现了远端 Agent：%s", self.transport.scheme, key)
            return discovered

        raise AgentNotFoundError(
            f"找不到收件人 {key!r}。\n"
            f"  注册表里已登记：{self.registry.ids() or '（无）'}\n"
            f"  当前传输方式：{self.transport.scheme}\n"
            f"（本地传输只能送到本进程；跨进程要等 P2PTransport）"
        )

    # ------------------------------------------------------------
    # 接收
    # ------------------------------------------------------------
    async def receive(
        self,
        agent_id: str,
        timeout: Optional[float] = None,
        strict_dedup: bool = False,
    ) -> InboundResult:
        """
        取一条消息，交给对应 Agent 处理。

        返回 InboundResult（见上面的说明：为什么不是 Message 或 None）。

        strict_dedup=True 时，重复消息会抛 DuplicateMessageError
        而不是被静默跳过 —— 调试和测试时很有用。
        """
        key = str(agent_id).strip()

        message = await self.transport.receive(key, timeout=timeout)
        if message is None:
            self.timeout_count += 1
            # 【注意】这里不写消息级日志 —— 因为压根没有消息。
            # 「等超时了」是 Agent 级别的现象，不是某条消息的状态。
            # 消息级的 TIMEOUT 属于「发出去了但对方没确认」，
            # 那需要 ACK 机制，本步骤不实现（状态常量已留好）。
            return InboundResult(status=ReceiveStatus.TIMEOUT, detail="没有等到消息")

        # --- 日志：从信箱取出 ---
        self._journal(
            message, MessageStatus.RECEIVED,
            actor=key,
            transport=self.transport.scheme,
            detail=f"已从 {key} 的收件箱取出",
        )

        # --- 防御性检查：确认没有投递错人 ---
        if message.to_agent != key:
            logger.warning(
                "投递异常：%s 的信箱里收到了发给 %s 的消息 %s",
                key, message.to_agent, message.message_id,
            )
            return InboundResult(
                status=ReceiveStatus.NO_HANDLER,
                message=message,
                detail=f"消息的收件人是 {message.to_agent}，不是 {key}",
            )

        # --- 去重（Step 4D.1：以持久化事实为准）---
        #
        # 【这里原来是内存 tracker 说了算】
        #     进程内 OrderedDict —— 重启就忘光了，
        #     同一条消息会被再处理一遍。
        #
        # 【现在谁说了算】
        #     有持久层（recorder）时 → **数据库**说了算：
        #         communication_messages.processed_once
        #         + claim_id 的原子认领
        #     内存 tracker 退化成快路径缓存，丢了也不影响正确性。
        #
        #     没有持久层时（比如纯粹测通信的单元测试）
        #     → 退回内存 tracker。这是一条**降级路径**，
        #       适用边界见 _claim_message 的说明。
        claim = self._claim_message(message, key)

        if not claim.claimed:
            self.duplicate_count += 1

            # 重复投递必须留痕。
            # 消息表里这个 message_id 仍然只有一行（UPSERT），
            # 但事件流里会多一条 DUPLICATE ——
            # 于是「有没有重复投递」是可查的，而不是被静默吞掉。
            self._journal(
                message, MessageStatus.DUPLICATE,
                actor=key,
                transport=self.transport.scheme,
                detail=(claim.detail or f"重复投递（{message.message_id}），已跳过"),
            )

            logger.debug("跳过消息 %s（%s）", message.message_id, claim.status)

            if strict_dedup:
                raise DuplicateMessageError(
                    f"重复消息：{message.message_id}（来自 {message.from_agent}）"
                )

            return InboundResult(
                status=ReceiveStatus.DUPLICATE,
                message=message,
                detail=f"这条消息不需要再处理（{claim.status}）",
            )

        # --- 找处理函数 ---
        handler = self._type_handlers.get(message.message_type)
        if handler is None:
            handler = self._handlers.get(key)

        if handler is None:
            logger.debug("%s 没有处理函数，消息 %s 被搁置", key, message.message_id)
            return InboundResult(
                status=ReceiveStatus.NO_HANDLER,
                message=message,
                detail=f"{key} 没有注册消息处理函数",
            )

        # --- 日志：开始处理 ---
        self._journal(
            message, MessageStatus.PROCESSING,
            actor=key,
            transport=self.transport.scheme,
            detail=f"{key} 开始处理",
        )

        # --- 处理 ---
        try:
            outcome = handler(message)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:  # noqa: BLE001
            # 处理函数抛异常 = 这条消息处理失败。
            # 这类失败最容易被忽略（异常被上层吞掉就查不到了），
            # 所以必须在这里留一条 FAILED。
            #
            # 【Step 4D.1：失败也要落成持久化事实】
            #     但**绝不**把失败标成「已处理」——
            #     失败可能是临时的，标成完成会让这条消息
            #     永远失去被正确处理的机会，而且没人发现。
            #
            #     mark_failed 只做三件事：
            #         failure_count + 1
            #         记下错误信息
            #         释放认领（不占着不放）
            #     processed_once 保持 0。
            if self.recorder is not None:
                try:
                    self.recorder.mark_failed(
                        message, key,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                except Exception:  # noqa: BLE001
                    # 记录失败时又失败：不能让这个异常盖住原始异常 ——
                    # 原始异常才是调用方需要看到的。
                    logger.exception(
                        "记录消息 %s 的失败状态时又出错了", message.message_id
                    )

            self._journal(
                message, MessageStatus.FAILED,
                actor=key,
                transport=self.transport.scheme,
                detail=f"{key} 处理消息时抛出异常",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise

        self.delivered_count += 1

        # --- 把「处理成功」变成持久化事实 ---
        #
        # 【为什么必须在记日志之前做】
        #     mark_processed 是去重的事实来源。
        #     如果先记日志、再标成功，中间崩溃就会出现
        #     「日志说处理完了，但去重不知道」——
        #     下次重发又会被处理一遍。
        #
        #     反过来（先标成功、再记日志）即使崩溃也安全：
        #     最坏是少一条日志，但**不会重复处理**。
        #     宁可日志缺一条，也不要副作用发生两次。
        if self.recorder is not None:
            first = self.recorder.mark_processed(message, key)
            # 同步内存缓存：这里才是「可以放进快路径」的唯一时机 ——
            # 因为此刻数据库已经确认成功了。
            self._tracker_for(key).remember(message.message_id)
            logger.debug(
                "消息 %s 处理成功（%s）",
                message.message_id,
                "首次成功" if first else "之前已成功过",
            )
        else:
            # 没有持久层：内存就是全部事实（降级路径）
            self._tracker_for(key).remember(message.message_id)

        # --- 日志：处理完成 ---
        self._journal(
            message, MessageStatus.PROCESSED,
            actor=key,
            transport=self.transport.scheme,
            detail=f"{key} 处理完成",
        )

        logger.debug("%s 处理了 %s", key, message)

        # --- 处理函数可能返回一条回复 ---
        reply = self._coerce_reply(message, outcome)
        if reply is not None:
            await self.send_message(reply)
            logger.debug("%s 回复了 %s", key, reply)

        return InboundResult(
            status=ReceiveStatus.DELIVERED,
            message=message,
            reply=reply,
            detail=f"已交给 {key} 处理",
        )

    # ------------------------------------------------------------
    # 去重（Step 4D.1）
    # ------------------------------------------------------------
    def _tracker_for(self, agent_id: str) -> SeenMessageTracker:
        """取（必要时创建）某个 Agent 的内存去重缓存。"""
        tracker = self._seen.get(agent_id)
        if tracker is None:
            tracker = SeenMessageTracker(self.seen_memory_size)
            self._seen[agent_id] = tracker
        return tracker

    def _claim_message(self, message: Message, owner: str) -> MessageClaim:
        """
        认领一条消息 —— 决定「这条消息该不该被处理」。

        ============================================================
        【两种模式，必须分清哪个是正确性来源】
        ============================================================

            ① 有持久层（recorder 存在）—— 正常模式

                数据库说了算：
                    communication_messages.processed_once  （成功事实）
                    claim_id 的条件 UPDATE                （原子认领）

                内存 tracker 只是**加速**：
                    已经确认处理过的 message_id 记在内存里，
                    下次直接跳过，不必查库。

                所以清空 tracker、换新实例、进程重启 ——
                正确性都不受影响。这正是 4D.1 要的效果。

            ② 没有持久层（recorder is None）—— 降级模式

                比如只测通信层、不接数据库的场景。
                这时内存 tracker 是**唯一**的事实来源。

                这是一条明确的降级路径，不是设计目标。
                它的局限（重启失忆）就是 4D.1 要修的问题本身 ——
                所以**生产路径必须注入 recorder**。

        【为什么不能只用内存 tracker 做快路径判断】

            内存说「见过」不等于「处理成功过」：
            比如第一次处理失败了，内存记下了 id，但数据库里
            processed_once 还是 0。此时直接跳过就错了。

            所以快路径只在**数据库确认过成功**的消息上生效
            （见 _claim_message 下面那行 tracker.remember 的位置 ——
             它只在 mark_processed 之后才被调用）。
        """
        if self.recorder is None:
            # ---- 降级模式：内存是唯一事实 ----
            tracker = self._tracker_for(owner)
            if tracker.check_and_add(message.message_id):
                return MessageClaim(
                    status=ClaimStatus.CLAIMED,
                    message_id=message.message_id,
                    owner=owner,
                    detail="认领成功（内存模式，无持久层）",
                )
            return MessageClaim(
                status=ClaimStatus.DUPLICATE,
                message_id=message.message_id,
                owner=owner,
                detail="内存记录显示这条消息见过（降级模式）",
            )

        # ---- 正常模式：数据库裁决 ----
        #
        # 快路径：内存里已经确认过「处理成功」的直接跳过，
        # 省掉一次数据库往返。
        tracker = self._tracker_for(owner)
        if tracker.seen(message.message_id):
            return MessageClaim(
                status=ClaimStatus.DUPLICATE,
                message_id=message.message_id,
                owner=owner,
                detail="内存缓存命中（数据库已确认处理过）",
            )

        return self.recorder.claim_message(message, owner)

    def was_processed(self, message_id: str) -> bool:
        """
        这条消息成功处理过吗 —— **跨进程重启仍然有效**。

        没有持久层时退回内存判断（降级模式，重启后会失忆）。
        """
        if self.recorder is not None:
            return self.recorder.was_processed(message_id)
        for tracker in self._seen.values():
            if tracker.seen(message_id):
                return True
        return False

    # ------------------------------------------------------------
    # 类型路由（扩展点）
    # ------------------------------------------------------------
    def register_type_handler(
        self, message_type: str, handler: MessageHandler
    ) -> None:
        """
        给某种消息类型注册专门的处理函数。

        【为什么要有这个，而不是在 Agent 里写 if/elif】

            ❌ Agent.handle_message 里长成：
                if msg.message_type == "TEXT": ...
                elif msg.message_type == "PING": ...
                elif msg.message_type == "TASK_OFFER": ...
                # 每加一种消息类型，就要改 Agent

            ✅ 现在：
                manager.register_type_handler("TASK_OFFER", task_manager.on_offer)

            这样 Step 4 接任务系统时，
            只需要注册一个新函数，Agent 和通信层都不用改。

        【注意】类型处理器优先级【高于】Agent 的通用处理器。
        """
        key = str(message_type).strip().upper()
        if not key:
            raise MessageValidationError("message_type 不能为空")
        if not callable(handler):
            raise MessageValidationError(
                f"handler 必须可调用，收到 {type(handler).__name__}"
            )
        self._type_handlers[key] = handler

    # ------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------
    def _journal(
        self,
        message: Message,
        status: str,
        actor: Optional[str] = None,
        transport: Optional[str] = None,
        address: Optional[Any] = None,
        detail: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        写一条通信日志。

        【为什么这里要吞掉异常】
            日志是【观测手段】，不该成为通信的单点故障。
            如果数据库满了导致消息发不出去，那是本末倒置。

            记录器内部已经处理了这一点（strict=False 时返回 None），
            这里再包一层是为了防止「记录器本身初始化异常」之类的
            意外情况把通信搞崩。

            测试里想要严格模式，用 CommunicationRecorder(strict=True)。
        """
        if self.recorder is None:
            return

        try:
            self.recorder.record(
                message=message,
                status=status,
                actor=actor,
                transport=transport,
                address=address,
                detail=detail,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "通信日志写入异常（不影响通信）：%s: %s",
                type(exc).__name__, exc,
            )

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------
    @staticmethod
    def _coerce_reply(
        original: Message, outcome: Any
    ) -> Optional[Message]:
        """
        把处理函数的返回值统一成一条回复消息。

        允许处理函数返回：
            Message   → 原样发出（高级用法）
            str       → 包成 {"text": "..."} 的回复
            dict      → 包成 payload 回复
            None      → 不回复（最常见）

        为什么要放宽这个？
            因为在 Step 4 里会到处出现这种写法：

                def on_ping(msg):
                    return "PONG"

            逼他写完整的 Message(...) 只会制造噪音。
        """
        if outcome is None:
            return None

        if isinstance(outcome, Message):
            return outcome

        if isinstance(outcome, str):
            return original.reply(text=outcome)

        if isinstance(outcome, Mapping):
            return original.reply(payload=outcome)

        # 其它类型：当成「没回复」，但要留下痕迹
        logger.warning(
            "处理函数返回了无法理解的类型 %s，已忽略",
            type(outcome).__name__,
        )
        return None

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def lookup(self, agent_id: str) -> AgentInfo:
        return self.registry.lookup(agent_id)

    def find(self, agent_id: str) -> Optional[AgentInfo]:
        return self.registry.find(agent_id)

    def has_local_handler(self, agent_id: str) -> bool:
        """
        这个 Agent 在本进程内有处理函数吗？

        对本地 Agent 是 True，对远端登记的 Agent 是 False。
        将来这个判断就是「直投还是走网络」的分岔点。
        """
        return str(agent_id).strip() in self._handlers

    def seen_tracker(self, agent_id: str) -> Optional[SeenMessageTracker]:
        return self._seen.get(str(agent_id).strip())

    def stats(self) -> Dict[str, Any]:
        return {
            "sent": self.sent_count,
            "delivered": self.delivered_count,
            "duplicates": self.duplicate_count,
            "timeouts": self.timeout_count,
            "registry": self.registry.stats(),
            "transport": self.transport.describe(),
            "type_handlers": sorted(self._type_handlers),
            "journal": self.recorder.stats() if self.recorder is not None else None,
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "transport": type(self.transport).__name__,
            "scheme": self.transport.scheme,
            "registered": self.registry.ids(),
            "local_handlers": sorted(self._handlers),
            "remote_agents": [i.agent_id for i in self.registry.remote_agents()],
            "journal_enabled": self.recorder is not None,
        }

    def __repr__(self) -> str:
        return (
            f"CommunicationManager(transport={self.transport.scheme!r}, "
            f"agents={self.registry.ids()})"
        )

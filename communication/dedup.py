"""
重复消息检测
============================================================

【为什么本地环境也需要这个】

    你可能会想：本地一次投递，怎么会重复？

    但现在就把这套机制建起来，是因为两件事：

    1. 未来的网络环境里，重复投递是【常态】而非异常
          · 对方超时了，发送方重发 → 收到两条
          · 网络抖动导致 ACK 丢失 → 收到两条
          · P2P 网络的多路径转发 → 收到两条

    2. 一旦重复被当成两条独立业务消息，后果很严重
          如果 TASK_OFFER 被处理两次，Agent B 可能会
          接两次同一个任务，然后交付两份成果，收两次钱。
          这种 bug 在本地测不出来，上线才炸。

    所以「收到过没有」这件事，必须从第一天就记下来。

【Step 4D.1：这里的态度变了】

    上面那段「为什么不做得更复杂」是 Step 3 写的。
    它当时是对的 —— 但 4D 之后不再成立，原因有两个：

        1. 后果变严重了
           3.x 时重复处理一条报价只是「多记一条报价」。
           4D 之后重复处理一条 SUBMIT 就是**多交付一次成果**。
           而 Workspace 阶段之后，它还会变成「多写一个文件」。

        2. 「重启就失忆」是真实缺陷，不是简化
           这个 tracker 是进程内的 OrderedDict。
           进程一重启就忘光了 —— 同一条消息会被再处理一遍。
           这不是「简单」，这是**错**。

【现在的分工】

    ┌─────────────────────────────────────────────────────┐
    │  持久化事实（唯一正确性来源）                        │
    │      communication_messages.processed_once           │
    │      + claim_id 的原子认领                           │
    │      → 跨进程重启依然有效                            │
    ├─────────────────────────────────────────────────────┤
    │  内存 tracker（只是快路径缓存）                      │
    │      避免每条消息都查一次库                          │
    │      **丢了也不影响正确性**                          │
    └─────────────────────────────────────────────────────┘

    有架构守卫盯着这条线：
    SeenMessageTracker 不许成为正确性的唯一来源。

    也就是说：把 tracker 清空、或者换一个全新的实例，
    重复消息仍然必须被识别出来 —— 因为事实在数据库里。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional

# 默认记住多少条。超出的按「最旧的先忘掉」处理。
DEFAULT_MEMORY_SIZE = 1024


# ============================================================
# 认领结果
# ============================================================
class ClaimStatus:
    """
    一次「认领这条消息」尝试的结果。

    【为什么要区分这三种，而不是一个布尔值】

        布尔值只能表达「我拿到了吗」，
        但「没拿到」有两个**完全不同**的原因：

            DUPLICATE  这条消息以前就成功处理过了 → 永远不该再处理
            BUSY       有别人正在处理它             → 现在不该处理，但也没处理完

        混在一起的话，日志里就分不清
        「收到一条早就处理过的重发」和
        「两个 worker 撞车了」—— 而这两件事的排查方向完全不同。
    """

    #: 认领成功，可以处理
    CLAIMED = "CLAIMED"

    #: 这条消息已经成功处理过（持久化事实）
    DUPLICATE = "DUPLICATE"

    #: 有别人正在处理（认领还没释放）
    BUSY = "BUSY"

    @classmethod
    def all(cls) -> List[str]:
        return [cls.CLAIMED, cls.DUPLICATE, cls.BUSY]


@dataclass(frozen=True)
class MessageClaim:
    """
    一次认领尝试的结果。

    【它只是一个结果值】
        不认识数据库、不认识 Message、不做任何 I/O。
        由 CommunicationRecorder 构造出来交给 Manager。
    """

    status: str
    message_id: str

    #: 谁持有着认领（BUSY / DUPLICATE 时有用）
    owner: Optional[str] = None

    #: 认领时间（排查用）
    claimed_at: Optional[str] = None

    #: 失败过几次（DUPLICATE 时能看出这条消息当初顺利不顺利）
    failure_count: int = 0

    #: 【Workspace Phase 0】这次认领是不是「接手了一个过期的认领」
    #:
    #:     为什么要在结果里区分它：
    #:         新认领     → 正常路径
    #:         接手过期   → 说明上一个 worker **崩了**（或者卡住了）
    #:
    #:     后者是要排查的信号。混在一起的话，日志里看不出
    #:     「这条消息被处理了两次」到底是重发还是崩溃恢复。
    reclaimed: bool = False

    #: 接手时，上一个认领是谁持有的（只有 reclaimed=True 时有值）
    previous_owner: Optional[str] = None

    detail: str = ""

    @property
    def claimed(self) -> bool:
        """我拿到了吗？"""
        return self.status == ClaimStatus.CLAIMED

    @property
    def duplicate(self) -> bool:
        return self.status == ClaimStatus.DUPLICATE

    @property
    def busy(self) -> bool:
        return self.status == ClaimStatus.BUSY

    def __bool__(self) -> bool:
        return self.claimed

    @property
    def recovered(self) -> bool:
        """这次认领是不是一次崩溃恢复（接手了过期租约）。"""
        return self.claimed and self.reclaimed

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message_id": self.message_id,
            "owner": self.owner,
            "claimed_at": self.claimed_at,
            "failure_count": self.failure_count,
            "reclaimed": self.reclaimed,
            "previous_owner": self.previous_owner,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        return f"{self.status}({self.message_id})"


class SeenMessageTracker:
    """
    记录「最近见过哪些 message_id」。

    【Step 4D.1 之后它只是快路径缓存】

        正确性**不**依赖它 —— 依赖 communication_messages.processed_once。
        它的作用是「避免每条消息都查一次库」。

        所以：可以清空它、可以换新实例、可以有界淘汰，
        重复消息仍然会被认领逻辑拦住。
    

    用法：
        tracker = SeenMessageTracker()
        tracker.is_new("msg_001")   # True  → 第一次见
        tracker.is_new("msg_001")   # False → 重复

    或者一次完成「检查 + 记录」：
        if tracker.check_and_add(message.message_id):
            handle(message)
    """

    def __init__(self, memory_size: int = DEFAULT_MEMORY_SIZE):
        if memory_size <= 0:
            raise ValueError(f"memory_size 必须大于 0，收到 {memory_size}")

        self.memory_size = int(memory_size)

        # OrderedDict 当有界集合用：
        #   · 键去重
        #   · 插入有序 → 可以按「最旧的」淘汰
        self._seen: "OrderedDict[str, None]" = OrderedDict()

        self.duplicates = 0
        self.total_seen = 0

    # ------------------------------------------------------------
    def seen(self, message_id: str) -> bool:
        """这条消息以前见过吗？（只查，不改状态）"""
        return str(message_id) in self._seen

    def is_new(self, message_id: str) -> bool:
        """以前没见过吗？（只查，不改状态）"""
        return not self.seen(message_id)

    def remember(self, message_id: str) -> None:
        """
        记下一条消息。

        注意：如果已经记过，这里不会重复计数 ——
        只有 check_and_add 才会把重复算进统计。
        """
        key = str(message_id)
        if key in self._seen:
            self._seen.move_to_end(key)
            return

        self._seen[key] = None
        self.total_seen += 1

        # 超容量就淘汰最旧的
        while len(self._seen) > self.memory_size:
            self._seen.popitem(last=False)

    def check_and_add(self, message_id: str) -> bool:
        """
        检查并记录，返回「是不是新的」。

            返回 True  → 新消息，正常处理
            返回 False → 重复消息，应该跳过，并已累加 duplicates 计数
        """
        if self.seen(message_id):
            self.duplicates += 1
            # 重复消息也刷新一下位置，避免它被过早淘汰
            self._seen.move_to_end(str(message_id))
            return False

        self.remember(message_id)
        return True

    # ------------------------------------------------------------
    def forget(self, message_id: str) -> bool:
        """忘记一条（主要给测试用）。"""
        key = str(message_id)
        if key in self._seen:
            del self._seen[key]
            return True
        return False

    def clear(self) -> None:
        self._seen.clear()
        self.duplicates = 0
        self.total_seen = 0

    @property
    def size(self) -> int:
        """当前记住多少条。"""
        return len(self._seen)

    def recent(self, limit: int = 10) -> List[str]:
        """最近见过的 ID（新的在前）。"""
        items = list(self._seen.keys())
        return list(reversed(items[-limit:]))

    def stats(self) -> dict:
        return {
            "memory_size": self.memory_size,
            "tracked": self.size,
            "unique_messages": self.total_seen,
            "duplicates": self.duplicates,
        }

    def __contains__(self, message_id: object) -> bool:
        return str(message_id) in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def __repr__(self) -> str:
        return (
            f"SeenMessageTracker(tracked={self.size}, "
            f"duplicates={self.duplicates})"
        )

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

【为什么不做得更复杂】

    真正的分布式幂等需要：
        · 持久化的消息日志
        · 带时间窗的去重
        · 跨节点的共识

    这些现在都不需要。当前只要：
        · 记住最近 N 条 message_id
        · 重复的直接跳过并计数

    内存有界、实现简单、行为可预测 —— 对本地双 Agent 完全够用。
    接口留好了，将来换成持久化实现时调用方不用改。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import List, Optional

# 默认记住多少条。超出的按「最旧的先忘掉」处理。
DEFAULT_MEMORY_SIZE = 1024


class SeenMessageTracker:
    """
    记录「最近见过哪些 message_id」。

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

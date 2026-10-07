"""
Hard Limit —— Runtime 强制边界（§十–§十三）
============================================================

【🔴 Hard Limit = Rule / Structure】

    max_continuous_runtime_seconds（示例 5400 = 90 分钟）

    到达后 Runtime **必须强制停止当前生产**。
    Agent **不能**通过继续思考 / 改 Strategy / 改 Action /
    请求更多时间 来绕过它。

【🔴 Hard Limit reached ≠ Stage Failed ≠ Stage Completed（§十二）】

    计划 10 章 → 实际第 7 章 → 到 90 分钟
        ✅ 保存 Checkpoint · Runtime Session STOPPED_HARD_LIMIT
        ✅ Stage 仍 **ACTIVE**（只要 Agent/Task 还准备继续）
        ❌ Stage = FAILED
        ❌ Stage = COMPLETED

【为什么 Stage 保持 ACTIVE 而不是 WAITING（§十三）】

    被强制停止的是 **Runtime Session**，不是 Stage。
    把 Session 状态和 Stage 状态混在一起，正是 §八 警告的重复状态机。
    → Stage=ACTIVE + Session=STOPPED_HARD_LIMIT 是更清晰的分离。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

#: Runtime Session 的结束原因（**注意这些不是 Stage 状态**）
SESSION_RUNNING = "RUNNING"
SESSION_STOPPED_AGENT = "STOPPED_AGENT"          # Agent 主动 stop/pause
SESSION_STOPPED_HARD_LIMIT = "STOPPED_HARD_LIMIT"
SESSION_STOPPED_CRASH = "STOPPED_CRASH"


@dataclass
class RuntimeSession:
    """
    一次**连续执行**。它**不是** Task，也**不是** Stage。

    🔒 Session 结束**不改变** Stage 语义（除 Hard Limit 会写 Checkpoint）。
    """

    limit_seconds: Optional[float] = None
    started_at: float = 0.0
    state: str = SESSION_RUNNING
    stop_reason: str = ""

    def start(self) -> "RuntimeSession":
        self.started_at = time.monotonic()
        self.state = SESSION_RUNNING
        self.stop_reason = ""
        return self

    def elapsed_seconds(self) -> float:
        if self.started_at <= 0:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)

    def remaining_seconds(self) -> Optional[float]:
        if not self.limit_seconds:
            return None
        return max(0.0, float(self.limit_seconds) - self.elapsed_seconds())

    def is_exceeded(self) -> bool:
        """
        Hard Limit 是否已达到。

        🔒 这是**结构判断**，不是策略判断 ——
           Runtime 有权也有义务执行它（§十一）。
        """
        if not self.limit_seconds or self.limit_seconds <= 0:
            return False
        return self.elapsed_seconds() >= float(self.limit_seconds)

    def enforce(self) -> bool:
        """
        Runtime 强制检查。达到就停 —— **Agent 说什么都不延长**（§十一）。

        返回 True 表示「本次被 Hard Limit 强制停止」。
        """
        if self.is_exceeded():
            self.state = SESSION_STOPPED_HARD_LIMIT
            self.stop_reason = "hard_limit"
            return True
        return False

    def stop_by_agent(self, reason: str = "") -> None:
        """Agent 主动停止（§十四 / §十五）。**与 Hard Limit 是两回事。**"""
        self.state = SESSION_STOPPED_AGENT
        self.stop_reason = reason or "agent_decision"

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "stop_reason": self.stop_reason,
            "limit_seconds": self.limit_seconds,
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
            "remaining_seconds": (round(self.remaining_seconds(), 1)
                                  if self.remaining_seconds() is not None
                                  else None),
        }

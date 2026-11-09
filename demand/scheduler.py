"""
RunScheduler —— 串行轮询参与者
================================

🔒 它**只调度**：
    - 决定下一个 turn 轮到谁；
    - 调用注入的 `run_turn(agent_id)`；
    - 读取注入的 `task_status()`；
    - 在 Task 终态 / 墙钟 / 轮数 / 无进展时停。

❌ 不建 Task、不选参与者、不设 DoD、不验收、不改 Task 状态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from demand.models import TurnOutcome

_TERMINAL = {"COMPLETED", "REJECTED", "CANCELLED", "EXPIRED", "FAILED"}


@dataclass
class SchedulerResult:
    exit_reason: str
    turns: int
    status: str
    outcomes: List[TurnOutcome] = field(default_factory=list)


class RunScheduler:
    def __init__(
        self,
        *,
        participants: List[str],
        run_turn: Callable[[str], TurnOutcome],
        task_status: Callable[[], str],
        max_turns: int = 30,
        deadline: Optional[float] = None,
        no_progress_cycles: int = 3,
        min_no_progress_turns: int = 8,
    ) -> None:
        self.participants = [str(p) for p in participants if p]
        self.run_turn = run_turn
        self.task_status = task_status
        self.max_turns = max(1, int(max_turns))
        self.deadline = deadline
        self.no_progress_cycles = max(1, int(no_progress_cycles))
        #: 小团队（尤其 1-2 人）的 cycle 很短；3 cycles 可能只有 3-6 turn。
        #: 至少经过这么多 turn 无强进展才停，避免在 Agent 正要恢复时停掉。
        self.min_no_progress_turns = max(1, int(min_no_progress_turns))
        #: 每个 Agent 只享受一次“截断恢复宽限”。
        self._truncation_grace_used: set[str] = set()

    def run(self) -> SchedulerResult:
        outcomes: List[TurnOutcome] = []
        turns = 0
        no_progress = 0
        last_strong_turn = 0

        status = str(self.task_status() or "")
        if status in _TERMINAL:
            return SchedulerResult(
                exit_reason="COMPLETED" if status == "COMPLETED"
                else f"TASK_{status}",
                turns=0, status=status, outcomes=outcomes)

        while turns < self.max_turns:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                return SchedulerResult(
                    exit_reason="TIME_LIMIT", turns=turns,
                    status=str(self.task_status() or ""), outcomes=outcomes)

            cycle_progress = False
            cycle_truncated: set[str] = set()
            for agent_id in self.participants:
                if turns >= self.max_turns:
                    break
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    return SchedulerResult(
                        exit_reason="TIME_LIMIT", turns=turns,
                        status=str(self.task_status() or ""), outcomes=outcomes)

                before = str(self.task_status() or "")
                outcome = self.run_turn(agent_id)
                outcomes.append(outcome)
                turns += 1
                after = str(self.task_status() or "")

                # 🔴 只有**强进展**（状态变化 / 写入 / 编辑 / 新目录）
                #    才重置 stall。普通协调消息不能让 run 无限延长。
                if after != before or outcome.strong_progress:
                    cycle_progress = True
                    last_strong_turn = turns
                if outcome.truncated:
                    cycle_truncated.add(str(agent_id))

                if after in _TERMINAL:
                    return SchedulerResult(
                        exit_reason=("COMPLETED" if after == "COMPLETED"
                                     else f"TASK_{after}"),
                        turns=turns, status=after, outcomes=outcomes)

            if cycle_progress:
                no_progress = 0
                continue

            turns_since_strong = turns - last_strong_turn
            would_stop = (
                no_progress + 1 >= self.no_progress_cycles
                and turns_since_strong >= self.min_no_progress_turns)
            graceful = cycle_truncated - self._truncation_grace_used
            if would_stop and graceful:
                # 🔴 认知被截断不是“Agent 选择不做”。给它一次恢复机会：
                #    下一轮会看到截断压力，可以拆小/提高预算。
                self._truncation_grace_used |= graceful
                continue

            no_progress += 1
            if would_stop:
                return SchedulerResult(
                    exit_reason="NO_PROGRESS", turns=turns,
                    status=str(self.task_status() or ""),
                    outcomes=outcomes)

        return SchedulerResult(
            exit_reason="MAX_TURNS", turns=turns,
            status=str(self.task_status() or ""), outcomes=outcomes)

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
    ) -> None:
        self.participants = [str(p) for p in participants if p]
        self.run_turn = run_turn
        self.task_status = task_status
        self.max_turns = max(1, int(max_turns))
        self.deadline = deadline
        self.no_progress_cycles = max(1, int(no_progress_cycles))

    def run(self) -> SchedulerResult:
        outcomes: List[TurnOutcome] = []
        turns = 0
        no_progress = 0

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

                if after != before or outcome.actions > 0:
                    cycle_progress = True

                if after in _TERMINAL:
                    return SchedulerResult(
                        exit_reason=("COMPLETED" if after == "COMPLETED"
                                     else f"TASK_{after}"),
                        turns=turns, status=after, outcomes=outcomes)

            if cycle_progress:
                no_progress = 0
            else:
                no_progress += 1
                if no_progress >= self.no_progress_cycles:
                    return SchedulerResult(
                        exit_reason="NO_PROGRESS", turns=turns,
                        status=str(self.task_status() or ""),
                        outcomes=outcomes)

        return SchedulerResult(
            exit_reason="MAX_TURNS", turns=turns,
            status=str(self.task_status() or ""), outcomes=outcomes)

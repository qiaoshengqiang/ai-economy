"""
Front 层 —— Human 的唯一入口
==============================

    Human → Front Agent → Brief → Team Lead
    Team Lead → Task → Work
    Work Result → Front Agent → Human

🔒 Front Agent：

    · 永不创建 Task；
    · 可以对话、使用自己的私有空间 / 草稿准备文件；
    · 通过 handoff 的 `brief.files` 把文件交给 Work Agent；
    · 不验收 Task；
    · 人类没有“接受”动作；后续返工由 Front 识别意图并路由。
"""

from front.contracts import (
    FrontBrief, FrontDecision, FrontIntent, FrontResult,
)
from front.parser import parse_front_actions, parse_front_decision
from front.runtime import (
    FRONT_SYSTEM_PROMPT, FrontAgentRuntime, FrontUnavailableError,
)

__all__ = [
    "FRONT_SYSTEM_PROMPT",
    "FrontBrief", "FrontDecision", "FrontIntent", "FrontResult",
    "FrontAgentRuntime", "FrontUnavailableError",
    "parse_front_actions", "parse_front_decision",
]

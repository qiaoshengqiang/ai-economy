"""
Front 层 —— Human 的唯一入口
==============================

    Human → Front Agent → Brief → Team Lead
    Team Lead → Task → Work
    Work Result → Front Agent → Human

🔒 Front Agent：

    · 永不创建 Task；
    · 不生产、不验收；
    · 只做 direct_reply / handoff / rework / follow_up / clarify；
    · 人类没有“接受”动作；后续返工由 Front 识别意图并路由。
"""

from front.contracts import (
    FrontBrief, FrontDecision, FrontIntent, FrontResult,
)
from front.parser import parse_front_decision
from front.runtime import FrontAgentRuntime

__all__ = [
    "FrontBrief", "FrontDecision", "FrontIntent", "FrontResult",
    "FrontAgentRuntime", "parse_front_decision",
]

"""
Cognitive Context V1 —— 把持久状态选择性地变成「本轮认知要看到的东西」。

    from cognition import CognitiveContextBuilder

🔒 边界：
    · Context Builder 只做 **Relevance Selection**，不做 Decision
    · Self Model 只作为**认知材料**进入，不伪装成 System Prompt Rules
    · Provider 不拥有 Agent 的 Self Model

🔴 Economy Awareness V1：
    · `EconomicContext` = Agent 当前**可以观察到的经济事实**（来自 Ledger）
    · 它**不是** Economy Policy，也**不是**经济人格 —— 只陈述事实，不给指令
    · `cognition/` **不 import** `core/ledger.py` / `core/economy.py`：
      经济事实通过组合根注入的**只读回调**取得
"""

from cognition.context import (
    BANNED_INTERNAL_TOKENS, BANNED_RULE_WORDS,
    CognitiveContext, CognitiveContextBuilder, ContextSource,
)
from cognition.economic import (
    CURRENCY_NOTE, EconomicContext, EconomicContextBuilder,
)

__all__ = ["CognitiveContext", "CognitiveContextBuilder", "ContextSource",
           "BANNED_RULE_WORDS", "BANNED_INTERNAL_TOKENS",
           "EconomicContext", "EconomicContextBuilder", "CURRENCY_NOTE"]

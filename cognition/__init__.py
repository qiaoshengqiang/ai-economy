"""
Cognitive Context V1 —— 把持久状态选择性地变成「本轮认知要看到的东西」。

    from cognition import CognitiveContextBuilder

🔒 边界：
    · Context Builder 只做 **Relevance Selection**，不做 Decision
    · Self Model 只作为**认知材料**进入，不伪装成 System Prompt Rules
    · Provider 不拥有 Agent 的 Self Model
"""

from cognition.context import (
    BANNED_INTERNAL_TOKENS, BANNED_RULE_WORDS,
    CognitiveContext, CognitiveContextBuilder, ContextSource,
)

__all__ = ["CognitiveContext", "CognitiveContextBuilder", "ContextSource",
           "BANNED_RULE_WORDS", "BANNED_INTERNAL_TOKENS"]

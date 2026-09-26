"""
LLM 层 —— Agent 可以调用的「外部智力资源」
============================================================

这个包提供这些东西：

    base.py     契约：LLMProvider（所有厂商都要满足的接口）
                      LLMResponse（统一的返回结构）
    router.py   路由：按名字找到 Provider（注册表，不用 if/elif）
    mock.py     兜底：不需要 Key 的假模型，让整条链路能跑起来
    recorder.py 记账：每次调用留下 token 用量

【这个包的定位很重要】

    Agent 是经济主体，LLM 是它购买的【生产资料】。

    所以 llm/ 这个包对外部世界的角色，就像「电力公司」对工厂：
        工厂（Agent）需要电（智力），就按统一接口去买，
        不需要知道电是煤烧的还是水冲的（DeepSeek / 豆包 / Grok）。

    正因为这样，llm/ 里【没有一行代码提到具体是哪家厂商】——
    唯一的例外是 router.py 里那三行「已登记、未实现」的占位提示，
    而那三行存在的目的，恰恰是告诉你去哪里补上实现。

【怎么加一个新厂商】

    1. 新建 llm/<厂商>.py
    2. 写一个继承 LLMProvider 的类，实现 generate()
    3. 在 router.py 的 build_default_registry() 里注册一行

    完事。Agent、Brain、Economy、Task 全部不需要改。
    这是本阶段最重要的验收标准。
"""

from llm.base import (
    LLMError,
    LLMProvider,
    LLMRequestError,
    LLMResponse,
    ProviderDisabledError,
    ProviderNotFoundError,
    ProviderNotImplementedError,
    estimate_messages_tokens,
    estimate_tokens,
    normalize_messages,
)
from llm.mock import MockProvider
from llm.recorder import LLMCallRecorder
from llm.router import LLMRouter, ProviderRegistry, build_default_registry

__all__ = [
    # base —— 契约
    "LLMProvider",
    "LLMResponse",
    "LLMError",
    "LLMRequestError",
    "ProviderNotFoundError",
    "ProviderNotImplementedError",
    "ProviderDisabledError",
    "normalize_messages",
    "estimate_tokens",
    "estimate_messages_tokens",
    # providers
    "MockProvider",
    # routing
    "LLMRouter",
    "ProviderRegistry",
    "build_default_registry",
    # 记账
    "LLMCallRecorder",
]

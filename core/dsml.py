"""工具调用格式 / DSML fallback 的运行配置（叶子模块）。

放在 `core/` 而不是 `llm/`：
    `actions/` 不许 import `llm/`（叶子层守卫），但它需要知道
    “现在是 shadow 还是 execute”；配置层也需要校验
    `tool_call_format` 的合法值。这些是运行配置，不是 LLM 实现。
"""

from __future__ import annotations

import os

DSML_MODE_ENV = "AIEOS_DSML_MODE"
DEFAULT_DSML_MODE = "execute"

#: 模型「工具调用格式」的完整词汇表。
#:
#:     standard  OpenAI / 绝大多数 OpenAI-compatible 端点的 `tool_calls`
#:     dsml      DeepSeek V4 系列的 DSML 标记（可能被代理原样泄漏为文本）
#:
#: 🔒 这是**运行配置**，不是 LLM 实现 —— 所以放在 `core/`，
#:    由 `core/config_resolver.py` 校验、`llm/` 按模型执行。
TOOL_CALL_FORMATS = ("standard", "dsml")
DEFAULT_TOOL_CALL_FORMAT = "standard"


def dsml_mode() -> str:
    """`execute`（默认，校验后执行）或 `shadow`（只解析不执行）。"""
    value = os.environ.get(DSML_MODE_ENV, DEFAULT_DSML_MODE)
    value = str(value or "").strip().lower()
    return "execute" if value == "execute" else "shadow"

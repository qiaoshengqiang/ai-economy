"""DSML fallback 的运行开关（叶子模块）。

放在 `core/` 而不是 `llm/`：
    `actions/` 不许 import `llm/`（叶子层守卫），但它需要知道
    “现在是 shadow 还是 execute”。这个开关是运行配置，不是 LLM 实现。
"""

from __future__ import annotations

import os

DSML_MODE_ENV = "AIEOS_DSML_MODE"
DEFAULT_DSML_MODE = "shadow"


def dsml_mode() -> str:
    """`shadow`（默认，只解析不执行）或 `execute`。"""
    value = os.environ.get(DSML_MODE_ENV, DEFAULT_DSML_MODE)
    value = str(value or "").strip().lower()
    return "execute" if value == "execute" else "shadow"

"""LLM 会话 ID —— 给 provider 的缓存 / 路由提示。

OpenCode Go 要求每次请求带一个**稳定的、按对话划分**的
`x-opencode-session`：同一个对话复用同一个值，不同对话不能共用。

🔒 这里只做**纯函数派生**：
   · 不用随机数，进程重启后仍然稳定；
   · 不落库，scope 本身来自已经持久化的 conversation_id / task_id /
     alarm_id；
   · 不在 ID 里暴露原始字符串，统一 hash。
"""

from __future__ import annotations

import hashlib

#: 与 OpenCode 的 `ses_...` 风格保持一致。
SESSION_PREFIX = "ses_aieos_"
SESSION_HASH_CHARS = 24


def build_session_id(scope: str, *, provider: str = "", model: str = "") -> str:
    """把逻辑 scope + provider/model 变成一个稳定的 session ID。"""
    text = str(scope or "").strip()
    if not text:
        return ""
    fingerprint = "|".join([
        text,
        f"provider={str(provider or '').strip().lower()}",
        f"model={str(model or '').strip()}",
    ])
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return SESSION_PREFIX + digest[:SESSION_HASH_CHARS]


def front_scope(conversation_id: str, *, agent_id: str = "") -> str:
    conversation = str(conversation_id or "local").strip() or "local"
    agent = str(agent_id or "").strip()
    suffix = f"|agent={agent}" if agent else ""
    return f"front|conv={conversation}{suffix}"


def task_scope(task_id: str, *, agent_id: str = "") -> str:
    task = str(task_id or "").strip()
    if not task:
        return ""
    agent = str(agent_id or "").strip()
    suffix = f"|agent={agent}" if agent else ""
    return f"task|{task}{suffix}"


def alarm_scope(alarm_id: str, *, agent_id: str = "") -> str:
    alarm = str(alarm_id or "").strip()
    if not alarm:
        return ""
    agent = str(agent_id or "").strip()
    suffix = f"|agent={agent}" if agent else ""
    return f"alarm|{alarm}{suffix}"


__all__ = [
    "SESSION_PREFIX", "SESSION_HASH_CHARS",
    "build_session_id", "front_scope", "task_scope", "alarm_scope",
]

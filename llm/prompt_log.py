"""
可选的 Prompt 日志
===================

用途：在 UI 里临时打开开关，记录每次发给模型的**输入**，用于观察
Front / Solo / Pi 实际看到的 prompt。

🔒 默认关闭。
🔒 只记录输入，不记录响应；响应已经在 rounds / task 目录里。
🔒 不记录 Authorization / API key。
🔒 写日志失败绝不能影响模型调用。

文件：

    runs/ui/prompts/prompts-YYYY-MM-DD.jsonl

每行一个 JSON 记录：

    {at, agent_id, job_id, task_id, phase, provider, model,
     purpose, system, messages, message_count}
"""

from __future__ import annotations

import contextvars
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_ENABLED = False
_ROOT: Optional[Path] = None
_LOCK = threading.Lock()
_JOB_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aieos_prompt_job_id", default="")


def configure(enabled: bool = False, root: Optional[Path] = None) -> None:
    """由组合根 / Controller 调用。关闭时不产生任何文件。"""
    global _ENABLED, _ROOT
    _ENABLED = bool(enabled)
    _ROOT = Path(root) if root is not None else None


def enabled() -> bool:
    return bool(_ENABLED and _ROOT is not None)


def bind_job(job_id: str) -> None:
    _JOB_ID.set(str(job_id or ""))


def clear_job() -> None:
    _JOB_ID.set("")


def current_job() -> str:
    return str(_JOB_ID.get() or "")


def _normalize_messages(messages: Any) -> List[Dict[str, Any]]:
    if messages is None:
        return []
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if isinstance(messages, dict):
        items = [messages]
    elif isinstance(messages, (list, tuple)):
        items = list(messages)
    else:
        return [{"role": "user", "content": str(messages)}]

    out: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            # 保留完整消息：assistant.tool_calls / tool.tool_call_id /
            # name / content blocks 都是「它到底做了什么」的证据。
            out.append(dict(item))
            continue
        data: Dict[str, Any] = {}
        for attr in ("role", "content", "tool_calls", "tool_call_id",
                     "name", "sections"):
            if hasattr(item, attr):
                data[attr] = getattr(item, attr)
        if not data:
            data = {"role": "user", "content": str(item)}
        out.append(data)
    return out


def record(
    agent_id: str,
    *,
    phase: str = "",
    system: str = "",
    messages: Any = None,
    task_id: str = "",
    job_id: str = "",
    provider: str = "",
    model: str = "",
    purpose: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """记录一次模型输入。关闭时直接返回。"""
    if not enabled():
        return
    normalized = _normalize_messages(messages)
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "at": now.isoformat(),
        "agent_id": str(agent_id or ""),
        "job_id": str(job_id or current_job()),
        "task_id": str(task_id or ""),
        "phase": str(phase or ""),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "purpose": str(purpose or ""),
        "system": str(system or ""),
        "messages": normalized,
        "message_count": len(normalized),
    }
    if extra:
        payload["extra"] = extra
    path = _ROOT / f"prompts-{now.strftime('%Y-%m-%d')}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        # 🔒 观测失败不能影响生产调用。
        return


__all__ = [
    "configure", "enabled", "bind_job", "clear_job", "current_job",
    "record",
]

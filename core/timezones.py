"""时区设置 —— 只影响「Agent 看到的时间」，不改变存储格式。

🔒 所有持久化时间仍然是 UTC ISO8601：
   · `agent_alarms.next_fire_at` / `expires_at`；
   · Activity Record 的 `started_at` / `finished_at`；
   · Task / Ledger / 消息时间。

时区只用于：
   · Front prompt 里的「当前时间」；
   · 人类在 UI 上看到的本地时间（前端自己按浏览器时区渲染）。

因此「加一个时区」不会让历史数据产生歧义。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 默认时区。V1 只提供这一个默认值，避免依赖宿主机时区。
DEFAULT_TIMEZONE = "Asia/Shanghai"

#: 设置页展示顺序；值必须是 IANA 时区名。
COMMON_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Asia/Shanghai", "北京 · UTC+08:00"),
    ("UTC", "UTC"),
    ("Asia/Tokyo", "东京 · UTC+09:00"),
    ("Asia/Singapore", "新加坡 · UTC+08:00"),
    ("Europe/London", "伦敦"),
    ("Europe/Berlin", "柏林"),
    ("America/New_York", "纽约"),
    ("America/Los_Angeles", "洛杉矶"),
)


def normalize_timezone(name: Any) -> str:
    """空值 → 默认时区；非法值 → ValueError。"""
    value = str(name or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"未知时区：{value!r}") from None
    return value


def now_in(name: str) -> datetime:
    return datetime.now(timezone.utc).astimezone(
        ZoneInfo(normalize_timezone(name)))


def _offset_text(value: datetime) -> str:
    raw = value.strftime("%z")
    if len(raw) == 5:
        return f"{raw[:3]}:{raw[3:]}"
    return raw or "+00:00"


def render_now(name: str) -> str:
    """给 prompt 用的一行当前时间：本地时区 + UTC。"""
    zone_name = normalize_timezone(name)
    local = now_in(zone_name)
    utc = local.astimezone(timezone.utc)
    return (
        f"{local.strftime('%Y-%m-%d %H:%M')} {_offset_text(local)}"
        f"（{zone_name}）"
        f" / UTC {utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )


__all__ = [
    "DEFAULT_TIMEZONE",
    "COMMON_TIMEZONES",
    "normalize_timezone",
    "now_in",
    "render_now",
]

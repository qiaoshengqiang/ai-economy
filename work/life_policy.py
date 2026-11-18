"""Agent Life 层策略 —— 从 `agents.<id>.life` 读取。

🔒 这是 **Runtime 策略**，不是 Task / Work 配置：

    life.enabled                 是否允许被消息 / 闹钟唤醒
    life.wake_on_message         消息到达是否唤醒
    life.wake_on_alarm           闹钟到期是否唤醒
    life.accept_delegated_alarms 是否接受别的 Agent（Front）设置的闹钟
    life.max_turns_per_hour      每小时最多 Life Turn 次数
    life.min_interval_seconds    两次 Life Turn 的最小间隔
    life.max_tokens_per_turn     单次 Life Turn 的输出上限
    life.max_cost_per_turn       单次 Life Turn 的成本告警线
    life.max_conversation_turns  单个 conversation 最多自动回复轮数
    life.allowed_actions         Life Turn 允许的动作

默认 `enabled=False`：升级后不会让所有 Agent 突然开始烧 token。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

#: Life Turn 允许的动作集合。
LIFE_ACTIONS: Tuple[str, ...] = (
    # 原有：消息 / 闹钟
    "message_send",
    "alarm_create",
    "alarm_list",
    "alarm_cancel",
    # 任务只读
    "task_list",
    "task_inspect",
    "task_content_list",
    "task_content_read",
    # 外部信息（只读）
    "web_search",
    "web_fetch",
    # 私有空间
    "private_list",
    "private_read",
    "private_write",
    "private_delete",
    # 私有记忆
    "short_term_memory_read",
    "short_term_memory_add",
    "short_term_memory_update",
    "short_term_memory_reinforce",
    "long_term_memory_search",
    "long_term_memory_read",
    "long_term_memory_consolidate",
    "no_op",
)

DEFAULT_ALLOWED_ACTIONS: Tuple[str, ...] = LIFE_ACTIONS


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


@dataclass(frozen=True)
class LifePolicy:
    enabled: bool = False
    wake_on_message: bool = False
    wake_on_alarm: bool = True
    accept_delegated_alarms: bool = False
    max_turns_per_hour: int = 20
    min_interval_seconds: int = 5
    max_tokens_per_turn: int = 1024
    max_cost_per_turn: float = 1.0
    max_conversation_turns: int = 6
    reply_max_chars: int = 4000
    allowed_actions: Tuple[str, ...] = DEFAULT_ALLOWED_ACTIONS

    @classmethod
    def from_raw(cls, raw: Any) -> "LifePolicy":
        """从 `agents.<id>.life` 读取；缺省 / 非对象 → 全默认（禁用）。

        非法字段不抛错：Life 是可选能力，配置写错时最安全的默认是
        **不启用**，而不是让整个 Runtime 起不来。但动作白名单会过滤，
        未知动作不会被静默执行。
        """
        if not isinstance(raw, Mapping):
            return cls()
        actions_raw = raw.get("allowed_actions")
        if actions_raw is None:
            actions = DEFAULT_ALLOWED_ACTIONS
        elif isinstance(actions_raw, str):
            actions = tuple(
                item.strip() for item in actions_raw.split(",")
                if item.strip())
        elif isinstance(actions_raw, (list, tuple)):
            actions = tuple(str(item).strip() for item in actions_raw
                            if str(item).strip())
        else:
            actions = DEFAULT_ALLOWED_ACTIONS
        # 未知动作不执行；no_op 不写进白名单，但 `allows()` 始终允许它。
        allowed = tuple(item for item in actions if item in LIFE_ACTIONS)
        # 去重，保留顺序。
        seen = set()
        allowed = tuple(
            item for item in allowed
            if not (item in seen or seen.add(item)))
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            wake_on_message=_as_bool(raw.get("wake_on_message"), False),
            wake_on_alarm=_as_bool(raw.get("wake_on_alarm"), True),
            accept_delegated_alarms=_as_bool(
                raw.get("accept_delegated_alarms"), False),
            max_turns_per_hour=_as_int(
                raw.get("max_turns_per_hour"), 20, minimum=1),
            min_interval_seconds=_as_int(
                raw.get("min_interval_seconds"), 5, minimum=1),
            max_tokens_per_turn=_as_int(
                raw.get("max_tokens_per_turn"), 1024, minimum=64),
            max_cost_per_turn=_as_float(
                raw.get("max_cost_per_turn"), 1.0, minimum=0.0),
            max_conversation_turns=_as_int(
                raw.get("max_conversation_turns"), 6, minimum=1),
            reply_max_chars=_as_int(
                raw.get("reply_max_chars"), 4000, minimum=100),
            allowed_actions=allowed,
        )

    def allows(self, action: str) -> bool:
        value = str(action or "")
        return value == "no_op" or value in self.allowed_actions

    def to_dict(self) -> dict:
        """给 UI / 设置页的归一化结果。"""
        return {
            "enabled": self.enabled,
            "wake_on_message": self.wake_on_message,
            "wake_on_alarm": self.wake_on_alarm,
            "accept_delegated_alarms": self.accept_delegated_alarms,
            "max_turns_per_hour": self.max_turns_per_hour,
            "min_interval_seconds": self.min_interval_seconds,
            "max_tokens_per_turn": self.max_tokens_per_turn,
            "max_cost_per_turn": self.max_cost_per_turn,
            "max_conversation_turns": self.max_conversation_turns,
            "reply_max_chars": self.reply_max_chars,
            "allowed_actions": list(self.allowed_actions),
        }


__all__ = ["LIFE_ACTIONS", "DEFAULT_ALLOWED_ACTIONS", "LifePolicy"]

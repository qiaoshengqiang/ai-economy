"""
Agent Identity —— 经济主体的身份
============================================================

【设计原则】Agent != LLM
    身份（Identity）和智力来源（Brain/LLM）是两件完全独立的事。
    Agent A 今天用 DeepSeek，明天换成 Grok，它的身份不变、钱包不变、
    账本不变、合同不变。这就是为什么身份要单独成为一个对象。

v0.1 只保留最小字段：agent_id / name / created_at
未来会扩展：public_key、reputation、home_world、owner、status
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now() -> datetime:
    """统一的时间来源：带时区的 UTC 时间。"""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """ISO 8601 字符串，用于写数据库和日志。"""
    return utc_now().isoformat()


@dataclass
class AgentIdentity:
    """
    一个 Agent 的身份。

    注意 agent_id 和 name 的区别：
        agent_id: 机器用的唯一标识，永远不变，跨机器也有效（agent_a）
        name:     人看显示名，以后可以改（"Agent A" -> "老A的编程铺子"）
    """

    agent_id: str
    name: str
    created_at: str = field(default_factory=utc_now_iso)

    # 未来扩展预留。v0.1 不使用，但先留好位置，
    # 这样以后加字段不会破坏已有的构造函数调用。
    public_key: Optional[str] = None
    owner: Optional[str] = None
    home_world: str = "local"

    # ------------------------------------------------------------
    # 构造与校验
    # ------------------------------------------------------------
    def __post_init__(self) -> None:
        self.agent_id = self.agent_id.strip()
        self.name = self.name.strip()

        if not self.agent_id:
            raise ValueError("agent_id 不能为空")
        if not self.name:
            raise ValueError("name 不能为空")

    @classmethod
    def create(
        cls,
        agent_id: str,
        name: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> "AgentIdentity":
        """
        创建一个全新身份。

        如果没给名字，就用 agent_id 当名字。
        """
        return cls(
            agent_id=agent_id,
            name=name or agent_id,
            owner=owner,
        )

    # ------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "created_at": self.created_at,
            "public_key": self.public_key,
            "owner": self.owner,
            "home_world": self.home_world,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentIdentity":
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            created_at=data.get("created_at", utc_now_iso()),
            public_key=data.get("public_key"),
            owner=data.get("owner"),
            home_world=data.get("home_world", "local"),
        )

    # ------------------------------------------------------------
    # 显示
    # ------------------------------------------------------------
    def __str__(self) -> str:
        return f"{self.name} ({self.agent_id})"

    def __repr__(self) -> str:
        return f"AgentIdentity(agent_id={self.agent_id!r}, name={self.name!r})"


def new_message_id(prefix: str = "msg") -> str:
    """
    生成一个短一点的唯一 ID，用于消息 / 任务 / 合同。

    格式：msg_3f9a2b1c
    用 uuid4 的十六进制前 8 位，本地原型足够，且可读。
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}"

"""
AgentRegistry —— Agent 目录
============================================================

【它的职责边界：只存数据，不存活对象】

    这一点是刻意的，也是本步骤的关键架构决定之一。

    ❌ 不这样：
        registry.register(agent_object)
        registry.find("agent_b").generate(...)      # 顺手就把 Agent 用了

    ✅ 而是这样：
        registry.register(AgentInfo(agent_id=..., address=..., capabilities=...))
        # 里面全是可序列化的数据，没有 Agent 实例

【为什么这个区分对「未来跨机器」是决定性的】

    想想未来：

        本机 Registry          记着 agent_a、agent_b（有本地绑定）
        远端 Registry / DHT    记着 agent_c、agent_d（只有地址，没有对象）

    如果 Registry 里存的是 Python 对象，那么「远端的 Agent」
    根本没法放进去 —— 你没有对方的对象。
    整个注册表就会退化成「一个本地字典」，
    以后再想扩展成网络发现，得推倒重写。

    只存数据的话，本地 Agent 和远端 Agent 在 Registry 里
    长得一模一样，区别只在「有没有本地信箱」——
    而那个信息属于 CommunicationManager，不属于 Registry。

【Capabilities 现在就放进数据结构】

    本步骤不实现「按能力发现 Agent」。
    但 capability 字段现在就要有，因为：
        · 加字段是破坏性变更（老的注册记录没有它）
        · 这是个纯数据结构，现在放进去零成本
        · Step 4 的「找会写 Python 的 Agent」直接就能用
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from communication.address import Address
from communication.errors import AgentNotFoundError, DuplicateAgentError


# ============================================================
# 在线状态
# ============================================================
class PresenceStatus:
    """
    Agent 在通信意义上的状态。

    【注意：这个名字和 agents.base_agent.AgentStatus 不是一回事】

        AgentStatus（经济状态）   ACTIVE / INSOLVENT / SUSPENDED
                                 我没钱了 / 我破产了

        PresenceStatus（通信状态） ONLINE / OFFLINE / UNKNOWN
                                 我现在能不能收到消息

    一个 Agent 可以「经济上破产但通信在线」——
    它还能收消息、还能谈判、还能接活翻身。
    把这两件事混成一个状态，就会写出
    「破产的 Agent 连消息都发不出去」这种荒谬逻辑。

    这是「经济状态」和「机器/网络状态」必须分离的一个具体体现。
    """

    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"

    ALL = {ONLINE, OFFLINE, UNKNOWN}


# ============================================================
# Agent 目录条目（纯数据）
# ============================================================
@dataclass
class AgentInfo:
    """
    注册表里关于一个 Agent 的全部信息。

    这里【没有】Agent 对象、没有 Brain、没有 Wallet。
    只有「怎么找到它」和「它能干什么」。
    """

    agent_id: str
    address: Address

    display_name: str = ""

    # 能力标签。本步骤只是存着，Step 4 的「按能力找人」会用到。
    capabilities: Tuple[str, ...] = ()

    # 通信状态（不是经济状态）
    presence: str = PresenceStatus.UNKNOWN

    # 它在哪个节点上。本地是 "local"；未来可能是 "node-2" / peer id。
    node: str = "local"

    # 是不是本进程内的 Agent。
    # 这个字段将来会决定「能不能直接投递」还是「要经过网络」。
    is_local: bool = True

    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.agent_id = str(self.agent_id or "").strip()
        if not self.agent_id:
            raise ValueError("AgentInfo.agent_id 不能为空")

        if not isinstance(self.address, Address):
            raise ValueError(
                f"AgentInfo.address 必须是 Address，"
                f"收到 {type(self.address).__name__}"
            )

        # 地址指向的必须是同一个身份，否则就是数据不一致
        if self.address.agent_id != self.agent_id:
            raise ValueError(
                f"地址和身份对不上：agent_id={self.agent_id!r}，"
                f"但 address.agent_id={self.address.agent_id!r}。\n"
                f"身份和地址可以不同（一个是身份、一个是位置），"
                f"但地址必须指向正确的身份。"
            )

        if isinstance(self.capabilities, str):
            # 允许写 "python"，自动变成 ("python",)
            self.capabilities = (self.capabilities,)
        else:
            self.capabilities = tuple(
                str(c).strip() for c in (self.capabilities or ()) if str(c).strip()
            )

        self.display_name = str(self.display_name or self.agent_id).strip()
        self.presence = str(self.presence or PresenceStatus.UNKNOWN).upper()
        if self.presence not in PresenceStatus.ALL:
            self.presence = PresenceStatus.UNKNOWN

        self.node = str(self.node or "local").strip()
        self.metadata = dict(self.metadata or {})

    # ------------------------------------------------------------
    def has_capability(self, capability: str) -> bool:
        return str(capability).strip() in self.capabilities

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "address": self.address.to_dict(),
            "display_name": self.display_name,
            "capabilities": list(self.capabilities),
            "presence": self.presence,
            "node": self.node,
            "is_local": self.is_local,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentInfo":
        return cls(
            agent_id=data.get("agent_id", ""),
            address=Address.from_dict(data.get("address", {})),
            display_name=data.get("display_name", ""),
            capabilities=tuple(data.get("capabilities") or ()),
            presence=data.get("presence", PresenceStatus.UNKNOWN),
            node=data.get("node", "local"),
            is_local=bool(data.get("is_local", True)),
            metadata=dict(data.get("metadata") or {}),
        )

    def __str__(self) -> str:
        caps = ",".join(self.capabilities) if self.capabilities else "无标签"
        return (
            f"{self.agent_id} @ {self.address} "
            f"[{self.presence}] ({caps})"
        )

    def __repr__(self) -> str:
        return (
            f"AgentInfo(agent_id={self.agent_id!r}, address={str(self.address)!r}, "
            f"presence={self.presence!r}, local={self.is_local})"
        )


# ============================================================
# 注册表
# ============================================================
class AgentRegistry:
    """
    Agent 目录。

    用法：
        registry = AgentRegistry()
        registry.register(info)
        registry.lookup("agent_b")     # 找不到抛 AgentNotFoundError
        registry.find("agent_b")       # 找不到返回 None

    【为什么 lookup 和 find 两个都要有】
        lookup：调用方认为「它一定在」→ 不在就是异常
        find  ：调用方只是想「看看在不在」→ 不在很正常

        逼所有调用方都用其中一个，会导致到处写
        `if x is None: raise ...` 或者 `try: ... except: pass`。
    """

    def __init__(self) -> None:
        self._agents: Dict[str, AgentInfo] = {}

    # ------------------------------------------------------------
    # 写
    # ------------------------------------------------------------
    def register(self, info: AgentInfo, replace: bool = False) -> AgentInfo:
        """
        登记一个 Agent。

        replace=False（默认）时，重复的 agent_id 会抛 DuplicateAgentError。
        为什么默认不覆盖？
            因为「同一个 ID 被登记两次」通常是 bug（比如重复初始化），
            静默覆盖会把这种 bug 藏起来。
        """
        if not isinstance(info, AgentInfo):
            raise ValueError(
                f"Registry 只接受 AgentInfo，收到 {type(info).__name__}。\n"
                f"注意：Registry 不存 Agent 对象 —— 只存数据。"
            )

        existing = self._agents.get(info.agent_id)
        if existing is not None and not replace:
            raise DuplicateAgentError(
                f"{info.agent_id} 已经登记过了。\n"
                f"  已有：{existing}\n"
                f"  新的：{info}\n"
                f"（如果确实要覆盖，用 register(info, replace=True)）"
            )

        self._agents[info.agent_id] = info
        return info

    def unregister(self, agent_id: str) -> bool:
        """注销。返回是否真的删掉了。"""
        return self._agents.pop(str(agent_id).strip(), None) is not None

    def update_presence(self, agent_id: str, presence: str) -> AgentInfo:
        """改通信状态。未来做心跳时用。"""
        info = self.lookup(agent_id)
        info.presence = str(presence).upper()
        return info

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------
    def lookup(self, agent_id: str) -> AgentInfo:
        """找不到就抛 AgentNotFoundError（而不是返回 None）。"""
        info = self.find(agent_id)
        if info is None:
            raise AgentNotFoundError(
                f"注册表里没有 {agent_id!r}。\n"
                f"已登记的 Agent：{self.ids() or '（无）'}"
            )
        return info

    def find(self, agent_id: str) -> Optional[AgentInfo]:
        """找不到返回 None。"""
        return self._agents.get(str(agent_id).strip())

    def exists(self, agent_id: str) -> bool:
        return str(agent_id).strip() in self._agents

    def all(self) -> List[AgentInfo]:
        return [self._agents[key] for key in sorted(self._agents)]

    def ids(self) -> List[str]:
        return sorted(self._agents)

    def local_agents(self) -> List[AgentInfo]:
        """
        本进程内的 Agent。

        【为什么现在就要有这个方法】
            未来的查找顺序会是：
                1. 本地有绑定吗？→ 直接投递
                2. 注册表里有远端地址吗？→ 走网络
                3. 都没有 → 让 Transport 去发现
            第 1 步就是靠这个判断的。
        """
        return [info for info in self.all() if info.is_local]

    def remote_agents(self) -> List[AgentInfo]:
        return [info for info in self.all() if not info.is_local]

    def by_capability(self, capability: str) -> List[AgentInfo]:
        """
        按能力标签筛选。

        【注意：这不是「发现」】
            它只是在注册表这份本地数据里做了一次过滤。
            真正的发现（去网络里找谁有这个能力）是后续版本的事。

            但把数据结构先摆好，那时的改动就只在「查询范围」
            而不是「加一个字段」。
        """
        return [info for info in self.all() if info.has_capability(capability)]

    @property
    def size(self) -> int:
        return len(self._agents)

    def stats(self) -> Dict[str, Any]:
        return {
            "total": self.size,
            "local": len(self.local_agents()),
            "remote": len(self.remote_agents()),
            "agents": self.ids(),
        }

    def clear(self) -> None:
        self._agents.clear()

    # ------------------------------------------------------------
    def __contains__(self, agent_id: object) -> bool:
        return str(agent_id) in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self):
        return iter(self.all())

    def __repr__(self) -> str:
        return f"AgentRegistry(agents={self.ids()})"

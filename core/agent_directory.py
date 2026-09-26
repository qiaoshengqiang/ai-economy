"""
Agent Directory —— 能力发现
============================================================

【它回答什么问题】

    只有一个问题：

        「谁**可能**具备这些能力？」

【它不回答什么问题】（这些是刻意的边界）

    ❌ 谁最合适            → 那是 Agent 自己的判断（未来才是 Coordinator）
    ❌ 谁最便宜            → 经济决策，不属于发现
    ❌ 谁成功率最高        → 信誉，属于后续步骤
    ❌ 该把任务派给谁      → **这是 Task Initiator Agent 的决定**
    ❌ 谁现在有空          → 调度，属于后续步骤

    换句话说：

        Directory 提供候选，不提供决策。

    这条边界非常重要 —— 一旦 Directory 开始「选人」，
    它就从「通讯录」变成了「中央调度器」，
    而那会把未来的去中心化设计提前锁死。

【与 communication.AgentRegistry 的区别】

    项目里已经有一个注册表：communication/registry.py 的 AgentRegistry。
    它回答的是完全不同的问题：

        AgentRegistry     「我怎么**送达**消息给它？」（地址、在线状态）
        AgentDirectory    「谁**会做**这种工作？」（能力）

    两者都存 agent_id，但一个服务于投递，一个服务于委派决策。

    【为什么不让 AgentDirectory 依赖 AgentRegistry】
        如果 Directory 去 import communication，就会形成
        core → communication → core 的循环，而且会让
        「能力发现」被绑死在「当前用的是 LocalTransport」上。

        所以 Directory 只存一个**位置提示字符串**（address），
        真正送信的时候由 Communication 自己去解析地址。

【P2P 扩展点】

    现在是：
        Agent A → LocalAgentDirectory → 内存里的 B

    未来是：
        Agent A → NetworkAgentDirectory → 另一台机器上的 C
        Agent A → P2P Discovery          → 网络里的 D

    A 的调用方式（find_capable）不需要变。

    【怎么保证这一点】
        · AgentDirectory 是抽象类，LocalAgentDirectory 只是其中一种实现
        · find_capable 的参数和返回类型都是纯数据
        · 有测试用一个「假想的远端 Directory」验证 A 的代码不用改
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.capability import CapabilityProfile


# ============================================================
# 异常
# ============================================================
class DirectoryError(Exception):
    """目录相关错误的基类。"""


class AgentNotInDirectoryError(DirectoryError):
    """目录里没有这个 Agent。"""


class DuplicateDirectoryEntryError(DirectoryError):
    """同一个 agent_id 被重复登记（且不允许替换）。"""


# ============================================================
# 目录条目
# ============================================================
@dataclass(frozen=True)
class DirectoryEntry:
    """
    目录里的一条记录。

    【注意它的字段范围】
        只有「身份 + 能力 + 位置提示 + 备注」。
        没有余额、没有信誉分、没有任务列表、没有权限。

        为什么刻意不放这些？
            因为它们属于不同的维度：
                余额    → Economy
                信誉分  → 未来的 Reputation（现在还不该存在）
                任务    → Task
                权限    → Workspace Gateway（还不该存在）

            一个「什么都存」的条目，最后会变成什么都往里塞。

    【address 为什么是字符串而不是 Address 对象】
        因为 Address 属于 communication 层。
        这里存一个位置**提示**（字符串），真正的地址解析由
        Communication 自己做（它有注册表 + discover 回退）。

        这样 core/ 不必依赖 communication/。
    """

    agent_id: str
    capabilities: CapabilityProfile = field(default_factory=CapabilityProfile.empty)

    #: 位置提示，例如 "local://agent_b" 或未来的 "p2p://12D3KooW"。
    #: 【它不是权限，也不保证可达】—— 只是给未来网络发现留的字段。
    address: Optional[str] = None

    #: 是否可用。纯数据 —— find_capable **不**按它过滤（见下）。
    available: bool = True

    #: 自由备注（比如节点名、版本）。不参与匹配。
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise DirectoryError("agent_id 必须是非空字符串")
        object.__setattr__(self, "agent_id", self.agent_id.strip())

        # 允许直接传 list / set，自动转成 CapabilityProfile
        if not isinstance(self.capabilities, CapabilityProfile):
            object.__setattr__(
                self, "capabilities", CapabilityProfile.of(self.capabilities)
            )

        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    # ------------------------------------------------------------
    def supports(self, capability: Any) -> bool:
        return self.capabilities.supports(capability)

    def covers(self, required: Optional[Iterable[Any]]) -> bool:
        return self.capabilities.covers(required)

    def missing(self, required: Optional[Iterable[Any]]):
        return self.capabilities.missing(required)

    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": self.capabilities.to_list(),
            "address": self.address,
            "available": self.available,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DirectoryEntry":
        return cls(
            agent_id=data["agent_id"],
            capabilities=CapabilityProfile.of(data.get("capabilities")),
            address=data.get("address"),
            available=bool(data.get("available", True)),
            metadata=data.get("metadata") or {},
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": self.capabilities.names,
            "address": self.address,
            "available": self.available,
        }

    def __repr__(self) -> str:
        return (
            f"DirectoryEntry(agent_id={self.agent_id!r}, "
            f"capabilities={self.capabilities.names!r})"
        )


# ============================================================
# 抽象接口
# ============================================================
class AgentDirectory(ABC):
    """
    Agent 目录的抽象接口。

    未来可以有多种实现：
        LocalAgentDirectory      进程内的字典（当前）
        NetworkAgentDirectory    向别的节点查询（未来）
        P2PDiscoveryDirectory    在网络里广播发现（未来）

    调用方（Agent A）只依赖这个接口，不依赖具体实现。

    【契约】
        · find_capable 只做「能力覆盖」判断，**不做排序、不做挑选**
        · find_capable 不因为 Agent 不可用就把它过滤掉 ——
          可用性是调用方自己判断的事（保持「只提供信息」的定位）
    """

    # ------------------------------------------------------------
    @abstractmethod
    def register(self, entry: DirectoryEntry,
                 replace: bool = False) -> DirectoryEntry:
        """登记一个 Agent。"""

    @abstractmethod
    def unregister(self, agent_id: str) -> bool:
        """注销一个 Agent。返回是否真的删掉了。"""

    @abstractmethod
    def get(self, agent_id: str) -> Optional[DirectoryEntry]:
        """按 ID 取。不存在返回 None。"""

    @abstractmethod
    def find_capable(
        self, required: Optional[Iterable[Any]]
    ) -> List[DirectoryEntry]:
        """找出**声明能力覆盖全部要求**的 Agent。"""

    @abstractmethod
    def all(self) -> List[DirectoryEntry]:
        """列出全部条目。"""

    # ------------------------------------------------------------
    # 以下是有默认实现的便捷方法
    # ------------------------------------------------------------
    def exists(self, agent_id: str) -> bool:
        return self.get(agent_id) is not None

    def require(self, agent_id: str) -> DirectoryEntry:
        entry = self.get(agent_id)
        if entry is None:
            raise AgentNotInDirectoryError(f"目录里没有这个 Agent：{agent_id}")
        return entry

    def count(self) -> int:
        return len(self.all())

    def stats(self) -> Dict[str, Any]:
        entries = self.all()
        capability_names = set()
        for entry in entries:
            capability_names.update(entry.capabilities.names)

        return {
            "agents": len(entries),
            "available": sum(1 for e in entries if e.available),
            "capabilities": sorted(capability_names),
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "directory": type(self).__name__,
            **self.stats(),
        }


# ============================================================
# 本机实现（当前唯一实现）
# ============================================================
class LocalAgentDirectory(AgentDirectory):
    """
    进程内的 Agent 目录。

    【它只是一个字典 —— 但调用方看不到这一点】
        这是关键：
            调用方写 directory.find_capable([...])
        而不是：
            AGENTS["agent_b"].capabilities

        所以将来把字典换成网络查询时，调用方一行都不用改。
    """

    def __init__(self, entries: Optional[Iterable[DirectoryEntry]] = None):
        self._entries: Dict[str, DirectoryEntry] = {}

        for entry in entries or []:
            self.register(entry)

    # ------------------------------------------------------------
    # 写
    # ------------------------------------------------------------
    def register(self, entry: DirectoryEntry,
                 replace: bool = False) -> DirectoryEntry:
        if not isinstance(entry, DirectoryEntry):
            raise DirectoryError(
                f"register() 需要 DirectoryEntry，"
                f"收到 {type(entry).__name__}"
            )

        if entry.agent_id in self._entries and not replace:
            raise DuplicateDirectoryEntryError(
                f"Agent {entry.agent_id} 已经在目录里。\n"
                f"如果要更新它的能力声明，传 replace=True。"
            )

        self._entries[entry.agent_id] = entry
        return entry

    def register_agent(
        self,
        agent_id: str,
        capabilities: Optional[Iterable[Any]] = None,
        address: Optional[str] = None,
        available: bool = True,
        metadata: Optional[Mapping[str, Any]] = None,
        replace: bool = False,
    ) -> DirectoryEntry:
        """
        便捷方法：直接给参数，不用先构造 DirectoryEntry。
        """
        return self.register(
            DirectoryEntry(
                agent_id=agent_id,
                capabilities=CapabilityProfile.of(capabilities),
                address=address,
                available=available,
                metadata=metadata or {},
            ),
            replace=replace,
        )

    def unregister(self, agent_id: str) -> bool:
        return self._entries.pop(str(agent_id), None) is not None

    def update_capabilities(
        self, agent_id: str, capabilities: Optional[Iterable[Any]]
    ) -> DirectoryEntry:
        """
        更新一个 Agent 的能力声明。

        【注意】这是**声明**更新，不是能力认证 ——
        Agent 说自己会什么，目录就记什么。
        """
        entry = self.require(agent_id)
        updated = DirectoryEntry(
            agent_id=entry.agent_id,
            capabilities=CapabilityProfile.of(capabilities),
            address=entry.address,
            available=entry.available,
            metadata=entry.metadata,
        )
        self._entries[agent_id] = updated
        return updated

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------
    def get(self, agent_id: str) -> Optional[DirectoryEntry]:
        return self._entries.get(str(agent_id))

    def find_capable(
        self, required: Optional[Iterable[Any]]
    ) -> List[DirectoryEntry]:
        """
        找出能力覆盖全部要求的 Agent。

        【刻意不做的三件事】

            1. 不排序
               返回顺序是 agent_id 排序（稳定），不是「谁知道更好」。
               一旦开始按「匹配度」排序，就等于偷偷做了能力评分。

            2. 不过滤 available
               不可用的 Agent 也会返回 —— 因为「谁有这个能力」和
               「谁现在能接活」是两个问题。调用方（Agent）自己决定。

            3. 不挑选
               返回**全部**符合条件的，绝不返回「一个最佳人选」。
               选谁是 Task Initiator Agent 的决定。
        """
        needed = CapabilityProfile.of(required)
        matches = [
            entry for entry in self._entries.values()
            if entry.capabilities.covers(needed)
        ]
        return sorted(matches, key=lambda e: e.agent_id)

    def all(self) -> List[DirectoryEntry]:
        return sorted(self._entries.values(), key=lambda e: e.agent_id)

    def available(self) -> List[DirectoryEntry]:
        """可用的 Agent（这个过滤是**调用方**的选择，不是 find_capable 的）。"""
        return [entry for entry in self.all() if entry.available]

    def with_capability(self, capability: Any) -> List[DirectoryEntry]:
        """具备某一个能力的 Agent（不管还有什么别的能力）。"""
        return [entry for entry in self.all() if entry.supports(capability)]

    # ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, agent_id: str) -> bool:
        return str(agent_id) in self._entries

    def __repr__(self) -> str:
        return f"LocalAgentDirectory({len(self._entries)} agents)"

"""
Address —— 通信地址
============================================================

【本步骤最重要的一条架构要求】

    Agent 身份 ≠ 通信地址。

    为什么必须分开？举三个未来一定会发生的场景：

    1. Agent 搬家
         agent_b 今天在你这台 Mac 上（local://agent_b），
         明天跑到了另一台机器（p2p://12D3Koo...）。
         如果 agent_id 本身就是地址，那么它一搬家，
         所有和它相关的账本记录、合同、声誉就全对不上了。
         身份必须稳定，地址可以变。

    2. 一个 Agent 多个入口
         agent_b 可能同时监听局域网和公网。
         身份只有一个，地址可以有好几个。

    3. 同一台机器上的身份迁移
         备份恢复、容器重启，地址变了，身份不变。

【所以这个文件只做一件事】

    描述「怎么找到某个 Agent」，而不是「某个 Agent 是谁」。
    身份在 core/identity.py，地址在这里。两者永不合并。

【支持的地址形态】

    local://agent_b           本进程内（当前唯一实现）
    p2p://12D3KooW...         P2P 网络（未来）
    ws://host:port/agent_b    WebSocket（未来）
    https://host/agents/b     HTTP（未来）

    这些 scheme 现在都不需要实现。
    但地址的数据结构必须能容纳它们，否则以后要推翻重来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping
from urllib.parse import urlparse

from communication.errors import InvalidAddressError

# ============================================================
# 已知的传输 scheme
# ============================================================
# 注意：这里【不是白名单】。
#   新 scheme 应该能直接用，不需要改这个文件 —— 否则
#   「未来加 P2PTransport」就又要动通信层了。
#   这个常量列表只用于「区分本地和远端」这类判断。
SCHEME_LOCAL = "local"

LOCAL_SCHEMES = frozenset({SCHEME_LOCAL})


@dataclass(frozen=True)
class Address:
    """
    一个 Agent 的通信地址。

    字段：
        agent_id: 这个地址指向哪个 Agent（逻辑身份，稳定不变）
        scheme:   用哪种传输方式（local / p2p / ws / https ...）
        endpoint: 该传输方式下的具体位置。
                  local 时等于 agent_id；
                  p2p 时是 peer id；
                  http 时是 host:port/path。

    【为什么 endpoint 和 agent_id 要分开存】
        以 p2p 为例：
            agent_id = "agent_b"          ← 逻辑身份
            endpoint = "12D3KooWAbc..."   ← 网络位置
        用 str() 显示时只显示网络位置（p2p://12D3KooWAbc...），
        因为那才是「怎么找到它」。
        但两个值都要留着：一个是给经济系统记账用的，
        一个是给传输层投递用的。
    """

    agent_id: str
    scheme: str = SCHEME_LOCAL
    endpoint: str = ""

    def __post_init__(self) -> None:
        agent_id = str(self.agent_id or "").strip()
        scheme = str(self.scheme or "").strip().lower()
        endpoint = str(self.endpoint or "").strip()

        if not agent_id:
            raise InvalidAddressError("Address 的 agent_id 不能为空")
        if not scheme:
            raise InvalidAddressError("Address 的 scheme 不能为空")
        if not endpoint:
            # local 场景下 endpoint 就是 agent_id，自动补上
            endpoint = agent_id

        # frozen dataclass 只能用 object.__setattr__ 改
        object.__setattr__(self, "agent_id", agent_id)
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "endpoint", endpoint)

    # ------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------
    @classmethod
    def for_local(cls, agent_id: str) -> "Address":
        """本进程内的地址：local://agent_x"""
        return cls(agent_id=agent_id, scheme=SCHEME_LOCAL, endpoint=agent_id)

    # 语义更直白的别名
    local = for_local

    # ------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> "Address":
        """
        把 "local://agent_b" 这种字符串解析成 Address。

        【关于 agent_id】
            从地址字符串里只能看出「位置」，看不出「身份」。
            对 local 来说两者恰好相同，所以能填上；
            对 p2p://12D3Koo... 来说，那个 peer id 并不等于 agent_id。

            所以解析远端地址时，agent_id 会被填成 endpoint 作为占位。
            如果你知道真实身份，请用构造函数显式指定：

                Address(agent_id="agent_b", scheme="p2p", endpoint="12D3Koo...")

            这个取舍是刻意的：与其假装能猜出身份，不如让调用方明确写出来。
        """
        raw = str(text or "").strip()
        if not raw:
            raise InvalidAddressError("地址不能为空")

        if "://" not in raw:
            raise InvalidAddressError(
                f"地址缺少 scheme：{raw!r}。\n"
                f"正确格式形如 local://agent_b、p2p://<peer-id>。"
            )

        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").strip().lower()
        if not scheme:
            raise InvalidAddressError(f"地址缺少 scheme：{raw!r}")

        # netloc 和 path 要【一起】保留。
        #
        # 为什么？看这个例子：
        #     ws://localhost:8080/agent_b
        #   netloc = "localhost:8080"
        #   path   = "/agent_b"
        # 只取 netloc 的话，路径就丢了，
        # 地址再也还原不回原来的样子（两台机器的不同 Agent
        # 会变成同一个地址）。
        netloc = (parsed.netloc or "").strip()
        path = (parsed.path or "").lstrip("/").strip()

        if netloc and path:
            endpoint = f"{netloc}/{path}"
        elif netloc:
            endpoint = netloc
        else:
            endpoint = path

        if not endpoint:
            raise InvalidAddressError(f"地址缺少主体部分：{raw!r}")

        return cls(agent_id=endpoint, scheme=scheme, endpoint=endpoint)

    # ------------------------------------------------------------
    # 判断
    # ------------------------------------------------------------
    @property
    def is_local(self) -> bool:
        """是不是本进程内的地址。"""
        return self.scheme in LOCAL_SCHEMES

    # ------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """完整信息（含 agent_id），用于持久化和网络传输。"""
        return {
            "agent_id": self.agent_id,
            "scheme": self.scheme,
            "endpoint": self.endpoint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Address":
        if not isinstance(data, Mapping):
            raise InvalidAddressError(
                f"Address 需要字典，收到 {type(data).__name__}"
            )
        return cls(
            agent_id=data.get("agent_id", ""),
            scheme=data.get("scheme", SCHEME_LOCAL),
            endpoint=data.get("endpoint", ""),
        )

    def __str__(self) -> str:
        """给人看的形态：只显示「怎么找到它」。"""
        return f"{self.scheme}://{self.endpoint}"

    def __repr__(self) -> str:
        return (
            f"Address(agent_id={self.agent_id!r}, scheme={self.scheme!r}, "
            f"endpoint={self.endpoint!r})"
        )

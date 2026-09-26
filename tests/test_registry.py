"""
Test: AgentRegistry（Agent 目录）
============================================================

重点不在「能存能取」，而在两件事：

    1. 注册表里只有【数据】，没有活对象
       这决定了未来远端 Agent 能不能作为一条普通记录存在。

    2. 通信状态 ≠ 经济状态
       破产的 Agent 依然可以在线收消息。
"""

from __future__ import annotations

import pytest

from communication import Address, AgentRegistry
from communication.errors import AgentNotFoundError, DuplicateAgentError
from communication.registry import AgentInfo, PresenceStatus


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def registry() -> AgentRegistry:
    return AgentRegistry()


@pytest.fixture
def info_a() -> AgentInfo:
    return AgentInfo(
        agent_id="agent_a",
        address=Address.for_local("agent_a"),
        display_name="Agent A",
        capabilities=("python_coding", "outsourcing"),
        presence=PresenceStatus.ONLINE,
    )


@pytest.fixture
def info_b() -> AgentInfo:
    return AgentInfo(
        agent_id="agent_b",
        address=Address.for_local("agent_b"),
        display_name="Agent B",
        capabilities=("testing",),
        presence=PresenceStatus.ONLINE,
    )


# ============================================================
# 1. AgentInfo 是纯数据
# ============================================================
class TestAgentInfoIsData:
    def test_has_no_agent_object(self, info_a):
        """条目里不能有 Agent 实例、Brain、Wallet 之类的东西。"""
        for forbidden in ("agent", "brain", "wallet", "instance", "handler"):
            assert not hasattr(info_a, forbidden), (
                f"AgentInfo 不该有 {forbidden} —— 它只存数据"
            )

    def test_is_serializable(self, info_a):
        """纯数据必须能序列化 —— 这是远端 Agent 能存在于同一张表里的前提。"""
        data = info_a.to_dict()
        restored = AgentInfo.from_dict(data)

        assert restored.agent_id == info_a.agent_id
        assert restored.capabilities == info_a.capabilities
        assert restored.address == info_a.address

    def test_capabilities_string_becomes_tuple(self):
        """写 "python" 也能用，自动变成元组。"""
        info = AgentInfo(
            agent_id="a", address=Address.for_local("a"), capabilities="python"
        )
        assert info.capabilities == ("python",)

    def test_has_capability(self, info_a):
        assert info_a.has_capability("python_coding") is True
        assert info_a.has_capability("数据库") is False

    def test_address_identity_must_match(self):
        """
        地址必须指向正确的身份。

        允许「身份和位置不同」，但不允许「地址指向别人」——
        那会导致消息发给了错误的 Agent。
        """
        with pytest.raises(ValueError, match="对不上"):
            AgentInfo(
                agent_id="agent_a",
                address=Address.for_local("agent_b"),   # 指向了别人
            )

    def test_empty_agent_id_rejected(self):
        with pytest.raises(ValueError, match="agent_id"):
            AgentInfo(agent_id="", address=Address.for_local("x"))

    def test_address_must_be_address_object(self):
        with pytest.raises(ValueError, match="Address"):
            AgentInfo(agent_id="a", address="local://a")  # type: ignore[arg-type]

    def test_display_name_defaults_to_id(self):
        info = AgentInfo(agent_id="a", address=Address.for_local("a"))
        assert info.display_name == "a"


# ============================================================
# 2. 注册 / 查找 / 注销
# ============================================================
class TestRegisterLookupUnregister:
    def test_register_and_lookup(self, registry, info_a):
        registry.register(info_a)
        found = registry.lookup("agent_a")

        assert found.agent_id == "agent_a"
        assert found.display_name == "Agent A"

    def test_find_returns_none_for_missing(self, registry):
        assert registry.find("不存在") is None

    def test_lookup_raises_for_missing(self, registry):
        """
        lookup 找不到时抛异常，而不是返回 None。

        为什么两个方法都要有？
            lookup：我认为它一定在 → 不在就是异常
            find  ：我只是看看     → 不在很正常
        逼所有人用其中一个，只会制造 if None 和 try/except 噪音。
        """
        with pytest.raises(AgentNotFoundError):
            registry.lookup("不存在")

    def test_lookup_error_lists_known_agents(self, registry, info_a, info_b):
        registry.register(info_a)
        registry.register(info_b)

        with pytest.raises(AgentNotFoundError) as excinfo:
            registry.lookup("agent_z")

        text = str(excinfo.value)
        assert "agent_a" in text
        assert "agent_b" in text

    def test_unregister(self, registry, info_a):
        registry.register(info_a)

        assert registry.unregister("agent_a") is True
        assert registry.exists("agent_a") is False

    def test_unregister_missing_returns_false(self, registry):
        assert registry.unregister("不存在") is False

    def test_duplicate_registration_rejected(self, registry, info_a):
        registry.register(info_a)

        with pytest.raises(DuplicateAgentError):
            registry.register(info_a)

    def test_duplicate_error_explains_the_workaround(self, registry, info_a):
        registry.register(info_a)

        with pytest.raises(DuplicateAgentError) as excinfo:
            registry.register(info_a)

        assert "replace=True" in str(excinfo.value)

    def test_replace_flag_allows_overwrite(self, registry, info_a):
        registry.register(info_a)

        updated = AgentInfo(
            agent_id="agent_a",
            address=Address.for_local("agent_a"),
            capabilities=("new_skill",),
        )
        registry.register(updated, replace=True)

        assert registry.lookup("agent_a").capabilities == ("new_skill",)

    def test_register_rejects_non_agentinfo(self, registry):
        """
        只接受 AgentInfo。

        这条校验防的是「图省事直接把 Agent 对象塞进来」——
        那样注册表就退化成局部字典，跨机器再也做不到了。
        """
        class FakeAgent:
            agent_id = "agent_a"

        with pytest.raises(ValueError, match="AgentInfo"):
            registry.register(FakeAgent())  # type: ignore[arg-type]


# ============================================================
# 3. 本地 / 远端
# ============================================================
class TestLocalVersusRemote:
    def test_local_agents(self, registry, info_a, info_b):
        registry.register(info_a)
        registry.register(info_b)

        assert len(registry.local_agents()) == 2

    def test_remote_agent_can_live_in_same_registry(self, registry, info_a):
        """
        【关键】远端 Agent 能作为一条普通记录存在。

        这正是「注册表只存数据」带来的好处：
        你没有对方的 Python 对象，但你依然可以登记它的地址。
        """
        registry.register(info_a)
        registry.register(
            AgentInfo(
                agent_id="agent_c",
                address=Address(agent_id="agent_c", scheme="p2p",
                                endpoint="12D3KooW"),
                is_local=False,
                node="node-2",
            )
        )

        assert registry.exists("agent_c")
        assert len(registry.local_agents()) == 1
        assert len(registry.remote_agents()) == 1

    def test_remote_address_uses_its_own_scheme(self, registry):
        info = AgentInfo(
            agent_id="agent_c",
            address=Address(agent_id="agent_c", scheme="p2p", endpoint="12D3KooW"),
            is_local=False,
        )
        registry.register(info)

        assert str(registry.lookup("agent_c").address) == "p2p://12D3KooW"

    def test_stats(self, registry, info_a):
        registry.register(info_a)
        registry.register(
            AgentInfo(
                agent_id="remote_one",
                address=Address(agent_id="remote_one", scheme="p2p",
                                endpoint="peer1"),
                is_local=False,
            )
        )

        stats = registry.stats()
        assert stats["total"] == 2
        assert stats["local"] == 1
        assert stats["remote"] == 1


# ============================================================
# 4. 按能力筛选
# ============================================================
class TestCapabilities:
    def test_by_capability(self, registry, info_a, info_b):
        registry.register(info_a)
        registry.register(info_b)

        python_agents = registry.by_capability("python_coding")

        assert len(python_agents) == 1
        assert python_agents[0].agent_id == "agent_a"

    def test_by_capability_empty_result(self, registry, info_a):
        registry.register(info_a)
        assert registry.by_capability("不存在的技能") == []

    def test_capabilities_survive_serialization(self, info_a):
        restored = AgentInfo.from_dict(info_a.to_dict())
        assert "python_coding" in restored.capabilities


# ============================================================
# 5. 通信状态 ≠ 经济状态
# ============================================================
class TestPresenceVersusEconomicStatus:
    """
    一个 Agent 可以「经济上破产」但「通信在线」。

    它还能收消息、还能谈判、还能接活翻身。
    把这两个状态混成一个，就会写出
    「破产的 Agent 连消息都发不出去」这种荒谬逻辑。
    """

    def test_insolvent_agent_can_still_be_online(self, economy, comm_manager):
        """
        真实场景：agent_poor 余额为 0（经济上破产），
        但它在通信注册表里依然是 ONLINE。
        """
        agent = economy.create_agent("agent_poor", mint=False)

        # 经济状态：破产
        assert agent.is_insolvent is True
        agent.update_status()
        assert agent.status == "INSOLVENT"

        # 通信状态：在线
        info = comm_manager.register(agent, capabilities=["cheap_labor"])

        assert info.presence == PresenceStatus.ONLINE
        assert comm_manager.registry.exists("agent_poor")

    def test_presence_values_are_distinct_from_agent_status(self):
        from agents.base_agent import AgentStatus

        # 两套状态值完全不重叠 —— 它们描述的是不同的东西
        assert not (PresenceStatus.ALL & AgentStatus.ALL)

    def test_update_presence(self, registry, info_a):
        registry.register(info_a)

        registry.update_presence("agent_a", PresenceStatus.OFFLINE)

        assert registry.lookup("agent_a").presence == PresenceStatus.OFFLINE

    def test_invalid_presence_becomes_unknown(self):
        info = AgentInfo(
            agent_id="a",
            address=Address.for_local("a"),
            presence="瞎写的状态",
        )
        assert info.presence == PresenceStatus.UNKNOWN


# ============================================================
# 6. 容器行为
# ============================================================
class TestContainerProtocol:
    def test_contains(self, registry, info_a):
        registry.register(info_a)

        assert "agent_a" in registry
        assert "agent_b" not in registry

    def test_len_and_iter(self, registry, info_a, info_b):
        registry.register(info_a)
        registry.register(info_b)

        assert len(registry) == 2
        assert {i.agent_id for i in registry} == {"agent_a", "agent_b"}

    def test_ids_is_sorted(self, registry, info_a, info_b):
        registry.register(info_b)
        registry.register(info_a)

        assert registry.ids() == ["agent_a", "agent_b"]

    def test_clear(self, registry, info_a):
        registry.register(info_a)
        registry.clear()

        assert len(registry) == 0

    def test_repr(self, registry, info_a):
        registry.register(info_a)
        assert "agent_a" in repr(registry)

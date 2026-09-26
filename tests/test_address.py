"""
Test: Address（通信地址）
============================================================

这个文件测的是 Step 3 最重要的一条架构要求：

    Agent 身份 ≠ 通信地址

所以测试的重点不是「能不能解析字符串」，
而是「身份和位置有没有被正确分开」。
"""

from __future__ import annotations

import pytest

from communication.address import SCHEME_LOCAL, Address
from communication.errors import InvalidAddressError


# ============================================================
# 1. 创建
# ============================================================
class TestCreation:
    def test_for_local(self):
        addr = Address.for_local("agent_b")

        assert addr.agent_id == "agent_b"
        assert addr.scheme == SCHEME_LOCAL
        assert addr.endpoint == "agent_b"
        assert addr.is_local is True

    def test_local_alias(self):
        assert Address.local("agent_b") == Address.for_local("agent_b")

    def test_endpoint_defaults_to_agent_id(self):
        addr = Address(agent_id="agent_b", scheme="local")
        assert addr.endpoint == "agent_b"

    def test_explicit_endpoint_is_kept(self):
        """P2P 场景：身份是 agent_b，但位置是那个 peer id。"""
        addr = Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")

        assert addr.agent_id == "agent_b"
        assert addr.endpoint == "12D3KooW"
        assert addr.is_local is False

    def test_scheme_is_lowercased(self):
        addr = Address(agent_id="a", scheme="LOCAL")
        assert addr.scheme == "local"

    def test_agent_id_is_stripped(self):
        assert Address(agent_id="  agent_b  ").agent_id == "agent_b"


# ============================================================
# 2. 校验
# ============================================================
class TestValidation:
    def test_empty_agent_id_rejected(self):
        with pytest.raises(InvalidAddressError, match="agent_id"):
            Address(agent_id="")

    def test_whitespace_agent_id_rejected(self):
        with pytest.raises(InvalidAddressError, match="agent_id"):
            Address(agent_id="   ")

    def test_empty_scheme_rejected(self):
        with pytest.raises(InvalidAddressError, match="scheme"):
            Address(agent_id="a", scheme="")


# ============================================================
# 3. 身份与地址分离（核心）
# ============================================================
class TestIdentityVersusAddress:
    """
    这些测试是「Agent 搬家」场景的可执行证明。
    """

    def test_same_identity_can_have_different_addresses(self):
        """
        同一个 Agent，今天在本地、明天在 P2P 网络。

        身份必须保持稳定 —— 否则它搬一次家，
        账本记录、合同、声誉就全都对不上了。
        """
        local = Address.for_local("agent_b")
        p2p = Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")

        # 身份相同
        assert local.agent_id == p2p.agent_id
        # 地址不同
        assert str(local) != str(p2p)

    def test_address_str_shows_location_not_identity(self):
        """
        str() 显示的是「怎么找到它」，不是「它是谁」。

        对 p2p 来说，你看到的是 peer id —— 那才是位置信息。
        """
        addr = Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")
        assert str(addr) == "p2p://12D3KooW"
        assert "agent_b" not in str(addr)

    def test_to_dict_keeps_both(self):
        """序列化时必须两个都留 —— 一个给记账，一个给投递。"""
        addr = Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")
        data = addr.to_dict()

        assert data["agent_id"] == "agent_b"
        assert data["endpoint"] == "12D3KooW"

    def test_roundtrip_keeps_identity_and_location(self):
        addr = Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")
        restored = Address.from_dict(addr.to_dict())

        assert restored == addr
        assert restored.agent_id == "agent_b"
        assert restored.endpoint == "12D3KooW"


# ============================================================
# 4. 字符串形态
# ============================================================
class TestStringForm:
    def test_local_str(self):
        assert str(Address.for_local("agent_b")) == "local://agent_b"

    def test_p2p_str(self):
        addr = Address(agent_id="x", scheme="p2p", endpoint="12D3KooW")
        assert str(addr) == "p2p://12D3KooW"

    def test_https_str(self):
        addr = Address(
            agent_id="x", scheme="https", endpoint="example.com/agents/b"
        )
        assert str(addr) == "https://example.com/agents/b"


# ============================================================
# 5. 解析
# ============================================================
class TestParsing:
    def test_parse_local(self):
        addr = Address.parse("local://agent_b")

        assert addr.scheme == "local"
        assert addr.endpoint == "agent_b"
        assert addr.agent_id == "agent_b"     # local 下两者相同
        assert addr.is_local is True

    def test_parse_p2p(self):
        """
        远端地址解析时，agent_id 只能填成 endpoint 占位。

        为什么允许这种「不完美」？
            因为一个 peer id 字符串里确实【不包含】逻辑身份。
            与其假装能猜出来，不如让调用方在知道身份时显式构造：
                Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")
        """
        addr = Address.parse("p2p://12D3KooW")

        assert addr.scheme == "p2p"
        assert addr.endpoint == "12D3KooW"
        assert addr.is_local is False

    def test_parse_ws_with_host_and_port(self):
        """
        netloc 和 path 都要留着。

        如果只取 netloc，"ws://h:8080/agent_b" 会变成 "ws://h:8080"，
        路径丢了 → 同一台机器上的不同 Agent 会指向同一个地址。
        """
        addr = Address.parse("ws://localhost:8080/agent_b")

        assert addr.scheme == "ws"
        assert addr.endpoint == "localhost:8080/agent_b"

    def test_parse_rejects_empty(self):
        with pytest.raises(InvalidAddressError, match="不能为空"):
            Address.parse("")

    def test_parse_rejects_missing_scheme(self):
        with pytest.raises(InvalidAddressError, match="scheme"):
            Address.parse("agent_b")

    def test_parse_rejects_scheme_only(self):
        with pytest.raises(InvalidAddressError, match="主体"):
            Address.parse("local://")

    def test_parse_then_str_roundtrip(self):
        for text in ("local://agent_b", "p2p://12D3KooW", "ws://h:1/x"):
            assert str(Address.parse(text)) == text


# ============================================================
# 6. 不可变性
# ============================================================
class TestImmutability:
    def test_address_is_frozen(self):
        """
        地址应该是不可变的。

        为什么？因为如果它可变，那么：
            registry 里存的地址可能被某个调用方悄悄改掉，
            然后所有人都在往一个已经不存在的地址发消息 ——
            而且查起来极其困难。
        """
        addr = Address.for_local("agent_b")

        with pytest.raises(Exception):
            addr.agent_id = "agent_c"  # type: ignore[misc]

    def test_addresses_are_hashable(self):
        """能放进 set / 当字典键。"""
        a = Address.for_local("agent_b")
        b = Address.for_local("agent_b")

        assert a == b
        assert len({a, b}) == 1

    def test_different_addresses_are_not_equal(self):
        assert Address.for_local("a") != Address.for_local("b")


# ============================================================
# 7. 为未来 scheme 预留
# ============================================================
class TestFutureSchemes:
    def test_unknown_scheme_is_allowed(self):
        """
        新 scheme 不该被拒绝。

        如果这里做了白名单，那么以后加 P2PTransport 时
        就要先回来改这个文件 —— 这正是我们要避免的。
        """
        for scheme in ("p2p", "ws", "quic", "libp2p", "ipfs", "future-thing"):
            addr = Address(agent_id="x", scheme=scheme, endpoint="somewhere")
            assert addr.scheme == scheme
            assert addr.is_local is False

    def test_only_local_is_local(self):
        assert Address(agent_id="x", scheme="local").is_local is True
        assert Address(agent_id="x", scheme="p2p").is_local is False
        assert Address(agent_id="x", scheme="ws").is_local is False

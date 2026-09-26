"""
Test: Agent Directory —— 能力发现（Step 4B）
============================================================

【这一组测试在守什么】

    Directory 只提供**信息**，不提供**决策**。

    它必须能回答：
        「谁声明了这些能力？」

    它必须**不能**回答：
        「谁最合适？」「该选谁？」「谁最好？」

    所以这里的测试分两类：
        · 功能类：register / unregister / get / find_capable
        · 边界类：没有排序、没有评分、不过滤可用性、不自动选人
"""

from __future__ import annotations

import pytest

from core.agent_directory import (
    AgentDirectory,
    AgentNotInDirectoryError,
    DirectoryEntry,
    DirectoryError,
    DuplicateDirectoryEntryError,
    LocalAgentDirectory,
)
from core.capability import CapabilityProfile


@pytest.fixture
def directory() -> LocalAgentDirectory:
    d = LocalAgentDirectory()
    d.register_agent("agent_a", ["task_decomposition", "outsourcing", "code_review"])
    d.register_agent("agent_b", ["python_coding", "data_analysis", "csv_processing"])
    return d


# ============================================================
# 1. 登记与注销
# ============================================================
class TestRegister:
    def test_register_entry(self):
        d = LocalAgentDirectory()
        entry = DirectoryEntry(agent_id="agent_x", capabilities=["python"])
        d.register(entry)

        assert d.get("agent_x") == entry
        assert d.count() == 1

    def test_register_agent_convenience(self):
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["python", "csv"])

        assert d.get("agent_x").capabilities.names == ["csv", "python"]

    def test_register_requires_entry(self):
        d = LocalAgentDirectory()
        with pytest.raises(DirectoryError):
            d.register("不是条目")

    def test_duplicate_rejected_by_default(self):
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["python"])

        with pytest.raises(DuplicateDirectoryEntryError):
            d.register_agent("agent_x", ["csv"])

    def test_duplicate_allowed_with_replace(self):
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["python"])
        d.register_agent("agent_x", ["csv"], replace=True)

        assert d.get("agent_x").capabilities.names == ["csv"]

    def test_update_capabilities(self):
        """Agent 可以更换自己的能力声明。"""
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["python"])
        d.update_capabilities("agent_x", ["python", "rust"])

        assert d.get("agent_x").capabilities.names == ["python", "rust"]

    def test_update_unknown_agent(self):
        d = LocalAgentDirectory()
        with pytest.raises(AgentNotInDirectoryError):
            d.update_capabilities("agent_不存在", ["python"])

    def test_unregister(self, directory):
        assert directory.unregister("agent_b") is True
        assert directory.get("agent_b") is None
        assert directory.count() == 1

    def test_unregister_unknown(self, directory):
        assert directory.unregister("agent_不存在") is False

    def test_exists(self, directory):
        assert directory.exists("agent_a") is True
        assert directory.exists("agent_不存在") is False

    def test_require_unknown_raises(self, directory):
        with pytest.raises(AgentNotInDirectoryError):
            directory.require("agent_不存在")

    def test_contains(self, directory):
        assert "agent_a" in directory
        assert "agent_不存在" not in directory

    def test_constructor_with_entries(self):
        d = LocalAgentDirectory([
            DirectoryEntry(agent_id="a", capabilities=["python"]),
            DirectoryEntry(agent_id="b", capabilities=["csv"]),
        ])

        assert d.count() == 2


# ============================================================
# 2. 查找
# ============================================================
class TestLookup:
    def test_get(self, directory):
        entry = directory.get("agent_b")

        assert entry.agent_id == "agent_b"
        assert "python_coding" in entry.capabilities

    def test_get_unknown_returns_none(self, directory):
        assert directory.get("agent_不存在") is None

    def test_all_is_sorted(self, directory):
        assert [e.agent_id for e in directory.all()] == ["agent_a", "agent_b"]

    def test_all_empty(self):
        assert LocalAgentDirectory().all() == []


# ============================================================
# 3. find_capable（核心）
# ============================================================
class TestFindCapable:
    def test_single_capability(self, directory):
        results = directory.find_capable(["python_coding"])

        assert [e.agent_id for e in results] == ["agent_b"]

    def test_multiple_capabilities_all_required(self, directory):
        """多个要求是「全部满足」，不是「满足任意一个」。"""
        results = directory.find_capable(["python_coding", "data_analysis"])

        assert [e.agent_id for e in results] == ["agent_b"]

    def test_no_match(self, directory):
        assert directory.find_capable(["rust"]) == []

    def test_one_missing_excludes(self, directory):
        """差一个就不算候选 —— 因为任务做不完。"""
        assert directory.find_capable(["python_coding", "rust"]) == []

    def test_empty_requirement_matches_everyone(self, directory):
        """没有能力要求 → 所有 Agent 都是候选。"""
        assert len(directory.find_capable([])) == 2

    def test_multiple_agents(self):
        """多个 Agent 满足同一要求时，**全部**返回。"""
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["python"])
        d.register_agent("agent_y", ["python", "csv"])
        d.register_agent("agent_z", ["python", "sql"])

        results = d.find_capable(["python"])

        assert [e.agent_id for e in results] == ["agent_x", "agent_y", "agent_z"]

    def test_results_are_sorted_by_id(self):
        """
        【排序是按 ID，不是按「谁更好」】

        结果顺序稳定且可预测。如果哪天变成了「匹配度排序」，
        那就等于偷偷做了能力评分。
        """
        d = LocalAgentDirectory()
        for name in ("agent_z", "agent_a", "agent_m"):
            d.register_agent(name, ["python"])

        assert [e.agent_id for e in d.find_capable(["python"])] == [
            "agent_a", "agent_m", "agent_z"
        ]

    def test_case_insensitive_matching(self):
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["Python_Coding"])

        assert len(d.find_capable(["python_coding"])) == 1
        assert len(d.find_capable(["PYTHON_CODING"])) == 1

    def test_accepts_capability_profile(self, directory):
        results = directory.find_capable(
            CapabilityProfile.of(["python_coding"])
        )
        assert len(results) == 1

    def test_accepts_none(self, directory):
        assert len(directory.find_capable(None)) == 2

    def test_does_not_filter_unavailable(self):
        """
        【重要边界】不可用的 Agent 也会被返回。

        「谁有这个能力」和「谁现在有空」是两个问题。
        过滤是调用方（Agent）自己的决定，Directory 只提供信息。
        """
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["python"], available=False)

        results = d.find_capable(["python"])

        assert len(results) == 1
        assert results[0].available is False

    def test_does_not_choose_one(self, directory):
        """绝不返回「一个最佳人选」—— 永远返回完整列表。"""
        results = directory.find_capable(["python_coding"])

        assert isinstance(results, list)
        assert len(results) == 1     # 这里恰好只有一个，但仍是列表


# ============================================================
# 4. 辅助查询
# ============================================================
class TestHelperQueries:
    def test_with_capability(self, directory):
        assert [e.agent_id for e in directory.with_capability("outsourcing")] == [
            "agent_a"
        ]

    def test_available(self):
        d = LocalAgentDirectory()
        d.register_agent("agent_x", ["python"], available=True)
        d.register_agent("agent_y", ["python"], available=False)

        assert [e.agent_id for e in d.available()] == ["agent_x"]

    def test_stats(self, directory):
        stats = directory.stats()

        assert stats["agents"] == 2
        assert stats["available"] == 2
        assert "python_coding" in stats["capabilities"]

    def test_describe(self, directory):
        info = directory.describe()

        assert info["directory"] == "LocalAgentDirectory"
        assert info["agents"] == 2

    def test_len(self, directory):
        assert len(directory) == 2

    def test_repr(self, directory):
        assert "LocalAgentDirectory" in repr(directory)


# ============================================================
# 5. 条目
# ============================================================
class TestDirectoryEntry:
    def test_basic(self):
        entry = DirectoryEntry(agent_id="agent_x", capabilities=["python"])

        assert entry.agent_id == "agent_x"
        assert entry.capabilities.names == ["python"]
        assert entry.address is None
        assert entry.available is True

    @pytest.mark.parametrize("bad", ["", "   ", None, 123])
    def test_invalid_agent_id(self, bad):
        with pytest.raises(DirectoryError):
            DirectoryEntry(agent_id=bad)

    def test_capabilities_coerced(self):
        """传 list 也能自动转成 CapabilityProfile。"""
        entry = DirectoryEntry(agent_id="agent_x", capabilities=["a", "b"])

        assert isinstance(entry.capabilities, CapabilityProfile)
        assert entry.capabilities.names == ["a", "b"]

    def test_immutable(self):
        entry = DirectoryEntry(agent_id="agent_x")
        with pytest.raises(Exception):
            entry.agent_id = "agent_y"

    def test_supports_and_covers(self):
        entry = DirectoryEntry(agent_id="agent_x", capabilities=["python", "csv"])

        assert entry.supports("python") is True
        assert entry.covers(["python"]) is True
        assert entry.covers(["python", "sql"]) is False
        assert entry.missing(["python", "sql"]) == {"sql"}

    def test_address_is_plain_string(self):
        """
        【架构边界】地址是字符串，不是 communication.Address。

        这样 core/ 不必依赖 communication/。
        """
        entry = DirectoryEntry(
            agent_id="agent_x", address="local://agent_x"
        )

        assert isinstance(entry.address, str)
        assert entry.address == "local://agent_x"

    def test_serialization(self):
        entry = DirectoryEntry(
            agent_id="agent_x", capabilities=["python"],
            address="local://agent_x", metadata={"node": "n1"},
        )
        restored = DirectoryEntry.from_dict(entry.to_dict())

        assert restored.agent_id == entry.agent_id
        assert restored.capabilities == entry.capabilities
        assert restored.address == entry.address
        assert restored.metadata == entry.metadata

    def test_describe(self):
        info = DirectoryEntry(agent_id="x", capabilities=["python"]).describe()

        assert info["agent_id"] == "x"
        assert info["capabilities"] == ["python"]


# ============================================================
# 6. 边界：不决策、不评分、不越权
# ============================================================
class TestDirectoryBoundaries:
    @pytest.mark.parametrize(
        "forbidden",
        [
            # 决策
            "best", "select", "choose", "pick", "recommend", "suggest",
            # 评分 / 信誉
            "score", "rank", "rating", "reputation", "weight",
            # 调度
            "assign", "dispatch", "schedule", "allocate",
            # 经济
            "transfer", "pay", "price", "balance", "wallet",
            # 权限
            "grant", "permission", "capability_check", "authorize",
            # 任务状态
            "set_task_status", "complete_task", "accept_task",
            # LLM
            "llm", "ask_model", "generate",
            # 文件 / Git
            "read_file", "write_file", "git",
        ],
    )
    def test_forbidden_attribute_absent(self, directory, forbidden):
        """
        Directory 只能「查」，不能「决定」也不能「越权」。
        """
        assert not hasattr(directory, forbidden), (
            f"AgentDirectory 不该有 {forbidden!r} —— "
            f"它只回答「谁声明了这些能力」，不做决策也不越权"
        )

    def test_is_abstract_interface(self):
        """AgentDirectory 必须是抽象类 —— 否则它就不是一个可替换的接口。"""
        with pytest.raises(TypeError):
            AgentDirectory()

    def test_find_capable_is_the_only_matching_entry_point(self, directory):
        """
        匹配只能通过 find_capable（或它的便捷包装）。

        没有 find_best / find_one / find_preferred 这类东西。
        """
        matching_methods = [
            name for name in dir(directory)
            if not name.startswith("_") and "find" in name.lower()
        ]

        assert matching_methods == ["find_capable"], (
            f"目录里出现了别的查找方法：{matching_methods}"
        )


# ============================================================
# 7. 为未来实现留的口子
# ============================================================
class TestFutureImplementations:
    def test_a_different_implementation_can_be_plugged_in(self):
        """
        【P2P / 网络扩展的证明】

        写一个完全不同的实现（模拟「远端目录」），
        调用方只需要依赖 AgentDirectory 这个接口。

        这不只是口头承诺 —— 下面这个类就是现场定义的，
        而调用方的代码（find_capable 的用法）一个字都没变。
        """
        class FakeRemoteDirectory(AgentDirectory):
            """模拟「未来从别的节点查能力」的目录。"""

            def __init__(self):
                self._remote = {
                    "agent_c": DirectoryEntry(
                        agent_id="agent_c",
                        capabilities=["python_coding"],
                        address="p2p://12D3KooW",
                    ),
                }
                self.queries = 0

            def register(self, entry, replace=False):
                self._remote[entry.agent_id] = entry
                return entry

            def unregister(self, agent_id):
                return self._remote.pop(agent_id, None) is not None

            def get(self, agent_id):
                return self._remote.get(agent_id)

            def find_capable(self, required):
                self.queries += 1
                profile = CapabilityProfile.of(required)
                return sorted(
                    (e for e in self._remote.values()
                     if e.capabilities.covers(profile)),
                    key=lambda e: e.agent_id,
                )

            def all(self):
                return sorted(self._remote.values(), key=lambda e: e.agent_id)

        # 调用方只依赖接口
        def find_helpers(directory: AgentDirectory, needed):
            return [e.agent_id for e in directory.find_capable(needed)]

        remote = FakeRemoteDirectory()

        assert find_helpers(remote, ["python_coding"]) == ["agent_c"]
        assert remote.queries == 1

        # 同一个调用函数，换一个实现照样工作
        local = LocalAgentDirectory()
        local.register_agent("agent_b", ["python_coding"])

        assert find_helpers(local, ["python_coding"]) == ["agent_b"]

    def test_interface_has_expected_contract(self):
        """接口必须有这几个方法 —— 未来实现要照着写。"""
        for method in ("register", "unregister", "get", "find_capable", "all"):
            assert hasattr(AgentDirectory, method), f"接口缺少 {method}"

    def test_entry_can_carry_a_remote_address_hint(self):
        """
        条目能带一个「位置提示」，为未来的网络发现留字段。

        注意它只是字符串 —— 真正的送达仍由 Communication 负责。
        """
        entry = DirectoryEntry(
            agent_id="agent_c",
            capabilities=["python_coding"],
            address="p2p://12D3KooW",
        )

        assert entry.address.startswith("p2p://")
        # 但它没有任何「联系方式」的能力
        assert not hasattr(entry, "send")
        assert not hasattr(entry, "connect")

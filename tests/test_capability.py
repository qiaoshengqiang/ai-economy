"""
Test: Capability —— 能力声明与匹配（Step 4B）
============================================================

【这一组测试在守什么】

    能力模块必须保持**极其简单**：

        只有 名字 + 集合运算。

    如果哪天它开始有「分数」「等级」「成功率」「排名」，
    这些测试会失败 —— 从而被迫先想清楚那属于哪一步。

    能力（能不能做）和信誉（做得好不好）是**两个维度**，
    混在一起以后就再也拆不开了。

【另一件重要的事】

    声明 ≠ 认证。

    Agent 说自己会 postgresql，只表示它**声称**会。
    系统没有证明任何事。这也由测试固定下来。
"""

from __future__ import annotations

import pytest

from core.capability import (
    Capability,
    CapabilityError,
    CapabilityProfile,
    InvalidCapabilityError,
    covers,
    missing_capabilities,
    normalize_capability,
)


# ============================================================
# 1. 归一化
# ============================================================
class TestNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("python", "python"),
            ("Python", "python"),
            ("PYTHON", "python"),
            ("  csv  ", "csv"),
            ("Data_Analysis", "data_analysis"),
            ("PostgreSQL", "postgresql"),
            ("scikit-learn", "scikit-learn"),
            ("node.js", "node.js"),
        ],
    )
    def test_normalized(self, raw, expected):
        """
        大小写和空格不该影响匹配 ——
        Agent 声明 "Python"、任务要求 "python" 必须能匹配上。
        """
        assert normalize_capability(raw) == expected

    @pytest.mark.parametrize("bad", ["", "   ", None, 123, [], {}])
    def test_invalid_rejected(self, bad):
        with pytest.raises(InvalidCapabilityError):
            normalize_capability(bad)

    @pytest.mark.parametrize("bad", ["hello world", "a/b", "c\\d", "emoji🙂", "a:b"])
    def test_illegal_characters_rejected(self, bad):
        """
        不允许空格、斜杠、冒号之类 ——
        否则「这是能力名还是路径」就分不清了。
        """
        with pytest.raises(InvalidCapabilityError):
            normalize_capability(bad)


# ============================================================
# 2. 单个能力
# ============================================================
class TestCapability:
    def test_create(self):
        assert Capability("python").name == "python"

    def test_normalized_on_create(self):
        assert Capability("  Python  ").name == "python"

    def test_immutable(self):
        cap = Capability("python")
        with pytest.raises(Exception):
            cap.name = "rust"

    def test_equality(self):
        assert Capability("python") == Capability("PYTHON")

    def test_hashable(self):
        assert len({Capability("python"), Capability("PYTHON")}) == 1

    def test_of_passthrough(self):
        cap = Capability("python")
        assert Capability.of(cap) is cap

    def test_of_from_string(self):
        assert Capability.of("python") == Capability("python")

    def test_str(self):
        assert str(Capability("python")) == "python"

    def test_has_no_score(self):
        """能力上不该有任何分数类字段。"""
        cap = Capability("python")

        for forbidden in ("score", "level", "weight", "proficiency",
                          "confidence", "rating", "rank"):
            assert not hasattr(cap, forbidden), (
                f"Capability 不该有 {forbidden!r} —— "
                f"能力是「会不会」，不是「有多好」"
            )


# ============================================================
# 3. 能力档案
# ============================================================
class TestCapabilityProfile:
    def test_create_from_list(self):
        profile = CapabilityProfile.of(["python", "csv"])

        assert profile.names == ["csv", "python"]
        assert profile.count() == 2

    def test_create_from_set(self):
        assert CapabilityProfile.of({"python", "csv"}).count() == 2

    def test_create_from_capability_objects(self):
        profile = CapabilityProfile.of([Capability("python"), "csv"])

        assert profile.names == ["csv", "python"]

    def test_empty(self):
        profile = CapabilityProfile.empty()

        assert profile.is_empty()
        assert profile.names == []
        assert not profile

    def test_none_is_empty(self):
        assert CapabilityProfile.of(None).is_empty()

    def test_duplicates_collapse(self):
        """
        【重复能力】声明两次 python 不代表更强 —— 集合天然去重。
        """
        profile = CapabilityProfile.of(["python", "python", "Python"])

        assert profile.count() == 1
        assert profile.names == ["python"]

    def test_order_irrelevant(self):
        assert (CapabilityProfile.of(["a", "b"]).names
                == CapabilityProfile.of(["b", "a"]).names)

    def test_single_string_is_one_capability(self):
        """传一个字符串应该当成「一个能力」，而不是「一串字符」。"""
        profile = CapabilityProfile.of("python")

        assert profile.names == ["python"]
        assert profile.count() == 1

    def test_immutable(self):
        profile = CapabilityProfile.of(["python"])

        with pytest.raises(Exception):
            profile._capabilities = frozenset()

    def test_equality(self):
        assert (CapabilityProfile.of(["a", "b"])
                == CapabilityProfile.of(["b", "a"]))

    def test_hashable(self):
        assert len({CapabilityProfile.of(["a"]), CapabilityProfile.of(["a"])}) == 1

    def test_iteration(self):
        assert list(CapabilityProfile.of(["python", "csv"])) == ["csv", "python"]

    def test_contains(self):
        profile = CapabilityProfile.of(["python"])

        assert "python" in profile
        assert "Python" in profile
        assert "rust" not in profile

    def test_contains_does_not_raise_on_garbage(self):
        """`in` 不该因为非法名字就抛异常。"""
        assert "has space" not in CapabilityProfile.of(["python"])


# ============================================================
# 4. 匹配（核心）
# ============================================================
class TestMatching:
    def test_supports(self):
        profile = CapabilityProfile.of(["python", "csv"])

        assert profile.supports("python") is True
        assert profile.supports("Python") is True    # 大小写无关
        assert profile.supports("rust") is False

    def test_covers_all_required(self):
        """
        【关键语义】covers 要求**全部**满足，不是任意一个。

        任务要求 python + csv，只会 python 的 Agent 不算候选 ——
        因为它做不完这个任务。
        """
        profile = CapabilityProfile.of(["python", "csv", "sql"])

        assert profile.covers(["python"]) is True
        assert profile.covers(["python", "csv"]) is True
        assert profile.covers(["python", "csv", "sql"]) is True
        assert profile.covers(["python", "rust"]) is False
        assert profile.covers([]) is True             # 无要求 → 谁都行

    def test_covers_empty_required(self):
        assert CapabilityProfile.of([]).covers([]) is True

    def test_empty_profile_covers_nothing_required(self):
        assert CapabilityProfile.empty().covers(["python"]) is False

    def test_missing(self):
        """【缺失能力】比对返回 False 有用得多 —— 能直接说差什么。"""
        profile = CapabilityProfile.of(["python"])

        assert profile.missing(["python", "csv", "rust"]) == {"csv", "rust"}
        assert profile.missing(["python"]) == set()
        assert profile.missing([]) == set()

    def test_subset_relation(self):
        """匹配就是子集运算：required ⊆ declared。"""
        declared = CapabilityProfile.of(["python", "csv", "sql"])

        assert CapabilityProfile.of(["python"]).to_list() == ["python"]
        assert declared.covers(CapabilityProfile.of(["python", "csv"]))

    def test_module_level_covers(self):
        assert covers(["python", "csv"], ["python"]) is True
        assert covers(["python"], ["python", "csv"]) is False

    def test_module_level_missing(self):
        assert missing_capabilities(["python"], ["python", "csv"]) == {"csv"}


# ============================================================
# 5. 集合运算
# ============================================================
class TestSetOperations:
    def test_union(self):
        result = CapabilityProfile.of(["a"]).union(CapabilityProfile.of(["b"]))
        assert result.names == ["a", "b"]

    def test_intersection(self):
        result = (CapabilityProfile.of(["a", "b"])
                  .intersection(CapabilityProfile.of(["b", "c"])))
        assert result.names == ["b"]

    def test_difference(self):
        result = (CapabilityProfile.of(["a", "b"])
                  .difference(CapabilityProfile.of(["b"])))
        assert result.names == ["a"]

    def test_operations_do_not_mutate(self):
        first = CapabilityProfile.of(["a"])
        second = CapabilityProfile.of(["b"])

        first.union(second)

        assert first.names == ["a"]
        assert second.names == ["b"]


# ============================================================
# 6. 序列化
# ============================================================
class TestSerialization:
    def test_to_list_is_sorted(self):
        """有序是为了输出稳定 —— 便于比对和 diff。"""
        assert CapabilityProfile.of(["z", "a", "m"]).to_list() == ["a", "m", "z"]

    def test_from_list(self):
        assert CapabilityProfile.from_list(["b", "a"]).names == ["a", "b"]

    def test_from_dict_with_key(self):
        profile = CapabilityProfile.from_dict({"capabilities": ["python"]})
        assert profile.names == ["python"]

    def test_from_dict_with_list(self):
        assert CapabilityProfile.from_dict(["python"]).names == ["python"]

    def test_from_dict_none(self):
        assert CapabilityProfile.from_dict(None).is_empty()

    def test_roundtrip(self):
        original = CapabilityProfile.of(["python", "csv"])
        assert CapabilityProfile.from_list(original.to_list()) == original

    def test_describe(self):
        info = CapabilityProfile.of(["python"]).describe()

        assert info["capabilities"] == ["python"]
        assert info["count"] == 1


# ============================================================
# 7. 声明 ≠ 认证
# ============================================================
class TestDeclarationIsNotProof:
    def test_declaration_carries_no_verification(self):
        """
        【这个测试固定一个事实：能力只是「声称」】

        一个 Agent 可以在目录里声明自己是 postgresql 专家，
        系统不会（也不该在 Step 4B）验证这一点。

        能力模块里不该有任何「已验证」「已认证」的概念。
        """
        profile = CapabilityProfile.of(["postgresql"])

        for forbidden in ("verified", "certified", "proven", "validated",
                          "attested", "trusted"):
            assert not hasattr(profile, forbidden), (
                f"CapabilityProfile 不该有 {forbidden!r} —— "
                f"声明不等于已证实"
            )

    def test_declaring_anything_is_allowed(self):
        """
        任何合法名字都能声明 —— 没有白名单、没有审核。

        系统不知道 agent_b 是不是真的会 rust，
        它只记录「agent_b 说自己会 rust」。
        """
        profile = CapabilityProfile.of(["rust", "quantum_computing", "alchemy"])

        assert profile.count() == 3

    def test_agent_id_does_not_affect_matching(self):
        """
        匹配只看能力，不看是谁。

        同样的能力声明，任何 agent_id 结果都一样 ——
        没有「因为它是 agent_b 所以优先」这种事。
        """
        assert (CapabilityProfile.of(["python"]).covers(["python"])
                == CapabilityProfile.of(["python"]).covers(["python"]))


# ============================================================
# 8. 没有评分 / 排名 / 信誉
# ============================================================
class TestNoScoring:
    @pytest.mark.parametrize(
        "forbidden",
        [
            "score", "rank", "rating", "weight", "confidence",
            "reputation", "best", "select_best", "recommend",
            "probability", "success_rate", "priority",
        ],
    )
    def test_no_scoring_api_on_profile(self, forbidden):
        """
        【Step 4B 的硬性要求】

        能力模块不许出现评分 / 排名 / 推荐。
        一旦有了 score，能力（能不能做）和信誉（做得好不好）
        就混在一起了 —— 以后再也拆不开。
        """
        profile = CapabilityProfile.of(["python"])

        assert not hasattr(profile, forbidden), (
            f"CapabilityProfile 不该有 {forbidden!r} —— "
            f"那属于未来的信誉/表现体系，不是能力"
        )

    def test_no_scoring_api_on_module(self):
        import core.capability as module

        for forbidden in ("score", "rank", "best", "reputation"):
            for name in dir(module):
                assert forbidden not in name.lower(), (
                    f"core/capability.py 里出现了 {name!r}"
                )

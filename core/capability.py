"""
Capability —— Agent 的能力声明与匹配
============================================================

【这个文件解决什么问题】

    之前没有 Task 的时候，Agent 之间是直接对话的。
    现在有了 Task，就出现一个新问题：

        「这个任务需要什么能力？」
        「谁会这个？」

    本文件提供前半句的数据结构：**能力声明**。

【三条必须记住的边界】（每一条都有架构守卫测试）

    1. ❌ 不做能力评分
       不要写：
           python = 93
           postgresql = 87
           agent_b.score = 0.93
       那会把「能力」和「表现 / 信誉 / 经济」混在一起。
       一旦混了，以后想单独改进信誉模型就得动能力模块。

       当前只回答一个布尔问题：
           这个 Agent 声明的能力，**覆盖**任务要求的能力吗？

       也就是：required ⊆ declared

    2. ❌ 不做能力认证
       「声明」不等于「已证实」：

           Capability declaration  ≠  Proven ability

       Agent 声明自己有 postgresql 能力，只能表示它**声称**会。
       系统并没有证明它一定能做好。
       以后可以用任务成功率、返工率、交付时间建立「能力表现」，
       但那属于后续步骤。

    3. ❌ 不依赖任何其它层
       不碰 llm、不碰 wallet / ledger、不碰文件系统、不碰 Git、
       不碰 Workspace 权限、不碰任何 Transport 实现。
       它是整个项目里最底层的东西之一 —— 只做名字的集合运算。

【为什么能力名要做归一化】

    "Python" / "python" / " python " 应该是同一个能力。
    否则 Agent A 声明 "Python"、任务要求 "python" 就会匹配不上 ——
    这种 bug 很难查，因为两边看起来都对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Union


# ============================================================
# 异常
# ============================================================
class CapabilityError(Exception):
    """能力相关错误的基类。"""


class InvalidCapabilityError(CapabilityError):
    """能力名本身非法（空、非字符串、非法字符）。"""


# ============================================================
# 能力名归一化
# ============================================================
# 允许的字符：小写字母、数字、下划线、连字符、点
# 为什么限制？
#     能力名会被存进数据库、放进 JSON、出现在日志和匹配逻辑里。
#     允许任意字符（空格、斜杠、表情符号）会让匹配变得不可预测，
#     也会让「这到底是能力名还是路径」变得分不清。
_CAPABILITY_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyz0123456789_-."
)


def normalize_capability(name: Any) -> str:
    """
    把一个能力名归一化。

        "Python"      → "python"
        "  CSV  "     → "csv"
        "Data_Analysis" → "data_analysis"

    不做「同义词合并」（比如把 postgres 和 postgresql 视为同一个）——
    因为那需要一张同义词表，而这一步不需要那么复杂。
    同义词属于未来「能力词汇表」的事。
    """
    if not isinstance(name, str):
        raise InvalidCapabilityError(
            f"能力名必须是字符串，收到 {type(name).__name__}：{name!r}"
        )

    text = name.strip().lower()
    if not text:
        raise InvalidCapabilityError("能力名不能为空")

    illegal = set(text) - _CAPABILITY_ALLOWED
    if illegal:
        raise InvalidCapabilityError(
            f"能力名 {name!r} 含有不允许的字符：{''.join(sorted(illegal))}\n"
            f"只允许小写字母、数字、下划线、连字符和点。"
        )

    return text


# ============================================================
# 单个能力
# ============================================================
@dataclass(frozen=True, order=True)
class Capability:
    """
    一个能力声明。

    用法：
        Capability("python")
        Capability("PostgreSQL")        # 自动归一化成 "postgresql"

    【它只是一个名字】
        没有等级、没有熟练度、没有分数、没有权重。
        需要那些东西的时候，应该是另一个概念（能力表现 / 信誉），
        而不是往这个类型上加字段。
    """

    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_capability(self.name))

    @classmethod
    def of(cls, value: Union[str, "Capability"]) -> "Capability":
        """从字符串或 Capability 构造。"""
        if isinstance(value, Capability):
            return value
        return cls(value)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Capability({self.name!r})"


# ============================================================
# 能力档案（一组能力）
# ============================================================
@dataclass(frozen=True)
class CapabilityProfile:
    """
    一个 Agent 声明的**全部**能力（或一个 Task 要求的全部能力）。

    用法：
        profile = CapabilityProfile.of(["python", "csv", "data_analysis"])

        profile.supports("python")                    # True
        profile.covers(["python", "csv"])             # True
        profile.covers(["python", "rust"])            # False
        profile.missing(["python", "rust"])           # {"rust"}

    【为什么用 frozenset 而不是 list】
        能力是**集合**，不是有序列表：
            · 顺序没有意义
            · 不允许重复（声明两次 python 不代表更强）
            · 匹配就是子集运算
        用 frozenset 让这些性质由类型保证，而不是靠约定。
    """

    _capabilities: frozenset = field(default_factory=frozenset)

    # ------------------------------------------------------------
    def __post_init__(self) -> None:
        # 允许传进来 set / list / dict_keys，统一成 frozenset
        raw = self._capabilities
        if raw is None:
            raw = frozenset()

        normalized: Set[str] = set()
        for item in raw:
            if isinstance(item, Capability):
                normalized.add(item.name)
            else:
                normalized.add(normalize_capability(item))

        object.__setattr__(self, "_capabilities", frozenset(normalized))

    # ------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------
    @classmethod
    def of(cls, values: Optional[Iterable[Any]]) -> "CapabilityProfile":
        """
        从任意可迭代对象构造。

        接受：None / [] / ["python"] / [Capability("python")] / {"python"}
        这样调用方不用关心传进来的到底是列表还是集合。
        """
        if values is None:
            return cls()
        if isinstance(values, (str, Capability)):
            # 单个字符串是很常见的笔误 —— 明确支持它，语义是「一个能力」
            return cls(frozenset([values]))
        return cls(frozenset(values))

    @classmethod
    def empty(cls) -> "CapabilityProfile":
        return cls()

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def supports(self, capability: Union[str, Capability]) -> bool:
        """
        是否具备某一个能力。

        这就是本文件的核心操作，没有别的。
        """
        return normalize_capability(
            capability.name if isinstance(capability, Capability) else capability
        ) in self._capabilities

    def covers(self, required: Optional[Iterable[Any]]) -> bool:
        """
        是否覆盖**全部**要求的能力。

        即：required ⊆ declared

        注意是**全部**，不是任意一个：
            任务要求 python + csv，
            只会 python 的 Agent 不算候选 —— 因为任务做不完。
        """
        needed = CapabilityProfile.of(required)
        return needed._capabilities <= self._capabilities

    def missing(self, required: Optional[Iterable[Any]]) -> Set[str]:
        """
        缺哪些能力。

        比对返回一个 False 有用得多 ——
        能直接告诉 Agent「你差什么」，也让日志可读。
        """
        needed = CapabilityProfile.of(required)
        return set(needed._capabilities - self._capabilities)

    # ------------------------------------------------------------
    # 集合运算
    # ------------------------------------------------------------
    def union(self, other: "CapabilityProfile") -> "CapabilityProfile":
        return CapabilityProfile(self._capabilities | other._capabilities)

    def intersection(self, other: "CapabilityProfile") -> "CapabilityProfile":
        return CapabilityProfile(self._capabilities & other._capabilities)

    def difference(self, other: "CapabilityProfile") -> "CapabilityProfile":
        return CapabilityProfile(self._capabilities - other._capabilities)

    # ------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------
    @property
    def names(self) -> List[str]:
        """排序后的能力名列表（排序是为了输出稳定，便于比对）。"""
        return sorted(self._capabilities)

    def is_empty(self) -> bool:
        return not self._capabilities

    def count(self) -> int:
        return len(self._capabilities)

    def __len__(self) -> int:
        return len(self._capabilities)

    def __iter__(self):
        return iter(self.names)

    def __contains__(self, item) -> bool:
        try:
            return self.supports(item)
        except InvalidCapabilityError:
            return False

    def __bool__(self) -> bool:
        return bool(self._capabilities)

    # ------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------
    def to_list(self) -> List[str]:
        """存进数据库 / JSON 用的形式（有序，便于阅读和 diff）。"""
        return self.names

    def to_dict(self) -> Dict[str, Any]:
        return {"capabilities": self.names}

    @classmethod
    def from_list(cls, values: Optional[Iterable[Any]]) -> "CapabilityProfile":
        return cls.of(values)

    @classmethod
    def from_dict(cls, data: Any) -> "CapabilityProfile":
        """从字典或列表构造（两种写法都接受）。"""
        if data is None:
            return cls.empty()
        if isinstance(data, Mapping):
            return cls.of(data.get("capabilities"))
        return cls.of(data)

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "capabilities": self.names,
            "count": self.count(),
        }

    def __str__(self) -> str:
        if not self._capabilities:
            return "（无能力声明）"
        return ", ".join(self.names)

    def __repr__(self) -> str:
        return f"CapabilityProfile({self.names!r})"


# ============================================================
# 纯函数形式的匹配（方便在不构造对象的地方使用）
# ============================================================
def covers(declared: Optional[Iterable[Any]],
           required: Optional[Iterable[Any]]) -> bool:
    """
    声明的能力是否覆盖要求的能力。

    等价于：CapabilityProfile.of(declared).covers(required)

    【为什么还提供函数形式】
        有些调用点（比如日志、过滤）不想先构造对象。
        函数形式更直接：covers(agent.capabilities, task.required_capabilities)
    """
    return CapabilityProfile.of(declared).covers(required)


def missing_capabilities(declared: Optional[Iterable[Any]],
                         required: Optional[Iterable[Any]]) -> Set[str]:
    """缺哪些能力（函数形式）。"""
    return CapabilityProfile.of(declared).missing(required)

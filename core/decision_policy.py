"""
Decision Policy —— Agent 的低成本经济决策
============================================================

【这个文件解决什么问题】

    Agent 收到一个报价，要决定：

        接受？还价？拒绝？

    最贵的做法是每一轮都问大模型：

        A 的 LLM 想 → B 的 LLM 想 → A 的 LLM 想 → B 的 LLM 想 → …

    但一次任务可能来回十几轮报价。如果每轮都调模型，
    **谈判成本可能高于任务本身的价值**。

    所以结构化经济决策必须由**本地逻辑**完成：

        Agent
          ↓
        DecisionPolicy      ← 本文件（纯本地、无网络、无模型）
          ↓
        Decision（结构化结果）
          ↓
        TaskService          ← 校验 + 执行
          ↓
        Task Core            ← 改状态

【三条必须守住的边界】（每一条都有架构守卫测试）

    1. ❌ Policy 不修改任何状态
       不改 Task、不改 Wallet、不改 Ledger、不发消息、不碰 Workspace。
       它只**读** DecisionContext，**返回** Decision。
       这样它才能被毫无副作用地测试、重放、替换。

    2. ❌ Policy 不依赖任何上层
       不 import llm、不 import communication、不 import wallet / ledger、
       不碰文件系统。它是纯函数式的领域组件。

    3. ❌ Policy 里不出现具体 Agent 的名字
       绝对不许写 `if agent_id == "agent_b": ...`
       参数必须来自 Agent **自己的配置** ——
       B 和 C 用同一个类，只是参数不同。

【为什么要 DecisionContext】

    决策不能只看价格，还要看上下文：

        我是买方还是卖方？（方向不同，好坏的判断相反）
        这是第几轮还价？
        任务预算是多少？

    而且未来还要能加入「决策成本」：

        估算任务成本 / 当前负载 / 谈判成本 / token 成本

    所以现在就把上下文做成一个**独立的对象**，
    将来加字段不需要改 Policy 的签名。

【未来扩展（本步骤不实现）】

    DecisionPolicy
    ├── RuleBasedDecisionPolicy       ← 现在实现
    ├── LocalModelDecisionPolicy      ← 未来：便宜的本地小模型
    └── BrainDecisionPolicy           ← 未来：值得时才调昂贵的大模型

    接口已经让它成为可能：Policy 是普通的 Python 组件，
    未来一个 Heartbeat / Agent Runtime 可以直接调用它，
    不需要经过 HTTP 或 CLI。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Union

from core.money import DEFAULT_CURRENCY, Money


# ============================================================
# 决策类型
# ============================================================
class DecisionType:
    """Agent 可以做出的三种经济决策。

    注意：这三个字符串和协议里的 message_type 一致
    （PROTOCOL.md 6.2），但本文件**不 import communication** ——
    两边一致由测试保证，而不是靠依赖。
    """

    ACCEPT = "ACCEPT"
    COUNTER_OFFER = "COUNTER_OFFER"
    REJECT = "REJECT"

    @classmethod
    def all(cls) -> List[str]:
        return [cls.ACCEPT, cls.COUNTER_OFFER, cls.REJECT]


class DecisionError(Exception):
    """决策相关错误的基类。"""


class InvalidDecisionError(DecisionError):
    """Decision 本身不合法（比如 ACCEPT 却没有价格）。"""


# ============================================================
# 决策
# ============================================================
@dataclass(frozen=True)
class Decision:
    """
    一个结构化的经济决策。

    【它是**决定**，不是**执行**】
        它只说「我想怎么样」，不产生任何副作用。
        把它交给 TaskService 之后，才会被校验并落到 Task 状态上。

        这样做的好处：
            · 可以先把决策算出来、记下来、测试它，再决定要不要执行
            · 同一个决策可以被重放（未来做审计 / 复盘时很有用）
            · Policy 可以被替换成任何实现，只要输出这个结构
    """

    decision_type: str

    #: ACCEPT / COUNTER_OFFER 时必须有价格；REJECT 时为空。
    price: Optional[Money] = None

    #: 人类可读的理由（会进日志，也方便演示和排查）。
    reason: str = ""

    #: 哪个策略做出的决定（便于将来对比不同策略）。
    policy: str = ""

    #: 🔒 预留：这次决策本身花了多少钱。
    #: 未来 AI Economy 会把 LLM / Tool / Compute / Network 成本都算进来，
    #: 所以现在就把字段留好 —— 但本步骤恒为 None，**不做任何扣费**。
    decision_cost: Optional[Money] = None

    # ------------------------------------------------------------
    def __post_init__(self) -> None:
        if self.decision_type not in DecisionType.all():
            raise InvalidDecisionError(
                f"未知的决策类型：{self.decision_type!r}，"
                f"合法值：{DecisionType.all()}"
            )
        if self.decision_type == DecisionType.REJECT and self.price is not None:
            raise InvalidDecisionError(
                "REJECT 不该带价格 —— 拒绝就是不谈了，"
                "带一个价格会让下游不知道该不该用它"
            )
        if self.decision_type in (DecisionType.ACCEPT,
                                  DecisionType.COUNTER_OFFER):
            if self.price is None:
                raise InvalidDecisionError(
                    f"{self.decision_type} 必须带价格"
                )
            if not isinstance(self.price, Money):
                raise InvalidDecisionError(
                    f"价格必须是 Money，收到 {type(self.price).__name__}"
                )

    # ------------------------------------------------------------
    @property
    def accepted(self) -> bool:
        return self.decision_type == DecisionType.ACCEPT

    @property
    def countered(self) -> bool:
        return self.decision_type == DecisionType.COUNTER_OFFER

    @property
    def rejected(self) -> bool:
        return self.decision_type == DecisionType.REJECT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_type": self.decision_type,
            "price": self.price.to_dict() if self.price else None,
            "reason": self.reason,
            "policy": self.policy,
            "decision_cost": (
                self.decision_cost.to_dict() if self.decision_cost else None
            ),
        }

    def __str__(self) -> str:
        if self.price is not None:
            return f"{self.decision_type} @ {self.price}"
        return self.decision_type

    def __repr__(self) -> str:
        return (
            f"Decision({self.decision_type!r}, price={self.price}, "
            f"reason={self.reason!r})"
        )


# ============================================================
# 决策上下文
# ============================================================
@dataclass(frozen=True)
class DecisionContext:
    """
    做决策时能看到的**本地**信息。

    【为什么价格不够】
        同一个价格对不同角色意义相反：
            卖方收到 60  → 不错
            买方收到 60  → 有点贵
        所以必须知道「我在这笔交易里是哪一方」。

        这个方向不是从 agent_id 推出来的，而是从
        「我是不是这个任务的发起方」推出来的 ——
        所以它适用于任何两个 Agent，不是只适用于 A 和 B。

    【未来扩展位】
        §34 要求接口不能阻止未来加入成本信息。
        所以下面留了三个预留字段（现在恒为 None）：
            estimated_task_cost / current_workload / negotiation_cost
    """

    agent_id: str
    task_id: str

    #: 对方这次给出的价格。
    #: 对 TASK_OFFER 来说，这就是它的 budget（协议规定接受即按 budget 成交）。
    offered_price: Money

    #: 我是不是这个任务的发起方。
    #: True  → 我是买方（付钱的一方），价格越低越好
    #: False → 我是卖方（干活的一方），价格越高越好
    is_initiator: bool

    #: 任务的预算上限（如果已知）。
    budget: Optional[Money] = None

    #: 我已经还过几次价。
    #: 用来避免「无限还价」。
    negotiation_round: int = 0

    #: 我最多还几次价（由策略决定，放在这里方便策略读取）。
    max_rounds: int = 0

    #: 当前这条消息的类型（TASK_OFFER / COUNTER_OFFER）。
    message_type: str = ""

    # ---- 🔒 以下为未来成本核算预留，本步骤恒为 None ----
    estimated_task_cost: Optional[Money] = None
    current_workload: Optional[int] = None
    negotiation_cost: Optional[Money] = None

    # ------------------------------------------------------------
    def __post_init__(self) -> None:
        if not isinstance(self.offered_price, Money):
            raise DecisionError(
                f"offered_price 必须是 Money，"
                f"收到 {type(self.offered_price).__name__}"
            )

    # ------------------------------------------------------------
    @property
    def side(self) -> str:
        """我在这笔交易里的角色：buyer（付钱）或 seller（干活）。"""
        return "buyer" if self.is_initiator else "seller"

    @property
    def rounds_exhausted(self) -> bool:
        """我还价次数是否已经用完了。"""
        return self.negotiation_round >= self.max_rounds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "offered_price": self.offered_price.to_dict(),
            "is_initiator": self.is_initiator,
            "side": self.side,
            "budget": self.budget.to_dict() if self.budget else None,
            "negotiation_round": self.negotiation_round,
            "max_rounds": self.max_rounds,
            "message_type": self.message_type,
        }

    def __repr__(self) -> str:
        return (
            f"DecisionContext(agent={self.agent_id!r}, side={self.side!r}, "
            f"offered={self.offered_price}, round={self.negotiation_round}/"
            f"{self.max_rounds})"
        )


# ============================================================
# 抽象接口
# ============================================================
class DecisionPolicy(ABC):
    """
    经济决策策略。

    实现者只需要回答一个问题：

        「这个报价，我接受、还价、还是拒绝？」

    【契约】
        · evaluate() **必须没有副作用** —— 不改任何状态、不发消息
        · 必须是**确定性**的（同样输入给同样输出），
          否则测试会不稳定，也没法重放
        · 默认**不调用任何模型**

    未来的实现：
        LocalModelDecisionPolicy   便宜的本地小模型
        BrainDecisionPolicy        值得时才调昂贵的大模型
    """

    #: 策略名（进日志、进 Decision.policy）
    name: str = "DecisionPolicy"

    @abstractmethod
    def evaluate(self, context: DecisionContext) -> Decision:
        """根据上下文返回一个决策。**不产生任何副作用。**"""

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {"policy": self.name}

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


# ============================================================
# 第一版：确定性规则策略
# ============================================================
def _as_money(value: Union[int, float, Money, None],
              field_name: str,
              currency: str) -> Optional[Money]:
    """
    把配置里的数字或 Money 统一成 Money。

    允许直接写数字（配置里更自然）：
        minimum_price: 20      →  Money(20, "AIC")
    也允许明确指定货币：
        minimum_price: {amount: 20, currency: "AIC"}
    """
    if value is None:
        return None
    if isinstance(value, Money):
        return value
    if isinstance(value, Mapping):
        return Money.from_dict(value)
    if isinstance(value, bool):
        raise DecisionError(f"{field_name} 不能是布尔值")
    if isinstance(value, (int, float)):
        return Money(value, currency)
    raise DecisionError(
        f"{field_name} 必须是数字或金额对象，收到 {type(value).__name__}"
    )


class RuleBasedDecisionPolicy(DecisionPolicy):
    """
    确定性的规则策略 —— **不调用任何模型**。

    参数（都来自 Agent 自己的配置）：

        accept_price      接受线。卖家：报价 ≥ 它就接；买家：报价 ≤ 它就接
        opening_ask       我第一次还价时报的价
        walk_away_price   绝对底线，比它更差就直接拒绝
        max_rounds        我最多还几次价（防止无限还价）
        accept_first_offer 是否接受第一个报价（默认 False，见下）

    【四条规则，按顺序判断】

        1. 已经还过价，且报价达到 accept_price        → ACCEPT
        2. 报价比 walk_away_price 更差                → REJECT
        3. 我还价的次数已经用完                        → REJECT
        4. 否则                                        → COUNTER_OFFER

    【为什么默认不接受第一个报价】

        因为 TASK_OFFER 里的 `budget` 是**上限**，不是报价。

            买方说「我最多出 100」≠「我出 100」

        卖方收到 100 的预算，正确的做法是**报出自己的价格**
        （比如 60），而不是直接按 100 成交 —— 后者等于主动放弃议价空间。

        这条规则让谈判存在，而且它是**确定性、零成本**的。
        如果你希望「对方给的够好就立刻拿下」，把 accept_first_offer 设成 True。

    【还价的让步方式】

        第 r 轮还价的价格，从 opening_ask 线性让步到 walk_away_price：

            ask(r) = opening_ask + (walk_away - opening_ask) * r / (max_rounds - 1)

        也就是「第一次报自己的价，最后一轮报到自己的底线」。
        这样还价会收敛，不会原地打转。
    """

    name = "RuleBasedDecisionPolicy"

    def __init__(
        self,
        accept_price: Union[int, float, Money],
        opening_ask: Union[int, float, Money, None] = None,
        walk_away_price: Union[int, float, Money, None] = None,
        max_rounds: int = 3,
        accept_first_offer: bool = False,
        currency: str = DEFAULT_CURRENCY,
    ):
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool):
            raise DecisionError("max_rounds 必须是整数")
        if max_rounds < 1:
            raise DecisionError(
                f"max_rounds 至少是 1（收到 {max_rounds}）—— "
                f"否则连一次还价都不允许"
            )

        self.currency = currency
        self.max_rounds = max_rounds
        self.accept_first_offer = bool(accept_first_offer)

        self.accept_price = _as_money(accept_price, "accept_price", currency)
        # 没给还价锚点就用接受线 —— 表示「不还价，够线就接」
        self.opening_ask = _as_money(
            opening_ask, "opening_ask", currency
        ) or self.accept_price
        # 没给底线就用接受线
        self.walk_away_price = _as_money(
            walk_away_price, "walk_away_price", currency
        ) or self.accept_price

        if self.accept_price is None:
            raise DecisionError("accept_price 不能为空")

    # ------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        currency: str = DEFAULT_CURRENCY,
    ) -> "RuleBasedDecisionPolicy":
        """
        从 Agent 自己的配置构造。

        【为什么必须从配置来】
            §6 明确禁止硬编码某个 Agent 的策略：

                ❌ if agent_id == "agent_b": minimum = 20

            每个 Agent 的配置里写自己的参数，用同一个类。
            B 和 C 的差别只在于配置文件不同。
        """
        if not isinstance(config, Mapping):
            raise DecisionError(
                f"决策策略配置必须是字典，收到 {type(config).__name__}"
            )

        accept = config.get("accept_price")
        if accept is None:
            raise DecisionError(
                "配置里缺少 accept_price —— 它是唯一的必需参数。\n"
                "（Agent 必须知道自己接受什么价格）"
            )

        return cls(
            accept_price=accept,
            opening_ask=config.get("opening_ask"),
            walk_away_price=config.get("walk_away_price"),
            max_rounds=int(config.get("max_rounds", 3)),
            accept_first_offer=bool(config.get("accept_first_offer", False)),
            currency=config.get("currency", currency),
        )

    # ------------------------------------------------------------
    # 方向判断：同一组数字，买卖双方的「好」是相反的
    # ------------------------------------------------------------
    def _meets_accept_line(self, offered: Money, context: DecisionContext) -> bool:
        if context.side == "seller":
            return offered >= self.accept_price
        return offered <= self.accept_price

    def _worse_than_walk_away(self, offered: Money,
                              context: DecisionContext) -> bool:
        if context.side == "seller":
            return offered < self.walk_away_price
        return offered > self.walk_away_price

    def current_ask(self, round_number: int) -> Money:
        """
        第 round_number 轮我还价时报多少。

        线性让步：第一次报 opening_ask，最后一轮报到 walk_away_price。
        """
        if self.max_rounds <= 1:
            return self.opening_ask

        capped = max(0, min(round_number, self.max_rounds - 1))
        ratio = capped / (self.max_rounds - 1)

        start = self.opening_ask.amount
        end = self.walk_away_price.amount
        return Money(start + (end - start) * ratio, self.currency)

    # ------------------------------------------------------------
    def evaluate(self, context: DecisionContext) -> Decision:
        """
        四条规则，按顺序判断。

        【为什么顺序很重要】
            「够线就接」必须排在「还价次数用完」前面 ——
            否则最后一轮即使价格合适也会被拒绝。
        """
        offered = context.offered_price

        # 货币必须一致 —— 否则比较没有意义（Money 会自己抛异常，
        # 但这里提前给出更好懂的说明）
        if offered.currency != self.accept_price.currency:
            return Decision(
                decision_type=DecisionType.REJECT,
                reason=(
                    f"报价货币 {offered.currency} 与我的策略货币 "
                    f"{self.accept_price.currency} 不一致"
                ),
                policy=self.name,
            )

        # ---- 规则 1：达到接受线，且（已经还过价 或 允许接受首报）----
        can_accept = context.negotiation_round >= 1 or self.accept_first_offer
        if self._meets_accept_line(offered, context) and can_accept:
            return Decision(
                decision_type=DecisionType.ACCEPT,
                price=offered,
                reason=(
                    f"报价 {offered} 达到接受线 {self.accept_price}"
                    f"（第 {context.negotiation_round} 轮）"
                ),
                policy=self.name,
            )

        # ---- 规则 2：比底线还差，直接拒绝 ----
        if self._worse_than_walk_away(offered, context):
            return Decision(
                decision_type=DecisionType.REJECT,
                reason=(
                    f"报价 {offered} 已经越过底线 {self.walk_away_price}"
                ),
                policy=self.name,
            )

        # ---- 规则 3：还价次数用完，不再无限还价 ----
        if context.rounds_exhausted:
            return Decision(
                decision_type=DecisionType.REJECT,
                reason=(
                    f"已经还价 {context.negotiation_round} 次"
                    f"（上限 {self.max_rounds}），且报价 {offered} "
                    f"仍未达到接受线 {self.accept_price}"
                ),
                policy=self.name,
            )

        # ---- 规则 4：还价 ----
        ask = self.current_ask(context.negotiation_round)

        # 还价不能超过预算（协议 §14：price <= budget）
        if context.budget is not None and ask > context.budget:
            ask = context.budget

        # 【这里刻意不做「还价不能比对方报的更差」的修正】
        #
        #     曾经写过这样一段：
        #         卖方还价 60，对方报了 100 → 把还价抬到 100
        #     看上去"合理"，其实是错的 —— 那等于**直接接受**，
        #     只是伪装成还价，而且会让谈判永远停在第一轮。
        #
        #     正确理解是：
        #         budget 是买方的**上限**，不是"桌上的价格"。
        #         卖方报 60 是在**给自己的价格命名**，
        #         低于对方上限完全正常（也留出了对方还价的空间）。
        #
        #     如果某个 Agent 想"对方给的已经超过我要的，立刻拿下"，
        #     把 accept_first_offer 设成 True 就行 —— 那是它的选择。

        return Decision(
            decision_type=DecisionType.COUNTER_OFFER,
            price=ask,
            reason=(
                f"第 {context.negotiation_round + 1} 轮还价："
                f"报价 {offered} 未达到接受线 {self.accept_price}，"
                f"还价 {ask}"
            ),
            policy=self.name,
        )

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "policy": self.name,
            "accept_price": str(self.accept_price),
            "opening_ask": str(self.opening_ask),
            "walk_away_price": str(self.walk_away_price),
            "max_rounds": self.max_rounds,
            "accept_first_offer": self.accept_first_offer,
            "currency": self.currency,
            "calls_llm": False,
        }

    def __repr__(self) -> str:
        return (
            f"RuleBasedDecisionPolicy(accept={self.accept_price}, "
            f"ask={self.opening_ask}, walk_away={self.walk_away_price}, "
            f"max_rounds={self.max_rounds})"
        )

"""
Economic Context —— Agent 当前**可以观察到的**经济事实
========================================================

【它回答的问题】

    「此刻，我这个 Agent 面对的经济世界是什么样？」

【🔴 它不是 Economy Policy，也不是经济人格】

    ❌ 不写「你应该省钱」
    ❌ 不写「优先用便宜模型」
    ❌ 不写「避免花费」
    ❌ 不写 profit_target / economic_score / recommended_model / 任何指令

    ✅ 只陈述事实。**Agent 拿这些事实做什么，是 Agent 自己的事。**

    依据（任务书 §九）：

        Context 可以写：
            Economic context:
            Currency: AIC
            Current balance: 99.42
        但禁止加入：
            You should save AIC. / You must minimize costs. / Prefer cheap models.

【🔴 数据来源：Ledger，且只有 Ledger】

        Ledger / Economy
              ↓            ← 本节唯一的依赖方向
        EconomicContextBuilder
              ↓
        CognitiveContext.economic_context
              ↓
        LLM（Provider 只收到最终渲染结果，不知道 Economy 存在）

    三个明确的**禁止来源**：
        ❌ Self Model —— 它可能过期，而且它**不许**成为 Ledger 的第二事实源
        ❌ LLM       —— 模型不能凭空产生经济事实
        ❌ 实验脚本   —— 不能猜一个余额填进去

【🔒 为什么 Builder 收的是「回调」而不是 Ledger 对象】

    与 `WorkspaceGateway` 的 `is_participant` 同一种手法：

        cognition/  不 import core/ledger.py
        main.py     注入三个**只读**取值函数

    这样认知层**在结构上无法**修改经济状态 —— 它拿到的是值，不是账本。
    而且 `cognition/` 可以脱离整个 Economy 单独测试。

【⚠️ 当前可靠可得的字段（§十一 要求如实区分）】

    ✅ current_balance        Ledger.balance_of() —— 权威、实时、可靠
    ✅ last_call / recent_calls
                              llm_calls 的只读摘要（token / cost_ai /
                              token_source）
    ✅ current_task_usage     llm_calls 按 task_id 的聚合
    ✅ current_session_usage  llm_calls 按 session 起点的聚合
    ⚠️ current_task_cost      旧字段；若未注入 usage 回调则依赖
                              `cost_of_task`，否则如实 unavailable
    ⚠️ current_session_cost   旧字段；若未注入 usage 回调则依赖
                              `cost_since`，否则如实 unavailable

    取不到的字段进 `unavailable`，**渲染时明确写出来**，
    而不是默默少一行 —— 少了哪一行必须能被看见。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.identity import utc_now_iso

#: 默认货币。真实值由组合根从 `economy.currency` 注入，不在这里硬编码业务。
DEFAULT_CURRENCY = "AIC"

#: 渲染时用的事实说明。
#:
#: 🔒 刻意只解释**这个单位是什么**，不解释「你应该怎么对待它」。
CURRENCY_NOTE = (
    "AIC is the Agent's internal production currency used to account "
    "for AI production resources consumed by this Agent."
)


@dataclass(frozen=True)
class EconomicContext:
    """
    一次 Cognition 时点的经济事实快照。

    🔒 **它是快照，不是账本。** 每次建立 Cognitive Context 时重新取值 ——
       绝不缓存、绝不在 Agent 初始化时写死（§十）。
    """

    currency: str = DEFAULT_CURRENCY
    #: None = 当前取不到（**不是** 0）。0 是一个真实的余额。
    current_balance: Optional[float] = None
    current_task_cost: Optional[float] = None
    current_session_cost: Optional[float] = None

    #: 最近一次 LLM 调用（来自 llm_calls）：
    #: call_id · total_tokens · cost_ai · cost_known · token_source。
    last_call: Optional[Dict[str, Any]] = None
    #: 最近若干次调用（newest first），只读摘要。
    recent_calls: Tuple[Dict[str, Any], ...] = ()
    #: 当前 Task 的 token / AIC 聚合（来自 llm_calls）。
    current_task_usage: Optional[Dict[str, Any]] = None
    #: 当前 Session 的 token / AIC 聚合（来自 llm_calls）。
    current_session_usage: Optional[Dict[str, Any]] = None

    #: 快照时刻（UTC ISO）。有了它才能回答「这个数字是什么时候的」。
    as_of: str = ""

    #: 本次快照里**取不到**的字段名。§十一：如实报告，不伪造。
    unavailable: Tuple[str, ...] = ()

    #: 🔴 §十三：与这个 Agent 有关、且**还在流程里**的生产性委托。
    #:
    #: 每项形如：
    #:     {"offer_id": ..., "role": "requester"|"provider",
    #:      "counterparty": ..., "price_aic": 5.0, "status": "OFFERED"}
    #:
    #: 🔒 **只提供事实**（§十三 / §九）：
    #:    它绝不会写「你应该接受 / 拒绝」，也不会写「这个价偏高」。
    #:    判断价格是不是划算，是 Agent 自己的事。
    #:
    #: ⚠️ 这一项是**委托**，不是 Communication —— 见 `delegation/` 的
    #:    「Communication is not commerce」。
    pending_offers: Tuple[Dict[str, Any], ...] = ()

    #: 🔴 Task Production Budget V1（§七）：这个 Task 的生产资源边界。
    #:
    #: 形如：
    #:     {"budget_aic": 50.0, "spent_aic": 3.2, "remaining_aic": 46.8,
    #:      "mode": "OBSERVE_ONLY", "currency": "AIC",
    #:      "is_exhausted": False}
    #:
    #: 🔒 **只提供事实**（§七 明令禁止）：
    #:    ❌「你应该省钱」❌「必须控制成本」❌「预算不足要停止」
    #:    ❌「尽量少调用」❌「必须外包」
    #:
    #: ⚠️ `mode == OBSERVE_ONLY` 时预算**不阻断**任何东西 ——
    #:    即使超预算，Runtime 仍可继续生产。
    task_budget: Optional[Dict[str, Any]] = None

    #: 各字段的来源（可追溯）。全部指向系统组件，不指向 Self Model / LLM。
    sources: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return self.current_balance is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "currency": self.currency,
            "current_balance": self.current_balance,
            "current_task_cost": self.current_task_cost,
            "current_session_cost": self.current_session_cost,
            "last_call": dict(self.last_call) if self.last_call else None,
            "recent_calls": [dict(c) for c in self.recent_calls],
            "current_task_usage": (dict(self.current_task_usage)
                                   if self.current_task_usage else None),
            "current_session_usage": (dict(self.current_session_usage)
                                      if self.current_session_usage else None),
            "as_of": self.as_of,
            "unavailable": list(self.unavailable),
            "sources": dict(self.sources),
            "pending_offers": [dict(o) for o in self.pending_offers],
            "task_budget": dict(self.task_budget) if self.task_budget else None,
        }

    # ------------------------------------------------------------
    def to_lines(self) -> List[str]:
        """
        渲染成**纯事实**的若干行。

        🔒 逐条自检（任务书 §九）：
            这里没有任何一句在告诉 Agent 该怎么做。
            取不到的字段**显式写出来**，不静默省略 ——
            否则「成本是 0」和「成本取不到」看起来一样。
        """
        lines = ["Economic context:", f"Currency: {self.currency}"]

        if self.current_balance is None:
            lines.append("Current balance: not currently available")
        else:
            lines.append(f"Current balance: {self.current_balance:.4f} "
                         f"{self.currency}")

        if self.current_task_cost is None:
            lines.append("Current task cost: not currently available")
        else:
            lines.append(f"Current task cost: {self.current_task_cost:.4f} "
                         f"{self.currency}")

        if self.current_session_cost is None:
            lines.append("Current session cost: not currently available")
        else:
            lines.append(f"Current session cost: "
                         f"{self.current_session_cost:.4f} {self.currency}")

        if self.last_call:
            lc = self.last_call
            known = bool(lc.get("cost_known", True))
            tokens = lc.get("total_tokens")
            tokens_txt = (f"{int(tokens)} tokens"
                          if known and tokens is not None else "tokens unknown")
            cost = lc.get("cost_ai")
            cost_txt = (f"{float(cost):.6f} {self.currency}"
                        if known and cost is not None else "cost unknown")
            lines.append(
                f"Last LLM call: {tokens_txt}, {cost_txt}, "
                f"source={lc.get('token_source') or 'unknown'}")

        if self.recent_calls:
            lines.append("Recent LLM calls (newest first):")
            for c in self.recent_calls:
                known = bool(c.get("cost_known", True))
                tokens = c.get("total_tokens")
                cost = c.get("cost_ai")
                tokens_txt = (f"{int(tokens)} tok"
                              if known and tokens is not None else "tok=?")
                cost_txt = (f"{float(cost):.6f} AIC"
                            if known and cost is not None else "AIC=?")
                lines.append(
                    f"- {c.get('call_id')}: {tokens_txt}, {cost_txt}, "
                    f"source={c.get('token_source') or 'unknown'}, "
                    f"model={c.get('provider') or '?'}/"
                    f"{c.get('model') or '?'}")

        if self.current_task_usage:
            u = self.current_task_usage
            lines.append(
                f"Current task usage: {int(u.get('calls') or 0)} calls, "
                f"{int(u.get('total_tokens') or 0)} tokens, "
                f"{float(u.get('cost_ai') or 0.0):.6f} {self.currency}")
            if int(u.get("unknown_calls") or 0):
                lines.append(
                    f"  ({int(u.get('unknown_calls'))} calls have unknown tokens)")
        if self.current_session_usage:
            u = self.current_session_usage
            lines.append(
                f"Current session usage: {int(u.get('calls') or 0)} calls, "
                f"{int(u.get('total_tokens') or 0)} tokens, "
                f"{float(u.get('cost_ai') or 0.0):.6f} {self.currency}")
            if int(u.get("unknown_calls") or 0):
                lines.append(
                    f"  ({int(u.get('unknown_calls'))} calls have unknown tokens)")

        # §十三：待处理的委托 —— 只列事实，不给建议
        if self.pending_offers:
            lines.append("Pending task delegations involving you:")
            for o in self.pending_offers:
                price = o.get("price_aic")
                price_txt = (f"{float(price):.4f} {self.currency}"
                             if price is not None else "no price yet")
                lines.append(
                    f"- {o.get('offer_id')}: your role is "
                    f"{o.get('role')}, counterparty "
                    f"{o.get('counterparty')}, status {o.get('status')}, "
                    f"price {price_txt}")
        else:
            lines.append("Pending task delegations involving you: none")

        # §七：Task 生产预算 —— 同样只陈述事实
        if self.task_budget:
            tb = self.task_budget
            cur = tb.get("currency") or self.currency

            def _amt(key: str) -> str:
                v = tb.get(key)
                return ("not currently available" if v is None
                        else f"{float(v):.4f} {cur}")

            lines.append("Task production budget:")
            lines.append(f"Budget: {_amt('budget_aic')}")
            lines.append(f"Spent: {_amt('spent_aic')}")
            lines.append(f"Remaining: {_amt('remaining_aic')}")
            lines.append(f"Mode: {tb.get('mode')}")

        lines.append(CURRENCY_NOTE)
        if self.as_of:
            lines.append(f"(Facts read from the Ledger at {self.as_of}.)")
        return lines


# ================================================================
# Builder
# ================================================================
class EconomicContextBuilder:
    """
    从**注入的只读取值函数**建立 `EconomicContext`。

    🔒 三个回调都是**纯读**的。Builder 不持有 Ledger、不持有 Economy、
       **不能**修改任何经济状态 —— 结构上就做不到。

    参数：
        balance_of(agent_id) -> float | None
            这个 Agent 当前的真实余额（权威源：Ledger）。
        cost_of_task(task_id) -> float | None
            某个 Task 已消耗的 AIC。没有 task 维度数据时返回 None。
        cost_since(agent_id, since_iso) -> float | None
            某个 Agent 从 `since_iso` 起消耗的 AIC（会话维度的近似）。
        recent_calls_of(agent_id, limit) -> list | None
            最近 N 次 `llm_calls` 摘要（token / cost / source）。
        usage_of_task(task_id) -> dict | None
            某个 Task 的 token / AIC / 调用次数聚合。
        usage_since(agent_id, since_iso) -> dict | None
            某个 Agent 从 `since_iso` 起的用量聚合。
        currency
            货币单位，由组合根从 economy.currency 注入。
    """

    def __init__(
        self,
        *,
        balance_of: Optional[Callable[[str], Optional[float]]] = None,
        cost_of_task: Optional[Callable[[str], Optional[float]]] = None,
        cost_since: Optional[Callable[[str, str], Optional[float]]] = None,
        recent_calls_of: Optional[Callable[[str, int], Optional[list]]] = None,
        usage_of_task: Optional[Callable[[str], Optional[dict]]] = None,
        usage_since: Optional[Callable[[str, str], Optional[dict]]] = None,
        pending_offers_of: Optional[Callable[[str], Optional[list]]] = None,
        task_budget_of: Optional[Callable[[str], Optional[dict]]] = None,
        currency: str = DEFAULT_CURRENCY,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.balance_of = balance_of
        self.cost_of_task = cost_of_task
        self.cost_since = cost_since
        #: LLM 用量（只读）：最近调用 / task 聚合 / session 聚合。
        self.recent_calls_of = recent_calls_of
        self.usage_of_task = usage_of_task
        self.usage_since = usage_since
        #: §十三：与某个 Agent 相关的未决委托。同样是**只读**回调。
        self.pending_offers_of = pending_offers_of
        #: §七：Task 生产预算（按 task_id 取）。同样是**只读**回调。
        self.task_budget_of = task_budget_of
        self.currency = currency or DEFAULT_CURRENCY
        self.clock = clock

    # ------------------------------------------------------------
    def build(
        self,
        agent_id: str,
        *,
        task_id: Optional[str] = None,
        session_started_at: Optional[str] = None,
    ) -> EconomicContext:
        """
        取一次**实时**快照。

        🔒 每次调用都重新问 Ledger —— 不缓存、不记忆。
           所以一次真实成本发生之后，下一次 Context 里的 balance 就会变（§十）。
        """
        unavailable: List[str] = []
        sources: Dict[str, str] = {}

        balance = self._safe(self.balance_of, agent_id)
        if balance is None:
            unavailable.append("current_balance")
        else:
            sources["current_balance"] = "Ledger.balance_of"

        recent: List[Dict[str, Any]] = []
        if self.recent_calls_of is not None:
            recent = [
                dict(x) for x in self._safe_list(
                    self.recent_calls_of, agent_id, 5)
                if isinstance(x, dict)
            ]
        last_call = recent[0] if recent else None

        task_usage: Optional[Dict[str, Any]] = None
        task_cost: Optional[float] = None
        if task_id:
            if self.usage_of_task is not None:
                task_usage = self._safe_dict(self.usage_of_task, task_id)
                if task_usage is not None:
                    value = task_usage.get("cost_ai")
                    if isinstance(value, (int, float)):
                        task_cost = float(value)
                        sources["current_task_cost"] = \
                            "llm_calls aggregate by task_id"
                else:
                    unavailable.append("current_task_usage")
            if task_cost is None and self.cost_of_task is not None:
                task_cost = self._safe(self.cost_of_task, task_id)
                if task_cost is not None:
                    sources["current_task_cost"] = "llm_calls by task_id"
            if task_cost is None:
                unavailable.append("current_task_cost")
        else:
            # 没有 task_id 就没有「任务成本」这个概念可言 —— 如实报告
            unavailable.append("current_task_cost")

        session_usage: Optional[Dict[str, Any]] = None
        session_cost: Optional[float] = None
        if session_started_at:
            if self.usage_since is not None:
                session_usage = self._safe_dict(
                    self.usage_since, agent_id, session_started_at)
                if session_usage is not None:
                    value = session_usage.get("cost_ai")
                    if isinstance(value, (int, float)):
                        session_cost = float(value)
                        sources["current_session_cost"] = \
                            "llm_calls aggregate since session start"
                else:
                    unavailable.append("current_session_usage")
            if session_cost is None and self.cost_since is not None:
                session_cost = self._safe(
                    self.cost_since, agent_id, session_started_at)
                if session_cost is not None:
                    sources["current_session_cost"] = \
                        "llm_calls since session start"
            if session_cost is None:
                unavailable.append("current_session_cost")
        else:
            unavailable.append("current_session_cost")

        pending = self._safe_list(self.pending_offers_of, agent_id)
        budget = self._safe_dict(self.task_budget_of, task_id) \
            if task_id else None

        if recent:
            sources["last_call"] = "llm_calls recent"
            sources["recent_calls"] = "llm_calls recent"

        return EconomicContext(
            currency=self.currency,
            pending_offers=tuple(pending),
            task_budget=budget,
            current_balance=balance,
            current_task_cost=task_cost,
            current_session_cost=session_cost,
            last_call=last_call,
            recent_calls=tuple(recent),
            current_task_usage=task_usage,
            current_session_usage=session_usage,
            as_of=self.clock(),
            unavailable=tuple(unavailable),
            sources=sources,
        )

    # ------------------------------------------------------------
    @staticmethod
    def _safe_list(fn: Optional[Callable], *args: Any) -> list:
        """
        调一个只读的「列表」取值函数，**失败就是空列表**。

        🔒 委托列表是**附加信息**，取不到不该让整段经济事实消失
           —— 所以这里退化成「没有待处理委托」，而余额那一行照常。
        """
        if fn is None:
            return []
        try:
            value = fn(*args)
        except Exception:                                     # noqa: BLE001
            return []
        if not isinstance(value, (list, tuple)):
            return []
        return [dict(v) for v in value if isinstance(v, dict)]

    @staticmethod
    def _safe_dict(fn: Optional[Callable], *args: Any) -> Optional[dict]:
        """调一个只读的「字典」取值函数，**失败就是 None**（不是空 dict）。"""
        if fn is None:
            return None
        try:
            value = fn(*args)
        except Exception:                                     # noqa: BLE001
            return None
        return dict(value) if isinstance(value, dict) else None

    @staticmethod
    def _safe(fn: Optional[Callable], *args: Any) -> Optional[float]:
        """
        调一个只读取值函数，**失败就是 None，不猜**。

        🔒 经济事实取不到时必须说「取不到」——
           绝不返回 0.0 顶替。0 是一个真实的余额，
           把「不知道」写成 0 会让 Agent 以为自己破产了。
        """
        if fn is None:
            return None
        try:
            value = fn(*args)
        except Exception:                                     # noqa: BLE001
            return None
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

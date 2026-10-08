"""
LLM Cost Accounting —— 把 token 消耗变成 AIC 成本
============================================================

【为什么需要它（§0.5 审计 A-02）】

    审计发现：`LLMCallRecorder.cost_ai` **恒为 0**（源码注释就是「还没算」）。
    后果是 §0.5.3「激励塑造行为」**结构性不成立** ——
    Agent 可以无限调用 LLM 而没有任何经济后果。

    本模块建立最小真实成本链：

        LLM Call → Cost Calculator → AIC Cost
                 → Ledger / Agent Economic State

【🔒 最重要的边界：成本规则 ≠ Agent Strategy】

    本模块**只回答**「这次调用花了多少 AIC」。

    它**不回答**、也**不许回答**：

        ❌ 「成本高，所以你应该少思考」
        ❌ 「超过 X AIC 就别调了」
        ❌ 「这么贵，换个便宜模型吧」

    那些是 **Agent Strategy**（§0.5.2 / §0.5.10 Q1）。

    系统只负责**告诉 Agent 花了多少**；
    「值不值得」是 Agent 自己的判断。

    → 有测试断言：本模块里**不出现**任何
      「应该 / 不要 / 必须 / 阈值 / 拒绝」这类决策词。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: 默认换算：每 1000 token 多少 AIC。
#: ⚠️ 这是一个**可配置的记账参数**，不是「合理价格」的判断。
DEFAULT_AI_PER_1K_TOKENS = 0.01


def _config_get(config: Any, dotted: str, default: Any) -> Any:
    """兼容 `core.config.Config`（认点号）与普通嵌套 dict。"""
    if config is None:
        return default
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(dotted, default)
        except Exception:                          # pragma: no cover
            return default
    return default


@dataclass(frozen=True)
class CostRule:
    """
    一条**明确、可配置**的成本换算规则。

    🔒 它只有数字，没有判断。改它不影响任何 Agent 的策略。
    """

    ai_per_1k_tokens: float = DEFAULT_AI_PER_1K_TOKENS

    #: 按 `"provider/model"` 覆盖费率（§五：不同模型可以不同价）。
    #: 🔒 仍然只是**数字**，不含任何「该不该用这个模型」的判断。
    #: 查不到就回落到 `ai_per_1k_tokens`。
    model_rates: Optional[Mapping] = None
    #: token 不知道时（API 没返回 usage）怎么算。
    #: `"zero"`   → 记 0（**但 cost_known=False**，调用方知道这是「不知道」）
    #: `"unknown"` → 同样记 0，语义更明确
    unknown_token_policy: str = "zero"

    @classmethod
    def from_resolved(cls, cost: Mapping[str, Any]) -> "CostRule":
        """
        从 **ResolvedConfig.cost** 构造（生产路径）。

        🔒 这里读的是**已经解析过的**配置段，不再自己去点号路径里找。
           `llm.cost.*` 那条旧路径在 Configuration V1 里已经不存在了 ——
           继续读它会让费率**静默退化成 Python 默认值**。

        🔒 只改配置**来源**，不改任何费率语义与 AIC 结算逻辑。
        """
        raw = dict(cost or {})
        rate = raw.get("ai_per_1k_tokens", None)
        try:
            rate_f = float(rate) if rate is not None else DEFAULT_AI_PER_1K_TOKENS
        except (TypeError, ValueError):
            rate_f = DEFAULT_AI_PER_1K_TOKENS
        policy = raw.get("unknown_token_policy", "zero")
        rates: Dict[str, float] = {}
        raw_rates = raw.get("model_rates") or {}
        if isinstance(raw_rates, Mapping):
            for k, v in raw_rates.items():
                try:
                    rates[str(k)] = max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
        return cls(ai_per_1k_tokens=max(0.0, rate_f),
                   model_rates=rates,
                   unknown_token_policy=str(policy or "zero"))

    @classmethod
    def from_config(cls, config: Any) -> "CostRule":
        """
        ⚠️ **DEPRECATED** —— 读的是已经废弃的 `llm.cost.*` 点号路径。

        保留只为：
          · Configuration V1 之前的旧调用方
          · 测试里想拿一份「纯默认值」规则（传 `{}`）

        🔒 生产路径必须用 `from_resolved()`。有守卫禁止
           `main.py` 再调用这个方法。
        """
        rate = _config_get(config, "llm.cost.ai_per_1k_tokens", None)
        policy = _config_get(config, "llm.cost.unknown_token_policy",
                             "zero")
        try:
            rate_f = float(rate) if rate is not None \
                else DEFAULT_AI_PER_1K_TOKENS
        except (TypeError, ValueError):            # pragma: no cover
            rate_f = DEFAULT_AI_PER_1K_TOKENS
        raw_rates = _config_get(config, "llm.cost.model_rates", {}) or {}
        rates = {}
        if isinstance(raw_rates, Mapping):
            for k, v in raw_rates.items():
                try:
                    rates[str(k)] = max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
        return cls(ai_per_1k_tokens=max(0.0, rate_f),
                   model_rates=rates,
                   unknown_token_policy=str(policy or "zero"))

    def rate_for(self, provider: Optional[str],
                 model: Optional[str]) -> float:
        """查这个 (provider, model) 的费率；查不到返回默认。"""
        rates = self.model_rates or {}
        if provider and model:
            hit = rates.get(f"{provider}/{model}")
            if hit is not None:
                return hit
        if model:
            hit = rates.get(str(model))
            if hit is not None:
                return hit
        return self.ai_per_1k_tokens

    def compute(self, *, input_tokens: Optional[int],
                output_tokens: Optional[int],
                total_tokens: Optional[int] = None,
                provider: Optional[str] = None,
                model: Optional[str] = None):
        """
        算出这次调用的 AIC 成本。

        返回 `(cost_ai, tokens_used, known)`。

        ⚠️ `known=False` 表示**token 拿不到** —— 此时成本记 0，
           但调用方**必须知道这是「不知道」而不是「免费」**。
           （这与项目既有的 token_source 三态是同一个原则。）
        """
        tokens = total_tokens
        if tokens is None:
            if input_tokens is None and output_tokens is None:
                return 0.0, 0, False
            tokens = int(input_tokens or 0) + int(output_tokens or 0)

        tokens = int(tokens)
        if tokens <= 0:
            # 0 token 是**真的 0**（比如空输入），成本确实是 0
            return 0.0, tokens, True

        cost = (tokens / 1000.0) * self.rate_for(provider, model)
        return round(cost, 6), tokens, True


class CostCalculator:
    """
    很薄的一层：`LLMResponse` / token 数 → AIC 成本。

    🔒 它不碰 LLM · 不碰 Agent · 不碰 Strategy · 不碰 Ledger。
       记账是 Ledger 的事；本类只算数。
    """

    def __init__(self, rule: Optional[CostRule] = None) -> None:
        self.rule = rule or CostRule()

    @classmethod
    def from_resolved(cls, cost: Mapping[str, Any]) -> "CostCalculator":
        """从 **ResolvedConfig.cost** 构造（生产路径）。"""
        return cls(CostRule.from_resolved(cost))

    @classmethod
    def from_config(cls, config: Any) -> "CostCalculator":
        """⚠️ **DEPRECATED** —— 见 `CostRule.from_config`。"""
        return cls(CostRule.from_config(config))

    def cost_of(self, response: Any) -> tuple:
        """从 `LLMResponse` 算成本 → `(cost_ai, tokens, known)`。"""
        if response is None:
            return 0.0, 0, False
        return self.rule.compute(
            input_tokens=getattr(response, "input_tokens", None),
            output_tokens=getattr(response, "output_tokens", None),
            total_tokens=getattr(response, "total_tokens", None),
            provider=getattr(response, "provider", None),
            model=getattr(response, "model", None),
        )

    def __repr__(self) -> str:
        return (f"CostCalculator(ai_per_1k_tokens="
                f"{self.rule.ai_per_1k_tokens})")

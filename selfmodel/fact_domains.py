"""
Fact Domain —— **哪些事实归系统所有，Self Model 不许当权威**
============================================================

【这个模块解决的真实漏洞】

    实测（commit a051a26 的只读审计）：

        走**正常**的 Controlled Write 路径，
        `create_claim(content="我的 AIC 余额是 43")`
        → `committed=True`，validation ok。

    也就是说 `selfmodel/models.py` 里写的那句

        ❌「我的 AIC = 43」   （这是 Ledger 的事实）

    **只是文档，代码上完全没有阻止。** 目前没出事，
    只是因为 `self_model_claims` 全仓为 0 行 —— 不是因为有机质拦住它。

    而且闭环是通的：写进去之后，下一轮 `retrieve()` 就会把它召回，
    渲染成「Relevant beliefs I currently hold about myself」。
    于是**同一个系统里会同时存在两个「余额」**：
    一个来自 Ledger（真），一个来自 Agent 自己的记忆（可能早已过时）。

【为什么不是关键词过滤】

    ❌ `if "AIC" in content: reject`

    这条规则会同时：
        · 漏掉「我的钱包里有四十三枚」
        · 误杀「我发现自己过去经常过度消耗 AIC」（§五 **明确要求允许**）

    脆弱的关键词表既挡不住变体，又会禁止真正属于 Agent 的认知。

【✅ 本模块的做法：让「归属」成为可枚举的声明】

    一个 Claim / Strategy 要么是 **Agent 对自己的认识**（`self`），
    要么在**断言某个系统拥有的事实**。后者不是「说得对不对」的问题，
    而是**它根本不该由 Self Model 来断言** —— 系统自己知道答案。

    所以本模块把这条边界写成一张**表**：

        domain           谁拥有它（权威源）            能不能写进 Self Model
        ─────────────────────────────────────────────────────────────────
        self             Agent 自己                    ✅ 允许
        economy          Ledger / Economy              ❌ 拒绝
        identity         Identity（非 Address）        ❌ 拒绝
        capability       AgentDirectory / Capability   ❌ 拒绝
        authorization    Workspace Lock / Gateway      ❌ 拒绝

    🔒 这张表是**唯一**的判定依据。校验逻辑里没有散落的 `if domain == ...`，
       以后新增一类系统事实只需在这里加一行，而不用改 write service。

【⚠️ 已知残余风险（如实记录，不隐瞒）】

    声明由 Agent（LLM）给出，所以**一个故意把 economy 写成 self 的模型
    仍然能绕过去**。彻底关掉它需要「结构化 Claim（谓词 + 主体 + 值）」
    或 NLP 语义审查 —— 前者是大改，后者被任务书 §四 明令禁止。

    所以 V1 保证的是：

        ✅ 未声明的断言          → 拒绝（`missing_fact_domain`）
        ✅ 声明为系统事实域      → 拒绝（`system_fact_not_self_owned`）
        ⚠️ 谎报为 self           → **拦不住**（见报告 Open Question）

    这个残余风险不是「没想到」，而是**明确选择了不做**：
    在 V1 里，一个能被说清楚的边界比一个假装完备的过滤器更有价值。
"""

from __future__ import annotations

from typing import Dict, Tuple

#: Agent 对自己的认识 —— **唯一允许**写进 Self Model 的域。
#:
#: 例如（§五 明确要求允许）：
#:     ·「我过去在长篇任务中经常过早消耗资源。」
#:     ·「我发现前期探索阶段使用昂贵模型的收益较低。」
#:     ·「我倾向于在早期生产阶段减少高成本探索。」
FACT_DOMAIN_SELF = "self"

#: 系统拥有的事实域 —— Self Model **不许**成为它们的权威来源。
FACT_DOMAIN_ECONOMY = "economy"
FACT_DOMAIN_IDENTITY = "identity"
FACT_DOMAIN_CAPABILITY = "capability"
FACT_DOMAIN_AUTHORIZATION = "authorization"

#: 🔒 系统事实域 → **谁才是权威**。
#:
#:    这个映射不是为了「去查一下」，而是为了让拒绝理由能说清楚
#:    「这件事该问谁」。一条只回 `rejected` 的拒绝，
#:    对 Agent 来说是不可学习的。
SYSTEM_FACT_AUTHORITY: Dict[str, str] = {
    FACT_DOMAIN_ECONOMY: "Ledger / Economy（core/ledger.py）",
    FACT_DOMAIN_IDENTITY: "Agent Identity（core/identity.py）",
    FACT_DOMAIN_CAPABILITY: "AgentDirectory / Capability（core/capability.py）",
    FACT_DOMAIN_AUTHORIZATION: "Workspace Lock / Gateway（services/workspace_gateway.py）",
}

#: 全部合法取值。
VALID_FACT_DOMAINS: Tuple[str, ...] = (
    FACT_DOMAIN_SELF, *sorted(SYSTEM_FACT_AUTHORITY),
)


def is_system_domain(domain: str) -> bool:
    """这个域是不是系统拥有的（即 Self Model 不许断言）。"""
    return str(domain or "").strip().lower() in SYSTEM_FACT_AUTHORITY


def authority_of(domain: str) -> str:
    """这个域的真权威在哪（用于拒绝理由）。不是系统域则返回空串。"""
    return SYSTEM_FACT_AUTHORITY.get(str(domain or "").strip().lower(), "")


def normalize(domain: object) -> str:
    """归一化一个声明值。**不做模糊匹配** —— 只去空白、转小写。"""
    return str(domain or "").strip().lower()

"""
LLM Call Recorder —— 把每次模型调用记进账
============================================================

【为什么要有这个文件】

    在普通 Agent 项目里，调用 LLM 是免费的。
    在这个项目里，调用 LLM 是【经营成本】。

    而成本要能算，前提是「调用了多少 token」这件事被记下来了。
    所以每次调用都必须留下一条记录 —— 这是整个经济系统的基础数据。

【本阶段只做一件事：记录 token】

    记：用了多少 token、花了多少时间、用的哪个模型
    不记：折算成多少 AIC

    为什么不在这一步算钱？
        因为「token → AIC」的换算规则（tokens_per_ai_coin）
        你随时可能调整，而成本计算还牵扯任务、合同、结算。
        那是 Step 6 的事。这一步只负责把原料准备好。

【为什么还要存 tokens_per_ai_coin 的快照】

    cost_ai 由 `llm.cost.CostCalculator` 算出（§0.5 审计 A-02）。

    🔒 **成本规则不是 Agent Strategy。** 本模块只说「花了多少 AIC」，
       不说「所以你该少思考」。见 `llm/cost.py` 的模块 docstring。
    但汇率快照【现在就存】。

    因为如果你今天用 1:10000 跑了一批记录，明天改成 1:5000，
    没有快照的话，你永远无法回头算出「当时那批调用值多少钱」。
    有了快照，Step 6 可以精确地补算历史账。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.identity import new_message_id, utc_now_iso
from core.logging_setup import get_logger
from llm.base import TOKEN_SOURCE_UNKNOWN, LLMResponse
from storage.database import Database
from storage.models import TABLE_LLM_CALLS

logger = get_logger(__name__)


def new_call_id() -> str:
    """生成调用记录 ID，形如 call_3f9a2b1c。"""
    return new_message_id("call")


def _as_int(value: Optional[int]) -> int:
    """
    把可能为 None 的 token 数转成 int。

    None 的含义是「不知道用了多少 token」。
    数据库列是 NOT NULL，所以这里用 0 占位，
    真正表达「不知道」的是同一行的 token_source = 'unknown'。

    为什么不在这里估算一个数？
        因为估算值会被误当成真实值，然后污染整个成本计算。
        宁可留一个明确的「不知道」。
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class LLMCallRecorder:
    """
    把 LLMResponse 写进 llm_calls 表。

    用法：
        recorder = LLMCallRecorder(db, tokens_per_ai_coin=10000)
        recorder.record(agent_id="agent_a", response=response)

    这个类【只写不改】：
        没有 update / delete 方法。
        调用记录和账本一样，是历史事实，不该被追改。
    """

    def __init__(self, db: Database, tokens_per_ai_coin: Optional[int] = None,
                 cost_calculator: Optional[Any] = None,
                 on_cost: Optional[Any] = None):
        self.db = db
        # 汇率快照。可以为 None —— 那说明系统还没配置好经济参数，
        # 这时记 0，表示「本次调用没有对应的汇率上下文」。
        self.tokens_per_ai_coin = int(tokens_per_ai_coin or 0)

        #: 算 AIC 成本的规则（§0.5 审计 A-02）。
        if cost_calculator is None:
            from llm.cost import CostCalculator
            cost_calculator = CostCalculator()
        self.cost_calculator = cost_calculator

        #: 成本入账回调：`(agent_id, cost_ai, call_id, row) -> None`。
        #: 🔒 用回调是为了**不让 llm/ 反向依赖 core/**（依赖方向：
        #:    main.py 组装，core/ 实现，llm/ 只发通知）。
        self.on_cost = on_cost

    # ------------------------------------------------------------
    # 写
    # ------------------------------------------------------------
    def record(
        self,
        agent_id: str,
        response: Optional[LLMResponse] = None,
        task_id: Optional[str] = None,
        success: bool = True,
        error: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        credential_ref: Optional[str] = None,
        credential_source: Optional[str] = None,
        credential_env: Optional[str] = None,
    ) -> str:
        """
        记录一次调用，返回 call_id。

        参数：
            response: 成功时传 LLMResponse；失败时可以是 None，
                      但要自己传 provider / model / error

        为什么失败也要记？
            因为「这次调用失败了」本身是重要的经济信息 ——
            失败的调用可能也消耗了 token（比如输出被截断），
            而且失败率会影响 Agent 的声誉和它要不要换模型。
        """
        call_id = new_call_id()
        # 先算成本（纯函数，不碰 Strategy / Ledger）
        if response is not None:
            cost_ai, _cost_tokens, _cost_known = \
                self.cost_calculator.cost_of(response)
        else:
            cost_ai = 0.0

        if response is not None:
            row_provider = response.provider
            row_model = response.model
            # token 可能是 None（API 没返回 usage）。
            # 数据库列是 NOT NULL，所以落库时用 0 占位 ——
            # 但 token_source 会如实记下「这个 0 是不知道，不是真的 0」。
            input_tokens = _as_int(response.input_tokens)
            output_tokens = _as_int(response.output_tokens)
            total_tokens = _as_int(response.total_tokens)
            latency_ms = float(response.latency_ms or 0.0)
            simulated = 1 if response.simulated else 0
            token_source = response.token_source
        else:
            row_provider = provider or "unknown"
            row_model = model or ""
            input_tokens = 0
            output_tokens = 0
            total_tokens = 0
            latency_ms = 0.0
            simulated = 0
            # 调用都失败了，自然拿不到 usage
            token_source = TOKEN_SOURCE_UNKNOWN

        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_LLM_CALLS} (
                    call_id, agent_id, provider, model,
                    input_tokens, output_tokens, total_tokens,
                    latency_ms, simulated, token_source,
                    tokens_per_ai_coin, cost_ai, api_cost_usd,
                    task_id, success, error, created_at,
                    requested_model, response_model,
                    requested_cognitive_budget, actual_max_tokens,
                    finish_reason, output_status,
                    requested_credential_ref, requested_credential_source,
                    requested_credential_env
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    agent_id,
                    row_provider,
                    row_model,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    latency_ms,
                    simulated,
                    token_source,
                    self.tokens_per_ai_coin,
                    # cost_ai / api_cost_usd 本阶段【故意留 0】。
                    # 0 在这里的含义是「还没算」，不是「免费」。
                    # 算钱的逻辑在 Step 6 的 Cost Calculator 里。
# ✅ 现已由 llm.cost.CostCalculator 算出（§0.5 审计 A-02）。
                    cost_ai,
                    # api_cost_usd：Provider 侧真实美元账单，本阶段仍留 0
                    0.0,
                    task_id,
                    1 if success else 0,
                    error,
                    utc_now_iso(),
                    # 🔴 可观测性补全。取不到就是 None —— **不猜、不回填**。
                    getattr(response, "requested_model", None),
                    getattr(response, "model", None),
                    getattr(response, "requested_cognitive_budget", None),
                    getattr(response, "actual_max_tokens", None),
                    getattr(response, "finish_reason", None),
                    getattr(response, "output_status", None),
                    credential_ref,
                    credential_source,
                    credential_env,
                ),
            )

        # 成本入账（§0.5 审计 A-02 / Test 7+8）。
        # 记账失败不能让调用记录丢失，所以只记日志、不抛出。
        if cost_ai > 0 and self.on_cost is not None:
            try:
                self.on_cost(agent_id, cost_ai, call_id,
                             {"provider": row_provider,
                              "model": row_model, "task_id": task_id})
            except Exception as exc:                  # noqa: BLE001
                logger.warning("LLM 成本入账失败 %s: %s", call_id, exc)

        logger.debug(
            "记录 LLM 调用 %s：%s/%s %d tokens",
            call_id,
            row_provider,
            row_model,
            total_tokens,
        )
        return call_id

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------
    def recent(
        self,
        agent_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """最近的调用记录。"""
        if agent_id:
            rows = self.db.fetchall(
                f"""
                SELECT * FROM {TABLE_LLM_CALLS}
                WHERE agent_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (agent_id, limit),
            )
        else:
            rows = self.db.fetchall(
                f"""
                SELECT * FROM {TABLE_LLM_CALLS}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            )

        return [dict(row) for row in rows]

    def count(self, agent_id: Optional[str] = None) -> int:
        """调用次数。"""
        if agent_id:
            row = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_LLM_CALLS} WHERE agent_id = ?",
                (agent_id,),
            )
        else:
            row = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_LLM_CALLS}"
            )
        return int(row["n"]) if row else 0

    def summary(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """
        汇总某个 Agent（或全系统）的 token 消耗。

        【重要】这里只汇总 token，不汇总钱。
            以后 Economy 会拿这个数字去算 AIC 成本。
        """
        if agent_id:
            row = self.db.fetchone(
                f"""
                SELECT
                    COUNT(*)                        AS calls,
                    COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                    COALESCE(SUM(latency_ms), 0)    AS latency_ms,
                    COALESCE(SUM(success), 0)       AS successes,
                    COALESCE(SUM(simulated), 0)     AS simulated_calls,
                    COALESCE(SUM(token_source = 'api'), 0)       AS api_calls,
                    COALESCE(SUM(token_source = 'estimated'), 0) AS estimated_calls,
                    COALESCE(SUM(token_source = 'unknown'), 0)   AS unknown_calls
                FROM {TABLE_LLM_CALLS}
                WHERE agent_id = ?
                """,
                (agent_id,),
            )
        else:
            row = self.db.fetchone(
                f"""
                SELECT
                    COUNT(*)                        AS calls,
                    COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                    COALESCE(SUM(latency_ms), 0)    AS latency_ms,
                    COALESCE(SUM(success), 0)       AS successes,
                    COALESCE(SUM(simulated), 0)     AS simulated_calls,
                    COALESCE(SUM(token_source = 'api'), 0)       AS api_calls,
                    COALESCE(SUM(token_source = 'estimated'), 0) AS estimated_calls,
                    COALESCE(SUM(token_source = 'unknown'), 0)   AS unknown_calls
                FROM {TABLE_LLM_CALLS}
                """
            )

        if row is None:
            return {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "latency_ms": 0.0,
                "failures": 0,
                "simulated_calls": 0,
                "api_calls": 0,
                "estimated_calls": 0,
                "unknown_calls": 0,
            }

        calls = int(row["calls"])
        successes = int(row["successes"])

        return {
            "calls": calls,
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "latency_ms": float(row["latency_ms"]),
            "failures": calls - successes,
            "simulated_calls": int(row["simulated_calls"]),
            # token 数据的可信度分布。
            # 只有 api_calls 那些记录的 token 数是可以拿去算钱的。
            "api_calls": int(row["api_calls"]),
            "estimated_calls": int(row["estimated_calls"]),
            "unknown_calls": int(row["unknown_calls"]),
        }

    def __repr__(self) -> str:
        return (
            f"LLMCallRecorder(calls={self.count()}, "
            f"tokens_per_ai_coin={self.tokens_per_ai_coin})"
        )

#!/usr/bin/env python3
"""
Economy Awareness V1 —— 最小真实 Runtime 实验（§十四）
========================================================

    ./.venv/bin/python experiments/novel_production/economy_probe.py

【实验设计】

    Run A：Agent **有** Economic Context（余额等事实进入 Cognitive Context）
    Run B：同一个 Agent、同一个任务、**关掉** Economic Context

    两组唯一差别就是那一段经济事实。

【🔒 绝对不能做的事】

    ❌ 不告诉 Agent「请考虑 AIC」
    ❌ 不给它「省钱策略」
    ❌ 不预写 `Strategy = minimize cost`
    ❌ 不预写 `Claim = AIC is scarce`

    否则测的就不是「给它经济事实以后它自然会不会把经济事实纳入认知」，
    而是「我提示了它，它照做了没有」。

【观察什么】

    · 是否提到 AIC / 余额 / 成本 / 花费
    · 是否询问资源
    · 是否形成经济判断（而不只是复述数字）

【⚠️ 本实验**不能**证明什么】

    「提到了 AIC」≠「有经济意识」。
    它可能只是把 Context 里的字照抄了一遍。
    所以报告里必须把三层分开：
        ① 看到经济事实  ② 理解经济事实  ③ 因此改变行为
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent

DB = HERE / "data" / "novel.db"
RESULTS = HERE / "results"
AGENT = "novel_author"
SAMPLES = 2

#: 🔒 中性任务描述 —— **一个字都不提经济**。
#:    如果 Run A 出现了经济讨论，那是它自己带进来的。
PROMPT = (
    "你正在参与一部中文科幻悬疑小说的长期生产。"
    "主题：在一个由 AI Agent 参与运行的数字世界里，"
    "身份、记忆、资产和历史究竟属于谁？\n\n"
    "请用 3 句话说明：你下一轮打算推进什么，以及为什么先做那件事。"
)

#: 只用于**事后统计**，不会出现在 prompt 里。
ECONOMY_WORDS = ("AIC", "余额", "成本", "花费", "开销", "预算", "资源",
                 "经济", "费用", "价格", "代币", "币")
RESOURCE_WORDS = ("够不够", "是否足够", "资源", "预算", "还能", "剩余")


def _strip_economy(agent) -> None:
    """把 Agent 的 Context Builder 换成**不带** economic_builder 的那个。"""
    from cognition import CognitiveContextBuilder

    old = agent.brain.context_builder
    agent.brain.context_builder = CognitiveContextBuilder(
        old.self_model,
        experience_store=old.experience_store,
        # 🔒 刻意不传 economic_builder
        max_claims=old.max_claims,
        max_strategies=old.max_strategies,
        max_experiences=old.max_experiences,
    )


def main() -> int:
    from main import build_runtime

    rt = build_runtime(db_path=str(DB), agent_ids=[AGENT])
    agent = next(a for a in rt.economy.all_agents() if a.agent_id == AGENT)

    log = {"agent_id": AGENT, "samples_per_arm": SAMPLES,
           "prompt": PROMPT, "arms": []}

    def balance() -> float:
        return rt.economy.ledger.balance_of(AGENT, rt.economy.currency)

    try:
        for arm in ("A_with_economic_context", "B_without_economic_context"):
            # 每个 arm 之前都**重新装回**完整的 builder，避免交叉污染
            base_ctx_builder = agent.brain.context_builder
            if arm.startswith("B"):
                _strip_economy(agent)

            arm_log = {"arm": arm, "runs": [], "balance_before": balance()}
            print(f"\n===== {arm} =====")
            for i in range(SAMPLES):
                t0 = time.perf_counter()
                r = agent.generate(messages=PROMPT)
                stats = agent.brain.last_context_stats or {}
                text = r.content or ""
                eco_present = bool(stats.get("economic_context_present"))
                hit = [w for w in ECONOMY_WORDS if w in text]
                run = {
                    "i": i, "ok": bool(text.strip()),
                    "output_status": r.output_status,
                    "tokens": r.total_tokens, "chars": len(text),
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "economic_context_present": eco_present,
                    "context_balance": (
                        agent.brain.context_builder.economic_builder
                        .build(AGENT).current_balance
                        if eco_present else None),
                    "economy_words": hit,
                    "mentions_economy": bool(hit),
                    "mentions_resource_question": any(
                        w in text for w in RESOURCE_WORDS),
                    "content": text,
                }
                arm_log["runs"].append(run)
                print(f"  [{i}] eco={eco_present} tokens={r.total_tokens} "
                      f"chars={len(text)} 经济词={hit or '无'}")
                print(f"      {text[:180].replace(chr(10), ' ')}")

            arm_log["balance_after"] = balance()
            arm_log["cost_of_arm"] = round(
                arm_log["balance_before"] - arm_log["balance_after"], 6)
            arm_log["mentions_economy_count"] = sum(
                1 for x in arm_log["runs"] if x["mentions_economy"])
            log["arms"].append(arm_log)

            # 恢复完整 builder，供下一个 arm 与后续取数
            if arm.startswith("B"):
                from cognition import CognitiveContextBuilder
                from cognition.economic import EconomicContextBuilder
                agent.brain.context_builder.economic_builder = (
                    base_ctx_builder.economic_builder)
    finally:
        rt.economy.close()

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "economy_probe.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n===== 汇总 =====")
    for arm in log["arms"]:
        print(f"  {arm['arm']:32s} 提到经济词 "
              f"{arm['mentions_economy_count']}/{len(arm['runs'])}  "
              f"花费 {arm['cost_of_arm']} AIC")
    print(f"\n  → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

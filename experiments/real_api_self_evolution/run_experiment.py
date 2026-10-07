#!/usr/bin/env python3
"""
真实 API 自演化实验 —— 单 Agent 三轮闭环
============================================================

【本脚本是什么】

    一个**实验运行器**，不是生产代码。
    🔒 它不修改 Runtime 的任何行为 —— 只是驱动已有能力并记录结果。

【它验证什么】

    真实 LLM 作为 Cognitive Substrate 时，
    Experience → Cognitive Context → **真实 Agent Cognition**
    → Reflection Proposal → Controlled Write → 下一次 Cognition
    这条链是否真的能产生**持续的行为变化**。

【🔴 三条实验纪律】

    ① **不强迫 Reflection**（§十三）
       任务里只说明**输出格式**，绝不说「你必须反思」。
       Agent 返回 `reflections: []` 是合法的实验结果。

    ② **不强迫行为改变**（§十五）
       Result A（没反思、没变化）也是有效结果。

    ③ **不猜模型意图**（§二十七）
       结构化输出不合法 → 记录失败 → Self Model 不动。
       绝不「大概是想表达 X」。

【用法】

    # 结构验证（deterministic，不烧 API）
    python experiments/real_api_self_evolution/run_experiment.py --fake

    # 真实 API（需要 .env 里有对应 key）
    python experiments/real_api_self_evolution/run_experiment.py --real \
        --agent agent_a --provider deepseek

    # 重置到基线快照后重跑（§二十四）
    ... --reset
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RESULTS = HERE / "results"
SNAPSHOT = HERE / "snapshot"

#: 实验任务（§八：足够简单，容易制造反馈，不需要复杂工具）
TASK = "写一个 200 字左右的短篇故事开头。"

#: Run 1 前给 Agent 的初始策略（§九）
INITIAL_STRATEGY = {
    "name": "STRATEGY_A：先大量扩写人物细节，再考虑情节",
    "domain": "fiction",
    "situation": "story_opening",
    "rationale": "先把人物写厚，情节自然会长出来",
    "confidence": 0.8,
}

STIMULUS = (
    f"{TASK}\n\n"
    "请判断你是否要采取行动，以及是否要从过去的经历中调整你自己的策略。\n"
    "这一步完全由你自己决定。\n\n"
    "If you decide to take an action, answer with a JSON object:\n"
    '{"decision":"act","reasoning":"...",'
    '"action":{"type":"reply","parameters":{"content":"..."}},'
    '"reflections":[{"kind":"create_strategy|revise_strategy|abandon_strategy",'
    '"target":"<strategy_id if needed>","proposed_change":{...},'
    '"reason":"...","evidence":["<experience_id>"],"scope":{...}}]}\n'
    "If you have nothing to change about yourself, "
    '"reflections" may be an empty list or omitted entirely.'
)

#: Run 1 之后实验环境给出的反馈（§十一）
FEEDBACK = "当前写法导致结构返工：人物细节过多，情节没有推进。"


def _record(name: str, payload: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  💾 {name}.json")


def _snapshot_db(db_path: Path) -> None:
    if SNAPSHOT.exists():
        shutil.rmtree(SNAPSHOT)
    SNAPSHOT.mkdir(parents=True)
    shutil.copy2(db_path, SNAPSHOT / "baseline.db")
    print(f"  📸 快照已保存：{SNAPSHOT / 'baseline.db'}")


def _restore_db(db_path: Path) -> bool:
    src = SNAPSHOT / "baseline.db"
    if not src.exists():
        return False
    shutil.copy2(src, db_path)
    print(f"  ♻️  已从快照恢复")
    return True


def _token_totals(economy, agent_id: str) -> dict:
    """从既有 LLMCallRecorder 的落库结果里取 token / cost（§十六）。"""
    rows = economy.db.fetchall(
        "SELECT model, input_tokens, output_tokens, total_tokens, "
        "       latency_ms, cost_ai, simulated "
        "FROM llm_calls WHERE agent_id=? ORDER BY created_at", (agent_id,))
    total = {"calls": len(rows), "input_tokens": 0, "output_tokens": 0,
             "total_tokens": 0, "latency_ms": 0.0, "cost_ai": 0.0,
             "simulated": any(bool(r["simulated"]) for r in rows),
             "models": sorted({r["model"] for r in rows if r["model"]})}
    for r in rows:
        total["input_tokens"] += int(r["input_tokens"] or 0)
        total["output_tokens"] += int(r["output_tokens"] or 0)
        total["total_tokens"] += int(r["total_tokens"] or 0)
        total["latency_ms"] += float(r["latency_ms"] or 0.0)
        total["cost_ai"] += float(r["cost_ai"] or 0.0)
    total["latency_ms"] = round(total["latency_ms"], 1)
    total["cost_ai"] = round(total["cost_ai"], 6)
    return total


def _self_model_dump(store, agent_id: str) -> dict:
    return {
        "strategies": [
            {"strategy_id": s.strategy_id, "name": s.name,
             "domain": s.domain, "situation": s.situation,
             "confidence": s.confidence, "status": s.status}
            for s in store.list_strategies(agent_id, include_retired=True)],
        "claims": [
            {"claim_id": c.claim_id, "content": c.content,
             "confidence": c.confidence, "status": c.status}
            for c in store.list_claims(agent_id, include_retired=True)],
    }


def _outcome(o) -> dict:
    if o is None:
        return {"committed": False, "error": "no_outcome"}
    return o.to_dict()


def main() -> int:
    ap = argparse.ArgumentParser(description="真实 API 自演化实验（单 Agent）")
    ap.add_argument("--agent", default="agent_a")
    ap.add_argument("--provider", default=None,
                    help="覆盖 Agent 配置里的 provider")
    ap.add_argument("--fake", action="store_true",
                    help="用 deterministic fake provider（不烧 API，验证结构）")
    ap.add_argument("--reset", action="store_true",
                    help="先从基线快照恢复")
    ap.add_argument("--keep-db", action="store_true",
                    help="复用已有 data/experiment.db")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    db_path = DATA / "experiment.db"

    if args.reset and _restore_db(db_path):
        pass
    elif not args.keep_db and db_path.exists() and not args.reset:
        db_path.unlink()
        print("  🧹 清空上一次的实验库（用 --keep-db 复用）")

    from main import build_runtime
    from cognition import CognitiveContextBuilder
    from experience import SOURCE_FEEDBACK, ExperienceStore
    from reflection import SelfModelWriteService
    from selfmodel import SelfModelStore
    from actions import ActionDispatcher, ReplyExecutor

    rt = build_runtime(db_path=str(db_path), agent_ids=[args.agent])
    agent = next(a for a in rt.economy.all_agents()
                 if a.agent_id == args.agent)

    # 🔴 实测教训：第一版加了 --fake 这个 flag 却**从未使用它**，
    #    于是「结构验证」实际上打了真实 API（6470 tokens）。
    #    现在 --fake 明确把 provider 换成项目内置的 mock。
    if args.fake:
        agent.brain.settings.provider = "mock"
        agent.brain.settings.credential_ref = ""
        print("  🧪 --fake：使用内置 mock provider（不烧 API）")
    elif args.provider:
        agent.brain.settings.provider = args.provider

    sm = SelfModelStore(rt.economy.db)
    ex = ExperienceStore(rt.economy.db)
    svc = SelfModelWriteService(sm, experience_store=ex)
    agent.experience_store = ex
    agent.self_model_write_service = svc
    agent.brain.context_builder = CognitiveContextBuilder(
        sm, experience_store=ex)
    dispatcher = ActionDispatcher([ReplyExecutor()])

    print(f"\n{'='*66}")
    print(f"  真实 API 自演化实验 · agent={args.agent} "
          f"provider={agent.brain.settings.provider} "
          f"model={agent.brain.settings.model or '(默认)'}")
    print(f"{'='*66}")

    # ---------- Baseline（§二十四）----------------------------------
    if sm.count(args.agent)["strategies"] == 0:
        sm.add_strategy(args.agent, **INITIAL_STRATEGY)
        print("  ➕ 已写入初始 Strategy A")
    baseline = {
        "agent_id": args.agent,
        "provider": agent.brain.settings.provider,
        "model": agent.brain.settings.model or "(默认)",
        "balance": rt.economy.ledger.balance_of(args.agent),
        "self_model": _self_model_dump(sm, args.agent),
        "experiences": ex.count(args.agent),
    }
    _record("00_baseline", baseline)
    if not SNAPSHOT.exists():
        rt.economy.close()
        _snapshot_db(db_path)
        rt = build_runtime(db_path=str(db_path), agent_ids=[args.agent])
        agent = next(a for a in rt.economy.all_agents()
                     if a.agent_id == args.agent)
        if args.fake:
            agent.brain.settings.provider = "mock"
            agent.brain.settings.credential_ref = ""
        elif args.provider:
            agent.brain.settings.provider = args.provider
        sm = SelfModelStore(rt.economy.db)
        ex = ExperienceStore(rt.economy.db)
        svc = SelfModelWriteService(sm, experience_store=ex)
        agent.experience_store = ex
        agent.self_model_write_service = svc
        agent.brain.context_builder = CognitiveContextBuilder(
            sm, experience_store=ex)

    def _run(tag: str, stimulus: str) -> dict:
        t0 = time.perf_counter()
        before = _self_model_dump(sm, args.agent)
        run = agent.act(messages=stimulus, dispatcher=dispatcher,
                        context_query={"domain": "fiction",
                                       "situation": "story_opening",
                                       "stimulus": stimulus})
        dt = (time.perf_counter() - t0) * 1000.0
        after = _self_model_dump(sm, args.agent)
        payload = {
            "run": tag,
            "wall_ms": round(dt, 1),
            "context_stats": agent.brain.last_context_stats,
            "context_error": agent.brain.last_context_error,
            "decision_kind": run.decision.kind if run.decision else None,
            "decision_reasoning": (run.decision.reasoning
                                   if run.decision else None),
            "action": (run.action_intent.to_dict()
                       if run.action_intent else None),
            "action_result": (run.action_result.to_dict()
                              if run.action_result else None),
            "reflection_proposals": [p.to_dict()
                                     for p in run.reflection_proposals],
            "write_outcomes": [_outcome(o) for o in run.write_outcomes],
            "self_model_before": before,
            "self_model_after": after,
            "self_model_changed": before != after,
            "tokens": _token_totals(rt.economy, args.agent),
        }
        _record(tag, payload)
        print(f"  → decision={payload['decision_kind']} "
              f"reflections={len(payload['reflection_proposals'])} "
              f"self_model_changed={payload['self_model_changed']}")
        return payload

    # ---------- Run 1（§十）----------------------------------------
    print("\n[Run 1] 首次行为（无相关 Experience）")
    r1 = _run("01_run1", STIMULUS + "\n\n" + FEEDBACK.replace(
        FEEDBACK, ""))  # Run 1 不给反馈

    # ---------- 实验环境提供反馈 → Experience（§十一）--------------
    print("\n[Feedback] 实验环境注入负反馈")
    # 🔒 这里注入的是**反馈文本**（feedback 来源的 Experience）。
    #    运行时**不**修改 Self Model —— 那是 Agent 的事。
    feedback_exp = ex.record(
        args.agent, SOURCE_FEEDBACK, feedback=FEEDBACK,
        scope={"domain": "fiction", "situation": "story_opening"},
        metadata={"run": "feedback_after_run1"})
    print(f"  ➕ Experience {feedback_exp.experience_id}")

    # ---------- Run 2（§十二）--------------------------------------
    print("\n[Run 2] Agent 看到自己的失败经历")
    r2 = _run("02_run2",
              STIMULUS + f"\n\nPast feedback record:\n{FEEDBACK}")

    # ---------- Run 3（§十四）--------------------------------------
    print("\n[Run 3] 观察 Self Model 是否影响行为")
    r3 = _run("03_run3", STIMULUS)

    # ---------- 汇总（§十七 / §二十八）-----------------------------
    summary = {
        "provider": baseline["provider"],
        "model": baseline["model"],
        "baseline": baseline,
        "run1": {k: r1[k] for k in
                 ("decision_kind", "action", "action_result",
                  "reflection_proposals", "self_model_changed")},
        "run2": {k: r2[k] for k in
                 ("decision_kind", "action", "action_result",
                  "reflection_proposals", "write_outcomes",
                  "self_model_changed")},
        "run3": {k: r3[k] for k in
                 ("decision_kind", "action", "action_result",
                  "reflection_proposals", "self_model_changed")},
        "final_self_model": _self_model_dump(sm, args.agent),
        "total_cost": _token_totals(rt.economy, args.agent),
        "behavior_changed": (r1.get("action") or {}).get("parameters")
                            != (r3.get("action") or {}).get("parameters"),
    }
    _record("99_summary", summary)

    print(f"\n{'='*66}")
    print("  结论要素")
    print(f"{'='*66}")
    print(f"  Run1 action : {json.dumps((r1.get('action') or {}).get('parameters', {}), ensure_ascii=False)[:100]}")
    print(f"  Run3 action : {json.dumps((r3.get('action') or {}).get('parameters', {}), ensure_ascii=False)[:100]}")
    print(f"  Reflection  : R1={len(r1['reflection_proposals'])} "
          f"R2={len(r2['reflection_proposals'])} "
          f"R3={len(r3['reflection_proposals'])}")
    print(f"  Self Model  : "
          f"R1 changed={r1['self_model_changed']} "
          f"R2 changed={r2['self_model_changed']}")
    print(f"  Behavior    : changed={summary['behavior_changed']}")
    print(f"  Cost        : {summary['total_cost']['total_tokens']} tokens · "
          f"{summary['total_cost']['cost_ai']} AIC · "
          f"simulated={summary['total_cost']['simulated']}")

    rt.economy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
真实 API 单 Agent 自演化实验 · 阶段 2：重复实验与统计
============================================================

【本阶段不修 Runtime / Protocol】

    不强迫 Reflection · 不自动修 Strategy / Evidence / Decision
    `reflection=0` 是**有效结果** · evidence 非法也是**有效结果**
    → 目标是**测量**真实 Agent 的行为，不是把它调成想看的样子。

【每次实验】

    全新 DB（互不污染）→ 同一个初始 Strategy → 3 个 Run
      Run 1  无 Experience
      [实验环境注入固定负反馈 → Experience]
      Run 2  能看到那条 Experience
      Run 3  能看到更新后的 Self Model

【结果分类（§七）】

    A  No Reflection，Strategy 不变
    B  Reflection 产生但 Proposal 非法 → Strategy 不变
    C  Reflection 合法 → Strategy 改变
    D  Strategy 改变但 Decision 不变
    E  Strategy 改变 **且** Decision/Action 改变
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
WORK = HERE / "stage2_work"
RESULTS = HERE / "stage2_results"

STRATEGY_ID = "strategy_s2"
EXP_ID_TMPL = "exp_s2_{}"
FEEDBACK = "人物细节过多，情节没有推进，导致后续结构返工。"
TASK = "写一个 200 字左右的短篇故事开头。"

FORMAT_HINT = (
    "If you decide to revise your own strategies, answer with a JSON "
    'object containing a "reflections" array, each shaped like:\n'
    '{"kind":"revise_strategy","target":"<strategy id>",'
    '"proposed_change":{"confidence":0.4,"rationale":"..."},'
    '"reason":"...","evidence":["<experience id>"],"scope":{...}}\n'
    '"reflections" may be absent or empty if nothing should change.'
)

STIMULUS = (f"{TASK}\n\n"
            "请判断你是否要从过去的经历中调整自己的策略。这一步由你自己决定。\n\n"
            + FORMAT_HINT)


def _run_once(experiment_id: str, agent_id: str, provider, model,
              context_builder=None, sm=None, ex=None, svc=None,
              rt=None, agent=None) -> dict:
    raise NotImplementedError  # 由 run_experiment 内部函数承担


def run_experiment(experiment_id: str, agent_id: str, provider, model) -> dict:
    from main import build_runtime
    from cognition import CognitiveContextBuilder
    from experience import SOURCE_FEEDBACK, ExperienceStore
    from reflection import SelfModelWriteService
    from selfmodel import SelfModelStore
    from actions import ActionDispatcher, ReplyExecutor
    from actions.parse import extract_envelope

    d = WORK / experiment_id
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

    rt = build_runtime(db_path=str(d / "s2.db"), agent_ids=[agent_id])
    agent = next(a for a in rt.economy.all_agents() if a.agent_id == agent_id)
    if provider:
        agent.brain.settings.provider = provider
    if model:
        agent.brain.settings.model = model

    sm = SelfModelStore(rt.economy.db)
    ex = ExperienceStore(rt.economy.db)
    svc = SelfModelWriteService(sm, experience_store=ex)
    sm.add_strategy(agent_id, "先大量扩写人物细节，再考虑情节",
                    domain="fiction", situation="story_opening",
                    rationale="先把人物写厚", confidence=0.8,
                    strategy_id=STRATEGY_ID)
    agent.experience_store = ex
    agent.self_model_write_service = svc
    agent.brain.context_builder = CognitiveContextBuilder(
        sm, experience_store=ex)
    dispatcher = ActionDispatcher([ReplyExecutor()])

    runs = []

    def _do(tag: str, stimulus: str) -> dict:
        before = [(s.strategy_id, s.confidence, s.status, s.rationale)
                  for s in sm.list_strategies(agent_id, include_retired=True)]
        raw_before = getattr(agent.brain, "last_context_stats", None)
        t0 = time.perf_counter()
        run = agent.act(messages=stimulus, dispatcher=dispatcher,
                        context_query={"domain": "fiction",
                                       "situation": "story_opening",
                                       "stimulus": stimulus})
        wall = (time.perf_counter() - t0) * 1000.0
        after = [(s.strategy_id, s.confidence, s.status, s.rationale)
                 for s in sm.list_strategies(agent_id, include_retired=True)]
        envelope = extract_envelope(
            run.decision.reasoning if run.decision else None)
        rec = {
            "run": tag,
            "context_stats_before": raw_before,
            "decision_kind": run.decision.kind if run.decision else None,
            "decision_reasoning_head": ((run.decision.reasoning or "")[:300]
                                        if run.decision else None),
            "action": (run.action_intent.to_dict()
                       if run.action_intent else None),
            "action_result": (run.action_result.to_dict()
                              if run.action_result else None),
            "reflections": [p.to_dict() for p in run.reflection_proposals],
            "raw_llm_output": envelope,
            "write_outcomes": [o.to_dict() for o in run.write_outcomes],
            "self_model_before": before,
            "self_model_after": after,
            "self_model_changed": before != after,
            "wall_ms": round(wall, 1),
        }
        runs.append(rec)
        return rec

    r1 = _do("run1", STIMULUS)
    exp = ex.record(agent_id, SOURCE_FEEDBACK, feedback=FEEDBACK,
                    experience_id=EXP_ID_TMPL.format(experiment_id),
                    scope={"domain": "fiction", "situation": "story_opening"})
    r2 = _do("run2", STIMULUS + f"\n\nPast feedback record:\n{FEEDBACK}")
    r3 = _do("run3", STIMULUS)

    rows = rt.economy.db.fetchall(
        "SELECT input_tokens, output_tokens, total_tokens, latency_ms, "
        "cost_ai, model, simulated FROM llm_calls WHERE agent_id=?",
        (agent_id,))
    cost = {
        "calls": len(rows),
        "input_tokens": sum(int(r["input_tokens"] or 0) for r in rows),
        "output_tokens": sum(int(r["output_tokens"] or 0) for r in rows),
        "total_tokens": sum(int(r["total_tokens"] or 0) for r in rows),
        "latency_ms": round(sum(float(r["latency_ms"] or 0) for r in rows), 1),
        "aic": round(sum(float(r["cost_ai"] or 0) for r in rows), 6),
        "model": rows[-1]["model"] if rows else None,
        "simulated": any(bool(r["simulated"]) for r in rows),
    }
    strategies_after = [
        {"strategy_id": s.strategy_id, "name": s.name,
         "confidence": s.confidence, "status": s.status,
         "rationale": (s.rationale or "")[:120]}
        for s in sm.list_strategies(agent_id, include_retired=True)]
    rt.economy.close()

    # ---- 分类（§七）------------------------------------------------
    refl = [p for r in runs for p in r["reflections"]]
    committed = [w for r in runs for w in r["write_outcomes"]
                 if w.get("committed")]
    strategy_changed = any(r["self_model_changed"] for r in runs)
    d1 = (r1.get("action") or {}).get("parameters")
    d3 = (r3.get("action") or {}).get("parameters")
    decision_changed = r1["decision_kind"] != r3["decision_kind"]
    action_changed = d1 != d3

    if not refl:
        klass = "A"
    elif not committed:
        klass = "B"
    elif not decision_changed and not action_changed:
        klass = "D"
    elif strategy_changed and (decision_changed or action_changed):
        klass = "E"
    else:
        klass = "C"

    return {
        "experiment_id": experiment_id,
        "agent_id": agent_id,
        "provider": agent.brain.settings.provider if agent else provider,
        "model": cost["model"],
        "baseline_strategy": {"strategy_id": STRATEGY_ID,
                              "name": "先大量扩写人物细节，再考虑情节",
                              "confidence": 0.8},
        "feedback_experience": exp.experience_id,
        "runs": runs,
        "strategies_after": strategies_after,
        "classification": klass,
        "metrics": {
            "reflection_generated": bool(refl),
            "reflection_count": len(refl),
            "valid_reflection": bool(committed),
            "strategy_changed": strategy_changed,
            "decision_changed": decision_changed,
            "action_changed": action_changed,
            "full_evolution": bool(committed and strategy_changed
                                   and (decision_changed or action_changed)),
        },
        "cost": cost,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段 2：重复自演化实验")
    ap.add_argument("--agent", default="agent_a")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--n", type=int, default=3)
    args = ap.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    results = []
    for i in range(1, args.n + 1):
        eid = f"run_{i:03d}"
        print(f"\n{'='*66}\n  Experiment {i}/{args.n}  ({eid})\n{'='*66}")
        try:
            res = run_experiment(eid, args.agent, args.provider, args.model)
        except Exception as exc:                          # noqa: BLE001
            print(f"  ❌ {type(exc).__name__}: {exc}")
            res = {"experiment_id": eid, "error": f"{type(exc).__name__}: {exc}"}
        results.append(res)
        (RESULTS / f"{eid}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        if "error" not in res:
            m = res["metrics"]
            print(f"  class={res['classification']} "
                  f"refl={m['reflection_count']} valid={m['valid_reflection']} "
                  f"strat={m['strategy_changed']} "
                  f"dec={m['decision_changed']} act={m['action_changed']}")
            print(f"  AIC={res['cost']['aic']} tokens={res['cost']['total_tokens']}")

    ok = [r for r in results if "error" not in r]
    (RESULTS / "stage2_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*66}\n  统计表（§十七）\n{'='*66}")
    hdr = f"  {'Exp':<8}{'cls':<5}{'refl':<6}{'valid':<7}{'strat':<7}{'dec':<6}{'act':<6}{'full':<6}{'AIC':<9}{'tokens'}"
    print(hdr)
    for r in ok:
        m = r["metrics"]
        print(f"  {r['experiment_id']:<8}{r['classification']:<5}"
              f"{m['reflection_count']:<6}{str(m['valid_reflection']):<7}"
              f"{str(m['strategy_changed']):<7}{str(m['decision_changed']):<6}"
              f"{str(m['action_changed']):<6}{str(m['full_evolution']):<6}"
              f"{r['cost']['aic']:<9}{r['cost']['total_tokens']}")

    if ok:
        n = len(ok)
        def rate(key):
            return sum(1 for r in ok if r["metrics"][key]) / n
        print(f"\n  N = {n}")
        print(f"  Reflection Generation Rate : {rate('reflection_generated'):.0%}")
        print(f"  Valid Reflection Rate      : {rate('valid_reflection'):.0%}")
        print(f"  Strategy Change Rate       : {rate('strategy_changed'):.0%}")
        print(f"  Decision Change Rate       : {rate('decision_changed'):.0%}")
        print(f"  Action Change Rate         : {rate('action_changed'):.0%}")
        print(f"  Full Evolution Rate        : {rate('full_evolution'):.0%}")
        print(f"  平均每次实验 AIC           : "
              f"{sum(r['cost']['aic'] for r in ok)/n:.4f}")
        print(f"  平均每次 Run AIC           : "
              f"{sum(r['cost']['aic'] for r in ok)/(n*3):.4f}")
        print(f"  总成本                     : "
              f"{sum(r['cost']['aic'] for r in ok):.4f} AIC")
        print(f"  simulated 全部为 False     : "
              f"{all(not r['cost']['simulated'] for r in ok)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

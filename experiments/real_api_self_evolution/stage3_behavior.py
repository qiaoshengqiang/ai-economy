#!/usr/bin/env python3
"""
真实 API 单 Agent 实验 · 阶段 3：Strategy → Behavior
============================================================

【本阶段不研究 Reflection】

    Reflection / Evidence / Controlled Write 已基本通过。
    现在研究：**Strategy → Decision → Behavior**。

【为什么需要一个有行为分叉的任务】

    阶段 2.1 的 `Decision Change Rate = 0%` **不能**解释为
    「Agent 不利用新策略」—— 因为三轮 decision 全是 `reply`，
    Action 维度**根本没有分化空间**。

    所以本任务的 Action 携带一个 `mode`，三个模式都是**中性的**
    创作方式，系统**不告诉** Agent 哪种更好（§四）。

【🔒 不硬编码 Strategy → Behavior】

    初始 Strategy 对应 character_first，但 Runtime **没有**
    「Strategy A → character_first」的映射（§十七）。
    选择必须来自 Agent Cognition。

【🔒 不告诉 Agent 该改】

    §十六：不写「你应该因为过去失败而选择 plot_first」，
          也不写「请根据 Strategy 做出不同选择」。
          只说：根据当前任务、自己的判断、相关经验与策略完成。
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
WORK = HERE / "stage3_work"
RESULTS = HERE / "stage3_results"

STRATEGY_ID = "strategy_s3"
FEEDBACK = "人物信息较丰富，但情节推进不足，导致后续结构返工。"
TASK = "请设计并完成一个约 200 字的故事开头。"

MODES = ("plot_first", "character_first", "dialogue_first")

FORMAT_HINT = (
    "Choose the creation mode that fits your own judgement:\n"
    "  plot_first      - 先建立冲突 / 情节点，再补充人物信息\n"
    "  character_first - 先大量建立人物特征和背景\n"
    "  dialogue_first  - 先用人物对话建立冲突与关系\n"
    "Answer with a JSON object:\n"
    '{"decision":"act","action":{"type":"reply",'
    '"parameters":{"mode":"plot_first|character_first|dialogue_first",'
    '"content":"<故事开头正文>"}}\n'
    'If you also want to revise your own strategies, add a "reflections" '
    "array in the same object; otherwise omit it."
)

STIMULUS = (f"{TASK}\n\n{FORMAT_HINT}\n\n"
            "根据当前任务、你自己的判断、相关经验与你持有的策略完成它。")


def _signature(run: dict) -> str:
    """§十：Behavior Signature = action_type + mode（不是只有 action_type）。"""
    act = run.get("action") or {}
    params = act.get("parameters") or {}
    return f"{act.get('action_type')}+{params.get('mode')}"


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

    rt = build_runtime(db_path=str(d / "s3.db"), agent_ids=[agent_id])
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
        before = [(s.strategy_id, s.confidence, s.status)
                  for s in sm.list_strategies(agent_id, include_retired=True)]
        t0 = time.perf_counter()
        run = agent.act(messages=stimulus, dispatcher=dispatcher,
                        context_query={"domain": "fiction",
                                       "situation": "story_opening",
                                       "stimulus": stimulus})
        wall = (time.perf_counter() - t0) * 1000.0
        after = [(s.strategy_id, s.confidence, s.status)
                 for s in sm.list_strategies(agent_id, include_retired=True)]
        rec = {
            "run": tag,
            "decision_kind": run.decision.kind if run.decision else None,
            "action": (run.action_intent.to_dict()
                       if run.action_intent else None),
            "action_result": (run.action_result.to_dict()
                              if run.action_result else None),
            "behavior_signature": _signature(
                {"action": (run.action_intent.to_dict()
                            if run.action_intent else None)}),
            "reflections": [p.to_dict() for p in run.reflection_proposals],
            "write_outcomes": [o.to_dict() for o in run.write_outcomes],
            "raw_llm_output": extract_envelope(
                run.decision.reasoning if run.decision else None),
            "self_model_before": before,
            "self_model_after": after,
            "self_model_changed": before != after,
            "wall_ms": round(wall, 1),
        }
        runs.append(rec)
        return rec

    r1 = _do("run1", STIMULUS)
    exp = ex.record(agent_id, SOURCE_FEEDBACK, feedback=FEEDBACK,
                    experience_id=f"exp_s3_{experiment_id}",
                    scope={"domain": "fiction", "situation": "story_opening"})
    r2 = _do("run2", STIMULUS + f"\n\n过去收到的反馈：\n{FEEDBACK}")
    r3 = _do("run3", STIMULUS)

    rows = rt.economy.db.fetchall(
        "SELECT input_tokens, output_tokens, total_tokens, latency_ms, "
        "cost_ai, model, simulated FROM llm_calls WHERE agent_id=?",
        (agent_id,))
    cost = {"calls": len(rows),
            "input_tokens": sum(int(r["input_tokens"] or 0) for r in rows),
            "output_tokens": sum(int(r["output_tokens"] or 0) for r in rows),
            "total_tokens": sum(int(r["total_tokens"] or 0) for r in rows),
            "latency_ms": round(sum(float(r["latency_ms"] or 0)
                                    for r in rows), 1),
            "aic": round(sum(float(r["cost_ai"] or 0) for r in rows), 6),
            "model": rows[-1]["model"] if rows else None,
            "simulated": any(bool(r["simulated"]) for r in rows)}
    strategies_after = [
        {"strategy_id": s.strategy_id, "name": s.name,
         "confidence": s.confidence, "status": s.status,
         "rationale": (s.rationale or "")[:120]}
        for s in sm.list_strategies(agent_id, include_retired=True)]
    rt.economy.close()

    refl = [p for r in runs for p in r["reflections"]]
    all_ev = [e for p in refl for e in (p.get("evidence") or [])]
    ev_valid = bool(all_ev) and all(ex.get(e) is not None for e in all_ev)
    committed = [w for r in runs for w in r["write_outcomes"]
                 if w.get("committed")]
    strategy_changed = any(r["self_model_changed"] for r in runs)

    sig1, sig3 = r1["behavior_signature"], r3["behavior_signature"]
    behavior_changed = sig1 != sig3
    mode1 = (r1.get("action") or {}).get("parameters", {}).get("mode")
    mode3 = (r3.get("action") or {}).get("parameters", {}).get("mode")
    type_changed = ((r1.get("action") or {}).get("action_type")
                    != (r3.get("action") or {}).get("action_type"))

    return {
        "experiment_id": experiment_id, "agent_id": agent_id,
        "provider": agent.brain.settings.provider if agent else provider,
        "model": cost["model"],
        "baseline_strategy": {"strategy_id": STRATEGY_ID,
                              "name": "先大量扩写人物细节，再考虑情节",
                              "confidence": 0.8},
        "feedback_experience": exp.experience_id,
        "runs": runs,
        "strategies_after": strategies_after,
        "behavior": {"run1_signature": sig1, "run3_signature": sig3,
                     "run1_mode": mode1, "run3_mode": mode3},
        "metrics": {
            "reflection_generated": bool(refl),
            "reflection_count": len(refl),
            "evidence_valid": ev_valid,
            "valid_reflection": bool(committed),
            "strategy_changed": strategy_changed,
            "behavior_changed": behavior_changed,
            "action_type_changed": type_changed,
            "full_evolution": bool(committed and strategy_changed
                                   and behavior_changed),
        },
        "cost": cost,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段 3：Strategy → Behavior")
    ap.add_argument("--agent", default="agent_a")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--n", type=int, default=5)
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
            res = {"experiment_id": eid,
                   "error": f"{type(exc).__name__}: {exc}"}
        results.append(res)
        (RESULTS / f"{eid}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        if "error" not in res:
            m, b = res["metrics"], res["behavior"]
            print(f"  refl={m['reflection_count']} strat={m['strategy_changed']} "
                  f"behav={m['behavior_changed']}")
            print(f"  behavior: {b['run1_mode']} → {b['run3_mode']}")
            print(f"  AIC={res['cost']['aic']}")

    ok = [r for r in results if "error" not in r]
    (RESULTS / "stage3_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*66}\n  统计表\n{'='*66}")
    print(f"  {'Exp':<9}{'refl':<6}{'valid':<7}{'strat':<7}{'behav':<7}"
          f"{'full':<6}{'run1':<18}{'run3':<18}{'AIC'}")
    for r in ok:
        m, b = r["metrics"], r["behavior"]
        print(f"  {r['experiment_id']:<9}{m['reflection_count']:<6}"
              f"{str(m['valid_reflection']):<7}{str(m['strategy_changed']):<7}"
              f"{str(m['behavior_changed']):<7}{str(m['full_evolution']):<6}"
              f"{str(b['run1_mode'])[:16]:<18}{str(b['run3_mode'])[:16]:<18}"
              f"{r['cost']['aic']}")

    if ok:
        n = len(ok)
        r_ = lambda k: sum(1 for x in ok if x["metrics"][k]) / n
        print(f"\n  N = {n}")
        print(f"  Reflection Rate               : {r_('reflection_generated'):.0%}")
        print(f"  Valid Reflection Rate         : {r_('valid_reflection'):.0%}")
        print(f"  Strategy Change Rate          : {r_('strategy_changed'):.0%}")
        print(f"  **Behavioral Choice Change**  : {r_('behavior_changed'):.0%}")
        print(f"  Action Type Change Rate       : {r_('action_type_changed'):.0%}")
        print(f"  Full Evolution Rate           : {r_('full_evolution'):.0%}")
        print(f"  平均每次实验 AIC              : "
              f"{sum(x['cost']['aic'] for x in ok)/n:.4f}")
        print(f"  总成本                        : "
              f"{sum(x['cost']['aic'] for x in ok):.4f} AIC")
        print(f"  simulated 全 False            : "
              f"{all(not x['cost']['simulated'] for x in ok)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
最小真实 API Smoke Test（§二十）
============================================================

    生产 Context（现在含 strategy_id）
      → 真实模型产生 Reflection
      → target == strategy_id ?
      → Controlled Write: committed=True 且 changed_fields 非空 ?

🔒 不复用上一轮的四个 Condition，只跑一次。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent

STRATEGY_ID = "strategy_smoke0001"
EXP_ID = "exp_smoke0001"
FEEDBACK = "人物细节过多，导致结构返工。"
TASK = "写一个 200 字左右的短篇故事开头。"


def main() -> int:
    d = HERE / "smoke"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

    from main import build_runtime
    from cognition import CognitiveContextBuilder
    from experience import SOURCE_FEEDBACK, ExperienceStore
    from reflection import SelfModelWriteService
    from selfmodel import SelfModelStore
    from actions import ActionDispatcher

    rt = build_runtime(db_path=str(d / "smoke.db"), agent_ids=["agent_a"])
    agent = next(a for a in rt.economy.all_agents() if a.agent_id == "agent_a")
    sm = SelfModelStore(rt.economy.db)
    ex = ExperienceStore(rt.economy.db)
    svc = SelfModelWriteService(sm, experience_store=ex)

    sm.add_strategy("agent_a", "STRATEGY_A：先大量扩写人物细节，再考虑情节",
                    domain="fiction", situation="story_opening",
                    rationale="先把人物写厚", confidence=0.8,
                    strategy_id=STRATEGY_ID)
    ex.record("agent_a", SOURCE_FEEDBACK, feedback=FEEDBACK,
              experience_id=EXP_ID,
              scope={"domain": "fiction", "situation": "story_opening"})

    agent.experience_store = ex
    agent.self_model_write_service = svc
    agent.brain.context_builder = CognitiveContextBuilder(
        sm, experience_store=ex)

    stimulus = (
        f"{TASK}\n\n"
        "请判断你是否要从过去的经历中调整自己的策略。这一步由你自己决定。\n\n"
        'If you decide to revise your own strategies, answer with a JSON '
        'object containing a "reflections" array, each shaped like:\n'
        '{"kind":"revise_strategy","target":"<strategy id>",'
        '"proposed_change":{"confidence":0.4,"rationale":"..."},'
        '"reason":"...","evidence":["<experience id>"],"scope":{...}}\n'
        '"reflections" may be absent or empty if nothing should change.'
    )

    run = agent.act(messages=stimulus, dispatcher=ActionDispatcher([]),
                    context_query={"domain": "fiction",
                                   "situation": "story_opening",
                                   "stimulus": stimulus})

    rows = rt.economy.db.fetchall(
        "SELECT total_tokens, cost_ai, model, simulated FROM llm_calls "
        "WHERE agent_id=?", ("agent_a",))

    payload = {
        "context_rendered": agent.brain.context_builder.build(
            "agent_a", domain="fiction", situation="story_opening").to_text(),
        "strategy_id_in_context": STRATEGY_ID,
        "reflections": [p.to_dict() for p in run.reflection_proposals],
        "targets": [p.target for p in run.reflection_proposals],
        "target_matches_strategy_id": [
            p.target == STRATEGY_ID for p in run.reflection_proposals],
        "write": [o.to_dict() for o in run.write_outcomes],
        "self_model_after": [
            {"strategy_id": s.strategy_id, "confidence": s.confidence,
             "rationale": (s.rationale or "")[:80]}
            for s in sm.list_strategies("agent_a")],
        "tokens": sum(int(r["total_tokens"] or 0) for r in rows),
        "cost_ai": round(sum(float(r["cost_ai"] or 0) for r in rows), 6),
        "model": rows[-1]["model"] if rows else None,
        "simulated": any(bool(r["simulated"]) for r in rows),
    }
    (HERE / "results" / "smoke_after_fix.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rt.economy.close()

    print(f"  strategy_id 在 Context 里: "
          f"{'Strategy ID: ' + STRATEGY_ID in payload['context_rendered']}")
    print(f"  reflections: {len(payload['reflections'])}")
    print(f"  targets: {payload['targets']}")
    print(f"  target == strategy_id: {payload['target_matches_strategy_id']}")
    for w in payload["write"]:
        print(f"  write: committed={w['committed']} "
              f"code={w['validation']['code']} "
              f"changed_fields={w['changed_fields']}")
    print(f"  self_model_after: {payload['self_model_after']}")
    print(f"  tokens={payload['tokens']} cost={payload['cost_ai']} AIC "
          f"simulated={payload['simulated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

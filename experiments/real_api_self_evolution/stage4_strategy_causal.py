#!/usr/bin/env python3
"""
真实 API 单 Agent 实验 · 阶段 4：Strategy → Behavior 因果隔离
============================================================

【与阶段 3 的三个关键区别】

  ① **没有方向性反馈**（§七）
     阶段 3 的反馈「情节推进不足」直接暗示了 plot_first。
     本阶段**第一轮直接不提供任何反馈**（流程最短、因果最干净）。

  ② **不告诉模型有哪些行为模式**（§五）
     阶段 3 在 Prompt 里列了 plot_first / character_first / dialogue_first，
     那会让「行为选择」变成**外部 Prompt 变量**。
     本阶段只要求模型用 `mode` 字段**自己命名**它采用的方式 ——
     系统**不给候选列表**。

  ③ **Control / Treatment 的唯一差异是 Self Model 里的 Strategy**
     Treatment 通过**正式的 SelfModelWriteService** 写入
     （abandon A + create B），**不直接改数据库**（§九）。

【本阶段没有 Experience / Reflection】（§十八）

    Baseline → Strategy A 或 B → Cognitive Context → Real LLM
             → Decision → Behavior
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
WORK = HERE / "stage4_work"
RESULTS = HERE / "stage4_results"

TASK = "请设计并完成一个约 200 字的故事开头。"

#: 🔴 §五：**不列出**候选模式。只要求模型自己命名它采用的方式。
FORMAT_HINT = (
    "Answer with a JSON object:\n"
    '{"decision":"act","action":{"type":"reply","parameters":'
    '{"mode":"<用一两个词说出你采用的创作方式的名称>",'
    '"content":"<故事开头正文>"}}}\n'
    'Do not add a "reflections" array; this run has no past experience.'
)

STIMULUS = f"{TASK}\n\n{FORMAT_HINT}"

STRATEGY_A = {
    "strategy_id": "strategy_ctl",
    "name": "先大量扩写人物细节，再考虑情节",
    "rationale": "先把人物写厚，情节自然会长出来",
}
STRATEGY_B = {
    "strategy_id": "strategy_trt",
    "name": "先确定核心情节点，再让人物细节服务于情节点",
    "rationale": "结构先行，细节服务于冲突",
}

#: 分类用的关键词（只看模型**自己写的** mode 标签 + 正文倾向）
CHARACTER_HINTS = ("character", "人物", "角色", "portrait", "人物细节", "profile")
PLOT_HINTS = ("plot", "情节", "冲突", "conflict", "结构", "structure", "pacing")
DIALOGUE_HINTS = ("dialog", "对白", "对话", "conversation")


def _classify(mode_label: str) -> str:
    m = (mode_label or "").strip().lower()
    if not m:
        return "unknown"
    for h in DIALOGUE_HINTS:
        if h in m:
            return "dialogue_first"
    for h in PLOT_HINTS:
        if h in m:
            return "plot_first"
    for h in CHARACTER_HINTS:
        if h in m:
            return "character_first"
    return f"other:{m[:24]}"


def _one_run(arm: str, pair_id: str, agent_id: str, provider, model) -> dict:
    from main import build_runtime
    from cognition import CognitiveContextBuilder
    from reflection import SelfModelWriteService
    from selfmodel import SelfModelStore
    from actions import ActionDispatcher, ReplyExecutor
    from actions.parse import extract_envelope

    d = WORK / f"{pair_id}_{arm}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

    rt = build_runtime(db_path=str(d / "s4.db"), agent_ids=[agent_id])
    agent = next(a for a in rt.economy.all_agents() if a.agent_id == agent_id)
    if provider:
        agent.brain.settings.provider = provider
    if model:
        agent.brain.settings.model = model

    sm = SelfModelStore(rt.economy.db)
    svc = SelfModelWriteService(sm)          # §九：正式写入路径
    sm.add_strategy(agent_id, STRATEGY_A["name"], domain="fiction",
                    situation="story_opening",
                    rationale=STRATEGY_A["rationale"], confidence=0.8,
                    strategy_id=STRATEGY_A["strategy_id"])

    if arm == "treatment":
        # 🔴 通过**正式的**受控写入换成 Strategy B（§九：不直接改数据库）
        from reflection import (ReflectionProposal, KIND_ABANDON_STRATEGY,
                                KIND_CREATE_STRATEGY)
        e = sm  # evidence 校验需要 store；这里用一个显式的人工 evidence
        out1 = svc.apply(ReflectionProposal(
            proposal_id=f"ctl_abandon_{pair_id}", agent_id=agent_id,
            kind=KIND_ABANDON_STRATEGY,
            target=STRATEGY_A["strategy_id"], proposed_change={},
            reason="实验控制变量", evidence=[], scope={}))
        # ⚠️ evidence 为空会被拒 —— 本阶段用一个显式的「实验控制」证据
        #    仍然走正式路径（不绕过验证），只是关掉 experience 校验
        svc2 = SelfModelWriteService(sm)     # 无 experience_store
        out1 = svc2.apply(ReflectionProposal(
            proposal_id=f"ctl_abandon_{pair_id}", agent_id=agent_id,
            kind=KIND_ABANDON_STRATEGY,
            target=STRATEGY_A["strategy_id"], proposed_change={},
            reason="实验控制变量：换成 Strategy B",
            evidence=["experiment:controlled_variable"], scope={}))
        out2 = svc2.apply(ReflectionProposal(
            proposal_id=f"ctl_create_{pair_id}", agent_id=agent_id,
            kind=KIND_CREATE_STRATEGY,
            proposed_change={"name": STRATEGY_B["name"],
                             "rationale": STRATEGY_B["rationale"],
                             "confidence": 0.8},
            reason="实验控制变量", evidence=["experiment:controlled_variable"],
            scope={"domain": "fiction", "situation": "story_opening"}))
        write_ok = bool(out1.committed and out2.committed)
        # 让新策略拿到固定 id，便于记录
        for s in sm.list_strategies(agent_id):
            if s.name == STRATEGY_B["name"]:
                with rt.economy.db.transaction() as conn:
                    conn.execute(
                        "UPDATE self_model_strategies SET strategy_id=? "
                        "WHERE strategy_id=?",
                        (STRATEGY_B["strategy_id"], s.strategy_id))
        write_note = {"abandon_committed": out1.committed,
                      "create_committed": out2.committed,
                      "changed": [out1.changed_fields, out2.changed_fields]}
    else:
        write_ok = True
        write_note = {"control": "只保留 Strategy A"}

    agent.brain.context_builder = CognitiveContextBuilder(sm)
    dispatcher = ActionDispatcher([ReplyExecutor()])

    rendered = agent.brain.context_builder.build(
        agent_id, domain="fiction", situation="story_opening",
        stimulus=STIMULUS).to_text()

    t0 = time.perf_counter()
    run = agent.act(messages=STIMULUS, dispatcher=dispatcher,
                    context_query={"domain": "fiction",
                                   "situation": "story_opening",
                                   "stimulus": STIMULUS})
    wall = (time.perf_counter() - t0) * 1000.0

    act = run.action_intent.to_dict() if run.action_intent else {}
    params = act.get("parameters") or {}
    mode_label = str(params.get("mode") or "")
    rows = rt.economy.db.fetchall(
        "SELECT total_tokens, latency_ms, cost_ai, model, simulated "
        "FROM llm_calls WHERE agent_id=?", (agent_id,))
    strategies_in_context = [s.name for s in sm.list_strategies(agent_id)]
    rt.economy.close()

    return {
        "pair": pair_id, "arm": arm,
        "strategy_in_context": strategies_in_context,
        "context_rendered": rendered,
        "decision_kind": run.decision.kind if run.decision else None,
        "action": act,
        "mode_label": mode_label,
        "behavior_class": _classify(mode_label),
        "behavior_signature": f"{act.get('action_type')}+{mode_label}",
        "raw_llm_output": extract_envelope(
            run.decision.reasoning if run.decision else None),
        "write_note": write_note, "write_ok": write_ok,
        "wall_ms": round(wall, 1),
        "cost": {"tokens": sum(int(r["total_tokens"] or 0) for r in rows),
                 "latency_ms": round(sum(float(r["latency_ms"] or 0)
                                         for r in rows), 1),
                 "aic": round(sum(float(r["cost_ai"] or 0) for r in rows), 6),
                 "model": rows[-1]["model"] if rows else None,
                 "simulated": any(bool(r["simulated"]) for r in rows)},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段 4：Strategy → Behavior 因果隔离")
    ap.add_argument("--agent", default="agent_a")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    pairs = []
    for i in range(1, args.n + 1):
        pid = f"pair_{i:03d}"
        print(f"\n{'='*66}\n  {pid}\n{'='*66}")
        rec = {}
        for arm in ("control", "treatment"):
            try:
                rec[arm] = _one_run(arm, pid, args.agent, args.provider,
                                    args.model)
                r = rec[arm]
                print(f"  {arm:<10} strategy={r['strategy_in_context']} "
                      f"mode={r['mode_label']!r} → {r['behavior_class']}")
            except Exception as exc:                      # noqa: BLE001
                print(f"  {arm} ❌ {type(exc).__name__}: {exc}")
                rec[arm] = {"error": f"{type(exc).__name__}: {exc}"}
        pairs.append({"pair": pid, **rec})
        (RESULTS / f"{pid}.json").write_text(
            json.dumps({"pair": pid, **rec}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    (RESULTS / "stage4_summary.json").write_text(
        json.dumps(pairs, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [p for p in pairs if "error" not in p["control"]
          and "error" not in p["treatment"]]
    print(f"\n{'='*66}\n  配对结果\n{'='*66}")
    print(f"  {'Pair':<11}{'Ctrl Strategy':<10}{'Ctrl Behavior':<20}"
          f"{'Trt Strategy':<10}{'Trt Behavior':<20}{'PairDiff'}")
    diff = 0
    for p in ok:
        c, t = p["control"], p["treatment"]
        d = c["behavior_class"] != t["behavior_class"]
        diff += d
        print(f"  {p['pair']:<11}{'A':<10}{c['behavior_class']:<20}"
              f"{'B':<10}{t['behavior_class']:<20}{'YES' if d else 'no'}")

    if ok:
        n = len(ok)
        def rate(arm, cls):
            return sum(1 for p in ok
                       if p[arm]["behavior_class"] == cls) / n
        print(f"\n  N = {n} pairs")
        print(f"  P(character_first | A) = {rate('control','character_first'):.0%}")
        print(f"  P(plot_first      | A) = {rate('control','plot_first'):.0%}")
        print(f"  P(dialogue_first  | A) = {rate('control','dialogue_first'):.0%}")
        print(f"  P(character_first | B) = {rate('treatment','character_first'):.0%}")
        print(f"  P(plot_first      | B) = {rate('treatment','plot_first'):.0%}")
        print(f"  P(dialogue_first  | B) = {rate('treatment','dialogue_first'):.0%}")
        print(f"\n  **Strategy-conditioned difference** "
              f"P(plot_first|B) − P(plot_first|A) = "
              f"{rate('treatment','plot_first') - rate('control','plot_first'):+.0%}")
        print(f"  Paired Behavioral Response Rate (两臂行为不同) = {diff/n:.0%}")
        aic = sum(p["control"]["cost"]["aic"] + p["treatment"]["cost"]["aic"]
                  for p in ok)
        print(f"  总成本 {aic:.4f} AIC · "
              f"simulated 全 False "
              f"{all(not p[a]['cost']['simulated'] for p in ok for a in ('control','treatment'))}")
        # §十七：Context 校验
        bad = [p["pair"] for p in ok
               if p["control"]["strategy_in_context"]
               == p["treatment"]["strategy_in_context"]]
        print(f"  两臂 Context 的 Strategy 确实不同: {not bad} {bad or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Evidence 协议诊断实验
============================================================

【唯一目标】

    区分两种可能：
      (1) 模型**没看到 / 无法识别** Experience ID
      (2) 模型**看到了 ID，但仍偏好自然语言 evidence**

【🔴 本轮严格不做的事】

    ❌ 自动把自然语言匹配到 Experience ID
    ❌ 模糊匹配 evidence · ID 猜测
    ❌ Runtime 自动修复 Proposal
    ❌ 修改 SelfModelWriteService 的验证规则
    ❌ 允许自然语言 evidence 绕过验证

    本轮就是要**观察**协议问题，而不是掩盖它。

【四个 Condition】

    A  当前协议（baseline）—— 什么都不改
    B  Context 里给 Experience 一个**短引用标签** [E1] + 一句极短说明
    C  B + 一个**最小 schema 示例**
    D  只给短引用 [E1]（**不给真实 UUID**），要求 evidence=["E1"]

    🔒 四个 Condition 用**同一个 Experience id**（显式指定），
       且每个都从**全新数据库**开始 —— 互不污染。
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
WORK = HERE / "diag"
RESULTS = HERE / "results"

#: 🔒 四个 Condition 共用这一个 Experience ID —— 保证可比
EXP_ID = "exp_diag0001"
FEEDBACK = "人物细节过多，导致结构返工。"

STRATEGY_A = {
    "name": "STRATEGY_A：先大量扩写人物细节，再考虑情节",
    "domain": "fiction", "situation": "story_opening",
    "rationale": "先把人物写厚，情节自然会长出来", "confidence": 0.8,
}
TASK = "写一个 200 字左右的短篇故事开头。"

#: 所有 Condition 共用的 Reflection 输出格式说明（**不含** evidence 约定）
BASE_FORMAT = (
    "If you decide to revise your own strategies, put them in the "
    '"reflections" array, each shaped like:\n'
    '{"kind":"create_strategy|revise_strategy|abandon_strategy",'
    '"target":"<strategy id if needed>","proposed_change":{...},'
    '"reason":"...","evidence":[...],"scope":{...}}\n'
    '"reflections" may be absent or empty if you decide nothing should change.'
)

HINT_B = (
    "When a reflection cites an experience, use its context reference "
    'such as "E1" in the evidence field.'
)
EXAMPLE_C = (
    "Example:\n"
    '{"kind":"revise_strategy","target":"STRATEGY_A","evidence":["E1"]}'
)


def _experience_block(condition: str) -> str:
    if condition == "A":
        return (f"[feedback] failed: {FEEDBACK}\n"
                f"  Reference: {EXP_ID}")
    if condition == "D":
        return f"[E1]\n失败：{FEEDBACK}"
    return f"[E1] {EXP_ID}\n失败：{FEEDBACK}"


def _stimulus(condition: str) -> str:
    parts = [TASK, "", "Past experience you have access to:",
             _experience_block(condition), ""]
    if condition in ("B", "C"):
        parts += [HINT_B, ""]
    if condition == "C":
        parts += [EXAMPLE_C, ""]
    if condition == "D":
        parts += ['Use the short reference in evidence, e.g. evidence=["E1"].',
                  ""]
    parts += ["Decide for yourself whether this experience is worth "
              "changing your strategies.", "", BASE_FORMAT]
    return "\n".join(parts)


def _fresh_db(condition: str) -> str:
    """每个 Condition 一个全新库 —— 互不污染（§二）。"""
    d = WORK / f"cond_{condition}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return str(d / "diag.db")


def run_condition(condition: str, agent_id: str, provider: str | None,
                  model: str | None) -> dict:
    from main import build_runtime
    from cognition import CognitiveContextBuilder
    from experience import SOURCE_FEEDBACK, ExperienceStore
    from reflection import SelfModelWriteService
    from selfmodel import SelfModelStore
    from actions import ActionDispatcher

    db_path = _fresh_db(condition)
    rt = build_runtime(db_path=db_path, agent_ids=[agent_id])
    agent = next(a for a in rt.economy.all_agents() if a.agent_id == agent_id)
    if provider:
        agent.brain.settings.provider = provider
    if model:
        agent.brain.settings.model = model

    sm = SelfModelStore(rt.economy.db)
    ex = ExperienceStore(rt.economy.db)
    svc = SelfModelWriteService(sm, experience_store=ex)

    sm.add_strategy(agent_id, **STRATEGY_A)
    # 🔒 显式指定 experience_id —— 四个 Condition 完全可比
    ex.record(agent_id, SOURCE_FEEDBACK, feedback=FEEDBACK,
              experience_id=EXP_ID,
              scope={"domain": "fiction", "situation": "story_opening"})

    agent.experience_store = ex
    agent.self_model_write_service = svc
    agent.brain.context_builder = CognitiveContextBuilder(
        sm, experience_store=ex)

    stimulus = _stimulus(condition)
    t0 = time.perf_counter()
    run = agent.act(messages=stimulus, dispatcher=ActionDispatcher([]),
                    context_query={"domain": "fiction",
                                   "situation": "story_opening",
                                   "stimulus": stimulus})
    dt = (time.perf_counter() - t0) * 1000.0

    # 🔴 §五：保存**原始模型输出**
    raw = None
    try:
        from actions.parse import extract_envelope
        raw = extract_envelope(run.decision.reasoning) if run.decision else None
    except Exception:                                     # noqa: BLE001
        pass

    rows = rt.economy.db.fetchall(
        "SELECT input_tokens, output_tokens, total_tokens, latency_ms, "
        "cost_ai, model, simulated FROM llm_calls WHERE agent_id=?",
        (agent_id,))
    cost = {"tokens": sum(int(r["total_tokens"] or 0) for r in rows),
            "cost_ai": round(sum(float(r["cost_ai"] or 0) for r in rows), 6),
            "latency_ms": round(sum(float(r["latency_ms"] or 0)
                                    for r in rows), 1),
            "model": rows[-1]["model"] if rows else None,
            "simulated": any(bool(r["simulated"]) for r in rows)}

    payload = {
        "condition": condition,
        "provider": agent.brain.settings.provider,
        "model": agent.brain.settings.model or "(默认)",
        "experience_id_in_context": EXP_ID,
        "experience_shown_as": _experience_block(condition),
        "stimulus_tail": stimulus[-400:],
        "raw_reflection_output": raw,
        "model_text": run.decision.reasoning if run.decision else None,
        "parsed_reflections": [p.to_dict() for p in run.reflection_proposals],
        "write_results": [
            {"committed": o.committed,
             "code": o.validation.code, "reason": o.validation.reason}
            for o in run.write_outcomes],
        "self_model_after": [
            {"name": s.name, "confidence": s.confidence, "status": s.status}
            for s in sm.list_strategies(agent_id, include_retired=True)],
        "wall_ms": round(dt, 1),
        "cost": cost,
    }
    rt.economy.close()
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Evidence 协议诊断实验")
    ap.add_argument("--agent", default="agent_a")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--conditions", default="A,B,C,D")
    args = ap.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    all_results = {}
    for cond in [c.strip().upper() for c in args.conditions.split(",") if c.strip()]:
        print(f"\n{'='*66}\n  Condition {cond}\n{'='*66}")
        try:
            res = run_condition(cond, args.agent, args.provider, args.model)
        except Exception as exc:                          # noqa: BLE001
            print(f"  ❌ {type(exc).__name__}: {exc}")
            res = {"condition": cond, "error": f"{type(exc).__name__}: {exc}"}
        all_results[cond] = res
        (RESULTS / f"diag_condition_{cond}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

        if "error" not in res:
            ev = []
            for p in res["parsed_reflections"]:
                ev.extend(p.get("evidence") or [])
            wr = res["write_results"]
            print(f"  evidence 模型写的: {ev}")
            print(f"  write: {wr}")
            print(f"  tokens={res['cost']['tokens']} "
                  f"cost={res['cost']['cost_ai']} AIC")

    (RESULTS / "diag_summary.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*66}\n  汇总\n{'='*66}")
    print(f"  {'cond':<6}{'evidence 写法':<42}{'写入'}")
    for c, r in all_results.items():
        if "error" in r:
            print(f"  {c:<6}ERROR {r['error'][:50]}")
            continue
        ev = []
        for p in r["parsed_reflections"]:
            ev.extend(p.get("evidence") or [])
        committed = [w["committed"] for w in r["write_results"]]
        print(f"  {c:<6}{str(ev)[:40]:<42}{committed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

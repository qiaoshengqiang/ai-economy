#!/usr/bin/env python3
"""
Strategy Target 引用协议诊断
============================================================

【唯一目标】

    诊断真实模型对 Strategy Target 的**引用偏好**：
      ① 没看到 strategy_id？
      ② 看到了多个表示后仍偏好人类可读名称？
      ③ 更适合上下文局部引用（[S1]）？

【🔴 严格不做】

    ❌ 修改 SelfModelWriteService · ❌ 允许 name 通过验证
    ❌ 模糊匹配 name · ❌ Runtime 猜测 target · ❌ name→id 自动转换
    ❌ 修改正式 Reflection 协议 · ❌ 实验器做映射

【变量控制（§三）】

    同一 Agent · 同一 Provider/Model · 同一 Experience · 同一 Task
    **只改变 Strategy 在 Context 里的表示方式**。
    **不加**任何「target 必须用 ID」的提示。

【为什么 stimulus 是手工构造的】

    生产 `CognitiveContextBuilder.to_text()` 的策略渲染格式是**固定的**，
    本轮要按条件改变它 —— 但**不能为了实验改生产渲染**（§十三）。
    所以实验器直接构造「模型实际看到的那段文本」：
      · Experience 部分与上一轮 Condition A **完全一致**
      · 只有 Strategy 表示按条件变化  ← 这就是被研究的变量
    Controlled Write 仍然走**生产的** `SelfModelWriteService`。
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
WORK = HERE / "diag_target"
RESULTS = HERE / "results"

EXP_ID = "exp_diag0001"
STRATEGY_ID = "strategy_diag0001"
STRATEGY_NAME = "STRATEGY_A"
FEEDBACK = "人物细节过多，导致结构返工。"
TASK = "写一个 200 字左右的短篇故事开头。"

#: 与上一轮**完全相同**的格式说明（不含 target 约定）
BASE_FORMAT = (
    "If you decide to revise your own strategies, put them in the "
    '"reflections" array, each shaped like:\n'
    '{"kind":"create_strategy|revise_strategy|abandon_strategy",'
    '"target":"<strategy id if needed>","proposed_change":{...},'
    '"reason":"...","evidence":[...],"scope":{...}}\n'
    '"reflections" may be absent or empty if you decide nothing should change.'
)

#: Experience 部分 —— 四个 Condition **完全一致**
EXPERIENCE_BLOCK = f"[E1] {EXP_ID}\n失败：{FEEDBACK}"


def _strategy_block(condition: str) -> str:
    """只有这一段按 Condition 变化（§四）。"""
    if condition == "A":
        # 当前生产格式：只有名字
        return (f"- {STRATEGY_NAME}\n"
                f"  Confidence: 0.8\n"
                f"  Scope: fiction / story_opening")
    if condition == "B":
        # + 真正的持久 ID，但**不加**任何说明
        return (f"- {STRATEGY_NAME}\n"
                f"  Strategy ID: {STRATEGY_ID}\n"
                f"  Confidence: 0.8\n"
                f"  Scope: fiction / story_opening")
    if condition == "C":
        # 上下文局部引用 + UUID
        return (f"[S1] {STRATEGY_NAME}\n"
                f"  Strategy ID: {STRATEGY_ID}\n"
                f"  Confidence: 0.8\n"
                f"  Scope: fiction / story_opening")
    if condition == "D":
        # 只给局部引用，**不展示 UUID**
        return (f"[S1] {STRATEGY_NAME}\n"
                f"  Confidence: 0.8\n"
                f"  Scope: fiction / story_opening")
    raise ValueError(condition)


def _stimulus(condition: str) -> str:
    return "\n".join([
        TASK, "",
        "Past experience you have access to:",
        EXPERIENCE_BLOCK, "",
        "Relevant strategies I currently hold:",
        _strategy_block(condition), "",
        "Decide for yourself whether this experience is worth "
        "changing your strategies.",
        "", BASE_FORMAT,
    ])


def _classify(target: str) -> str:
    t = (target or "").strip()
    if t in (STRATEGY_NAME,):
        return "name"
    if t in (STRATEGY_ID,):
        return "uuid"
    if t.strip("[]").upper() == "S1":
        return "local_reference"
    if not t:
        return "empty"
    return "other"


def run_condition(condition: str, agent_id: str, provider: str | None,
                  model: str | None) -> dict:
    from main import build_runtime
    from experience import SOURCE_FEEDBACK, ExperienceStore
    from reflection import SelfModelWriteService
    from selfmodel import SelfModelStore
    from actions.parse import extract_envelope
    from reflection.parse import parse_reflections

    d = WORK / f"cond_{condition}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

    rt = build_runtime(db_path=str(d / "diag.db"), agent_ids=[agent_id])
    agent = next(a for a in rt.economy.all_agents() if a.agent_id == agent_id)
    if provider:
        agent.brain.settings.provider = provider
    if model:
        agent.brain.settings.model = model

    sm = SelfModelStore(rt.economy.db)
    ex = ExperienceStore(rt.economy.db)
    svc = SelfModelWriteService(sm, experience_store=ex)

    # 🔒 四个 Condition 同一个 strategy_id、同一个 experience_id —— 完全可比
    sm.add_strategy(agent_id, STRATEGY_NAME, domain="fiction",
                    situation="story_opening", confidence=0.8,
                    rationale="先把人物写厚，情节自然会长出来",
                    strategy_id=STRATEGY_ID)
    ex.record(agent_id, SOURCE_FEEDBACK, feedback=FEEDBACK,
              experience_id=EXP_ID,
              scope={"domain": "fiction", "situation": "story_opening"})

    stimulus = _stimulus(condition)
    t0 = time.perf_counter()
    response = agent.generate(messages=stimulus)
    dt = (time.perf_counter() - t0) * 1000.0

    raw_text = getattr(response, "content", "") or ""
    envelope = extract_envelope(raw_text)
    proposals = parse_reflections(agent_id, envelope) if envelope else []
    write_results = []
    for p in proposals:
        try:
            o = svc.apply(p)
            write_results.append({"kind": p.kind, "target": p.target,
                                  "evidence": list(p.evidence),
                                  "committed": o.committed,
                                  "code": o.validation.code,
                                  "reason": o.validation.reason})
        except Exception as exc:                          # noqa: BLE001
            write_results.append({"kind": p.kind, "target": p.target,
                                  "error": f"{type(exc).__name__}: {exc}"})

    rows = rt.economy.db.fetchall(
        "SELECT input_tokens, output_tokens, total_tokens, latency_ms, "
        "cost_ai, model, simulated FROM llm_calls WHERE agent_id=?",
        (agent_id,))
    cost = {"calls": len(rows),
            "tokens": sum(int(r["total_tokens"] or 0) for r in rows),
            "cost_ai": round(sum(float(r["cost_ai"] or 0) for r in rows), 6),
            "latency_ms": round(sum(float(r["latency_ms"] or 0)
                                    for r in rows), 1),
            "model": rows[-1]["model"] if rows else None,
            "simulated": any(bool(r["simulated"]) for r in rows)}

    payload = {
        "condition": condition,
        "provider": agent.brain.settings.provider,
        "model": agent.brain.settings.model or "(默认)",
        "strategy_shown_as": _strategy_block(condition),
        "strategy_id_available_in_context": condition in ("B", "C"),
        "raw_model_text": raw_text,          # 🔴 §六：保留原始输出
        "envelope": envelope,
        "parsed_reflections": [p.to_dict() for p in proposals],
        "reflection_rate": 1 if proposals else 0,
        "targets": [p.target for p in proposals],
        "target_forms": [_classify(p.target) for p in proposals],
        "write_results": write_results,
        "wall_ms": round(dt, 1),
        "cost": cost,
    }
    rt.economy.close()
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Strategy Target 引用协议诊断")
    ap.add_argument("--agent", default="agent_a")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--conditions", default="A,B,C,D")
    args = ap.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    out = {}
    for cond in [c.strip().upper() for c in args.conditions.split(",") if c.strip()]:
        print(f"\n{'='*66}\n  Condition {cond}\n{'='*66}")
        try:
            res = run_condition(cond, args.agent, args.provider, args.model)
        except Exception as exc:                          # noqa: BLE001
            print(f"  ❌ {type(exc).__name__}: {exc}")
            res = {"condition": cond, "error": f"{type(exc).__name__}: {exc}"}
        out[cond] = res
        (RESULTS / f"target_condition_{cond}.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        if "error" not in res:
            print(f"  reflection_rate={res['reflection_rate']}")
            print(f"  targets={res['targets']} forms={res['target_forms']}")
            print(f"  write={res['write_results']}")

    (RESULTS / "target_summary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*66}\n  汇总\n{'='*66}")
    print(f"  {'cond':<5}{'refl':<6}{'target':<24}{'form':<16}{'write'}")
    forms = {}
    for c, r in out.items():
        if "error" in r:
            print(f"  {c:<5}ERROR")
            continue
        w = [x.get("committed") for x in r["write_results"]] or ["-"]
        tg = ", ".join(r["targets"]) or "-"
        fm = ", ".join(r["target_forms"]) or "-"
        print(f"  {c:<5}{r['reflection_rate']:<6}{tg[:22]:<24}{fm:<16}{w}")
        for f in r["target_forms"]:
            forms[f] = forms.get(f, 0) + 1
    print(f"\n  Target form 统计: {forms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

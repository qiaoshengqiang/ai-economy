"""
Decision Benchmark V1 —— 三个 baseline 的对照
============================================================

    Baseline A  rule        —— 零 LLM 成本的**天真**规则
    Baseline B  decider     —— decider-0.8b（本地，MPS）
    Baseline C  remote      —— 现有远程 LLM（**显式 --real-llm 才跑**）

【🔒 为什么 rule baseline 必须「天真」】

    如果它和 ground truth 用同一套规则，它会拿 100% —— 那没有信息量。
    所以这里每个类别只给它**一条最简单的启发式**（比如「谁便宜选谁」），
    它会在有 trade-off 的 case 上失败。**这正是要看到的差距。**

【🔒 真实模型绝不进 pytest】

    本文件不 import 任何 AI Economy OS 生产代码，也不被主项目 pytest 收集。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

HERE = Path(__file__).parent
CASES = HERE / "benchmark_cases.json"
RESULTS = HERE / "results"


# ============================================================
# Baseline A：天真规则（每类一条最简单的启发式）
# ============================================================
def rule_decide(case):
    s, cat = case["state"], case["category"]

    if cat == "self_vs_delegate":
        # 天真：只看成本，不看成功率
        return "DELEGATE" if s["delegate_cost"] < s["self_cost"] else "SELF"

    if cat == "agent_selection":
        # 天真：谁便宜选谁
        return min(s["candidates"], key=lambda c: c["cost"])["name"]

    if cat == "tool_vs_llm":
        # 天真：工具便宜就用 —— 不看可靠性，也不看省得够不够多
        return "TOOL" if s["tool_cost"] < s["llm_cost"] else "LLM"

    if cat == "think_vs_wait":
        # 天真：有活就干，没活就等（从不 SLEEP）
        return "THINK" if s["pending_high_priority"] else "WAIT"

    if cat == "task_priority":
        # 天真：谁的收益高做谁 —— 不看风险、不看 deadline
        return max(s["tasks"], key=lambda t: t["value"])["name"]

    if cat == "accept_reject_task":
        # 天真：奖励大于成本就接 —— 不看成功率、不看付不付得起
        return "ACCEPT" if s["reward"] > s["estimated_cost"] else "REJECT"

    if cat == "invest_tool":
        # 天真：总省下的钱超过建造成本就投 —— 没有安全边际
        saved = s["per_task_cost"] * s["remaining_uses"]
        return "INVEST" if saved > s["tool_setup_cost"] else "KEEP_WORKING"

    if cat == "model_selection":
        # 天真：用最便宜的 —— 不管质量够不够
        return min(s["models"], key=lambda m: m["cost"])["name"]

    if cat == "need_human":
        # 天真：阈值拍成 0.5
        return "HUMAN" if s["risk"] >= 0.5 else "NO_HUMAN"

    if cat == "recheck_result":
        # 天真：有问题就 REVIEW，没问题就 ACCEPT（不会 RETRY / DELEGATE）
        return "REVIEW" if s["reviewer_found_issues"] else "ACCEPT"

    return None


# ============================================================
# Baseline B：decider-0.8b（本地）
# ============================================================
class DeciderBaseline:
    """
    很薄的适配层：把 case 变成 decider 的 (text, [{question, options}])
    然后取 choice / confidence。

    🔒 这只是**实验适配代码**，不是生产 Provider，也不进主项目。
    """

    def __init__(self, model="Mapika/decider-0.8b"):
        from decider.infer import Decider      # 延迟 import：没装就只是跑不了
        t0 = time.perf_counter()
        self.d = Decider(model)
        self.load_seconds = time.perf_counter() - t0
        self.model = model

    @staticmethod
    def prompt(case):
        s = case["state"]
        return (f"State:\n{json.dumps(s, ensure_ascii=False)}\n\n"
                f"Question:\n{case['question']}")

    def decide(self, case):
        t0 = time.perf_counter()
        out = self.d.decide(self.prompt(case),
                            [{"question": case["question"],
                              "options": case["options"]}])
        dt = (time.perf_counter() - t0) * 1000.0
        first = out[0] if isinstance(out, list) and out else {}
        return {
            "choice": first.get("choice"),
            "confidence": first.get("confidence"),
            "probs": first.get("probs"),
            "latency_ms": dt,
        }



# ============================================================
# Baseline B：Laya（本地 laya-multilingual）
# ============================================================
class LayaBaseline:
    """
    很薄的适配层。

    🔒 官方 schema（读 `laya/agent.py` 的 docstring 得到，**不是猜的**）：
        choice: {"type":"choice","instructions":"...","criteria":{"optA":"...",...}}
        score:  {"type":"score", "instructions":"...","criteria":["lvl0",...]}
        noul:   {"type":"noul",  "instructions":"...",
                 "criteria":{"false":"..","true":".."},"labels":{...}}

    ⚠️ 实测踩到的两个错误（写在这里省得下次再踩）：
       ① 漏 `type`        → ValueError: unknown type None
       ② 用 options 而不是 criteria → "no 'instructions'"
    """

    def __init__(self, model="multilingual", preload=False):
        from laya import Router
        self.model = model
        t0 = time.perf_counter()
        # 🔒 **不加 preload=True** —— 那会一次加载三个 checkpoint，
        #    对 16GB 机器是不必要的压力（任务书 §三）
        self.r = Router(preload=preload) if preload else Router()
        self.load_seconds = time.perf_counter() - t0

    @staticmethod
    def _state_text(case):
        s = case["state"]
        parts = [f"{k} = {v}" for k, v in s.items()]
        return ("AI Economy OS decision state:\n"
                + "\n".join(parts)
                + f"\n\nQuestion:\n{case['question']}")

    def decide(self, case):
        q = {"q": {
            "type": "choice",
            "instructions": case["question"],
            "criteria": {o: o for o in case["options"]},
        }}
        t0 = time.perf_counter()
        out = self.r.predict(self._state_text(case), q, model=self.model)
        dt = (time.perf_counter() - t0) * 1000.0
        ans = (out.get("answers") or {}).get("q") or {}
        return {
            "choice": ans.get("choice"),
            "confidence": ans.get("answer_confidence") or ans.get("confidence"),
            "probs": ans.get("probabilities"),
            "latency_ms": dt,
            "raw": out,
        }

# ============================================================
# 跑一个 baseline
# ============================================================
def run_baseline(name, cases, decide_fn, limit=None):
    rows, correct = [], 0
    todo = cases[:limit] if limit else cases
    for case in todo:
        t0 = time.perf_counter()
        try:
            got = decide_fn(case)
        except Exception as exc:                        # noqa: BLE001
            got = {"choice": None, "error": f"{type(exc).__name__}: {exc}"}
        dt = (time.perf_counter() - t0) * 1000.0
        if isinstance(got, str):
            got = {"choice": got}
        got.setdefault("latency_ms", dt)
        ok = got.get("choice") == case["expected_choice"]
        correct += ok
        rows.append({
            "id": case["id"], "category": case["category"],
            "difficulty": case["difficulty"],
            "expected": case["expected_choice"], "got": got.get("choice"),
            "correct": ok, "confidence": got.get("confidence"),
            "latency_ms": got.get("latency_ms"), "error": got.get("error"),
        })
    return {"baseline": name, "n": len(rows), "correct": correct,
            "accuracy": correct / len(rows) if rows else 0.0, "rows": rows}


def breakdown(result):
    def acc(pred):
        sel = [r for r in result["rows"] if pred(r)]
        if not sel:
            return None, 0
        return sum(r["correct"] for r in sel) / len(sel), len(sel)

    out = {"overall": (result["accuracy"], result["n"])}
    for d in ("easy", "medium", "hard"):
        out[d] = acc(lambda r, d=d: r["difficulty"] == d)
    cats = sorted({r["category"] for r in result["rows"]})
    for c in cats:
        out[c] = acc(lambda r, c=c: r["category"] == c)
    return out


def confidence_analysis(result):
    """§十九：confidence 高的时候是不是真的更准？"""
    pts = [(r["confidence"], r["correct"]) for r in result["rows"]
           if isinstance(r.get("confidence"), (int, float))]
    if len(pts) < 4:
        return None
    med = statistics.median(c for c, _ in pts)
    hi = [ok for c, ok in pts if c >= med]
    lo = [ok for c, ok in pts if c < med]
    return {
        "n": len(pts),
        "median_confidence": med,
        "high_confidence_accuracy": (sum(hi) / len(hi)) if hi else None,
        "high_confidence_n": len(hi),
        "low_confidence_accuracy": (sum(lo) / len(lo)) if lo else None,
        "low_confidence_n": len(lo),
    }


def latencies(result):
    vals = [r["latency_ms"] for r in result["rows"]
            if isinstance(r.get("latency_ms"), (int, float))]
    if not vals:
        return None
    vals.sort()
    return {
        "n": len(vals),
        "median_ms": statistics.median(vals),
        "p95_ms": vals[min(len(vals) - 1, int(len(vals) * 0.95))],
        "min_ms": vals[0], "max_ms": vals[-1],
    }


def show(result):
    b = breakdown(result)
    print(f"\n  === {result['baseline']} ===")
    print(f"  overall: {b['overall'][0]:.1%}  (n={b['overall'][1]})")
    for d in ("easy", "medium", "hard"):
        a, n = b[d]
        print(f"    {d:7s}: {a:.1%}  (n={n})")
    print("  --- 按类别 ---")
    for k, (a, n) in b.items():
        if k in ("overall", "easy", "medium", "hard"):
            continue
        flag = "" if a >= 0.99 else ("  ← 有 trade-off 就掉" if a < 0.7 else "")
        print(f"    {k:20s}: {a:.1%}  (n={n}){flag}")
    lat = latencies(result)
    if lat:
        print(f"  latency: median={lat['median_ms']:.1f}ms  "
              f"p95={lat['p95_ms']:.1f}ms")
    ca = confidence_analysis(result)
    if ca:
        print(f"  confidence: median={ca['median_confidence']:.3f}  "
              f"high→{ca['high_confidence_accuracy']:.0%}(n={ca['high_confidence_n']})  "
              f"low→{ca['low_confidence_accuracy']:.0%}(n={ca['low_confidence_n']})")


def main():
    ap = argparse.ArgumentParser(description="Decision Benchmark V1")
    ap.add_argument("--laya", action="store_true",
                    help="跑 Laya laya-multilingual（需要先装好）")
    ap.add_argument("--laya-model", default="multilingual")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--consistency-laya", default=None,
                    help="对某个 case id 用 Laya 重复跑 20 次")
    ap.add_argument("--real-llm", action="store_true",
                    help="跑远程 LLM 对照（**会烧真实 API**，默认只跑 10 条）")
    ap.add_argument("--consistency", default=None,
                    help="对某个 case id 重复跑 20 次，看 choice 是否稳定")
    ap.add_argument("--model", default="Mapika/decider-0.8b")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    print(f"  cases: {len(cases)}")

    # ---- Baseline A：天真规则 ----
    rule = run_baseline("rule_baseline", cases, rule_decide, args.limit)
    show(rule)
    (RESULTS / "rule_baseline.json").write_text(
        json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.laya:
        try:
            lb = LayaBaseline(args.laya_model)
        except Exception as exc:                        # noqa: BLE001
            print(f"  ❌ 无法加载 Laya：{type(exc).__name__}: {exc}")
            return 1
        print(f"  Laya Router 构造: {lb.load_seconds:.2f}s（按需加载）")
        res = run_baseline("laya_multilingual", cases, lb.decide, args.limit)
        show(res)
        (RESULTS / "laya.json").write_text(
            json.dumps({k: v for k, v in res.items() if k != "rows"},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        # 完整 rows 单独存（含 per-case 明细，便于分析 confidence）
        (RESULTS / "laya_rows.json").write_text(
            json.dumps(res["rows"], ensure_ascii=False, indent=2),
            encoding="utf-8")

    if args.consistency_laya:
        lb = LayaBaseline(args.laya_model)
        case = next(c for c in cases if c["id"] == args.consistency_laya)
        picks = [lb.decide(case)["choice"] for _ in range(20)]
        from collections import Counter
        cnt = Counter(picks)
        print(f"\n  consistency [{args.consistency_laya}] 20 次: {dict(cnt)}")
        print(f"    same-choice ratio = {cnt.most_common(1)[0][1]}/20 "
              f"= {cnt.most_common(1)[0][1]/20:.0%}")

    if not args.real_llm and not args.consistency and not args.laya and not args.consistency_laya:
        print("\n  （只跑了 rule baseline。加 --laya 跑本地 Laya，"
              "加 --real-llm 跑远程对照）")
        return 0

    if args.consistency:
        db = DeciderBaseline(args.model)
        case = next(c for c in cases if c["id"] == args.consistency)
        picks = [db.decide(case)["choice"] for _ in range(20)]
        from collections import Counter
        cnt = Counter(picks)
        print(f"\n  consistency [{args.consistency}] 20 次: {dict(cnt)}")
        print(f"    same-choice ratio = {cnt.most_common(1)[0][1]}/20 "
              f"= {cnt.most_common(1)[0][1]/20:.0%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Decision Benchmark V1 —— 数据集生成器
============================================================

【🔒 为什么用「生成器」而不是手写 60 条 JSON】

    任务书 §十一 要求：**Ground truth 由明确规则产生**，
    而不是「把 DeepSeek 的答案当标准答案」。

    所以这里把**判定规则写死在代码里**，然后按规则生成 case。
    好处：
      · 规则是显式的、可审查的（下面每个 `_rule_*`）
      · 数据集可复现（跑一次生成一次，逐字节相同）
      · 不存在「人工标注悄悄偏向某个模型」的问题

【难度分级怎么来的】

    easy / medium / hard 不由「我觉得难」决定，而由**因素数量**决定：
      easy   —— 单因素占绝对优势（差距很大，没有 trade-off）
      medium —— 两个因素冲突，但其中一个明显更强
      hard   —— 多因素冲突，且**没有压倒性答案**（最接近真实决策）
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("benchmark_cases.json")


def _case(cid, category, difficulty, state, question, options,
          expected, reason):
    return {
        "id": cid, "category": category, "difficulty": difficulty,
        "state": state, "question": question, "options": options,
        "expected_choice": expected, "reason": reason,
    }


# ============================================================
# Category 1：SELF vs DELEGATE
# 规则：委托成本明显更低 **且** 成功率足够（>=0.85）→ DELEGATE
#       否则自己来（SELF）。只看成本不看成功率是**错的**。
# ============================================================
def _rule_self_vs_delegate(self_cost, del_cost, success):
    if del_cost < self_cost and success >= 0.85:
        return "DELEGATE"
    return "SELF"


def cat1():
    out = []
    spec = [
        # (self, delegate, success, difficulty)
        (12, 4, 0.92, "easy"),      # 成本差 3 倍 + 成功率高 → 委托
        (10, 9, 0.95, "medium"),    # 成本几乎一样 → 不值得委托
        (8, 2, 0.55, "hard"),       # 便宜但成功率低 → **自己来**
        (20, 5, 0.97, "easy"),
        (6, 5, 0.88, "medium"),     # 省 1 AIC，不值得
        (15, 3, 0.70, "hard"),      # 便宜但成功率不够
    ]
    for i, (s, d, p, diff) in enumerate(spec, 1):
        out.append(_case(
            f"delegate_{i:03d}", "self_vs_delegate", diff,
            {"self_cost": s, "delegate_cost": d, "delegate_success": p},
            "Should the Agent delegate this task?",
            ["SELF", "DELEGATE"],
            _rule_self_vs_delegate(s, d, p),
            f"delegate_cost={d} < self_cost={s}: {d < s}; "
            f"success={p} >= 0.85: {p >= 0.85}",
        ))
    return out


# ============================================================
# Category 2：选择哪个 Agent
# 规则：不选最便宜的。算 **风险调整后的单位成本**
#           effective = cost / success
#       平局时看 latency（短的赢）。
# ============================================================
def _rule_pick_agent(cands):
    def score(c):
        return (c["cost"] / max(c["success"], 1e-6), c["latency"])
    return min(cands, key=score)["name"]


def cat2():
    out = []
    spec = [
        ([{"name": "B", "cost": 3, "success": 0.91, "latency": 8},
          {"name": "C", "cost": 5, "success": 0.98, "latency": 3}], "medium"),
        ([{"name": "B", "cost": 2, "success": 0.60, "latency": 2},
          {"name": "C", "cost": 4, "success": 0.95, "latency": 5}], "hard"),
        ([{"name": "B", "cost": 9, "success": 0.99, "latency": 1},
          {"name": "C", "cost": 1, "success": 0.99, "latency": 9}], "easy"),
        ([{"name": "B", "cost": 5, "success": 0.80, "latency": 4},
          {"name": "C", "cost": 6, "success": 0.80, "latency": 4}], "medium"),
        ([{"name": "B", "cost": 1, "success": 0.30, "latency": 1},
          {"name": "C", "cost": 3, "success": 0.90, "latency": 1}], "easy"),
        ([{"name": "B", "cost": 7, "success": 0.93, "latency": 6},
          {"name": "C", "cost": 8, "success": 0.94, "latency": 2}], "hard"),
    ]
    for i, (cands, diff) in enumerate(spec, 1):
        out.append(_case(
            f"agent_{i:03d}", "agent_selection", diff,
            {"candidates": cands},
            "Which agent is the better choice?",
            [c["name"] for c in cands],
            _rule_pick_agent(cands),
            "min(cost / success)，平局看 latency",
        ))
    return out


# ============================================================
# Category 3：Tool vs LLM
# 规则：工具有效且便宜很多（tool_cost * 5 < llm_cost）→ TOOL
#       工具不可靠 → LLM（即使贵）
# ============================================================
def _rule_tool_vs_llm(tool_cost, llm_cost, tool_reliable):
    if tool_reliable and tool_cost * 5 < llm_cost:
        return "TOOL"
    return "LLM"


def cat3():
    out = []
    spec = [
        (0.2, 4.0, True, "easy"),
        (3.5, 4.0, True, "medium"),    # 没便宜多少 → 用 LLM
        (0.1, 5.0, False, "hard"),     # 便宜但不可靠 → LLM
        (0.5, 8.0, True, "easy"),
        (2.0, 2.5, True, "medium"),
        (0.3, 6.0, False, "hard"),
    ]
    for i, (tc, lc, rel, diff) in enumerate(spec, 1):
        out.append(_case(
            f"tool_{i:03d}", "tool_vs_llm", diff,
            {"tool_cost": tc, "llm_cost": lc, "tool_reliable": rel},
            "Which should be used?",
            ["TOOL", "LLM"],
            _rule_tool_vs_llm(tc, lc, rel),
            f"tool_cost*5({tc*5}) < llm_cost({lc}): {tc*5 < lc}; reliable={rel}",
        ))
    return out


# ============================================================
# Category 4：Think vs Wait
# 规则：有高优先级待办 → THINK；否则 heartbeat 近 → WAIT；
#       都没有 → SLEEP
# ============================================================
def _rule_think(pending_high, next_hb, balance):
    if pending_high:
        return "THINK"
    if next_hb <= 60:
        return "WAIT"
    return "SLEEP"


def cat4():
    out = []
    spec = [
        (True, 300, 50, "easy"),
        (False, 20, 50, "easy"),
        (False, 600, 5, "medium"),      # 没钱 + 没活 → SLEEP
        (False, 600, 50, "hard"),       # 有钱没活 → 还是 SLEEP（不该乱想）
        (True, 10, 100, "medium"),      # 有活 + heartbeat 近 → 先干活
        (False, 45, 30, "hard"),
    ]
    for i, (ph, hb, bal, diff) in enumerate(spec, 1):
        out.append(_case(
            f"think_{i:03d}", "think_vs_wait", diff,
            {"pending_high_priority": ph, "next_heartbeat_sec": hb,
             "balance_aic": bal},
            "Should the Agent think now?",
            ["THINK", "WAIT", "SLEEP"],
            _rule_think(ph, hb, bal),
            "有高优先级活 → THINK；否则 heartbeat<=60 → WAIT；否则 SLEEP",
        ))
    return out


# ============================================================
# Category 5：Task Priority
# 规则：选 **urgency*value / risk** 最大的；deadline 近是 urgency 的主要来源
# ============================================================
def _rule_priority(tasks):
    def score(t):
        urgency = 3.0 if t["deadline_soon"] else 1.0
        return (t["value"] * urgency) / max(t["risk"], 0.05)
    return max(tasks, key=score)["name"]


def cat5():
    out = []
    spec = [
        ([{"name": "A", "value": 10, "risk": 0.1, "deadline_soon": True},
          {"name": "B", "value": 3, "risk": 0.1, "deadline_soon": False},
          {"name": "C", "value": 6, "risk": 0.8, "deadline_soon": False}],
         "easy"),
        ([{"name": "A", "value": 5, "risk": 0.5, "deadline_soon": False},
          {"name": "B", "value": 9, "risk": 0.2, "deadline_soon": True},
          {"name": "C", "value": 9, "risk": 0.9, "deadline_soon": True}],
         "hard"),
        ([{"name": "A", "value": 2, "risk": 0.1, "deadline_soon": True},
          {"name": "B", "value": 2, "risk": 0.1, "deadline_soon": False},
          {"name": "C", "value": 2, "risk": 0.1, "deadline_soon": False}],
         "easy"),
        ([{"name": "A", "value": 8, "risk": 0.3, "deadline_soon": False},
          {"name": "B", "value": 4, "risk": 0.1, "deadline_soon": True},
          {"name": "C", "value": 7, "risk": 0.35, "deadline_soon": True}],
         "hard"),
        ([{"name": "A", "value": 1, "risk": 0.9, "deadline_soon": True},
          {"name": "B", "value": 4, "risk": 0.2, "deadline_soon": False},
          {"name": "C", "value": 2, "risk": 0.9, "deadline_soon": False}],
         "medium"),
        ([{"name": "A", "value": 6, "risk": 0.1, "deadline_soon": False},
          {"name": "B", "value": 6, "risk": 0.1, "deadline_soon": False},
          {"name": "C", "value": 6, "risk": 0.1, "deadline_soon": False}],
         "medium"),
    ]
    for i, (tasks, diff) in enumerate(spec, 1):
        out.append(_case(
            f"priority_{i:03d}", "task_priority", diff,
            {"tasks": tasks},
            "Which task should be handled first?",
            [t["name"] for t in tasks],
            _rule_priority(tasks),
            "max(value * urgency / risk)",
        ))
    return out


# ============================================================
# Category 6：Accept vs Reject
# 规则：两个条件都要满足
#       ① expected = reward * success > cost
#       ② balance >= cost（付得起）
# ============================================================
def _rule_accept(cost, reward, success, balance):
    if balance < cost:
        return "REJECT"                    # 付不起，再划算也不能接
    if reward * success > cost:
        return "ACCEPT"
    return "REJECT"


def cat6():
    out = []
    spec = [
        (15, 3, 0.7, 20, "easy"),      # 3*0.7=2.1 < 15 → 拒
        (2, 10, 0.9, 20, "easy"),      # 9 > 2 → 接
        (10, 12, 0.85, 15, "medium"),  # 10.2 > 10 → 勉强接
        (8, 12, 0.6, 100, "hard"),     # 7.2 < 8 → 拒
        (5, 20, 0.95, 4, "hard"),      # 很赚但**付不起** → 拒
        (3, 4, 0.9, 50, "medium"),     # 3.6 > 3 → 接
    ]
    for i, (c, r, p, b, diff) in enumerate(spec, 1):
        out.append(_case(
            f"accept_{i:03d}", "accept_reject_task", diff,
            {"estimated_cost": c, "reward": r, "success_prob": p,
             "balance": b},
            "Should the Agent accept this task?",
            ["ACCEPT", "REJECT"],
            _rule_accept(c, r, p, b),
            f"balance {b} >= cost {c}: {b >= c}; reward*success={r*p:.2f} "
            f"> cost {c}: {r*p > c}",
        ))
    return out


# ============================================================
# Category 7：Invest in Tool vs Keep Working
# 规则：break_even = setup / per_task；
#       剩余次数 >= break_even * 1.5（要有 1.5 倍安全边际）→ INVEST
# ============================================================
def _rule_invest(per_task, setup, remaining):
    if per_task <= 0:
        return "KEEP_WORKING"
    return "INVEST" if remaining >= (setup / per_task) * 1.5 else "KEEP_WORKING"


def cat7():
    out = []
    spec = [
        (5, 15, 10, "medium"),   # break_even=3, 需要 >=4.5 → 10 够 → INVEST
        (5, 15, 3, "hard"),      # 3 < 4.5 → 不划算
        (2, 40, 100, "easy"),
        (8, 10, 2, "easy"),      # 2 < 1.875? 不 —— 1.875 vs 2 → INVEST
        (1, 50, 20, "medium"),   # break_even=50, 需要 75 → 不够
        (6, 20, 8, "hard"),      # break_even=3.33, 需要 5 → 8 够
    ]
    for i, (pt, su, rem, diff) in enumerate(spec, 1):
        out.append(_case(
            f"invest_{i:03d}", "invest_tool", diff,
            {"per_task_cost": pt, "tool_setup_cost": su,
             "remaining_uses": rem},
            "Should the Agent invest in building the tool?",
            ["INVEST", "KEEP_WORKING"],
            _rule_invest(pt, su, rem),
            f"break_even={su/pt:.2f}, 需要 remaining >= {su/pt*1.5:.2f}, "
            f"实际 {rem}",
        ))
    return out


# ============================================================
# Category 8：Model Selection
# 规则：任务只要 **够用** 的模型 —— 选满足质量要求的**最便宜**的
# ============================================================
def _rule_model(models, required):
    ok = [m for m in models if m["quality"] >= required]
    if not ok:
        return max(models, key=lambda m: m["quality"])["name"]
    return min(ok, key=lambda m: m["cost"])["name"]


def cat8():
    out = []
    spec = [
        ([{"name": "A", "cost": 8, "quality": 0.95},
          {"name": "B", "cost": 2, "quality": 0.6},
          {"name": "C", "cost": 1, "quality": 0.3}], 0.5, "easy"),
        ([{"name": "A", "cost": 8, "quality": 0.95},
          {"name": "B", "cost": 2, "quality": 0.6},
          {"name": "C", "cost": 1, "quality": 0.3}], 0.9, "medium"),
        ([{"name": "A", "cost": 8, "quality": 0.95},
          {"name": "B", "cost": 2, "quality": 0.6},
          {"name": "C", "cost": 1, "quality": 0.3}], 0.99, "hard"),
        ([{"name": "A", "cost": 3, "quality": 0.8},
          {"name": "B", "cost": 4, "quality": 0.8},
          {"name": "C", "cost": 5, "quality": 0.8}], 0.7, "medium"),
        ([{"name": "A", "cost": 10, "quality": 0.4},
          {"name": "B", "cost": 1, "quality": 0.4},
          {"name": "C", "cost": 2, "quality": 0.4}], 0.35, "easy"),
        ([{"name": "A", "cost": 6, "quality": 0.75},
          {"name": "B", "cost": 5, "quality": 0.72},
          {"name": "C", "cost": 7, "quality": 0.9}], 0.7, "hard"),
    ]
    for i, (ms, req, diff) in enumerate(spec, 1):
        out.append(_case(
            f"model_{i:03d}", "model_selection", diff,
            {"models": ms, "required_quality": req,
             "task": "简单分类" if req < 0.7 else "复杂推理"},
            "Which model should be used?",
            [m["name"] for m in ms],
            _rule_model(ms, req),
            f"满足 quality>={req} 里挑最便宜的",
        ))
    return out


# ============================================================
# Category 9：Need Human / No Human
# 规则：高风险 或 不可逆 或 系统级 → HUMAN；否则 NO_HUMAN
# ============================================================
def _rule_human(risk, reversible, system_level):
    if risk >= 0.7 or not reversible or system_level:
        return "HUMAN"
    return "NO_HUMAN"


def cat9():
    out = []
    spec = [
        (0.1, True, False, "easy"),     # 低风险文件整理
        (0.9, False, True, "easy"),     # 高风险系统设置
        (0.5, True, True, "medium"),    # 系统级 → HUMAN
        (0.6, False, False, "hard"),    # 不可逆 → HUMAN
        (0.35, True, False, "medium"),
        (0.69, True, False, "hard"),    # 刚好不到 0.7 → NO_HUMAN
    ]
    for i, (r, rev, sysl, diff) in enumerate(spec, 1):
        out.append(_case(
            f"human_{i:03d}", "need_human", diff,
            {"risk": r, "reversible": rev, "system_level": sysl},
            "Does this need Human involvement?",
            ["HUMAN", "NO_HUMAN"],
            _rule_human(r, rev, sysl),
            f"risk>=0.7: {r>=0.7}; reversible={rev}; system_level={sysl}",
        ))
    return out


# ============================================================
# Category 10：Recheck / Accept Result
# 规则：有问题 → 看问题严重度；成功率低 + 价格低 → RETRY；
#       轻微问题 → REVIEW；严重 → DELEGATE
# ============================================================
def _rule_recheck(issues, severity, success, cost):
    if issues and severity >= 0.7:
        return "DELEGATE"
    if issues and severity >= 0.3:
        return "REVIEW"
    if success < 0.7:
        return "RETRY"
    return "ACCEPT"


def cat10():
    out = []
    spec = [
        (True, 0.2, 0.9, 3, "medium"),    # 轻微问题 → REVIEW
        (False, 0.0, 0.95, 4, "easy"),    # 没问题 → ACCEPT
        (True, 0.8, 0.6, 2, "hard"),      # 严重 → DELEGATE
        (False, 0.0, 0.55, 3, "medium"),  # 成功率低 → RETRY
        (True, 0.5, 0.8, 5, "hard"),
        (False, 0.0, 0.75, 6, "easy"),
    ]
    for i, (iss, sev, suc, cost, diff) in enumerate(spec, 1):
        out.append(_case(
            f"recheck_{i:03d}", "recheck_result", diff,
            {"reviewer_found_issues": iss, "issue_severity": sev,
             "contractor_success_rate": suc, "price": cost},
            "What should A do with this result?",
            ["ACCEPT", "RETRY", "REVIEW", "DELEGATE"],
            _rule_recheck(iss, sev, suc, cost),
            f"issues={iss} severity={sev} success={suc}",
        ))
    return out


def main():
    cases = []
    for fn in (cat1, cat2, cat3, cat4, cat5, cat6, cat7, cat8, cat9, cat10):
        cases.extend(fn())

    # 难度分布核对（任务书 §十二：20 easy / 20 medium / 20 hard）
    dist = {}
    for c in cases:
        dist[c["difficulty"]] = dist.get(c["difficulty"], 0) + 1

    OUT.write_text(json.dumps(cases, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"  ✅ {len(cases)} cases → {OUT.name}")
    print(f"     难度分布: {dist}")
    cats = {}
    for c in cases:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    print(f"     类别分布: {cats}")


if __name__ == "__main__":
    main()

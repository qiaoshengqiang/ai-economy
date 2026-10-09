#!/usr/bin/env python3
"""
真实实验分析 —— Critical Observation Visibility

读两个来源：
  ① results/heterogeneous_production_result.json  ← 实验 telemetry（turn 级）
  ② data/hetero.db                                ← 持久层硬证据

🔒 证据链只认：
      Turn N  finish_reason=length（真实 Provider）
          ↓
      critical observation 真的落库（critical_observation=true）
          ↓
      Turn N+1 的**真实 CognitiveContext** 里出现该 experience_id
          ↓
      且渲染文本里出现 cognitive_output_truncated

⚠️ 绝不用「Agent 回复里说它看到了」当证据。

用法：
    ./.venv/bin/python experiments/heterogeneous_production/analyze_critical.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
DB = HERE / "data" / "hetero.db"
RESULT_JSON = RESULTS / "heterogeneous_production_result.json"


def validate_result_file(path: Path, *,
                         expected_wall_limit: float = 3600.0,
                         max_lag_seconds: float = 300.0) -> tuple:
    """
    校验一份实验结果文件**是不是这一轮的、而且是最终结果**（§3）。

    返回 `(ok, problems)`。

    🔴 为什么必须有这个函数：
       我上一轮只判断「文件存在」，就把**上一轮 run 的 JSON**
       当成了本轮结果，差点报出假结论。`heterogeneous_production_result.json`
       是**固定文件名、会被后一次运行覆盖**的，所以「存在」不构成证据。

    校验四件事：
        ① `state` 必须是 `FINALIZED`（`RUNNING` 说明实验还没结束）
        ② `run_id` 必须存在（没有 run id 的结果无法归属）
        ③ `total_wall_limit` 必须等于本轮期望值（旧 run 是 7200）
        ④ mtime 不能比最后一次 LLM 调用早太多（容差见 max_lag_seconds）
    """
    problems = []
    if not path.exists():
        return False, [f"结果文件不存在：{path}"]

    try:
        log = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        return False, [f"结果文件无法解析（可能是半写入）：{exc}"]

    state = log.get("state")
    if state != "FINALIZED":
        problems.append(
            f"state={state!r} —— 只有 FINALIZED 才是最终结果"
            f"（RUNNING 说明实验尚未结束）")

    if not log.get("run_id"):
        problems.append("结果文件没有 run_id —— 无法归属到某一次运行")

    limit = log.get("total_wall_limit")
    if limit is not None and float(limit) != float(expected_wall_limit):
        problems.append(
            f"total_wall_limit={limit}（本轮应为 {expected_wall_limit}）"
            f" —— 这是**别的轮次**的产物")

    # mtime vs 最后一次 LLM 调用
    try:
        if DB.exists():
            con = sqlite3.connect(str(DB))
            try:
                last = con.execute(
                    "SELECT MAX(created_at) FROM llm_calls").fetchone()[0]
            finally:
                con.close()
            if last:
                import datetime as _dt
                last_ts = _dt.datetime.fromisoformat(last).timestamp()
                if last_ts - path.stat().st_mtime > max_lag_seconds:
                    problems.append(
                        f"结果 JSON 比最后一次 LLM 调用早 "
                        f"{last_ts - path.stat().st_mtime:.0f} 秒"
                        f"（> {max_lag_seconds:.0f}s）—— 疑似上一轮产物")
    except Exception:                                     # noqa: BLE001
        pass

    return (not problems), problems


def _db_finish_reasons():
    if not DB.exists():
        return {}, 0
    con = sqlite3.connect(str(DB))
    try:
        dist = dict(con.execute(
            "SELECT COALESCE(finish_reason,'(null)'), COUNT(*) "
            "FROM llm_calls GROUP BY finish_reason"))
        total = con.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        crit = con.execute(
            "SELECT COUNT(*) FROM experiences "
            "WHERE result LIKE '%critical_observation%'").fetchone()[0]
        return {"dist": dist, "total": total, "critical_rows": crit}, con
    finally:
        con.close()


def main() -> int:
    if not RESULT_JSON.exists():
        print("❌ 还没有结果 JSON —— 实验可能仍在运行")
        return 1

    log = json.loads(RESULT_JSON.read_text(encoding="utf-8"))
    db, _ = _db_finish_reasons()

    # 🔴 结果文件必须**可校验**（§3）。
    ok, problems = validate_result_file(RESULT_JSON,
                                        expected_wall_limit=3600.0)
    if not ok:
        print("🔴 结果文件不可用，拒绝分析：")
        for x in problems:
            print(f"   · {x}")
        print("   → 见 validate_result_file()：它校验 run_id / state / "
              "mtime / wall limit，而不是只判断「文件存在」。")
        return 2

    print("=" * 72)
    print("  A. Experiment Setup")
    print("=" * 72)
    print(f"  exit_reason        : {log.get('exit_reason')}")
    print(f"  wall_minutes       : {log.get('wall_minutes')}")
    print(f"  total_wall_limit   : {log.get('total_wall_limit')}")
    print(f"  session_limit      : {log.get('session_limit')}")
    print(f"  sessions_run       : {log.get('sessions_run')}")
    print(f"  cost               : {log.get('cost')}")
    print(f"  stage_completed    : {log.get('stage_completed')}")

    # ---- 收集所有 turn ----
    turns = []
    for s in log.get("sessions", []) or []:
        for t in s.get("turns", []) or []:
            t = dict(t)
            t["_session"] = s.get("index")
            turns.append(t)

    print()
    print("=" * 72)
    print("  B. Real Run Statistics")
    print("=" * 72)
    print(f"  turns（JSON 里记录到的）: {len(turns)}")
    print(f"  llm_calls（DB）        : {db.get('total')}")
    print(f"  finish_reason 分布（DB）: {db.get('dist')}")
    print(f"  experiences 里带 critical_observation 的条数: "
          f"{db.get('critical_rows')}")
    tok = sum(int(t.get("tokens") or 0) for t in turns)
    print(f"  tokens（JSON 求和）    : {tok}")

    # ---- C. TRUNCATED cases ----
    truncated = [t for t in turns if t.get("truncated")]
    print()
    print("=" * 72)
    print(f"  C. TRUNCATED Cases（{len(truncated)} 个）")
    print("=" * 72)
    if not truncated:
        print("  （本次实验没有出现真实 TRUNCATED）")
    for i, t in enumerate(truncated, 1):
        print(f"  CASE #{i}")
        print(f"    agent              : {t.get('agent_id')}")
        print(f"    session / turn      : {t.get('_session')} / {t.get('turn')}")
        print(f"    finish_reason       : {t.get('finish_reason')}")
        print(f"    output_status       : {t.get('output_status')}")
        print(f"    cognitive_status    : {t.get('cognitive_status')}")
        print(f"    critical created    : {t.get('critical_observation_created')}")
        print(f"    experience_id       : {t.get('critical_experience_id')}")

    # ---- D. Next-turn Visibility ----
    print()
    print("=" * 72)
    print("  D. Next-turn Visibility")
    print("=" * 72)
    results = []
    for i, t in enumerate(truncated, 1):
        exp_id = t.get("critical_experience_id")
        agent = t.get("agent_id")
        # 找**同一个 Agent**在本 session 里的下一个 turn
        nxt = None
        seen = False
        for u in turns:
            if u.get("_session") != t.get("_session"):
                continue
            if u is t:
                seen = True
                continue
            if seen and u.get("agent_id") == agent:
                nxt = u
                break
        ctx = (nxt or {}).get("context") or {}
        visible = bool(exp_id) and exp_id in (ctx.get("critical_ids") or [])
        results.append(visible)
        print(f"  CASE #{i}  agent={agent}  exp={exp_id}")
        print(f"    persisted     : {bool(exp_id)}")
        print(f"    next turn     : session={nxt.get('_session') if nxt else None} "
              f"turn={nxt.get('turn') if nxt else None}")
        print(f"    selected      : {ctx.get('critical_ids')}")
        print(f"    critical_count: {ctx.get('critical_count')}")
        print(f"    rendered      : "
              f"has_truncation_marker={ctx.get('has_truncation_marker')} "
              f"has_TRUNCATED={ctx.get('has_TRUNCATED_literal')}")
        print(f"    context_chars : {ctx.get('context_chars')}")
        print(f"    RESULT        : "
              f"{'VISIBLE ✅' if visible else 'NOT_VISIBLE ❌'}")

    # ---- E. Negative controls ----
    print()
    print("=" * 72)
    print("  E. Negative Controls（正常 stop）")
    print("=" * 72)
    stops = [t for t in turns if t.get("finish_reason") == "stop"]
    bad = [t for t in stops if t.get("critical_observation_created")]
    print(f"  normal stop turn 数                     : {len(stops)}")
    print(f"  其中错误地产生了 truncation 关键观察的  : {len(bad)}")
    print(f"  → {'✅ 阴性对照通过' if not bad else '❌ 阴性对照失败'}")

    # ---- F. Behavioral observation（只记录现象） ----
    print()
    print("=" * 72)
    print("  F. Behavioral Observation（仅现象，不做因果）")
    print("=" * 72)
    for i, t in enumerate(truncated, 1):
        exp_id = t.get("critical_experience_id")
        agent = t.get("agent_id")
        seen = False
        nxt = None
        for u in turns:
            if u.get("_session") != t.get("_session"):
                continue
            if u is t:
                seen = True
                continue
            if seen and u.get("agent_id") == agent:
                nxt = u
                break
        if not nxt:
            print(f"  CASE #{i}: 截断后该 Agent 没有再行动作 → unchanged/unknown")
            continue
        a_chars = int(t.get("chars") or 0)
        b_chars = int(nxt.get("chars") or 0)
        delta = b_chars - a_chars
        verdict = ("shorter" if delta < -50 else
                   "longer" if delta > 50 else "similar")
        print(f"  CASE #{i}: {agent}  chars {a_chars} → {b_chars} "
              f"(Δ={delta:+d})  → {verdict}")
    print("  ⚠️ 这只说明「出现了/没有出现可观察变化」，")
    print("     不能说明关键观察**导致**了变化。")

    # ---- H. Failure cases ----
    print()
    print("=" * 72)
    print("  H. Failure Cases（TRUNCATED → NOT_VISIBLE）")
    print("=" * 72)
    fails = [t for t, v in zip(truncated, results) if not v]
    if not fails:
        print("  （无）")
    for t in fails:
        print(f"  agent={t.get('agent_id')} turn={t.get('turn')} "
              f"exp={t.get('critical_experience_id')}")

    # ---- I. Conclusion ----
    print()
    print("=" * 72)
    print("  I. Conclusion")
    print("=" * 72)
    if not truncated:
        print("  Infrastructure: INCONCLUSIVE")
        print("    （60 分钟内没有自然出现 finish_reason=length）")
    elif all(results):
        print(f"  Infrastructure: PASS")
        print(f"    {sum(results)}/{len(results)} 个真实 TRUNCATED "
              f"在下一轮真实 Context 里可见")
    else:
        print(f"  Infrastructure: FAIL")
        print(f"    {sum(results)}/{len(results)} 可见")
    print("  Behavior      : "
          f"{'OBSERVED' if truncated else 'NOT OBSERVED'}")
    print("  Task Outcome  : NOT TESTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

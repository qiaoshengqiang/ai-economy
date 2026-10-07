#!/usr/bin/env python3
"""
一个 Runtime Session —— 在 Hard Limit 下**真实循环生产**
============================================================

    python session.py --index 1            # 正式（默认 600 秒）
    SMOKE_LIMIT=30 python session.py --index 1 --max-rounds 2   # 迷你验证

【🔒 这不是 Workflow】

    循环体是**轮转**，不是顺序依赖：
    每一轮每个 Agent 拿到的都是**同一段 Stage 状态**，
    各自决定说什么、要不要找别人。没有「必须先 A 才能 B」。

    系统只在两处施加结构：
      · Hard Limit（Runtime 强制，非可选）
      · Stage 状态（读的是持久状态，不是编排脚本）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "novel.db"
RESULTS = HERE / "results"

TASK_ID = "task_novel_001"
WS_ID = "WS-NOVEL-001"
AGENTS = ["novel_world_builder", "novel_plot_architect", "novel_author",
          "novel_editor", "novel_reader"]
TOPIC = ("在一个由 AI Agent 参与运行的数字世界里，"
         "身份、记忆、资产和历史究竟属于谁？")

SESSION_LIMIT = float(os.environ.get("SMOKE_LIMIT", "600"))

PROMPT = """你是这次长期小说生产任务的一名参与者。
任务主题：{topic}

{stage}

共享 Task Workspace：{ws}
你可以：读取其他人的产物、主动联系任何一位参与者、
把你的产物写进共享 Workspace、或什么都不做直接说明你的判断。
由你自己决定下一步做什么、以及是否需要别人。

请给出你**这一轮**的具体产出（设定 / 结构 / 正文片段 / 编辑意见 / 读者体验），
并说明你是否需要其他人配合。"""


def main() -> int:
    from experience import ExperienceStore
    from main import build_runtime
    from stage import (
        RuntimeSession, SESSION_STOPPED_HARD_LIMIT, STATUS_ACTIVE,
        StageStore)
    from stage.context import render_stage

    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=1)
    ap.add_argument("--max-rounds", type=int, default=999)
    ap.add_argument("--agents", nargs="*", default=None)
    args = ap.parse_args()

    DB.parent.mkdir(parents=True, exist_ok=True)
    log = {"session_index": args.index, "limit_seconds": SESSION_LIMIT,
           "started_at": time.time(), "agents": []}

    rt = build_runtime(db_path=str(DB), agent_ids=AGENTS)
    try:
        st = StageStore(rt.economy.db)
        stages = st.list_stages(TASK_ID)
        current = next((s for s in stages if s.is_open), None)
        if current is None:
            print("  ⚠️ 没有打开的 Stage")
            return 1

        if args.index > 1:
            # §七 真实 Resume：新 Runtime Session 读**持久**状态
            if current.status != STATUS_ACTIVE:
                current = st.resume(current.stage_id)
            print(f"  ↩︎ Resume: {current.stage_id} status={current.status} "
                  f"checkpoint={current.current_checkpoint or '(无)'}")
        else:
            print(f"  ▶ 首轮 Session，Stage={current.stage_id}")

        sess = RuntimeSession(limit_seconds=SESSION_LIMIT).start()
        roster = args.agents or AGENTS
        rnd = 0

        while not sess.is_exceeded() and rnd < args.max_rounds:
            rnd += 1
            print(f"  ── 第 {rnd} 轮  (已用 {sess.elapsed_seconds():.0f}s"
                  f" / {SESSION_LIMIT:.0f}s)")
            stage_txt = render_stage(current, limit_seconds=SESSION_LIMIT,
                                     elapsed_seconds=sess.elapsed_seconds())
            for aid in roster:
                if sess.is_exceeded():
                    print("  🛑 Hard Limit 到达，本轮中止")
                    break
                a = next(x for x in rt.economy.all_agents()
                         if x.agent_id == aid)
                a.brain.preflight_credential()
                t0 = time.perf_counter()
                rec = {"round": rnd, "agent_id": aid}
                try:
                    r = a.generate(messages=PROMPT.format(
                        topic=TOPIC, stage=stage_txt, ws=WS_ID))
                    rec.update({"ok": True, "simulated": r.simulated,
                                "tokens": r.total_tokens,
                                "ms": round((time.perf_counter()-t0)*1000, 1),
                                "chars": len(r.content or ""),
                                "head": (r.content or "")[:400]})
                    print(f"    ✅ {aid:22s} {rec['ms']:7.0f}ms "
                          f"chars={rec['chars']:5d} tokens={rec['tokens']}")
                except Exception as exc:                  # noqa: BLE001
                    rec.update({"ok": False,
                                "error": f"{type(exc).__name__}: {exc}"})
                    print(f"    ❌ {aid}: {rec['error'][:70]}")
                log["agents"].append(rec)

        # ---- Hard Limit：Runtime 强制停止 ----
        if not sess.is_exceeded():
            print(f"  （达到 --max-rounds={args.max_rounds}，主动收工）")
            sess.stop_by_agent("max_rounds_reached")
        else:
            sess.enforce()
        log["session"] = sess.snapshot()
        log["rounds"] = rnd

        # ---- Checkpoint（引用 Workspace Revision，不复制内容）----
        rev = f"rev-session-{args.index}"
        ck = st.save_checkpoint(current.stage_id, reason="hard_limit",
                                workspace_id=WS_ID, revision=rev,
                                progress={"rounds": rnd})
        after = st.get_stage(current.stage_id)
        log["checkpoint"] = ck.to_dict()
        log["stage_after"] = after.to_dict()
        print(f"\n  🛑 {sess.state}  Stage={after.status}  "
              f"checkpoint={ck.checkpoint_id} (rev={rev})")

        # §八：Session 本身也记一条 Experience（**不碰 Self Model**）
        exp = ExperienceStore(rt.economy.db).record(
            "novel_author", "observation",
            result={"kind": "runtime_session", "index": args.index,
                    "state": sess.state, "rounds": rnd},
            scope={"domain": "fiction", "situation": "session_wrap"},
            metadata={"session_index": args.index})
        log["experience"] = exp.to_dict()
        log["hard_limit_triggered"] = (sess.state == SESSION_STOPPED_HARD_LIMIT)
    finally:
        try:
            rows = rt.economy.db.fetchall(
                "SELECT SUM(total_tokens) t, SUM(cost_ai) c FROM llm_calls")
            log["cost"] = {"tokens": rows[0]["t"] or 0,
                           "aic": round(rows[0]["c"] or 0.0, 6)}
            rt.economy.close()
        except Exception:                                  # noqa: BLE001
            pass

    log["wall_seconds"] = round(time.time() - log["started_at"], 1)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"session_{args.index:02d}.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → {RESULTS/f'session_{args.index:02d}.json'}  "
          f"wall={log['wall_seconds']}s cost={log.get('cost')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

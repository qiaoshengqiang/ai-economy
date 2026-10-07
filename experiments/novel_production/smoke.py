#!/usr/bin/env python3
"""
AIECO 第一轮真实多 Agent 长期生产 —— Phase 3/4 + 10 分钟全链路冒烟
============================================================

【本脚本做什么】

    setup   Phase 3：建长期 Task + Task Workspace
            Phase 4：建 7 个 Stage（**只描述目标/交付物，不含步骤**）
    session 跑**一个** Runtime Session（Hard Limit 600 秒），
            然后关闭，再开**新** Runtime Session 恢复并继续。

【🔒 本脚本不做的事】

    · 不编排 Agent 顺序（§五 明令禁止 Workflow Script）
    · 不伪造 Reflection（§八：没发生就写「未发生」）
    · 不让 Feedback 直接改 Self Model
    · 不实现 Economy Policy
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "novel.db"
RESULTS = HERE / "results"

AGENTS = ["novel_world_builder", "novel_plot_architect", "novel_author",
          "novel_editor", "novel_reader"]

TASK_ID = "task_novel_001"
WS_ID = "WS-NOVEL-001"

STAGES = [
    ("形成故事世界、主要人物和核心剧情框架",
     ["world_bible.md", "characters.md", "core_conflict.md",
      "plot_outline.md"]),
    ("完成小说前段正文",
     ["chapters_front.md", "consistency_note.md"]),
    ("对已有内容进行编辑与读者体验评估",
     ["editor_report.md", "reader_report.md", "revision_suggestions.md"]),
    ("继续完成中段正文", ["chapters_middle.md"]),
    ("继续编辑与读者反馈", ["editor_report_2.md", "reader_report_2.md"]),
    ("完成后段与结局", ["chapters_end.md"]),
    ("完成最终文学修订", ["manuscript_final.md"]),
]

TOPIC = ("在一个由 AI Agent 参与运行的数字世界里，"
         "身份、记忆、资产和历史究竟属于谁？")


def _store(db_path=None):
    from main import build_runtime
    return build_runtime(db_path=str(db_path or DB), agent_ids=AGENTS)


# ================================================================
# Phase 3 + 4
# ================================================================
def cmd_setup(_args) -> int:
    from core.task import Task
    from stage import STATUS_ACTIVE, StageStore

    DB.parent.mkdir(parents=True, exist_ok=True)
    rt = _store()
    try:
        # --- Phase 3：Task ---
        ws = rt.workspace_gateway.ensure_workspace(WS_ID, AGENTS[0])
        print(f"  Task Workspace: {WS_ID}  ({ws})")

        existing = rt.tasks.get(TASK_ID)
        if existing is None:
            task = Task.create(
                creator="human_demand",
                description=(f"创作一部原创中文科幻悬疑小说，目标约 10000 字。"
                             f"主题：{TOPIC}"),
                workspace_id=WS_ID,
                task_id=TASK_ID,
            )
            rt.tasks.save(task)
            print(f"  ✅ Task 已创建: {TASK_ID}")
        else:
            print(f"  ↩︎  Task 已存在: {TASK_ID}（不重复创建）")

        # --- Phase 4：Stage ---
        st = StageStore(rt.economy.db)
        existing_stages = st.list_stages(TASK_ID)
        if existing_stages:
            print(f"  ↩︎  已有 {len(existing_stages)} 个 Stage（不重复创建）")
        else:
            for i, (goal, delivs) in enumerate(STAGES, 1):
                s = st.create_stage(
                    TASK_ID, goal,
                    deliverables=[{"name": d} for d in delivs],
                    status=STATUS_ACTIVE if i == 1 else "PENDING",
                    stage_id=f"stage_novel_{i}")
                print(f"  ✅ Stage {i}: {goal[:28]}… ({len(delivs)} 交付物)")

        info = {
            "task_id": TASK_ID, "workspace_id": WS_ID,
            "workspace_node": getattr(ws, "value", str(ws)),
            "stages": [s.to_dict() for s in st.list_stages(TASK_ID)],
            "agents": AGENTS,
            # 凭证预检（§九）
            "credentials": [],
        }
        for a in rt.economy.all_agents():
            try:
                info["credentials"].append(
                    {"agent_id": a.agent_id, **a.brain.preflight_credential()})
            except Exception as exc:                      # noqa: BLE001
                info["credentials"].append(
                    {"agent_id": a.agent_id, "error": str(exc)})
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "setup.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  ✅ Phase 3 + 4 完成 → {RESULTS/'setup.json'}")
        return 0
    finally:
        try: rt.economy.close()
        except Exception: pass


# ================================================================
# 冒烟 Session
# ================================================================
def _stage_brief(st) -> str:
    from stage.context import render_stage
    return render_stage(st, limit_seconds=SESSION_LIMIT)


SESSION_LIMIT = float(__import__("os").environ.get("SMOKE_LIMIT", "600"))   # 🔴 只用于本次冒烟（§四），**不写进正式配置**

#: 只让「自然需要」的 Agent 参与（§五）。
#: 🔒 这里**不是**执行顺序 —— 每个 Agent 拿到的都是同一段 Stage 状态，
#:    自己去决定要不要发言。
KICKOFF = (
    "你是这次长期小说生产任务的一名参与者。\n"
    "任务主题：{topic}\n\n"
    "{stage}\n\n"
    "这是共享 Task Workspace（{ws}）。\n"
    "你可以：读取其他参与者的产物、主动联系任何一位参与者、\n"
    "把你的产物写进共享 Workspace、或什么都不做直接说明你的判断。\n"
    "由你自己决定下一步做什么、以及是否需要别人。\n\n"
    "请给出你**这一轮**的具体产出（可以是设定、结构、正文片段、\n"
    "编辑意见或读者体验），并说明你是否需要其他人配合。"
)



def _one(aid, st, current, rt, log):
    """让**一个** Agent 在共享 Task Workspace 语境下产出一轮。"""
    a = next(x for x in rt.economy.all_agents() if x.agent_id == aid)
    a.brain.preflight_credential()          # §九：尽早暴露配置错误
    prompt = KICKOFF.format(topic=TOPIC, stage=_stage_brief(current), ws=WS_ID)
    t0 = time.perf_counter()
    rec = {"agent_id": aid}
    try:
        r = a.generate(messages=prompt)
        rec.update({"ok": True, "simulated": r.simulated,
                    "tokens": r.total_tokens,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "chars": len(r.content or ""),
                    "head": (r.content or "")[:400]})
        print(f"    ✅ {aid:22s} {rec['ms']:7.0f}ms "
              f"chars={rec['chars']:5d} tokens={rec['tokens']}")
    except Exception as exc:                          # noqa: BLE001
        rec.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        print(f"    ❌ {aid}: {rec['error'][:70]}")
    log["agents"].append(rec)
    return rec

def cmd_session(args) -> int:
    from experience import ExperienceStore
    from stage import (RuntimeSession, SESSION_STOPPED_HARD_LIMIT,
                       StageStore, STATUS_ACTIVE)

    DB.parent.mkdir(parents=True, exist_ok=True)
    rt = _store()
    log = {"session_index": args.index, "agents": [], "hard_limit": None,
           "started_at": time.time()}
    sess = None
    try:
        st = StageStore(rt.economy.db)
        stages = st.list_stages(TASK_ID)
        current = next((s for s in stages if s.is_open), None)
        if current is None:
            print("  ⚠️ 没有打开的 Stage")
            return 1
        if args.index > 1:
            # 真实 Resume（§七）：新 Runtime Session，读持久状态
            if current.status != STATUS_ACTIVE:
                current = st.resume(current.stage_id)
            print(f"  ↩︎ 新 Session 恢复 Stage: {current.stage_id} "
                  f"(status={current.status})")

        # 🔴 Session 计时**从第一轮生产之前**开始 ——
        #    这样 Hard Limit 约束的是**真实的生产时间**，
        #    而不是「先干完活再空等 10 分钟」。
        sess = RuntimeSession(limit_seconds=SESSION_LIMIT).start()

        roster = args.agents or AGENTS
        # 🔒 这是**轮转**，不是 Workflow 顺序 ——
        #    每一轮每个 Agent 拿到的都是同一段 Stage 状态，
        #    各自决定说什么。没有「必须先做 A 才能做 B」。
        rnd = 0
        while not sess.is_exceeded():
            rnd += 1
            print(f"  ── 第 {rnd} 轮（已用 {sess.elapsed_seconds():.0f}s "
                  f"/ {SESSION_LIMIT:.0f}s）")
            for aid in roster:
                if sess.is_exceeded():
                    print("  🛑 Hard Limit 到达，本轮中止")
                    break
                _one(aid, st, current, rt, log)
            if rnd >= args.max_rounds:
                print(f"  （达到 --max-rounds={args.max_rounds}，主动收工）")
                break

        for aid in ():
            a = next(x for x in rt.economy.all_agents() if x.agent_id == aid)
            a.brain.preflight_credential()      # 尽早暴露配置错误
            prompt = KICKOFF.format(
                topic=TOPIC, stage=_stage_brief(current), ws=WS_ID)
            t0 = time.perf_counter()
            rec = {"agent_id": aid}
            try:
                r = a.generate(messages=prompt)
                rec.update({
                    "ok": True, "simulated": r.simulated,
                    "tokens": r.total_tokens,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "chars": len(r.content or ""),
                    "head": (r.content or "")[:600],
                })
                print(f"  ✅ {aid:22s} {rec['ms']:7.0f}ms "
                      f"chars={rec['chars']:5d} tokens={rec['tokens']}")
            except Exception as exc:                      # noqa: BLE001
                rec.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                print(f"  ❌ {aid}: {rec['error'][:80]}")
            log["agents"].append(rec)

    # ---- Hard Limit：到点强制停止（Runtime 执行，Agent 不能绕过）----
    stopped = sess.enforce() if not sess.is_exceeded() else True
    if sess.state != SESSION_STOPPED_HARD_LIMIT:
        sess.enforce()
    stage_after = StageStore(rt.economy.db).get_stage(current.stage_id)
    ck = StageStore(rt.economy.db).save_checkpoint(
        current.stage_id, reason="hard_limit",
        workspace_id=WS_ID, revision=f"rev-session-{args.index}",
        progress={"agents_run": len(log["agents"])})
    log["hard_limit"] = {
        "session": sess.snapshot(), "stopped": stopped,
        "stage_status_after": stage_after.status if stage_after else None,
        "checkpoint": ck.to_dict(),
    }
    print(f"\n  🛑 Hard Limit 触发: {sess.state} "
          f"| Stage 状态={stage_after.status if stage_after else '?'} "
          f"| checkpoint={ck.checkpoint_id}")

    exp = ExperienceStore(rt.economy.db).record(
        AGENTS[2], "observation",
        result={"kind": "runtime_session", "index": args.index,
                "stopped": sess.state},
        scope={"domain": "fiction", "situation": "session_wrap"},
        metadata={"session_index": args.index})
    log["experience"] = exp.to_dict()
    try:
        rt.economy.close()
    except Exception: pass
    log["ended_at"] = time.time()
    log["wall_seconds"] = round(log["ended_at"] - log["started_at"], 1)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"session_{args.index:02d}.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → {RESULTS/f'session_{args.index:02d}.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["setup", "session"])
    ap.add_argument("--index", type=int, default=1)
    ap.add_argument("--agents", nargs="*", default=None)
    ap.add_argument("--max-rounds", type=int, default=999)
    args = ap.parse_args()
    return {"setup": cmd_setup, "session": cmd_session}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

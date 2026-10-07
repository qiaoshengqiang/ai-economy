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
sys.path.insert(0, str(HERE))

import shared_workspace as sw                          # noqa: E402

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
                # 🔒 5 个 Agent 是同一个 Task 的 **Participant**。
                #    WorkspaceGateway 的 `is_participant` 就查这里 ——
                #    没有这一步，5 个 Agent 谁都写不进共享 Workspace。
                participants=list(AGENTS),
            )
            rt.tasks.save(task)
            print(f"  ✅ Task 已创建: {TASK_ID}（participants={len(AGENTS)}）")
        else:
            # 本次实验前建的 Task 没有 participants —— 补上（幂等）
            missing = [a for a in AGENTS
                       if a not in (existing.participants or [])]
            if missing:
                existing.participants = list(existing.participants or []) + missing
                rt.tasks.save(existing)
                print(f"  ✅ Task 参与者已补齐: {missing}")
            print(f"  ↩︎  Task 已存在: {TASK_ID}"
                  f"（participants={len(existing.participants or [])}）")

        # --- Phase 3.5：5 份共享生产资料**真正**建进 Task Workspace ---
        #
        # 🔒 这是「Agent 产出只留在内存里」和「共享事实来源」的分界线。
        #    每个文件被 handoff 给它的自然责任人（Workspace Core 的正式入口）。
        prov = sw.provision(rt.workspace_gateway, WS_ID, AGENTS[0],
                            agent_ids=list(AGENTS))
        for item in prov:
            mark = "✅" if item.get("ok") and item.get("handoff_ok") else "⚠️"
            print(f"  {mark} {item['path']:20s} 负责人={item['owner']:20s} "
                  f"R{item.get('revision', '?')} {item['status']}"
                  + ("" if item.get("handoff_ok")
                     else f"  handoff失败={item.get('handoff_error')}"))
        ws_snap = sw.snapshot(rt.workspace_gateway, WS_ID, AGENTS[0])

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
            "participants": list(AGENTS),
            # §六：共享生产资料的真实 revision
            "artifacts": prov,
            "workspace_snapshot": ws_snap,
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
    """
    跑**协作** Session。

    🔒 这里**不再自己实现循环** —— 协作循环住在 `collab_session.py`。
       历史：本文件曾有一份 `cmd_session`，因为一次「字符串替换式修改」
       把 `try/finally` 结构改坏，导致整个文件 **SyntaxError**。
       修法不是再补一次补丁，而是**删掉重复实现**，只留一个入口。
    """
    import collab_session
    argv = ["collab_session.py", "--index", str(args.index),
            "--max-rounds", str(args.max_rounds)]
    old = sys.argv
    sys.argv = argv
    try:
        return collab_session.main()
    finally:
        sys.argv = old


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

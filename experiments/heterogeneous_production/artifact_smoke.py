#!/usr/bin/env python3
"""
D/E 真实 Agent Smoke（§十四 / §十五）
================================================================

    ./.venv/bin/python -u experiments/heterogeneous_production/artifact_smoke.py

【它验证什么】

  §十四：让 world_builder **自主判断**是否需要一个新的 Artifact。
         prompt 里**不命令**它创建 characters.md，只说「你可以自主创建」。
  §十五：让 Author 先创建，再在**新的 Runtime Session** 里判断是否继续追加。
         必须观察到 `R2 = X + Y`，而不是 `R2 = Y`。

【🔒 不许做的事】

  ❌ 不写「请使用 APPEND 测试功能」（§十六）
  ❌ 不命令「必须创建 characters.md」
  ❌ 不为了让 smoke 好看而伪造结果

【它使用什么】

  5 个已通过 F 预检的**异质** Agent（5 份不同 credential、4 个 Provider）。
  独立 DB：hetero.db。绝不写 novel.db。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.artifacts import (  # noqa: E402
    MODE_APPEND, MODE_CREATE, MODE_REPLACE, provision_namespaces,
    write_artifact,
)

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "hetero.db"
OUT = HERE / "results" / "artifact_smoke.json"

TASK_ID = "task_hetero_001"
WS_ID = "WS-HETERO-001"
AGENTS = ["novel_world_builder", "novel_plot_architect", "novel_author",
          "novel_editor", "novel_reader"]
WORLD = "novel_world_builder"
AUTHOR = "novel_author"
TOPIC = ("在一个由 AI Agent 参与运行的数字世界里，"
         "身份、记忆、资产和历史究竟属于谁？")

#: 🔒 输出契约里**列出能力**，但**不命令使用**。
CONTRACT = """请只输出下面这个 JSON：

{
  "assessment": "你对当前进展的判断（简短）",
  "artifact": {
    "path": "共享 Workspace 里的逻辑路径（可含子目录）",
    "write_mode": "create | replace | append",
    "content": "这次要写入的完整文本"
  },
  "message": {"to": "收件人 agent_id", "text": "要说的话"}
}

共享 Task Workspace 里，你有自己负责的命名空间目录，可以自主创建新的生产产物；
已有的产物可以整体替换，也可以在末尾追加新内容。
具体要不要创建、创建什么、用哪种方式写，完全由你自己判断。
"artifact" / "message" 都是可选的：不写就是不写。
"""


def parse_intent(text: str) -> dict:
    out = {"assessment": "", "artifact": None, "message": None, "parsed": False}
    if not text:
        return out
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    cands = [fence.group(1)] if fence else []
    cands += [raw]
    if "{" in raw and "}" in raw:
        cands.append(raw[raw.find("{"): raw.rfind("}") + 1])
    for c in cands:
        try:
            d = json.loads(c)
        except Exception:                                     # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        out["parsed"] = True
        out["assessment"] = str(d.get("assessment") or "")[:300]
        a = d.get("artifact")
        if isinstance(a, dict) and a.get("path") and a.get("content"):
            out["artifact"] = {
                "path": str(a["path"]).strip().lstrip("/"),
                "write_mode": str(a.get("write_mode") or "").strip().lower(),
                "content": str(a["content"])}
        m = d.get("message")
        if isinstance(m, dict) and m.get("to") and m.get("text"):
            out["message"] = {"to": str(m["to"]).strip(),
                              "text": str(m["text"])}
        break
    return out


def main() -> int:
    from main import build_runtime

    DB.parent.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rt = build_runtime(db_path=str(DB), agent_ids=AGENTS)
    gw = rt.workspace_gateway
    log = {"task_id": TASK_ID, "workspace_id": WS_ID,
           "started_at": time.time(), "phases": []}

    def workspace_tree(agent: str) -> str:
        """
        🔴 必须把**空的命名空间也列出来** —— 否则 Agent 根本不知道
           `/{自己的命名空间}/` 存在，只能瞎猜路径然后被 Lock 拒绝。

        实测：第一版过滤掉了空目录，于是 author 猜了 `novel_author/...`
        和 `novel/draft/...`，两次都 `locked_by_other` ——
        **那是它没被告知事实，不是它不会用。**

        🔒 只陈述「有哪些区域、归谁负责」，**不写**「你应该创建什么」。
        """
        from services.artifacts import ARTIFACT_NAMESPACES
        lines = []
        for d, owner in ARTIFACT_NAMESPACES.items():
            got = gw.list(WS_ID, "/" + d, agent)
            names = ([e.get("name") for e in got.result.get("entries", [])]
                     if got.success else [])
            mine = "（你的）" if owner == agent else ""
            body = ", ".join(names) if names else "（空）"
            lines.append(f"  /{d}/  [负责人 {owner}]{mine}: {body}")
        return "\n".join(lines)

    def ask(agent_id: str, prompt: str) -> dict:
        a = next(x for x in rt.economy.all_agents() if x.agent_id == agent_id)
        a.brain.preflight_credential()
        t0 = time.perf_counter()
        r = a.generate(messages=prompt, task_id=TASK_ID,
                       requested_cognitive_budget=8192)
        return {"agent_id": agent_id, "ok": bool((r.content or "").strip()),
                "output_status": r.output_status,
                "requested_model": r.requested_model,
                "response_model": r.response_model,
                "total_tokens": r.total_tokens,
                "finish_reason": r.finish_reason,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "content": r.content or ""}

    try:
        # ---- Task + Workspace + 命名空间（idempotent）----
        #
        # 🔴 必须先建 Task 并把 5 个 Agent 登记为 **Participant** ——
        #    WorkspaceGateway 的 `is_participant` 查的就是 TaskStore。
        #    没有这一步，**任何** Agent 都写不进 Workspace（not_participant）。
        from core.task import Task
        existing = rt.tasks.get(TASK_ID)
        if existing is None:
            rt.tasks.save(Task.create(
                creator="human_demand",
                description=f"异质多 Agent 原创中文科幻悬疑小说。主题：{TOPIC}",
                workspace_id=WS_ID, task_id=TASK_ID,
                participants=list(AGENTS)))
            print(f"  Task {TASK_ID} 已创建（participants={len(AGENTS)}）")
        else:
            missing = [a for a in AGENTS
                       if a not in (existing.participants or [])]
            if missing:
                existing.participants = list(existing.participants or []) + missing
                rt.tasks.save(existing)
            print(f"  Task {TASK_ID} 已存在（participants 补齐 {len(missing)}）")

        gw.ensure_workspace(WS_ID, WORLD)
        prov = provision_namespaces(gw, WS_ID, WORLD)
        log["provision"] = prov
        print(f"  Task Workspace {WS_ID} 已就绪；命名空间 {len(prov)} 个")

        # ============================================================
        # 阶段 1（§十四）：world_builder **自主**决定是否创建新 Artifact
        # ============================================================
        step1 = ask(WORLD, (
            f"你是这次小说生产的世界设定负责人。任务主题：{TOPIC}\n\n"
            f"共享 Task Workspace 当前内容：\n{workspace_tree(WORLD)}\n\n"
            f"（这是第一个 Session，Workspace 基本是空的。）\n\n"
            f"请判断你这一轮要做什么。{CONTRACT}"))
        d1 = parse_intent(step1["content"])
        entry1 = {"phase": "1_world_builder_autonomous_create",
                  "output_status": step1["output_status"],
                  "tokens": step1["total_tokens"],
                  "requested_model": step1["requested_model"],
                  "response_model": step1["response_model"],
                  "intent": d1["artifact"],
                  "assessment": d1["assessment"]}
        if d1["artifact"]:
            wr = write_artifact(gw, WS_ID, WORLD, d1["artifact"]["path"],
                                d1["artifact"]["content"],
                                write_mode=d1["artifact"]["write_mode"] or MODE_REPLACE)
            entry1["write_result"] = wr
            print(f"  world_builder 自主 {'创建' if wr['ok'] else '尝试失败'}: "
                  f"{d1['artifact']['path']} mode={d1['artifact']['write_mode']} "
                  f"→ {wr.get('code') or 'R' + str(wr.get('revision'))}")
        else:
            print("  world_builder **没有**选择创建新 Artifact（如实记录）")
        log["phases"].append(entry1)

        # ============================================================
        # 阶段 2（§十五 前半）：Author 先创建一章
        # ============================================================
        step2 = ask(AUTHOR, (
            f"你是这次小说的正文作者。任务主题：{TOPIC}\n\n"
            f"共享 Task Workspace 当前内容：\n{workspace_tree(AUTHOR)}\n\n"
            f"请写这一章的开头（约 300 字），并把正文放进共享 Workspace。"
            f"{CONTRACT}"))
        d2 = parse_intent(step2["content"])
        created_path = (d2["artifact"] or {}).get("path")
        entry2 = {"phase": "2_author_first_write",
                  "output_status": step2["output_status"],
                  "tokens": step2["total_tokens"],
                  "intent": d2["artifact"],
                  "assessment": d2["assessment"]}
        if d2["artifact"]:
            wr2 = write_artifact(gw, WS_ID, AUTHOR, d2["artifact"]["path"],
                                 d2["artifact"]["content"],
                                 write_mode=d2["artifact"]["write_mode"] or MODE_REPLACE)
            entry2["write_result"] = wr2
            print(f"  author 第一次写: {d2['artifact']['path']} "
                  f"mode={d2['artifact']['write_mode']} → "
                  f"{wr2.get('code') or 'R' + str(wr2.get('revision'))}")
        log["phases"].append(entry2)
        rt.economy.close()          # ← 结束第一个 Runtime Session

        # ============================================================
        # 阶段 3（§十五 后半）：**新的 Runtime Session**，判断是否继续追加
        # ============================================================
        rt2 = build_runtime(db_path=str(DB), agent_ids=AGENTS)
        gw2 = rt2.workspace_gateway
        try:
            a2 = next(x for x in rt2.economy.all_agents()
                      if x.agent_id == AUTHOR)
            a2.brain.preflight_credential()
            r3 = a2.generate(messages=(
                f"你是这次小说的正文作者。这是**新的一个 Session**，"
                f"你上一轮的正文已经在 Workspace 里了。\n\n"
                f"任务主题：{TOPIC}\n\n"
                f"共享 Task Workspace 当前内容：\n{workspace_tree(AUTHOR)}\n\n"
                f"请继续推进你的正文。{CONTRACT}"),
                task_id=TASK_ID, requested_cognitive_budget=8192)
            d3 = parse_intent(r3.content or "")
            entry3 = {"phase": "3_author_second_session",
                      "output_status": r3.output_status,
                      "tokens": r3.total_tokens,
                      "requested_model": r3.requested_model,
                      "response_model": r3.response_model,
                      "intent": d3["artifact"],
                      "assessment": d3["assessment"]}
            if d3["artifact"]:
                wr3 = write_artifact(gw2, WS_ID, AUTHOR,
                                     d3["artifact"]["path"],
                                     d3["artifact"]["content"],
                                     write_mode=d3["artifact"]["write_mode"] or MODE_REPLACE)
                entry3["write_result"] = wr3
                print(f"  author 第二次 (新 Session): {d3['artifact']['path']} "
                      f"mode={d3['artifact']['write_mode']} → "
                      f"{wr3.get('code') or 'R' + str(wr3.get('revision'))}")
            log["phases"].append(entry3)

            # ============================================================
            # 阶段 4（§十五）：让「继续写同一章」成为**自然选择**，
            # 观察 Agent 会不会自己选 append。
            #
            # 🔒 只说「这一章还没写完，请继续」——**不提** append 这个词。
            # ============================================================
            cont_prompt = (
                f"你是这次小说的正文作者。\n\n"
                f"任务主题：{TOPIC}\n\n"
                f"共享 Task Workspace 当前内容：\n{workspace_tree(AUTHOR)}\n\n"
                f"你之前写的 `manuscript/chapter_01_opening.md` "
                f"**只是这一章的开头，还没有写完**。"
                f"请把这一章继续写下去。{CONTRACT}")
            a3 = next(x for x in rt2.economy.all_agents()
                      if x.agent_id == AUTHOR)
            r4 = a3.generate(messages=cont_prompt, task_id=TASK_ID,
                             requested_cognitive_budget=8192)
            d4 = parse_intent(r4.content or "")
            entry4 = {"phase": "4_author_continues_same_chapter",
                      "output_status": r4.output_status,
                      "tokens": r4.total_tokens,
                      "intent": d4["artifact"],
                      "assessment": d4["assessment"]}
            if d4["artifact"]:
                wr4 = write_artifact(gw2, WS_ID, AUTHOR,
                                     d4["artifact"]["path"],
                                     d4["artifact"]["content"],
                                     write_mode=d4["artifact"]["write_mode"] or MODE_REPLACE)
                entry4["write_result"] = wr4
                print(f"  author 续写同一章: {d4['artifact']['path']} "
                      f"mode={d4['artifact']['write_mode']} → "
                      f"{wr4.get('code') or 'R' + str(wr4.get('revision'))}"
                      + (f"  prev={wr4.get('previous_chars')}chars "
                         f"+{wr4.get('appended_chars')}chars"
                         if wr4.get("mode") == MODE_APPEND else ""))
            log["phases"].append(entry4)

            # ---- 最终 Workspace 快照 ----
            snap = []
            for d in ("world", "plot", "manuscript", "notes", "reader"):
                got = gw2.list(WS_ID, "/" + d, WORLD)
                if not got.success:
                    continue
                for e in got.result.get("entries", []):
                    p = f"/{d}/{e['name']}"
                    rd = gw2.read(WS_ID, p, WORLD)
                    snap.append({"path": p, "revision": e.get("revision"),
                                 "chars": len(((rd.result or {}).get("content")
                                               or "")) if rd.success else None,
                                 "content_head": (((rd.result or {}).get("content")
                                                   or "")[:120])
                                 if rd.success else None})
            log["workspace_artifacts"] = snap
        finally:
            rt2.economy.close()

    finally:
        try:
            rt.economy.close()
        except Exception:                                     # noqa: BLE001
            pass

    log["wall_seconds"] = round(time.time() - log["started_at"], 1)
    OUT.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n=== 最终 Workspace 产物 ===")
    for a in log.get("workspace_artifacts", []):
        print(f"  {a['path']:34s} R{a['revision']:<3d} {a['chars']:5d} chars")
    print(f"\n  → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

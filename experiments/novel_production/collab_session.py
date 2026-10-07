#!/usr/bin/env python3
"""
最小真实协作实验 —— **通信 + 共享 Workspace 都在真实运行路径上**
================================================================

    SMOKE_LIMIT=240 python collab_session.py --index 11

【与 `session.py` 的唯一区别】

    `session.py`：5 个 Agent 各自调一次模型，产出打进 JSON 日志 → **并行独立生产**
    `collab_session.py`：每轮每个 Agent 先**取信箱**、看**共享 Workspace 的真实
                        revision 与内容**，产出再**真正写回 Workspace**，
                        并且可以**主动发消息给任何参与者**

【🔒 这不是 Workflow】

    1. 循环体是**轮转**：每轮每个 Agent 拿到同一份 Stage + 同一份 Workspace 快照，
       各自决定做什么。没有「必须先 A 才能 B」。
    2. 代码里**没有**任何「A 完成后通知 B」的自动投递。
       Agent 不写 message，就没有消息。
    3. 写别人负责的文件会**真的失败**（Workspace Lock），
       代码**不**替它重定向、**不**替它转发给负责人 ——
       要不要去找负责人，是 Agent 自己的判断。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channels                                     # noqa: E402
import shared_workspace as sw                       # noqa: E402

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

========== 共享 Task Workspace（{ws}）的**真实当前状态** ==========
{workspace}

这份 Workspace 是**共享事实来源**：你看到的 revision 是真实 revision。

========== 你的信箱 ==========
{inbox}

========== 参与这个 Task 的其他 Agent ==========
{roster}

（每个文件的「负责人」是它的正式责任人。只有负责人能改那个文件；
  你可以读任何文件。这不是指令，只是当前的事实。）

========== 你这一轮要做什么，完全由你自己判断 ==========
你可以：
  · 只做自己的事，不联系任何人
  · 主动联系任何一位参与者（请求信息 / 提出意见 / 说明你改了什么）
  · 回复你信箱里的消息，或者**无视**它
  · 接受或**拒绝**别人的建议
  · 修改你负责的文件
  · 什么都不做，直接说明你的判断

请用下面的 JSON 格式给出你这一轮的结果（**除了 JSON 不要再写别的**）：

{{
  "assessment": "你对当前进展的判断（简短）",
  "artifact": {{"path": "你负责的文件名", "content": "完整内容"}},
  "message": {{"to": "收件人 agent_id", "text": "要说的话", "reply_to": "可选：你在回复的 message_id"}}
}}

关于可选字段（由你决定，不写就是不写）：
  · "artifact"：只有在你想更新某个文件时才出现。**只能是你负责的文件**，
     写别人负责的文件会失败。不写 = 这一轮不改任何文件。
  · "message"：只有在你想联系别人时才出现。不写 = 这一轮不联系任何人。
     可以写成 null。不要在 message 里复制整篇正文 —— 正文属于 Workspace。
"""


# ================================================================
# 解析 Agent 的结构化意图（Runtime 执行，Agent 判断）
# ================================================================
def parse_intent(text: str) -> Dict[str, Any]:
    """
    从模型输出里抽出 JSON 意图。

    🔒 刻意**宽容**：模型经常包 ```json 代码块或加前后缀。
       宽容解析**不等于**宽松执行 —— 执行仍走真实的 Gateway 校验。
    """
    out: Dict[str, Any] = {"assessment": "", "artifact": None,
                           "message": None, "parsed": False}
    if not text:
        return out

    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    candidates.append(raw)
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.find("{"): raw.rfind("}") + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:                                     # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        out["parsed"] = True
        out["assessment"] = str(data.get("assessment") or "")[:400]
        art = data.get("artifact")
        if isinstance(art, dict) and art.get("path") and art.get("content"):
            out["artifact"] = {"path": str(art["path"]).strip().lstrip("/"),
                               "content": str(art["content"])}
        msg = data.get("message")
        if isinstance(msg, dict) and msg.get("to") and msg.get("text"):
            out["message"] = {
                "to": str(msg["to"]).strip(),
                "text": str(msg["text"]),
                "reply_to": (str(msg["reply_to"]).strip()
                             if msg.get("reply_to") else None),
            }
        break
    return out


def mentions(text: str, needles: List[str]) -> bool:
    """输出里是否**提到了**信箱里的东西（行为受影响的弱证据）。"""
    return any(n and n in (text or "") for n in needles)


def is_empty_completion(content: str, output_tokens: Optional[int]) -> bool:
    """
    「调用成功、花了钱、但**一个字都没拿到**」——必须与
    「Agent 主动决定什么都不说」区分开。

    【🔴 实测抓到的阻塞性 Bug（本实验 Session 1）】

        `novel_world_builder` 连续 4 轮 `chars=0`，
        但每轮 `output_tokens≈4300`，4 轮共烧掉约 25k token。

        一开始我把它读成「这个 Agent 选择不动手」——**那是错的**。
        真相：`deepseek-flash` 是**推理模型**，返回体里
        `message` 同时有 `content` 和 `reasoning_content`。
        当输出预算全被推理吃掉时，`content` 是**空字符串**，
        而 `completion_tokens` 依然很大。

        于是「接口成功」掩盖了「语义 no-op」——
        这正是本项目反复栽的那一类 Bug（见 AGENTS.md §12 与本仓历史）。

    🔒 判据刻意保守：**空 content + 真的花了 output token** 才算。
       空 content + 0 token 仍然是合法的「模型说它没什么可说」。
    """
    if (content or "").strip():
        return False
    return bool(output_tokens)


# ================================================================
# 主流程
# ================================================================
def main() -> int:
    from main import build_runtime
    from stage import (
        RuntimeSession, SESSION_STOPPED_HARD_LIMIT, STATUS_ACTIVE, StageStore)
    from stage.context import render_stage

    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=11)
    ap.add_argument("--max-rounds", type=int, default=999)
    args = ap.parse_args()

    DB.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    log: Dict[str, Any] = {
        "session_index": args.index, "limit_seconds": SESSION_LIMIT,
        "started_at": time.time(), "kind": "collaborative_smoke",
        "task_id": TASK_ID, "workspace_id": WS_ID, "agents": AGENTS,
        "turns": [], "comm_events": [],
    }

    rt = build_runtime(db_path=str(DB), agent_ids=AGENTS)
    bridge = channels.SyncBridge()
    #: 🔒 成本要报**本次 Session 的增量**，不是整库累计 ——
    #:    否则报告里的 AIC 会把以前所有实验一起算进来。
    cost_before = _cost(rt)
    try:
        st = StageStore(rt.economy.db)
        current = next((s for s in st.list_stages(TASK_ID) if s.is_open), None)
        if current is None:
            print("  ⚠️ 没有打开的 Stage")
            return 1
        if current.status != STATUS_ACTIVE:
            current = st.resume(current.stage_id)

        # ---- §六.5 / §十六 J：Resume 后必须知道自己恢复自**哪个真实 revision** ----
        #
        # 🔒 证据链：上一个 Session 落库的 Checkpoint.revision
        #    与**本次新进程读到的** Workspace 快照必须一致。
        #    这正是「Resume 读的是持久事实，不是内存里的旧对象」。
        prior = st.latest_checkpoint(current.stage_id)
        live = sw.snapshot(rt.workspace_gateway, WS_ID, AGENTS[0])
        log["resume"] = {
            "stage_id": current.stage_id,
            "stage_status": current.status,
            "stage_current_checkpoint": current.current_checkpoint,
            "prior_checkpoint": prior.to_dict() if prior else None,
            "checkpoint_revision": (prior.revision if prior else None),
            "live_workspace_revision": live["revision"],
            "revision_matches": bool(
                prior and prior.revision == live["revision"]),
        }
        if prior:
            print(f"  ↩︎ Resume: Stage={current.stage_id} "
                  f"status={current.status}")
            print(f"     上一个 Checkpoint {prior.checkpoint_id} "
                  f"revision={prior.revision}")
            print(f"     本进程读到的 Workspace revision={live['revision']}")
            print("     ✅ 一致：Resume 读的是持久事实"
                  if prior.revision == live["revision"]
                  else "     ⚠️ 不一致：Workspace 在两次 Session 之间被动过")

        # ---- Workspace 真实 revision：Session 开始时的基线 ----
        before = sw.snapshot(rt.workspace_gateway, WS_ID, AGENTS[0])
        log["workspace_before"] = before
        print(f"  ▶ Session {args.index}  Stage={current.stage_id} "
              f"status={current.status}")
        print(f"  ▶ Workspace 基线 revision: {sw.render_index(rt.workspace_gateway, WS_ID, AGENTS[0])}")

        sess = RuntimeSession(limit_seconds=SESSION_LIMIT).start()
        trace_id = f"trc_novel_{args.index}"
        rnd = 0

        while not sess.is_exceeded() and rnd < args.max_rounds:
            rnd += 1
            print(f"\n  ── 第 {rnd} 轮  (已用 {sess.elapsed_seconds():.0f}s"
                  f" / {SESSION_LIMIT:.0f}s)")
            stage_txt = render_stage(current, limit_seconds=SESSION_LIMIT,
                                     elapsed_seconds=sess.elapsed_seconds())
            for aid in AGENTS:
                if sess.is_exceeded():
                    print("  🛑 Hard Limit 到达，本轮中止")
                    break
                turn = _one_turn(rt, bridge, st, current, aid, rnd,
                                 stage_txt, trace_id, log)
                log["turns"].append(turn)
                _print_turn(aid, turn)

        if not sess.is_exceeded():
            print(f"\n  （达到 --max-rounds={args.max_rounds}，主动收工）")
            sess.stop_by_agent("max_rounds_reached")
        else:
            sess.enforce()
        log["session"] = sess.snapshot()
        log["rounds"] = rnd
        log["hard_limit_triggered"] = (
            sess.state == SESSION_STOPPED_HARD_LIMIT)

        # ---- Checkpoint：引用**真实** Workspace revision（§六）----
        after = sw.snapshot(rt.workspace_gateway, WS_ID, AGENTS[0])
        log["workspace_after"] = after
        ck = st.save_checkpoint(
            current.stage_id, reason="hard_limit", workspace_id=WS_ID,
            revision=after["revision"],       # 🔒 真实 revision，不是实验标签
            progress={"rounds": rnd})
        stage_after = st.get_stage(current.stage_id)
        log["checkpoint"] = ck.to_dict()
        log["stage_after"] = stage_after.to_dict()
        print(f"\n  🛑 {sess.state}  Stage={stage_after.status}")
        print(f"  🛑 checkpoint={ck.checkpoint_id}  revision={after['revision']}")

        log["comm"] = channels.summarize(log["comm_events"])
    finally:
        try:
            bridge.close()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            after_cost = _cost(rt)
            log["cost"] = {
                "tokens": after_cost["tokens"] - cost_before["tokens"],
                "aic": round(after_cost["aic"] - cost_before["aic"], 6),
                "cumulative": after_cost,
            }
            rt.economy.close()
        except Exception:                                     # noqa: BLE001
            pass

    log["wall_seconds"] = round(time.time() - log["started_at"], 1)
    (RESULTS / f"collab_{args.index:02d}.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  → {RESULTS/f'collab_{args.index:02d}.json'}  "
          f"wall={log['wall_seconds']}s cost={log.get('cost')}")
    return 0


def _cost(rt: Any) -> Dict[str, Any]:
    """整库累计的 token / AIC（Session 前后各取一次，差值才是本次成本）。"""
    try:
        row = rt.economy.db.fetchall(
            "SELECT SUM(total_tokens) t, SUM(cost_ai) c FROM llm_calls")[0]
        return {"tokens": int(row["t"] or 0), "aic": float(row["c"] or 0.0)}
    except Exception:                                         # noqa: BLE001
        return {"tokens": 0, "aic": 0.0}


def _one_turn(rt: Any, bridge: Any, st: Any, stage: Any, aid: str, rnd: int,
              stage_txt: str, trace_id: str, log: Dict[str, Any]) -> Dict[str, Any]:
    """一个 Agent 的一轮：取信箱 → 看共享状态 → 判断 → Runtime 执行。"""
    comm = rt.communication
    gw = rt.workspace_gateway
    turn: Dict[str, Any] = {"round": rnd, "agent_id": aid, "ok": False}

    # ---------- ① 取信箱（Event Wakeup 的证据在这里）----------
    inbox = channels.drain(comm, bridge, aid)
    inbox_ids = [m.message_id for m in inbox["received"]]
    turn["inbox"] = {
        "wakeup": inbox["wakeup"],
        "received_ids": inbox_ids,
        "consumed_ids": inbox["consumed"],
        "duplicates": inbox["duplicates"],
        "from": sorted({m.from_agent for m in inbox["received"]}),
    }
    if inbox["wakeup"]:
        log["comm_events"].append({"kind": "wakeup", "agent_id": aid,
                                   "round": rnd})
    for m in inbox["received"]:
        log["comm_events"].append({
            "kind": "received", "agent_id": aid, "round": rnd,
            "message_id": m.message_id, "from": m.from_agent})
    for mid in inbox["consumed"]:
        log["comm_events"].append({"kind": "consumed", "agent_id": aid,
                                   "round": rnd, "message_id": mid})

    # ---------- ② 组装真实状态 ----------
    roster = "\n".join(
        f"- {x}" + ("（你）" if x == aid else "") for x in AGENTS)
    prompt = PROMPT.format(
        topic=TOPIC, stage=stage_txt, ws=WS_ID,
        workspace=sw.render(gw, WS_ID, aid),
        inbox=channels.render_inbox(inbox["received"]),
        roster=roster)

    # ---------- ③ Agent 判断 ----------
    a = next(x for x in rt.economy.all_agents() if x.agent_id == aid)
    a.brain.preflight_credential()
    t0 = time.perf_counter()
    try:
        r = a.generate(messages=prompt)
    except Exception as exc:                                  # noqa: BLE001
        turn["error"] = f"{type(exc).__name__}: {exc}"
        return turn

    content = r.content or ""
    turn.update({"ok": True, "simulated": r.simulated,
                 "tokens": r.total_tokens,
                 "output_tokens": r.output_tokens,
                 "finish_reason": r.finish_reason,
                 "ms": round((time.perf_counter() - t0) * 1000, 1),
                 "chars": len(content), "head": content[:400]})

    # 🔴 空产出 ≠ 主动沉默 —— 必须分开记，否则报告会写成假的
    if is_empty_completion(content, r.output_tokens):
        turn["ok"] = False
        turn["empty_completion"] = True
        turn["error"] = (
            f"EmptyCompletion: 模型花了 {r.output_tokens} 个 output token "
            f"但 content 为空（推理模型把预算用在 reasoning_content 上）。"
            f" finish_reason={r.finish_reason}")
        return turn

    intent = parse_intent(content)
    turn["intent"] = {"parsed": intent["parsed"],
                      "assessment": intent["assessment"],
                      "artifact_path": (intent["artifact"] or {}).get("path"),
                      "message_to": (intent["message"] or {}).get("to")}

    # ---------- ④ Runtime 执行（不替 Agent 决定内容）----------
    # 4a. 写共享 Workspace —— 让 Gateway **真的**校验责任与 revision
    art = intent["artifact"]
    if art:
        wr = sw.apply_write(gw, WS_ID, aid, art["path"], art["content"])
        turn["workspace_write"] = wr
        if wr["ok"]:
            log["comm_events"].append({
                "kind": "workspace_write", "agent_id": aid, "round": rnd,
                "path": wr["path"], "revision": wr["revision"]})

    # 4b. 发消息 —— 不写就没有消息，代码**不**补发
    msg = intent["message"]
    if msg:
        res = channels.send(comm, bridge, from_agent=aid,
                            to_agent=msg["to"], text=msg["text"],
                            trace_id=trace_id, reply_to=msg.get("reply_to"))
        turn["message_sent"] = res
        if res.get("ok"):
            log["comm_events"].append({
                "kind": "sent", "agent_id": aid, "round": rnd,
                "from": aid, "to": msg["to"],
                "message_id": res["message_id"],
                "wakeup_signalled": res.get("wakeup_signalled"),
                "reply_to": msg.get("reply_to")})

    # 4c. 行为是否受信箱影响（**弱证据**，如实记录，不夸大）
    needles = [m.from_agent for m in inbox["received"]] + inbox_ids
    turn["references_inbox"] = mentions(content, needles)
    return turn


def _print_turn(aid: str, turn: Dict[str, Any]) -> None:
    if not turn.get("ok"):
        print(f"    ❌ {aid:22s} {turn.get('error', '')[:70]}")
        return
    bits = [f"tokens={turn['tokens']}"]
    ib = turn.get("inbox") or {}
    if ib.get("received_ids"):
        bits.append(f"📥{len(ib['received_ids'])}"
                    f"{'(wakeup)' if ib.get('wakeup') else ''}")
    wr = turn.get("workspace_write")
    if wr:
        bits.append(f"📝{'ok' if wr['ok'] else 'FAIL'} "
                    f"{wr['path']}"
                    + (f"→R{wr['revision']}" if wr.get("revision") else ""))
    ms = turn.get("message_sent")
    if ms:
        bits.append(f"✉️→{ms.get('to')}" if ms.get("ok")
                    else f"✉️FAIL {ms.get('error', '')[:30]}")
    if turn.get("references_inbox"):
        bits.append("🔗引用信箱")
    print(f"    ✅ {aid:22s} {'  '.join(bits)}")


if __name__ == "__main__":
    raise SystemExit(main())

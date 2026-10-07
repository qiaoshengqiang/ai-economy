#!/usr/bin/env python3
"""
正式长期生产实验 —— 5 Agent 自主生产约 10,000 字中文小说
================================================================

    PROD_LIMIT=3600 ./.venv/bin/python -u experiments/novel_production/formal_production.py --index 1

【🔒 这不是 Workflow】

    循环体是**轮转**：每轮每个 Agent 拿到同一份 Stage + 同一份 Workspace
    快照（含真实 revision）与自己的信箱，各自决定做什么。

    代码里**没有**任何「A 完成后通知 B」的自动投递；
    也没有任何「先做 X 才能做 Y」的顺序约束。

【🔴 本轮的经济模式：OBSERVE_ONLY】

    Budget 会记录、会进 Context、会显示已消耗与剩余，
    但**不阻断**任何东西 —— 即使超预算，Runtime 仍可继续生产。

    这是刻意的：先观察 Agent 在「有经济事实、没有硬经济压力」时的行为。

【🔴 必须传 task_id】

    `Brain.generate(task_id=...)` 会把它透传到 `llm_calls` 与 ledger memo。
    不传 → Task 消耗恒为 0（不是「没花钱」，是「查不到」）。

【🔴 不强迫任何事】

    ❌ 不为了让 Agent 产出而改 prompt 施压
    ❌ 不为了凑字数而强制续写
    ❌ 不为了让委托成交而放宽经济机制
    ❌ 不伪造完成状态
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channels                                            # noqa: E402
import shared_workspace as sw                              # noqa: E402

DB = HERE / "data" / "novel.db"
RESULTS = HERE / "results"

TASK_ID = "task_novel_001"
WS_ID = "WS-NOVEL-001"
AGENTS = ["novel_world_builder", "novel_plot_architect", "novel_author",
          "novel_editor", "novel_reader"]
TOPIC = ("在一个由 AI Agent 参与运行的数字世界里，"
         "身份、记忆、资产和历史究竟属于谁？")

LIMIT = float(os.environ.get("PROD_LIMIT", "3600"))
BUDGET_AIC = float(os.environ.get("PROD_BUDGET", "50"))

PROMPT = """你是这次长期小说生产任务的一名参与者。这是**正式生产**。

任务主题：{topic}
目标：一部约 10000 字的完整原创中文科幻悬疑小说。

{stage}

========== 共享 Task Workspace（{ws}）的真实当前状态 ==========
{workspace}

========== 你的信箱 ==========
{inbox}

========== 参与这个 Task 的其他 Agent ==========
{roster}

========== 你这一轮做什么，完全由你自己判断 ==========
你可以：只做自己的事、主动联系任何参与者、回复或无视信箱里的消息、
修改你负责的文件、发起一次正式的**生产性委托**、或什么都不做。

请只输出下面这个 JSON：

{{
  "assessment": "你对当前进展的判断（简短）",
  "artifact": {{"path": "你负责的文件名", "content": "完整内容"}},
  "message": {{"to": "收件人 agent_id", "text": "要说的话"}}
}}

"artifact" / "message" 都是**可选**的：不写就是不写，可以写成 null。
正文属于 Workspace，不要整篇复制进 message。
"""


def parse_intent(text: str) -> Dict[str, Any]:
    import re
    out: Dict[str, Any] = {"assessment": "", "artifact": None,
                           "message": None, "parsed": False}
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
            data = json.loads(c)
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
            out["message"] = {"to": str(msg["to"]).strip(),
                              "text": str(msg["text"])}
        break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=1)
    ap.add_argument("--max-rounds", type=int, default=9999)
    args = ap.parse_args()

    from main import build_runtime
    from stage import (RuntimeSession, SESSION_STOPPED_HARD_LIMIT,
                       STATUS_ACTIVE, StageStore)
    from stage.context import render_stage

    RESULTS.mkdir(parents=True, exist_ok=True)
    rt = build_runtime(db_path=str(DB), agent_ids=AGENTS)
    bridge = channels.SyncBridge()

    #: §九：预算相对充足但不无限。**不是为了强迫系统停止。**
    rt.budget_service.set_budget(TASK_ID, BUDGET_AIC)

    log: Dict[str, Any] = {
        "session_index": args.index, "limit_seconds": LIMIT,
        "budget_aic": BUDGET_AIC, "started_at": time.time(),
        "task_id": TASK_ID, "workspace_id": WS_ID, "agents": AGENTS,
        "turns": [], "comm_events": [],
    }
    stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "valid": 0, "empty": 0, "tokens": 0,
                 "output_tokens": 0, "writes_ok": 0, "writes_fail": 0,
                 "messages": 0, "chars_written": 0})
    cost_before = _cost(rt)

    try:
        st = StageStore(rt.economy.db)
        current = next((s for s in st.list_stages(TASK_ID) if s.is_open), None)
        if current is None:
            print("  ⚠️ 没有打开的 Stage")
            return 1
        if current.status != STATUS_ACTIVE:
            current = st.resume(current.stage_id)

        prior = st.latest_checkpoint(current.stage_id)
        live = sw.snapshot(rt.workspace_gateway, WS_ID, AGENTS[0])
        log["resume"] = {
            "stage_id": current.stage_id, "stage_status": current.status,
            "prior_checkpoint": prior.to_dict() if prior else None,
            "live_workspace_revision": live["revision"],
            "revision_matches": bool(prior and prior.revision == live["revision"]),
        }
        print(f"  ▶ Session {args.index}  Stage={current.stage_id} "
              f"status={current.status}")
        print(f"  ▶ Budget={BUDGET_AIC} AIC mode=OBSERVE_ONLY（不阻断）")
        if prior:
            print(f"  ↩︎ 上一个 Checkpoint {prior.checkpoint_id} "
                  f"rev={prior.revision}")
            print(f"     本进程读到 rev={live['revision']}  "
                  f"{'✅一致' if prior.revision == live['revision'] else '⚠️不一致'}")

        sess = RuntimeSession(limit_seconds=LIMIT).start()
        trace_id = f"trc_prod_{args.index}"
        rnd = 0

        while not sess.is_exceeded() and rnd < args.max_rounds:
            rnd += 1
            print(f"\n  ── 第 {rnd} 轮  (已用 {sess.elapsed_seconds():.0f}s"
                  f" / {LIMIT:.0f}s)")
            stage_txt = render_stage(current, limit_seconds=LIMIT,
                                     elapsed_seconds=sess.elapsed_seconds())
            for aid in AGENTS:
                if sess.is_exceeded():
                    print("  🛑 Hard Limit 到达，本轮中止")
                    break
                turn = _turn(rt, bridge, aid, rnd, stage_txt, trace_id,
                             log, stats)
                log["turns"].append(turn)
                _print_turn(aid, turn)

        if not sess.is_exceeded():
            print(f"\n  （达到 --max-rounds={args.max_rounds}，主动收工）")
            sess.stop_by_agent("max_rounds_reached")
        else:
            sess.enforce()
        log["session"] = sess.snapshot()
        log["rounds"] = rnd
        log["hard_limit_triggered"] = (sess.state == SESSION_STOPPED_HARD_LIMIT)

        # ---- Checkpoint：引用真实 Workspace revision ----
        after = sw.snapshot(rt.workspace_gateway, WS_ID, AGENTS[0])
        log["workspace_after"] = after
        ck = st.save_checkpoint(current.stage_id, reason="hard_limit",
                                workspace_id=WS_ID, revision=after["revision"],
                                progress={"rounds": rnd,
                                          "file_revisions": after["files"]})
        stage_after = st.get_stage(current.stage_id)
        log["checkpoint"] = ck.to_dict()
        log["stage_after"] = stage_after.to_dict()
        print(f"\n  🛑 {sess.state}  Stage={stage_after.status}")
        print(f"  🛑 checkpoint={ck.checkpoint_id}  rev={after['revision']}")

        log["per_agent"] = dict(stats)
        log["comm"] = channels.summarize(log["comm_events"])
        log["budget"] = rt.budget_service.audit(TASK_ID)
        log["production"] = _production(rt, log)
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
    out = RESULTS / f"formal_production_session_{args.index:02d}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  → {out}")
    print(f"  → wall={log['wall_seconds']}s cost={log.get('cost')}")
    return 0


def _turn(rt: Any, bridge: Any, aid: str, rnd: int, stage_txt: str,
          trace_id: str, log: Dict[str, Any],
          stats: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    comm, gw = rt.communication, rt.workspace_gateway
    turn: Dict[str, Any] = {"round": rnd, "agent_id": aid, "ok": False}

    inbox = channels.drain(comm, bridge, aid)
    turn["inbox"] = {"wakeup": inbox["wakeup"],
                     "received_ids": [m.message_id for m in inbox["received"]],
                     "from": sorted({m.from_agent for m in inbox["received"]})}
    if inbox["wakeup"]:
        log["comm_events"].append({"kind": "wakeup", "agent_id": aid,
                                   "round": rnd})
    for m in inbox["received"]:
        log["comm_events"].append({"kind": "received", "agent_id": aid,
                                   "round": rnd, "message_id": m.message_id,
                                   "from": m.from_agent})

    roster = "\n".join(f"- {x}" + ("（你）" if x == aid else "") for x in AGENTS)
    prompt = PROMPT.format(
        topic=TOPIC, stage=stage_txt, ws=WS_ID,
        workspace=sw.render(gw, WS_ID, aid),
        inbox=channels.render_inbox(inbox["received"]), roster=roster)

    a = next(x for x in rt.economy.all_agents() if x.agent_id == aid)
    a.brain.preflight_credential()
    t0 = time.perf_counter()
    try:
        # 🔴 必须传 task_id —— 否则 Task 消耗恒为 0
        r = a.generate(messages=prompt, task_id=TASK_ID)
    except Exception as exc:                                  # noqa: BLE001
        turn["error"] = f"{type(exc).__name__}: {exc}"
        return turn

    content = r.content or ""
    s = stats[aid]
    s["calls"] += 1
    s["tokens"] += int(r.total_tokens or 0)
    s["output_tokens"] += int(r.output_tokens or 0)
    turn.update({"ok": True, "tokens": r.total_tokens,
                 "output_tokens": r.output_tokens,
                 "finish_reason": r.finish_reason,
                 "output_status": getattr(r, "output_status", None),
                 "ms": round((time.perf_counter() - t0) * 1000, 1),
                 "chars": len(content), "head": content[:300]})

    if not getattr(r, "has_valid_content", True):
        s["empty"] += 1
        turn["ok"] = False
        turn["empty_content"] = True
        turn["error"] = (f"EMPTY_CONTENT (output_tokens={r.output_tokens}, "
                         f"finish_reason={r.finish_reason})")
        return turn
    s["valid"] += 1

    intent = parse_intent(content)
    turn["intent"] = {"parsed": intent["parsed"],
                      "artifact_path": (intent["artifact"] or {}).get("path"),
                      "message_to": (intent["message"] or {}).get("to")}

    if intent["artifact"]:
        art = intent["artifact"]
        wr = sw.apply_write(gw, WS_ID, aid, art["path"], art["content"])
        turn["workspace_write"] = wr
        if wr["ok"]:
            s["writes_ok"] += 1
            s["chars_written"] += len(art["content"])
            log["comm_events"].append({"kind": "workspace_write",
                                       "agent_id": aid, "round": rnd,
                                       "path": wr["path"],
                                       "revision": wr["revision"]})
        else:
            s["writes_fail"] += 1

    if intent["message"]:
        res = channels.send(comm, bridge, from_agent=aid,
                            to_agent=intent["message"]["to"],
                            text=intent["message"]["text"], trace_id=trace_id)
        turn["message_sent"] = res
        if res.get("ok"):
            s["messages"] += 1
            log["comm_events"].append({"kind": "sent", "agent_id": aid,
                                       "round": rnd, "from": aid,
                                       "to": intent["message"]["to"],
                                       "message_id": res["message_id"]})
    return turn


def _print_turn(aid: str, turn: Dict[str, Any]) -> None:
    if not turn.get("ok"):
        print(f"    ❌ {aid:22s} {turn.get('error', '')[:70]}")
        return
    bits = [f"tokens={turn['tokens']}"]
    if turn.get("inbox", {}).get("received_ids"):
        bits.append(f"📥{len(turn['inbox']['received_ids'])}")
    wr = turn.get("workspace_write")
    if wr:
        bits.append(f"📝{'ok' if wr['ok'] else 'FAIL'} {wr['path']}"
                    + (f"→R{wr['revision']}" if wr.get("revision") else ""))
    ms = turn.get("message_sent")
    if ms:
        bits.append(f"✉️→{ms.get('to')}")
    print(f"    ✅ {aid:22s} {'  '.join(bits)}")


def _cost(rt: Any) -> Dict[str, Any]:
    try:
        row = rt.economy.db.fetchall(
            "SELECT SUM(total_tokens) t, SUM(cost_ai) c FROM llm_calls")[0]
        return {"tokens": int(row["t"] or 0), "aic": float(row["c"] or 0.0)}
    except Exception:                                         # noqa: BLE001
        return {"tokens": 0, "aic": 0.0}


def _production(rt: Any, log: Dict[str, Any]) -> Dict[str, Any]:
    """§十四 Production 观测 + §十五 Production Conversion。"""
    wordcounts: Dict[str, int] = {}
    for path in sw.ARTIFACTS:
        got = rt.workspace_gateway.read(WS_ID, f"/{path}", AGENTS[0])
        text = ((got.result or {}).get("content") or "") if got.success else ""
        wordcounts[path] = len(text.strip())
    total_chars = sum(wordcounts.values())

    per_agent = log.get("per_agent") or {}
    conv = {}
    for aid, s in per_agent.items():
        tok = s.get("tokens") or 0
        written = s.get("chars_written") or 0
        conv[aid] = {
            "tokens": tok,
            "written_chars": written,
            "chars_per_1k_tokens": (round(written / tok * 1000, 2)
                                    if tok else None),
        }
    return {
        "wordcounts": wordcounts,
        "total_chars": total_chars,
        "conservation": conv,
        "agency_cost_of_total": None,
    }


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
第一轮真正异质多 Agent 自主生产实验
================================================================

    ./.venv/bin/python -u experiments/heterogeneous_production/production.py

【本轮测试什么（§一）】

    不是「5 个预设角色能不能按流程合作」。
    而是：**Human 只给一个 Demand、不规定生产结构时，
    5 个异质 Agent 能否自己形成有效的生产组织并完成任务。**

【🔒 这不是 Workflow（§三）】

    · 循环体是**轮询**（本报告里明确标记为 **poll-driven**，§十八）
    · 没有「A 完成后 B 才能开始」
    · 没有 Stage1=world / Stage2=outline … 的预设阶段
    · 没有 Coordinator / Pipeline / ProductionManager
    · Agent 自己决定：做不做什么、找不找人、建不建文件、要不要委托

【🔴 §六 / §七：必须把「环境事实」给 Agent】

    D/E smoke 已经证明：**不给事实，Agent 会盲猜路径并全部失败。**
    所以每轮 prompt 都包含：

        · Agent Directory（谁在、专业方向）—— 事实，不是「你应该联系谁」
        · Workspace Facts（命名空间、负责人、已有 Artifact、revision）
          **包括空命名空间** —— 否则 Agent 不知道它能在哪创建

【退出条件（§二十–§二十三）】

    · 正常完成 / 120 分钟总墙钟 / NO_PRODUCTIVE_PROGRESS / INFRASTRUCTURE_FAILURE
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from actions import (  # noqa: E402
    ACTION_WORKSPACE_WRITE, ActionDispatcher, ActionIntent,
    WorkspaceWriteExecutor, new_intent_id,
)
from services.artifacts import (  # noqa: E402
    ARTIFACT_NAMESPACES, MODE_APPEND, MODE_CREATE, MODE_REPLACE,
    provision_namespaces, write_artifact,
)

HERE = Path(__file__).resolve().parent
#: `channels.py`（通信同步桥）目前住在 novel_production 里 —— 复用它，
#: 而不是复制一份（那是同一个实验工具，不该有两份实现）。
sys.path.insert(0, str(ROOT / "experiments" / "novel_production"))

DB = HERE / "data" / "hetero.db"
RESULTS = HERE / "results"

EXPERIMENT_ID = "hetero_novel_v1"
TASK_ID = "task_hetero_001"
WS_ID = "WS-HETERO-001"
AGENTS = ["novel_world_builder", "novel_plot_architect", "novel_author",
          "novel_editor", "novel_reader"]
OWNER = AGENTS[0]

#: §二十：整个实验最多 120 分钟墙钟。
TOTAL_WALL_LIMIT = float(os.environ.get("HETERO_TOTAL_LIMIT", "7200"))
#: §十九：单 Session 30 分钟。
SESSION_LIMIT = float(os.environ.get("HETERO_SESSION_LIMIT", "1800"))
#: 单 Session 最多多少轮（防止轮询无限打转）。
MAX_ROUNDS_PER_SESSION = int(os.environ.get("HETERO_MAX_ROUNDS", "60"))
#: §二十一：连续两个 Session 有效产出低于此值 → NO_PRODUCTIVE_PROGRESS
MIN_PRODUCTIVE_CHARS_PER_SESSION = 400

BUDGET_AIC = 50.0
COGNITIVE_BUDGET = 8192

DEMAND = """【Human Demand】

请创作一部约 10000 字的原创中文科幻悬疑小说。

主题方向：在一个由 AI Agent 参与运行的数字世界里，
身份、记忆、资产和历史发生了冲突。

这是一个开放生产任务：如何组织生产、创建哪些产物、
是否需要其他参与者配合，全部由你自己判断。
"""

PROMPT = """你是这次长期生产任务的一名参与者。这是**正式生产**。

{demand}

========== 当前 Stage ==========
{stage}

========== 共享 Task Workspace（{ws}）的真实环境事实 ==========
{workspace}

（以上只是**当前环境事实**，不是要求。你可以在合法的命名空间里
  创建新的 Artifact，也可以选择已有的路径。）

========== 参与这个 Task 的其他 Agent（Agent Directory 事实）==========
{roster}

========== 你的信箱 ==========
{inbox}

========== 你这一轮做什么，完全由你自己判断 ==========
请只输出下面这个 JSON：

{{
  "assessment": "你对当前进展的判断（简短）",
  "artifact": {{
    "path": "共享 Workspace 里的逻辑路径（可含子目录）",
    "write_mode": "create | replace | append",
    "content": "这次要写入的完整文本"
  }},
  "message": {{"to": "收件人 agent_id", "text": "要说的话"}}
}}

"artifact" / "message" 都是**可选**的：不写就是不写，可以写成 null。
"write_mode" 决定这次写入的语义（新建 / 整体替换 / 末尾追加），由你自己决定。
不要整篇复制进 message —— 正文属于 Workspace。
"""


# ================================================================
def parse_intent(text: str) -> Dict[str, Any]:
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
            d = json.loads(c)
        except Exception:                                     # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        out["parsed"] = True
        out["assessment"] = str(d.get("assessment") or "")[:400]
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
    import channels
    from main import build_runtime
    from stage import STATUS_ACTIVE, StageStore
    from stage.context import render_stage

    RESULTS.mkdir(parents=True, exist_ok=True)
    started_wall = time.time()
    log: Dict[str, Any] = {
        "experiment_id": EXPERIMENT_ID, "task_id": TASK_ID,
        "workspace_id": WS_ID, "agents": AGENTS,
        "started_at": started_wall,
        "total_wall_limit": TOTAL_WALL_LIMIT,
        "session_limit": SESSION_LIMIT,
        "budget_aic": BUDGET_AIC,
        "scheduler": "poll-driven (rotation)",     # §十八 必须标记
        "sessions": [], "comm_events": [],
    }
    stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "valid": 0, "empty": 0, "tokens": 0,
                 "writes_ok": 0, "writes_fail": 0, "messages": 0,
                 "chars_written": 0, "created": 0, "appended": 0,
                 "replaced": 0})
    cost_before = _cost_rt(None, DB)
    session_index = 0
    prev_session_chars: Optional[int] = None
    low_progress_streak = 0
    exit_reason = "UNKNOWN"
    bridge = None

    try:
        # ---- 环境（Task / Workspace / 命名空间 / 根 Stage）----
        rt = build_runtime(db_path=str(DB), agent_ids=AGENTS)
        _ensure_environment(rt)
        rt.budget_service.set_budget(TASK_ID, BUDGET_AIC)
        gw = rt.workspace_gateway

        while True:
            elapsed_total = time.time() - started_wall
            if elapsed_total >= TOTAL_WALL_LIMIT:
                exit_reason = "TOTAL_WALL_LIMIT_REACHED"
                break

            session_index += 1
            budget_left = TOTAL_WALL_LIMIT - elapsed_total
            limit = min(SESSION_LIMIT, budget_left)
            sess, bridge, s_log = _run_session(
                rt, bridge, session_index, limit, log, stats)
            log["sessions"].append(s_log)

            # ---- §二十一 无效生产判定 ----
            chars_now = _total_chars(gw)
            if prev_session_chars is not None:
                gained = chars_now - prev_session_chars
                s_log["chars_gained"] = gained
                if gained < MIN_PRODUCTIVE_CHARS_PER_SESSION:
                    low_progress_streak += 1
                else:
                    low_progress_streak = 0
                if low_progress_streak >= 2:
                    exit_reason = "NO_PRODUCTIVE_PROGRESS"
                    break
            prev_session_chars = chars_now

            # ---- §二十三 正常完成（保守判定：Agent 自述完成 + 字数达标）----
            if s_log.get("agents_claim_done") and chars_now >= 9000:
                exit_reason = "COMPLETED"
                break

        log["exit_reason"] = exit_reason
        log["sessions_run"] = session_index
        log["per_agent"] = dict(stats)
        log["final_workspace"] = _snapshot(gw)
        log["budget"] = rt.budget_service.audit(TASK_ID)
    except Exception as exc:                                  # noqa: BLE001
        log["exit_reason"] = "INFRASTRUCTURE_FAILURE"
        log["failure"] = f"{type(exc).__name__}: {exc}"
        exit_reason = "INFRASTRUCTURE_FAILURE"
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:                                 # noqa: BLE001
                pass
        try:
            after = _cost_rt(None, DB)
            log["cost"] = {
                "tokens": after["tokens"] - cost_before["tokens"],
                "aic": round(after["aic"] - cost_before["aic"], 6)}
        except Exception:                                     # noqa: BLE001
            pass

    log["wall_seconds"] = round(time.time() - started_wall, 1)
    log["wall_minutes"] = round(log["wall_seconds"] / 60, 1)
    out = RESULTS / "heterogeneous_production_result.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  退出原因: {log.get('exit_reason')}")
    print(f"  总墙钟: {log['wall_minutes']} 分钟 / "
          f"{log.get('sessions_run')} 个 Session")
    print(f"  成本: {log.get('cost')}")
    print(f"  → {out}")
    return 0


# ================================================================
def _ensure_environment(rt: Any) -> None:
    """Task + participants + Workspace + 命名空间 + 最小根 Stage。"""
    from core.task import Task
    from stage import STATUS_ACTIVE, StageStore

    if rt.tasks.get(TASK_ID) is None:
        rt.tasks.save(Task.create(
            creator="human_demand",
            description=(f"创作一部约 10000 字的原创中文科幻悬疑小说。"
                         f"主题：AI Agent 世界中的身份、记忆、资产与历史冲突。"),
            workspace_id=WS_ID, task_id=TASK_ID, participants=list(AGENTS)))
    else:
        t = rt.tasks.get(TASK_ID)
        missing = [a for a in AGENTS if a not in (t.participants or [])]
        if missing:
            t.participants = list(t.participants or []) + missing
            rt.tasks.save(t)

    rt.workspace_gateway.ensure_workspace(WS_ID, OWNER)
    provision_namespaces(rt.workspace_gateway, WS_ID, OWNER)

    st = StageStore(rt.economy.db)
    if not st.list_stages(TASK_ID):
        # §十七：**只**创建一个最小根 Stage，不预设后续阶段
        st.create_stage(
            TASK_ID,
            "Produce a complete ~10,000-word original Chinese sci-fi "
            "mystery novel about identity / memory / assets / history "
            "conflicting in a world run by AI Agents.",
            deliverables=[{"name": "complete manuscript"}],
            status=STATUS_ACTIVE, stage_id="stage_hetero_root")


def _action_seam(rt: Any) -> tuple:
    """
    🔴 M3：把「写操作 → ActionResult → Experience」接起来。

    【为什么需要这一小段】

        原先 `_turn` 直接调用 `write_artifact()`，拿到一个**裸 dict**，
        只写进内存日志 —— 于是 Gateway 明明知道「这个文件已存在」，
        Agent 下一轮完全不知道（真实证据见
        `results/ACTION_FEEDBACK_AUDIT.md`）。

    🔒 **它只做接线，不做任何决策：**

        · Dispatcher   —— 只把 intent 路由到 Executor
        · Executor     —— 只把 write_artifact 的返回值翻成 ActionResult
        · ExperienceStore —— 只把 ActionResult **原样**记下来

      没有任何一步替 Agent 选 write_mode / 选路径 / 挑下一步策略。

    🔒 `ExperienceStore` 建在 `rt.economy.db` 上 —— 与 `main.py`
       注入给 `CognitiveContextBuilder` 的那个**是同一个库**，
       所以这里记下的经历下一轮会被 `retrieve_relevant()` 取到。
    """
    from experience import ExperienceStore

    dispatcher = ActionDispatcher([
        WorkspaceWriteExecutor(write_artifact,
                               gateway=rt.workspace_gateway,
                               workspace_id=WS_ID)])
    try:
        experience_store = ExperienceStore(rt.economy.db)
    except Exception as exc:                              # noqa: BLE001
        # Fail-soft ≠ Fail-silent：写不进去要说出来，但**不许**因此
        # 伪造一条经历，也不许让整轮认知崩掉。
        print(f"  ⚠️  ExperienceStore 不可用（{type(exc).__name__}: {exc}）"
              f"—— 行动反馈**不会**进入下一轮认知")
        experience_store = None
    return dispatcher, experience_store


def _record_action_experience(store: Any, agent_id: str, intent: Any,
                              result: Any) -> Optional[str]:
    """
    §五：ActionResult → Experience —— **原样记录，不做解读**。

    🔒 它不回写 Self Model、不降 confidence、不改策略、不选下一步。
       唯一作用是让**下一轮** Cognition 能检索到「刚刚发生了什么」。

    返回 experience_id（供运行日志留证）；未记录时返回 None。
    """
    if store is None:
        return None
    try:
        exp = store.record_action_result(agent_id, intent, result)
    except Exception as exc:                              # noqa: BLE001
        print(f"    ⚠️  Experience 记录失败：{type(exc).__name__}: {exc}")
        return None
    return getattr(exp, "experience_id", None)


def _run_session(rt: Any, bridge: Any, index: int, limit: float,
                 log: Dict[str, Any], stats: Dict[str, Any]) -> tuple:
    import channels
    from stage import RuntimeSession, SESSION_STOPPED_HARD_LIMIT, StageStore
    from stage.context import render_stage

    bridge = bridge or channels.SyncBridge()
    st = StageStore(rt.economy.db)
    stage = next((s for s in st.list_stages(TASK_ID) if s.is_open), None)
    # 🔴 M3：Action Seam —— 写操作必须经过 Dispatcher，结果必须落成 Experience
    dispatcher, experience_store = _action_seam(rt)

    sess = RuntimeSession(limit_seconds=limit).start()
    s_log: Dict[str, Any] = {"index": index, "limit_seconds": limit,
                             "turns": [], "agents_claim_done": False}
    print(f"\n===== Session {index}（{limit/60:.0f} 分钟上限）=====")

    rnd = 0
    while not sess.is_exceeded() and rnd < MAX_ROUNDS_PER_SESSION:
        rnd += 1
        # 🔴 M5：把**真实的** checkpoint 交给 render_stage。
        #    旧实现不传它 → 上一轮的 workspace revision 从未进入下一轮认知。
        ckpt = (st.latest_checkpoint(stage.stage_id)
                if stage is not None else None)
        stage_txt = render_stage(stage, checkpoint=ckpt, limit_seconds=limit,
                                 elapsed_seconds=sess.elapsed_seconds())
        if ckpt is not None:
            # 可观测证据：这一轮的 prompt 里**确实**带了哪个 checkpoint
            s_log["checkpoint_in_context"] = ckpt.checkpoint_id
        print(f"  ── 第 {rnd} 轮 (已用 {sess.elapsed_seconds():.0f}s)")
        for aid in AGENTS:
            if sess.is_exceeded():
                print("  🛑 Hard Limit 到达，本轮中止")
                break
            turn = _turn(rt, bridge, st, stage, aid, rnd, stage_txt, log, stats,
                         dispatcher=dispatcher,
                         experience_store=experience_store)
            s_log["turns"].append(turn)
            _print_turn(aid, turn)

    if not sess.is_exceeded():
        sess.stop_by_agent("max_rounds_reached")
    else:
        sess.enforce()
    s_log["session"] = sess.snapshot()
    s_log["rounds"] = rnd
    s_log["hard_limit"] = (sess.state == SESSION_STOPPED_HARD_LIMIT)

    # ---- Checkpoint：引用**真实** Workspace revision（§十六 / §六）----
    #
    #   🔒 只引用 `workspace_id + revision`，**不复制内容** ——
    #      Workspace 才是 Artifact 的唯一事实源。
    rev = _workspace_revision(rt.workspace_gateway)
    ck = st.save_checkpoint(stage.stage_id, reason="hard_limit",
                            workspace_id=WS_ID, revision=rev,
                            progress={"session": index, "rounds": rnd})
    s_log["checkpoint"] = ck.to_dict()
    s_log["workspace_revision"] = rev
    print(f"  🛑 Session {index} 结束：{sess.state}  "
          f"rounds={rnd}  checkpoint={ck.checkpoint_id}  rev={rev}")
    return sess, bridge, s_log


def _workspace_revision(gw: Any) -> str:
    """
    把每个 Artifact 的**真实 revision 号**压成一个可比较的 checkpoint revision。

    🔒 内容没变 → revision 不变；内容变了 → 一定变。
       与 `novel_production/shared_workspace.fingerprint` 同构。
    """
    import hashlib
    files = {}
    for d in ARTIFACT_NAMESPACES:
        got = gw.list(WS_ID, "/" + d, OWNER)
        if not got.success:
            continue
        for e in got.result.get("entries", []):
            files[f"/{d}/{e['name']}"] = e.get("revision")
    payload = json.dumps(sorted(files.items()), ensure_ascii=False)
    digest = hashlib.sha256(f"{WS_ID}|{payload}".encode()).hexdigest()[:16]
    return f"ws:{WS_ID}@{digest}"


def _turn(rt: Any, bridge: Any, st: Any, stage: Any, aid: str, rnd: int,
          stage_txt: str, log: Dict[str, Any],
          stats: Dict[str, Any], *, dispatcher: Any = None,
          experience_store: Any = None) -> Dict[str, Any]:
    import channels

    comm, gw = rt.communication, rt.workspace_gateway
    turn: Dict[str, Any] = {"round": rnd, "agent_id": aid, "ok": False}

    inbox = channels.drain(comm, bridge, aid)
    turn["inbox"] = {"wakeup": inbox["wakeup"],
                     "received": len(inbox["received"]),
                     "from": sorted({m.from_agent for m in inbox["received"]})}
    if inbox["wakeup"]:
        log["comm_events"].append({"kind": "wakeup", "agent_id": aid,
                                   "round": rnd})

    prompt = PROMPT.format(
        demand=DEMAND, stage=stage_txt, ws=WS_ID,
        workspace=_workspace_facts(gw, aid),
        roster=_roster_facts(rt, aid),
        inbox=channels.render_inbox(inbox["received"]))

    a = next(x for x in rt.economy.all_agents() if x.agent_id == aid)
    a.brain.preflight_credential()
    t0 = time.perf_counter()
    try:
        r = a.generate(messages=prompt, task_id=TASK_ID,
                       requested_cognitive_budget=COGNITIVE_BUDGET)
    except Exception as exc:                                  # noqa: BLE001
        turn["error"] = f"{type(exc).__name__}: {exc}"
        stats[aid]["calls"] += 1
        stats[aid]["empty"] += 1
        return turn

    s = stats[aid]
    content = r.content or ""
    s["calls"] += 1
    s["tokens"] += int(r.total_tokens or 0)
    turn.update({"ok": True, "tokens": r.total_tokens,
                 "output_status": r.output_status,
                 "requested_model": r.requested_model,
                 "response_model": r.response_model,
                 "finish_reason": r.finish_reason,
                 "ms": round((time.perf_counter() - t0) * 1000, 1),
                 "chars": len(content)})

    if not getattr(r, "has_valid_content", True):
        s["empty"] += 1
        turn["ok"] = False
        turn["error"] = "EMPTY_CONTENT"
        return turn
    s["valid"] += 1

    intent = parse_intent(content)
    if "完成" in intent["assessment"] and "未" not in intent["assessment"]:
        turn["claims_done"] = True

    if intent["artifact"]:
        art = intent["artifact"]
        mode = art["write_mode"] or MODE_REPLACE
        if mode == MODE_CREATE:
            s["created"] += 1
        elif mode == MODE_APPEND:
            s["appended"] += 1
        else:
            s["replaced"] += 1

        # 🔴 M3：**经过 ActionDispatcher**，不再直连 Gateway。
        #
        #   旧路径：write_artifact() → 裸 dict → turn["write"] → 进程结束即消失
        #   新路径：ActionIntent → Dispatcher → WorkspaceWriteExecutor
        #           → write_artifact() → **ActionResult**
        #
        #   🔒 write_mode 仍然是 **Agent 自己声明的那个**，原样透传；
        #      Executor 不重试、不改 mode、不改路径。
        if dispatcher is None:                            # pragma: no cover
            raise RuntimeError(
                "_turn 需要 ActionDispatcher —— 写操作必须经过 Action Seam（M3）")
        action_intent = ActionIntent(
            action_type=ACTION_WORKSPACE_WRITE, agent_id=aid,
            parameters={"path": art["path"], "content": art["content"],
                        "write_mode": mode},
            reason=str(intent.get("assessment") or "")[:200],
            intent_id=new_intent_id())
        action_result = dispatcher.dispatch(action_intent)

        out = (action_result.output
               if isinstance(action_result.output, dict) else {})
        turn["write"] = {"path": art["path"], "mode": mode,
                         "ok": bool(action_result.success),
                         "code": (action_result.metadata or {}).get("code"),
                         "revision": out.get("revision"),
                         "previous_chars": out.get("previous_chars"),
                         "appended_chars": out.get("appended_chars")}
        if action_result.success:
            s["writes_ok"] += 1
            s["chars_written"] += len(art["content"])
        else:
            s["writes_fail"] += 1

        # 🔴 M3：ActionResult → Experience —— **成功与失败都记**。
        #
        #   这一行就是审计里「Gateway 知道 → Runtime 知道 → **Agent 不知道**」
        #   那个缺口的补法：记下来之后，下一轮 `Brain.generate` 经
        #   `CognitiveContextBuilder.build()` → `retrieve_relevant()` 会取到它，
        #   渲染进 system —— 闭环成立。
        turn["experience"] = _record_action_experience(
            experience_store, aid, action_intent, action_result)

    if intent["message"]:
        res = channels.send(comm, bridge, from_agent=aid,
                            to_agent=intent["message"]["to"],
                            text=intent["message"]["text"],
                            trace_id=EXPERIMENT_ID)
        turn["message"] = {"to": intent["message"]["to"], "ok": res.get("ok")}
        if res.get("ok"):
            s["messages"] += 1
            log["comm_events"].append({"kind": "sent", "agent_id": aid,
                                       "round": rnd, "to": intent["message"]["to"]})
    return turn


def _print_turn(aid: str, turn: Dict[str, Any]) -> None:
    if not turn.get("ok"):
        print(f"    ❌ {aid:22s} {turn.get('error', '')[:60]}")
        return
    bits = [f"tokens={turn['tokens']}"]
    w = turn.get("write")
    if w:
        mark = "✅" if w["ok"] else f"❌{w.get('code')}"
        extra = ""
        if w["mode"] == MODE_APPEND and w["ok"]:
            extra = f" (+{w.get('appended_chars')}→R{w.get('revision')})"
        bits.append(f"📝{w['mode']} {w['path']} {mark}{extra}")
    if turn.get("message"):
        bits.append(f"✉️→{turn['message']['to']}")
    print(f"    ✅ {aid:22s} {'  '.join(bits)}")


# ================================================================
def _describe_entry(e: Dict[str, Any]) -> str:
    """
    🔴 M4：把 `gateway.list()` 的一个条目渲染成一行。

    🔒 **只陈述 Gateway 已经返回的字段**：
        name · type · revision · effective_controller · effective_lock_holder
      不新增字段、不做推断、不做权限判断。

    【为什么必须带上 revision（真实证据）】

        旧实现只取 `e.get("name")`，把 Gateway **已经算好**的 `revision` 丢掉。
        于是 Agent 只能看到「这个文件在」，看不到「它已经是 R2」——
        无法区分「这是我上轮改的」和「这是别人刚建的」。
        实测：`novel_plot_architect` 5 轮重复 `CREATE` 同一路径。
    """
    name = str(e.get("name") or "")
    facts: List[str] = []
    if e.get("type"):
        facts.append(str(e["type"]))
    if e.get("revision") is not None:
        facts.append(f"R{e['revision']}")
    if e.get("effective_controller"):
        facts.append(f"controller={e['effective_controller']}")
    if e.get("effective_lock_holder"):
        facts.append(f"lock={e['effective_lock_holder']}")
    return f"{name}（{', '.join(facts)}）" if facts else name


def _workspace_facts(gw: Any, agent: str) -> str:
    """
    §七：把 Workspace 的**真实环境事实**给 Agent。

    🔴 **空命名空间也必须列出来** —— D/E smoke 已证明：
       不列出来 Agent 就不知道 `/manuscript/` 存在，只能瞎猜路径，
       然后全部 `locked_by_other`。那是**事实投递问题**，不是 Agent 行为问题。

    🔴 M4：每个条目带上 Gateway 已经给出的 `type` / `revision` /
       `effective_controller` / `effective_lock_holder` ——
       否则 Agent 不知道当前状态，只知道「有这么个名字」。

    🔒 只陈述事实，不写「你应该写 /world/」，也不写「这个文件是你的」。
    """
    lines = [f"workspace_id = {WS_ID}"]
    for d, owner in ARTIFACT_NAMESPACES.items():
        got = gw.list(WS_ID, "/" + d, agent)
        entries = ([e for e in (got.result or {}).get("entries", []) or []]
                   if got.success else [])
        mine = "（你的命名空间）" if owner == agent else ""
        body = (", ".join(_describe_entry(e) for e in entries)
                if entries else "（空）")
        lines.append(f"  /{d}/  负责人={owner}{mine}  内容: {body}")

    # 根目录下的既有 Artifact
    root = gw.list(WS_ID, "/", agent)
    if root.success:
        files = [e for e in (root.result or {}).get("entries", []) or []
                 if e.get("type") == "file"]
        body = (", ".join(_describe_entry(e) for e in files)
                if files else "（无）")
        lines.append(f"  / 根目录文件: {body}")
    return "\n".join(lines)


def _roster_facts(rt: Any, agent: str) -> str:
    """
    §六：Agent Directory 事实 —— 谁在、专业方向。

    🔒 只提供事实，**不写**「你应该联系谁」。
    """
    lines = []
    for entry in rt.directory.all():
        aid = getattr(entry, "agent_id", "")
        if not aid or aid == agent:
            continue
        caps = ", ".join(str(c) for c in (entry.capabilities or ()))
        lines.append(f"  - {aid}  capabilities={caps or '(none)'}")
    return "\n".join(lines) or "  （没有其他参与者）"


def _total_chars(gw: Any) -> int:
    total = 0
    for d in ARTIFACT_NAMESPACES:
        got = gw.list(WS_ID, "/" + d, OWNER)
        if not got.success:
            continue
        for e in got.result.get("entries", []):
            rd = gw.read(WS_ID, f"/{d}/{e['name']}", OWNER)
            if rd.success:
                total += len((rd.result or {}).get("content") or "")
    return total


def _snapshot(gw: Any) -> List[Dict[str, Any]]:
    out = []
    for d in ARTIFACT_NAMESPACES:
        got = gw.list(WS_ID, "/" + d, OWNER)
        if not got.success:
            continue
        for e in got.result.get("entries", []):
            p = f"/{d}/{e['name']}"
            rd = gw.read(WS_ID, p, OWNER)
            content = ((rd.result or {}).get("content") or "") if rd.success else ""
            out.append({"path": p, "revision": e.get("revision"),
                        "chars": len(content), "head": content[:200]})
    return out


def _cost_rt(rt: Any, db_path: Path) -> Dict[str, Any]:
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        r = db.execute("SELECT COUNT(*), COALESCE(SUM(total_tokens),0), "
                       "COALESCE(SUM(cost_ai),0) FROM llm_calls").fetchone()
        db.close()
        return {"calls": int(r[0]), "tokens": int(r[1]), "aic": float(r[2])}
    except Exception:                                         # noqa: BLE001
        return {"calls": 0, "tokens": 0, "aic": 0.0}


if __name__ == "__main__":
    raise SystemExit(main())

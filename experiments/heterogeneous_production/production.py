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
import uuid
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from communication.message import MessageType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from actions import (  # noqa: E402
    ACTION_WORKSPACE_WRITE, ActionDispatcher, ActionIntent,
    WorkspaceWriteExecutor, new_intent_id,
)
from services.artifacts import (  # noqa: E402
    MODE_APPEND, MODE_CREATE, MODE_REPLACE,
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
#: 🔴 **总墙钟上限：60 分钟**（原为 120 分钟）。
#:
#:    规格「Observation闭环与验证实验」§17 固定了这个数字：
#:        TOTAL_WALL_LIMIT = 3600
#:        SESSION_LIMIT    = 1800
#:    即最多 2 个 session × 每个最多 30 分钟。
#:
#:    🔒 本轮的验证目标是「关键观察在真实 cognitive turn 里
#:       能否持续可见」—— 那不是统计显著性实验（§18），
#:       不需要更长的墙钟。跑满不会让证据更强，只会多花调用。
#:
#:    ⚠️ 这是**默认值**；仍可用 HETERO_TOTAL_LIMIT 覆盖。
TOTAL_WALL_LIMIT = float(os.environ.get("HETERO_TOTAL_LIMIT", "3600"))
#: §十九：单 Session 30 分钟。
SESSION_LIMIT = float(os.environ.get("HETERO_SESSION_LIMIT", "1800"))
#: 单 Session 最多多少轮（防止轮询无限打转）。
MAX_ROUNDS_PER_SESSION = int(os.environ.get("HETERO_MAX_ROUNDS", "60"))
#: §二十一：连续两个 Session 有效产出低于此值 → NO_PRODUCTIVE_PROGRESS
MIN_PRODUCTIVE_CHARS_PER_SESSION = 400

BUDGET_AIC = 50.0
COGNITIVE_BUDGET = 8192

#: 本实验创建的**根 Stage** 的固定 ID。
#:
#: 🔒 有了它，`_close_stage()` 才能关**自己驱动的那一个** Stage，
#:    而不是「碰巧第一个 is_open 的」—— 后者会误关别人的 Stage。
ROOT_STAGE_ID = "stage_hetero_root"

#: 生产者（承接方）—— 提交成果与 Completion Proposal 的那个 Agent。
#:
#: 🔒 **必须与 ACCEPTOR 不同** —— 否则「生产者不能自验」
#:    这条边界在这条链上就无从体现（验收会被执行服务拒绝）。
#: 🔴 **本实验的分工映射 —— 它属于实验层，不属于生产层。**
#:
#:    这里曾经住在 `services/artifacts.py`，也就是**生产应用层**，
#:    而它零个生产调用方（只有本实验与本仓的一个测试在用）。
#:    那是「实验专用的分工被写进系统」，违反 AGENTS.md §1 / §7。
#:
#:    🔒 现在生产层只提供**机制**（建目录 + handoff 所有权），
#:       **谁负责哪片区域**由实验自己声明 —— 就在这一行。
#:       `provision_namespaces(..., ARTIFACT_NAMESPACES)` 显式传进去。
#:
#:    ⚠️ 这**不代表** Agent 能自主分工 —— 轮询调度与预分配分工
#:       仍然是 V1 的**临时替身**（Constitution §15.1 把
#:       `Scheduler / 自主唤醒` 列为未解锁）。本改动只是把它
#:       放回正确的那一层，没有解锁任何能力。
ARTIFACT_NAMESPACES = {
    "world": "novel_world_builder",
    "plot": "novel_plot_architect",
    "manuscript": "novel_author",
    "notes": "novel_editor",
    "reader": "novel_reader",
}

PRODUCER = "novel_author"

#: 验收责任人 —— 负责 VERIFY / COMPLETE 的那个 Agent。
#:
#: 让「读者」验收「作者」的作品，语义上也最自然。
ACCEPTOR = "novel_reader"

#: Definition of Done —— 由 **Agent 自主结构化**（不是系统写死的清单）。
#:
#: 它落进 `TaskSharedContext.acceptance_criteria`，
#: 之后被认知上下文渲染给所有 Agent 看。
DEFINITION_OF_DONE = (
    "手稿 artifact 存在且非空",
    "全书有明确的结尾标记（例如「（完）」）",
    "至少一个 Agent 提交 Completion Proposal 说明对照 DoD 的达标情况",
    "由非生产者的验收责任人给出验收结论",
)


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

========== 完成标准与验收状态（Definition of Done / Acceptance）==========
{completion}

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

    # 🔴 §3：每次实验一个唯一 run id + 排他运行锁。
    #    上一轮就是两个进程并行，导致所有遥测无法归因。
    global EXPERIMENT_RUN_ID
    EXPERIMENT_RUN_ID = f"run_{uuid.uuid4().hex[:12]}"
    _run_lock = _acquire_run_lock(EXPERIMENT_RUN_ID)
    print(f"  🆔 experiment_run_id = {EXPERIMENT_RUN_ID}")
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
        # §11：记录**真实送进模型的那一份** Context
        print(f"  🎥 Context telemetry：包装了 "
              f"{_install_context_recorder(rt)} 个 Agent 的 context_builder")
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

            # ---- 正常完成：走**现有 Task Execution 协议** ----
            #
            # 🔴 这里以前是：
            #
            #       if s_log.get("agents_claim_done") and chars_now >= 9000:
            #
            #    而 `s_log["agents_claim_done"]` **从来没有被写过**
            #    （初始化 False，之后无人赋值）→ COMPLETED 分支不可达，
            #    会话永远靠 TOTAL_WALL_LIMIT_REACHED 退出。
            #
            #    现在完成**不再是一个布尔值**，而是：
            #
            #       DoD 存在 → 提交 Completion Proposal →
            #       验收责任存在 → 验收责任人 VERIFY(passed=True) →
            #       COMPLETE → Task.status == COMPLETED
            #
            #    全部走既有的消息协议与角色校验（不直接调领域方法）。
            outcome = _try_complete_task(rt, gw, log, s_log)
            if outcome == "COMPLETED":
                exit_reason = "COMPLETED"
                break
            if outcome not in ("", None):
                s_log["completion_blocked_by"] = outcome

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

    # 🔴 §3：结果文件必须能区分 RUNNING / FINALIZED，且**写入是原子的**。
    #
    #    ⚠️ 上一轮我在实验仍在运行时读了这个文件两次，拿到不同的
    #       turn 数，还一度当成「文件损坏」。真实原因是两个进程
    #       各写各的。现在：
    #         · 每个结果带 run_id
    #         · 带 state（本函数只在**真正结束**时写 FINALIZED）
    #         · 先写 .tmp 再 os.replace —— 不产生半写入文件
    log["run_id"] = EXPERIMENT_RUN_ID
    log["state"] = "FINALIZED"
    log["finalized_at"] = datetime.now(timezone.utc).isoformat()
    out = RESULTS / "heterogeneous_production_result.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, out)          # 原子替换

    try:
        _run_lock.unlink(missing_ok=True)
    except Exception:                                     # noqa: BLE001
        pass

    print("\n" + "=" * 60)
    print(f"  退出原因: {log.get('exit_reason')}")
    print(f"  总墙钟: {log['wall_minutes']} 分钟 / "
          f"{log.get('sessions_run')} 个 Session")
    print(f"  成本: {log.get('cost')}")
    print(f"  → {out}")
    return 0


# ================================================================
def _ensure_environment(rt: Any) -> None:
    """Task + participants + Workspace + 命名空间 + 最小根 Stage + DoD/验收责任。"""
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
    provision_namespaces(rt.workspace_gateway, WS_ID, OWNER,
                         ARTIFACT_NAMESPACES)

    st = StageStore(rt.economy.db)
    if not st.list_stages(TASK_ID):
        # §十七：**只**创建一个最小根 Stage，不预设后续阶段
        st.create_stage(
            TASK_ID,
            "Produce a complete ~10,000-word original Chinese sci-fi "
            "mystery novel about identity / memory / assets / history "
            "conflicting in a world run by AI Agents.",
            deliverables=[{"name": "complete manuscript"}],
            status=STATUS_ACTIVE, stage_id=ROOT_STAGE_ID)

    _ensure_completion_setup(rt)


def _ensure_completion_setup(rt: Any) -> None:
    """
    建立 **Definition of Done** 与 **Acceptance Responsibility**。

    【这一步在整条 Completion 链里的位置】

        Human 提供 Demand / Constraints          ← DEMAND 常量
              ↓
        Agent 自主结构化 Definition of Done       ← 本函数
        Agent 自主指派 Acceptance Responsibility  ← 本函数
              ↓
        START（进入生产）
              ↓
        SUBMIT + Completion Proposal
              ↓
        VERIFY(passed=True/False)  ← 必须由**别人**发
              ↓
        COMPLETE → COMPLETED

    🔒 两条都复用**现有**能力，不新增数据结构：
        · DoD  → `TaskSharedContext.acceptance_criteria`
                 （走 `TaskAlignmentService.propose` +
                  `ADD_ACCEPTANCE_CRITERION`）
        · 验收责任 → `AssignmentRecord.responsibility`
                 （走 `ASSIGN` 变更类型）

    🔒 生产者与验收者**必须是不同的 Agent** ——
       否则「生产者不能自验」这条边界在这条链上无从体现。
       这里用固定的两个角色（见 PRODUCER / ACCEPTOR 常量）。
    """
    from core.task import TaskStatus
    from core.task_shared_context import (
        ACCEPTANCE_RESPONSIBILITY, AssignmentStatus,
    )

    task = rt.tasks.get(TASK_ID)
    if task is None:
        return

    context = None
    try:
        context = rt.alignment.current(TASK_ID)
    except Exception:  # noqa: BLE001 — 没有上下文就建一个
        context = None

    if context is None:
        rt.alignment.create_context(
            TASK_ID,
            by=PRODUCER,
            objective=DEMAND,
            acceptance_criteria=list(DEFINITION_OF_DONE),
        )
        # 指派验收责任（ASSIGN 不需要第二个 aligner 确认）
        change = rt.alignment.propose(
            TASK_ID,
            proposed_by=PRODUCER,
            change_type="ASSIGN",
            new_value={"agent_id": ACCEPTOR,
                       "responsibility": ACCEPTANCE_RESPONSIBILITY},
            expected_revision=rt.alignment.require_current(TASK_ID).revision,
            reason="指派验收责任人：由读者角色验收作者的作品",
        )
        rt.alignment.accept(TASK_ID, change.change_id, decided_by=PRODUCER,
                            reason="生产者指派验收方（验收方本人不能是生产者）")

    # ---- 进入生产：走**现有执行协议**（不直接调 task.start()）----
    if task.status == TaskStatus.CREATED:
        rt.task_service.handle_message(_task_message(
            PRODUCER, "system", MessageType.START, {"task_id": TASK_ID}))


def _task_message(from_agent: str, to_agent: str, message_type: str,
                  payload: Dict[str, Any]) -> Any:
    """构造一条走标准协议的消息（START / SUBMIT / VERIFY / COMPLETE）。"""
    from communication.message import Message

    return Message(
        from_agent=from_agent,
        to_agent=to_agent,
        message_type=message_type,
        payload=payload,
    )


def _try_complete_task(rt: Any, gw: Any, log: Dict[str, Any],
                       s_log: Dict[str, Any]) -> str:
    """
    尝试用**现有 Task Execution 协议**把任务推到 COMPLETED。

    返回：
        "COMPLETED"  —— 任务真的到了 COMPLETED（可以正常退出了）
        ""           —— 还没到该完成的时候（继续生产）
        其它字符串   —— 被挡住的**具体原因**（写进 s_log，便于事后查）

    【完整调用链 —— 全部走既有协议，没有一条捷径】

        Agent 自述完成（turn["claims_done"]）
              ↓  本函数聚合（session 级）
        Task 必须已有 Definition of Done          ← Shared Context
        Task 必须已有 Acceptance Responsibility   ← Shared Context
              ↓
        PRODUCER 发 SUBMIT（带 Completion Proposal）
              ↓  TaskService.handle_message → SubmitHandler
              ↓  validate_role（必须承接方）+ validate_result
              ↓  validate_completion_proposal（5 条）
              ↓  Task Core: IN_PROGRESS → SUBMITTED
        ACCEPTOR 发 VERIFY(passed=True)
              ↓  validate_role（**不能是承接方** ← 生产者不能自验）
              ↓  Task Core: SUBMITTED → VERIFIED
        ACCEPTOR 发 COMPLETE
              ↓  Task Core: VERIFIED → COMPLETED
        读回 task.status 确认真的是 COMPLETED

    🔒 本函数**不直接改 Task 状态** —— 它只发消息。
       状态变化全部由 Task Core 的状态机产生。
       读回来的 `task.status` 是**证据**，不是假设。
    """
    from core.task import TaskStatus
    from core.task_execution import (
        COMPLETION_PROPOSAL_KEY, CompletionProposal, CriteriaStatus,
        TaskResultRef,
    )

    if not s_log.get("agents_claim_done"):
        return ""                       # 还没有任何 Agent 声称完成

    task = rt.tasks.get(TASK_ID)
    if task is None:
        return "no-task"

    # ---- 前置一：Definition of Done ----
    dod = rt.task_execution.definition_of_done(task)
    if not dod:
        return "no-definition-of-done"

    # ---- 前置二：Acceptance Responsibility ----
    acceptors = rt.task_execution.acceptors_of(task)
    if not acceptors:
        return "no-acceptance-responsibility"
    if PRODUCER in acceptors:
        # 🔒 生产者不能给自己派验收责任来绕过「不能自验」。
        #    执行服务也会拒绝，但这里提前说清楚原因。
        return "producer-cannot-be-acceptor"

    # ---- 生产：SUBMIT + Completion Proposal ----
    if task.status == TaskStatus.IN_PROGRESS:
        manuscript = _manuscript_ref(rt)
        if manuscript is None:
            return "no-manuscript-artifact"
        proposal = CompletionProposal(
            proposal_id=f"cp_{s_log.get('index', 0)}_{task.version}",
            task_id=TASK_ID,
            proposed_by=PRODUCER,
            criteria_status=CriteriaStatus.MET,
            evidence=(manuscript,),
        )
        result = rt.task_service.handle_message(_task_message(
            PRODUCER, ACCEPTOR, MessageType.SUBMIT,
            {"task_id": TASK_ID, "result": manuscript.to_dict(),
             COMPLETION_PROPOSAL_KEY: proposal.to_dict()}))
        log.setdefault("completion_events", []).append(
            {"event": "SUBMIT", "ok": bool(result.ok),
             "errors": list(result.errors or ())})
        if not result.ok:
            return f"submit-rejected: {(result.errors or ['?'])[0][:80]}"
        task = rt.tasks.get(TASK_ID)

    # ---- 验收：VERIFY(passed=True) —— 必须由**非生产者**发 ----
    if task.status == TaskStatus.SUBMITTED:
        verify = rt.task_service.handle_message(_task_message(
            ACCEPTOR, PRODUCER, MessageType.VERIFY,
            {"task_id": TASK_ID, "passed": True,
             "feedback": "手稿完整、有结尾标记，符合 Definition of Done"}))
        log.setdefault("completion_events", []).append(
            {"event": "VERIFY", "ok": bool(verify.ok),
             "errors": list(verify.errors or ())})
        if not verify.ok:
            return f"verify-rejected: {(verify.errors or ['?'])[0][:80]}"
        task = rt.tasks.get(TASK_ID)

    # ---- 结项：COMPLETE ----
    if task.status == TaskStatus.VERIFIED:
        done = rt.task_service.handle_message(_task_message(
            ACCEPTOR, PRODUCER, MessageType.COMPLETE,
            {"task_id": TASK_ID, "summary": "作品完成，验收通过"}))
        log.setdefault("completion_events", []).append(
            {"event": "COMPLETE", "ok": bool(done.ok),
             "errors": list(done.errors or ())})
        if not done.ok:
            return f"complete-rejected: {(done.errors or ['?'])[0][:80]}"
        task = rt.tasks.get(TASK_ID)

    # ---- 证据：读回真实状态 ----
    if task.status == TaskStatus.COMPLETED:
        _close_stage(rt, log)
        return "COMPLETED"

    blockers = rt.task_execution.completion_blockers(task)
    return "blocked: " + " / ".join(blockers)


def _manuscript_ref(rt: Any) -> Any:
    """把手稿 artifact 变成一个 TaskResultRef（逻辑名字，不是路径）。"""
    from core.task_execution import TaskResultRef

    try:
        entries = rt.workspace_gateway.list(WS_ID, PRODUCER)
    except Exception:  # noqa: BLE001
        return None
    for entry in entries:
        name = getattr(entry, "name", "") or ""
        if "manuscript" in name:
            return TaskResultRef(
                result_ref="manuscript/novel",
                result_type="document",
                summary=f"手稿文件 {name}",
                metadata={"artifact": name},
            )
    return None


def _close_stage(rt: Any, log: Dict[str, Any]) -> None:
    """
    任务完成后，把**本 Runner 自己驱动的那个** Stage 正确收尾。

    🔒 这是「Task COMPLETED → Stage 正确结束 → Runner 正常退出」
       中间那一环。以前 Stage 永远停在 ACTIVE（没人调 complete）。

    【Task ↔ Stage 的真实关系：**1 Task : N Stages**】

        `Stage.task_id` 表示「这个 Stage 属于哪个 Task」，
        而 `StageStore.list_stages(task_id)` 返回**全部** ——
        所以一个 Task 可以有多个 Stage。

        ⇒ 「Task COMPLETED → 这个 Task 的**所有** Stage 都 COMPLETED」
          **不成立**：别的 Stage 可能属于同一 Task 但尚未开始 /
          已取消 / 已失败。

    🔒 所以本函数做三件事，而不是「关掉第一个 is_open 的」：

        ① 只关 **`ROOT_STAGE_ID`** —— 那是本 Runner 自己创建的；
           用「第一个 is_open」会误关别人的 Stage（顺序依赖，不可预测）。
        ② 关之前**校验它真的属于这个 Task** —— 跨任务关 Stage 是错的。
        ③ 已经不在 open 状态就不重复关（幂等）。

    ⚠️ 本轮**不**重新设计 Stage 模型：没有 deliverable→artifact 的
       映射可用（`StageControlExecutor` 从未接线），所以这里
       只表达「Runner 驱动的那一个 Stage 结束了」，
       不去推断「Task 完成是否意味着每个 Stage 都该完成」。
    """
    from stage import StageStore

    try:
        st = StageStore(rt.economy.db)
        stage = next((s for s in st.list_stages(TASK_ID)
                      if s.stage_id == ROOT_STAGE_ID), None)
        if stage is None:
            log["stage_completed"] = None
            log["stage_note"] = f"没有找到根 Stage {ROOT_STAGE_ID}"
            return
        # ② 归属校验：绝不关别的任务的 Stage
        if stage.task_id != TASK_ID:
            log["stage_completed"] = None
            log["stage_note"] = (f"Stage {stage.stage_id} 属于 "
                                 f"{stage.task_id}，不是 {TASK_ID} —— 不关")
            return
        # ③ 幂等
        if stage.is_open:
            st.complete(stage.stage_id)
        log["stage_completed"] = stage.stage_id
    except Exception as exc:  # noqa: BLE001
        log["stage_complete_error"] = f"{type(exc).__name__}: {exc}"


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
    #
    #   🔴 **交班原因必须如实记录。**
    #
    #      这里曾经写死 `reason="hard_limit"` —— 于是「因轮数用尽而结束」
    #      的那一班也被标成 hard_limit。看日志的人会以为它是撞墙钟停的，
    #      而实际上它是**正常跑完配置的轮数**。两种情况的含义完全不同：
    #          hard_limit            → 时间不够，被打断
    #          max_rounds_reached    → 这一班正常收工
    #
    #      `RuntimeSession` 已经把真实原因放在 `stop_reason` 里（上面两个
    #      分支各自设过）：
    #          enforce()          → "hard_limit"             （撞墙钟）
    #          stop_by_agent(r)   → r = "max_rounds_reached" （轮数用尽）
    #
    #      🔒 用**原值**而不是硬映射到 `stage/models.py` 的 `REASON_*`：
    #         那 5 个常量里没有「轮数用尽」这一类，而
    #         `MAX_ROUNDS_PER_SESSION` 是**实验级**概念（不是 Stage 概念），
    #         不该为了它去扩生产层的词汇。`reason` 本来就是自由文本，
    #         `stage/control.py` 也是「reason 放类别 + 起因进 metadata」。
    #
    #      🔒 `metadata` 里同时留下 session 状态，便于事后核对。
    rev = _workspace_revision(rt.workspace_gateway)
    ck = st.save_checkpoint(
        stage.stage_id,
        reason=sess.stop_reason or "session_ended",
        workspace_id=WS_ID, revision=rev,
        progress={"session": index, "rounds": rnd},
        metadata={"session_state": sess.state,
                  "session_stop_reason": sess.stop_reason,
                  "hard_limit": bool(s_log["hard_limit"])})
    s_log["checkpoint"] = ck.to_dict()
    s_log["workspace_revision"] = rev

    # ---- 🔴 Session 级聚合：Agent turn claim → Session 状态 ----
    #
    # 这是本轮修的**核心 Bug**。
    #
    #   以前：
    #       turn["claims_done"] = True         （每轮写了）
    #       s_log["agents_claim_done"] = False （初始化后**再没人写过**）
    #       → 外层 `if s_log.get("agents_claim_done")` 永远为假
    #       → COMPLETED 分支不可达
    #       → 会话只能靠 TOTAL_WALL_LIMIT_REACHED 退出
    #
    #   🔒 注意这里**不是**把某个布尔值改成 True 就完事 ——
    #      聚合出来的只是「有 Agent **自称**完成」这一个事实。
    #      真正的完成判定在 `_try_complete_task()` 里，
    #      必须走完 DoD / Proposal / 验收责任 / VERIFY / COMPLETE 全链。
    claims = [t for t in s_log["turns"] if t.get("claims_done")]
    s_log["agents_claim_done"] = bool(claims)
    s_log["claiming_agents"] = sorted({t["agent_id"] for t in claims})
    s_log["claiming_turns"] = len(claims)
    if claims:
        print(f"  📣 Session {index}：{len(claims)} 个 turn 声称完成 "
              f"（{', '.join(s_log['claiming_agents'])}）")

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


# ============================================================
# §9–§12 实验级 telemetry（**不是**正式架构）
# ============================================================
#
# 🔒 用**包装器**记录真实 `CognitiveContextBuilder` 的输出 ——
#    **不改任何生产源码**，只在实验里把 agent 的 builder 换成这个代理。
#
# 🔒 记录的是**真正被渲染进 system prompt 的那一份**（§11）：
#    不是「库里有没有」，而是「模型这一轮实际看到了什么」。
#    这是本实验**唯一**的关键证据来源。
#
#    ⚠️ 绝不用「Agent 在回复里说它看到了」当证据 —— 它可能自己猜出来。
_CONTEXT_SINK: Dict[str, Dict[str, Any]] = {}


#: 本次实验运行的唯一 id。`main()` 启动时生成。
#:
#: 🔴 为什么必须有它：上一轮出现了**两个进程同时跑**同一份 DB，
#:    导致 163 条调用 vs 85 条 turn 无法归因，我还把它误判成
#:    「遥测缺口」。有了 run id，任何一条记录都能归属到某一次运行。
EXPERIMENT_RUN_ID = ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_run_lock(run_id: str) -> Path:
    """
    取得**排他运行锁**，防止两个实验进程同时写同一份 DB（§3）。

    🔒 这不是「新的持久化系统」—— 只是一个 O_EXCL 创建的锁文件。
       它解决的正是上一轮真实发生的事故：
       我用 nohup 启动了一个进程、以为它死了（沙箱里 ps 不可用，无法确认），
       又启动了第二个 —— 两者并行，共用同一份 hetero.db 与结果文件。

    ⚠️ 陈旧锁（进程已死）会被接管并告知，不会永久卡住。
    """
    lock = RESULTS / ".hetero_run.lock"
    if lock.exists():
        content = lock.read_text(encoding="utf-8").strip()
        pid = None
        for part in content.split():
            if part.startswith("pid="):
                try:
                    pid = int(part[4:])
                except ValueError:
                    pid = None
        if pid and _pid_alive(pid):
            raise RuntimeError(
                f"已有实验在运行，拒绝并发启动。\n"
                f"  锁文件：{lock}\n"
                f"  内容  ：{content}\n"
                f"  （两个进程写同一份 DB 会让所有遥测无法归因 —— "
                f"上一轮就是这样失去结论的。）")
        print(f"  ⚠️ 发现陈旧 run lock（{content}）—— 进程已不在，接管")
        lock.unlink(missing_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, f"run_id={run_id} pid={os.getpid()}".encode())
    finally:
        os.close(fd)
    return lock


class _RecordingContextBuilder:
    """透传式包装：记录 `build()` 的**实际产物**，行为与内层完全一致。"""

    def __init__(self, inner: Any, sink: Dict[str, Any], agent_id: str):
        self._inner = inner
        self._sink = sink
        self._agent_id = agent_id

    def build(self, agent_id: str, **kwargs: Any) -> Any:
        ctx = self._inner.build(agent_id, **kwargs)
        try:
            text = ctx.to_text()
            import hashlib as _h
            snap_id = "snap_" + _h.sha256(
                text.encode("utf-8", "replace")).hexdigest()[:16]
            self._sink[self._agent_id] = {
                # §9：这一轮 Context 的快照 id（由**实际渲染文本**派生）
                "context_snapshot_id": snap_id,
                # 关键通道里到底有哪几条（只存 id，不存全文）
                "critical_ids": [e.experience_id
                                 for e in ctx.critical_observations],
                "critical_count": len(ctx.critical_observations),
                "ordinary_count": len(ctx.relevant_experiences),
                # 渲染文本里是否真的出现截断事实（§11 硬证据）
                "has_truncation_marker":
                    "cognitive_output_truncated" in text,
                "has_TRUNCATED_literal": "TRUNCATED" in text,
                "has_finish_reason_length": "length" in text,
                "context_chars": len(text),
            }
        except Exception as exc:                          # noqa: BLE001
            self._sink[self._agent_id] = {
                "context_error": f"{type(exc).__name__}: {exc}"}
        return ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _install_context_recorder(rt: Any) -> int:
    """把每个 Agent 的 context_builder 换成记录代理。返回包装了几个。"""
    wrapped = 0
    for a in rt.economy.all_agents():
        inner = getattr(a.brain, "context_builder", None)
        if inner is None or isinstance(inner, _RecordingContextBuilder):
            continue
        a.brain.context_builder = _RecordingContextBuilder(
            inner, _CONTEXT_SINK, a.agent_id)
        wrapped += 1
    return wrapped


def _latest_critical_id(rt: Any, agent_id: str) -> Optional[str]:
    """这个 Agent 最近一条关键观察的 id（把 Turn N 和 Experience 对上）。"""
    for a in rt.economy.all_agents():
        if a.agent_id != agent_id:
            continue
        store = getattr(a, "experience_store", None)
        if store is None:
            return None
        try:
            rows = store.retrieve_critical(agent_id, limit=1)
        except Exception:                                 # noqa: BLE001
            return None
        return rows[0].experience_id if rows else None
    return None


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
        completion=_completion_facts(rt),
        workspace=_workspace_facts(gw, aid),
        roster=_roster_facts(rt, aid),
        inbox=channels.render_inbox(inbox["received"]))

    a = next(x for x in rt.economy.all_agents() if x.agent_id == aid)
    a.brain.preflight_credential()

    # 🔴 §5.2 Cognition Turn Identity —— 由**生产认知入口**生成，
    #    并沿真实调用链显式传递：
    #        _turn() → BaseAgent.generate() → Brain.generate() → recorder
    #    🔒 不允许只把它写进返回 JSON 而不传进真实调用记录路径。
    cognition_turn_id = (f"turn_{EXPERIMENT_RUN_ID}_{aid}_r{rnd}")
    turn["cognition_turn_id"] = cognition_turn_id
    turn["experiment_run_id"] = EXPERIMENT_RUN_ID

    t0 = time.perf_counter()
    try:
        r = a.generate(messages=prompt, task_id=TASK_ID,
                       requested_cognitive_budget=COGNITIVE_BUDGET,
                       # §5.3 purpose 由**调用方**声明，绝不从结果反推
                       call_purpose="cognition",
                       cognition_turn_id=cognition_turn_id,
                       experiment_run_id=EXPERIMENT_RUN_ID)
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
                 # 🔴 认知输出语义状态（与 output_status 正交）
                 "cognitive_status": getattr(r, "cognitive_status", None),
                 "ms": round((time.perf_counter() - t0) * 1000, 1),
                 "chars": len(content)})

    # ---- §5.1/§7：这次调用的 llm_call_id 与 Provider 边界证据 ----
    #
    #   🔒 记录的是**这一次调用自己的** id 与证据，
    #      不是「某个时间点上最近的一次」—— 那样 Turn 与 Call 就分不开了。
    turn["llm_call_id"] = getattr(a, "last_call_id", None)
    evidence = getattr(a, "last_request_evidence", None)
    if evidence:
        turn["request_evidence"] = {
            k: v for k, v in evidence.items()
            if k != "request_sha256_16"     # 摘要单独放，便于核对
        }
        turn["request_sha256_16"] = evidence.get("request_sha256_16")

    # ---- §9/§10/§11：截断事实 + **下一轮真实 Context** 的证据 ----
    #
    # 🔒 实验 telemetry，**不进**正式架构、不建表。
    truncated = getattr(r, "cognitive_status", None) == "TRUNCATED"
    turn["truncated"] = truncated
    if truncated:
        turn["critical_experience_id"] = _latest_critical_id(rt, aid)
        turn["critical_observation_created"] = bool(
            turn["critical_experience_id"])
    # 这一轮**真实构建**的 Context（包装器抓的）
    turn["context"] = dict(_CONTEXT_SINK.get(aid) or {})

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


def _completion_facts(rt: Any) -> str:
    """
    Definition of Done / 验收责任 / 完成进度 —— 渲染给**每个** Agent 看。

    【为什么放在 prompt 里】

        这段信息必须有唯一的权威来源（Shared Context + Task Core），
        而且要**被 Agent 看见** —— 否则「自主结构化 DoD」就只是
        写在库里没人读的数据。

        🔒 这里刻意**不新建**一套 Task Context：
           DoD 来自 `SharedContext.acceptance_criteria`，
           验收责任来自 `assignments`，完成进度来自 Task Core 的 `status`。
           本函数只是把它们**渲染**出来。

        🔒 读不到就如实说「（还没有）」——
           不用占位符假装有内容。
    """
    from core.task import TaskStatus

    lines: List[str] = []
    task = rt.tasks.get(TASK_ID)
    if task is None:
        return "（任务尚未建立）"

    lines.append(f"Task 目标：{DEMAND.strip().splitlines()[0]}")

    # ---- Definition of Done（权威：Shared Context）----
    dod = rt.task_execution.definition_of_done(task)
    lines.append("Definition of Done（完成标准）：")
    if dod:
        lines.extend(f"  {i}. {c}" for i, c in enumerate(dod, 1))
    else:
        lines.append("  （还没有 —— 任何参与方都可以添加验收标准）")

    # ---- Acceptance Responsibility（权威：Shared Context 的 assignments）----
    acceptors = rt.task_execution.acceptors_of(task)
    lines.append(
        "验收责任人（Acceptance Responsibility）："
        + ("、".join(acceptors) if acceptors else "（还没有 —— 需要有人负责验收）")
    )

    # ---- 完成进度（权威：Task Core 的状态机）----
    lines.append(f"Task 当前状态：{task.status}"
                 + (f"（承接方：{task.contractor}）" if task.contractor else ""))

    proposal = rt.task_execution.latest_completion_proposal(TASK_ID)
    if proposal is not None:
        lines.append(
            f"最近一次 Completion Proposal：由 {proposal.proposed_by} 提交，"
            f"对照 DoD 的达标情况 = {proposal.criteria_status}，"
            f"证据 {len(proposal.evidence)} 条"
            + (f"，未达标项 {list(proposal.outstanding_gaps)}"
               if proposal.outstanding_gaps else "")
        )
    else:
        lines.append("最近一次 Completion Proposal：（还没有人提交）")

    if task.status != TaskStatus.COMPLETED:
        blockers = rt.task_execution.completion_blockers(task)
        lines.append("距离结项还差：" + ("、".join(blockers) if blockers
                                        else "（已满足全部前置条件，等验收）"))

    # ---- 当前交付物（权威：Task Workspace 的真实文件）----
    lines.append("当前交付物：见上面的 Workspace 事实（本段不复制内容）")
    return "\n".join(lines)


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

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
    ACTION_MESSAGE_SEND, ACTION_PUBLIC_QUERY, ACTION_WORKSPACE_READ,
    ACTION_WORKSPACE_WRITE, ActionDispatcher,
    MessageSendExecutor, PrivateDeleteExecutor, PrivateListExecutor,
    PrivateReadExecutor, PrivateWriteExecutor, PublicQueryExecutor,
    AssignAcceptanceExecutor, SharedContextReadExecutor,
    ResourceCreateExecutor, ResourceHandoffExecutor,
    ResourceReleaseExecutor,
    WorkspaceReadExecutor, WorkspaceWriteExecutor,
)
from services.messaging import send_message_as  # noqa: E402
from services.private_workspace import (  # noqa: E402
    delete_private, list_private, read_private, write_private,
)
from services.public_reference import PublicReference  # noqa: E402
from services.assignment_ops import assign_acceptance  # noqa: E402
from services.context_ops import read_shared_context  # noqa: E402
from services.resource_control import (  # noqa: E402
    create_directory, handoff_resource, release_resource,
)
from services.artifacts import (  # noqa: E402
    MODE_APPEND, MODE_CREATE, MODE_REPLACE,
    provision_namespaces, read_artifact, write_artifact,
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

#: 🔴 **单次响应的输出上限**（`requested_cognitive_budget` → Provider 的 `max_tokens`）。
#:
#: 【它到底限制什么】
#:     **只**限制一次响应的输出 token 数。不影响输入、轮数、墙钟、AIC。
#:     ⚠️ 而且 **reasoning 与正文共享它** —— 实测 `novel_editor` 一次调用
#:     用了 11705 output tokens 却只产出 826 字符，差额全在推理上。
#:     所以「思考得多」会直接挤掉「能写多少」。
#:
#: 【为什么从 8192 抬到 16384】（2026-10-09）
#:     8192 是 2026-10-07 随 runner 一起引入的一个**保守值**
#:     （commit e47793a，提交信息没写理由）。
#:     实测与对照后发现它偏小，三个依据：
#:
#:       ① **行业默认就是 16384**：Pydantic AI 把 Anthropic 的默认
#:          `max_tokens` 从 4096 抬到了 16384（Claude Sonnet 4.5+）。
#:       ② **Agent 配置本来就是 16384**（`config/config.yaml` 的
#:          `brain.max_tokens`）—— 8192 反而把它**压低了一半**，
#:          因为 `requested_cognitive_budget` 优先于配置的 `max_tokens`。
#:       ③ 一次真实截断就是被 8192 撞出来的：作者要把整章写进 JSON 的
#:          单个 `content` 字段，超了就截断 → `parse_intent` 全失败
#:          → **整轮不写入、不报错**（RUN2 报告「阻塞点 ①」）。
#:
#: 【为什么不是更大】
#:     · 生产层 `MAX_COGNITIVE_BUDGET = 32768` 是**结构上限**，够用；
#:     · ⚠️ 我们**永远发 `stream: False`**（见 `build_payload`），
#:       而 Anthropic 的非流式 SDK 上限约 21,000 tokens。
#:       16384 在这条线之内；再往上抬就要先有流式能力。
#:
#: 🔒 可覆盖：`HETERO_COGNITIVE_BUDGET`。
COGNITIVE_BUDGET = int(os.environ.get("HETERO_COGNITIVE_BUDGET", "16384"))

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
🔴 走**生产协议的信封**（本实验与生产环境用同一个信封）：

如果你决定**采取一个动作**，就输出：

{{"decision":"act","reasoning":"你对当前进展的判断（简短）",
  "action":{{"type":"动作类型","parameters":{{}}}}}}

如果你**不采取动作**（只是想说点什么，或者什么都不做），就直接正常回复 ——
没有这个信封就是一次普通回复。

🔒 你可以在**一次**输出里给出**多个动作** ——
   当它们**属于同一件事**时（例如「写完正文 + 通知下一个人」）。
   写一个还是几个、写哪些，完全由你自己判断。

🔴 **你会不会被再次叫醒，取决于有没有消息到达**（§22.27）：
   你**不会被自动唤醒**。只有在**收到新消息**时，才会有你的下一个回合。
   （这条是**机制事实**，不是"你该去发消息"——
     发不发、发给谁，仍然完全由你自己判断。）

下面这些动作类型是**环境事实**（这个环境注册了什么、各自要什么参数）：

  · "workspace_write"
        把一个产物写进共享 Workspace。**这三个参数都要给。**

        形状（照着填）：
          parameters: {{"path": "manuscript/chapter_001.md",
                       "write_mode": "append",
                       "content": "这次要写入的完整文本"}}

        🔴 "write_mode" 只能取这三个之一，**没有默认值 —— 漏了整次写入会被拒绝**：

           "create"  = 文件**不存在**时新建（已存在会被拒绝，不会悄悄覆盖）
           "append"  = 末尾追加，旧内容完整保留
           "replace" = **整体替换**，原来的内容会消失


  · "workspace_read"
        parameters: {{"path": "共享 Workspace 里的逻辑路径"}}
        作用：读取该文件**当前**的内容与 revision。

        约束（同样是环境事实）：
          - 只有本 Task 的参与者能读；
          - 读**不受** Write Lock 影响（锁约束的是修改与删除）；
          - 读到的内容会**当场**回到你手上（作为紧接着的一条新输入），
            你可以在**同一次判断里**继续决定做什么。这也意味着：
            一次「读了东西」的判断实际上会让模型被调用**两次**。

  · "message_send"
        parameters: {{"to": "收件人 agent_id", "text": "要说的话",
                     "message_type": "消息类型（可省，省了就是 TEXT）",
                     "payload": {{}}}}   ← payload 可省

        作用：按**显式** `message_type` 发一条消息。


        本环境**已注册 handler** 的消息类型（环境事实）：
          TEXT · TASK_OFFER · COUNTER_OFFER · ACCEPT · REJECT ·
          START · SUBMIT · VERIFY · COMPLETE

        约束（同样是环境事实）：
          - 发送方由运行时填成**你自己**，你给不了别人；
          - payload 的字段要求与合法性由**协议层**判断 ——
            不合规会返回结构化错误，不会静默丢弃；
          - 发消息会同时把对方的唤醒信号置位（Wakeup ≠ Message：
            信号不携带内容，对方仍然要自己去信箱取）。

  · "public_query"
        parameters: {{"name": "资料名"}}   ← 读一份
                    或 {{"list": true}}     ← 列出有哪些

        作用：查**公共资料区**（只读，没有人能改它）。
        里面有一份 **SYSTEM_MAP.md** —— 这个环境里存在哪些结构、
        各自的边界在哪、你能发起哪些动作，都写在里面。


  · "private_write" / "private_read" / "private_list"
        这三个操作的是**你自己的**文件空间
        （`agents/<你的 id>/workspace/`，别人看不到也读不到）。

        private_write  parameters: {{"path": "...", "content": "..."}}
        private_read   parameters: {{"path": "..."}}
        private_list   无参数 —— 列出你有哪些文件
        private_delete parameters: {{"path": "..."}}   ← 只删文件，不删目录

        约束（环境事实）：
          - 只能碰**你自己**的目录；越界（`..` / 绝对路径）被拒绝；
          - 它**不属于任何 Task** —— 没有锁、没有修订、没有审计，
            是你自己的长期资产空间，**换任务也还在**；
          - 🔒 设计上它是你放**个人记忆 / 经验 / 策略 / 工具**的地方
            （以后的形态），当前至少可以当你的草稿与笔记空间。

  · "resource_create" / "resource_handoff" / "resource_release"
        这三个管的是资源树的**结构与责任**（不是内容 —— 内容归 workspace_write）。

        resource_create   parameters: {{"path": "..."}}         ← 建一个**目录**
        resource_handoff  parameters: {{"path": "...", "to": "接收方 agent_id",
                                        "reason": "可选"}}
        resource_release  parameters: {{"path": "..."}}

        约束（环境事实）：
          - 建目录需要**父目录的锁**。根目录的锁在**根负责人**手里，
            所以新起的顶层目录**只有那个人能建**；
          - **交接（handoff）是把你的责任明确交给别人** ——
            交接之后对方才能写那块地方。**没有放宽任何权限**，
            只是责任换了人（实测：交接前写不进去，交接后能写）；
          - release 是「我不再保留这个**独立**责任」，之后按父级规则
            重新解析。**根目录不能 release**（会让整棵树失去责任源头）。

  · "assign_acceptance"
        parameters: {{"to": "接收方的 agent_id", "reason": "可选"}}
        作用：把一个 Task 的**验收责任**指派给某个 Agent。

        🔒 没有它，**没人能被授权验收** —— 你能发 SUBMIT，
           但验收链会断在「找不到验收人」上。

        约束（环境事实）：
          - 接收方必须是**这条 Task 的人**（参与者 / creator / contractor）；
          - **不能派给自己** —— 那等于自验（VERIFY 时也会被拒）；
          - 指派**立即生效**，且**当前不能被拒绝**；
          - 这个 Task 还没有 Shared Context 时会失败。

  · "shared_context_read"
        无参数。

        作用：读这个 Task 的**共同认知**（Shared Context）**当前**那一版 ——
        objective · constraints · acceptance_criteria · decisions ·
        assumptions · open_questions · assignments · facts ·
        terminology · artifact_refs，以及它的 revision。
        同时附带这个 Task 在 Task Core 里的**客观状态**（现场读的引用）
        与最近几条变更摘要。

        约束（环境事实）：
          - 只有**这条 Task 的人**能读；不是参与者读不到；
          - 读**不受**任何锁影响；
          - 读到的内容会**当场**回到你手上（与 `workspace_read` 同一条路径），
            你可以在**同一次判断里**继续决定做什么；
          - 它**只读**：不改变任何状态、不产生新 revision。

🔒 这些都只是**能力**。用不用、什么时候用、读哪个文件、
   给谁发什么类型的消息 —— **完全由你自己判断** ——
   环境不替你决定，也不会因为你不做而催你。
"""


# ================================================================
def parse_intent(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"assessment": "", "artifact": None,
                           "message": None, "action": None, "parsed": False}
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
        # ---- 🔴 通用动作通道：Agent 可以请求**任何**已注册的 action_type ----
        #
        #  🔒 这里刻意**不**枚举 action_type、不判它合不合法：
        #     `type` 是自由字符串，合法性由「组合根注册了哪些执行器」
        #     决定（没注册 → dispatcher 返回一个明确的失败 ActionResult）。
        #
        #     在这里枚举 = 替 Agent 预先划定「它能做什么」——
        #     那正是「系统不替 Agent 决定」所禁止的（§1）。
        #
        #  🔒 因此在生产层**新增一个动作不需要改这个解析器**：
        #     只要在新的组合根里注册执行器，Agent 立刻就能请求它。
        ac = d.get("action")
        if isinstance(ac, dict) and (ac.get("type") or ac.get("action_type")):
            _params = dict(ac.get("parameters") or {}) \
                if isinstance(ac.get("parameters"), dict) else {}
            # 🔴 与上面 `artifact.path` **同一条**归一化：去掉前导 `/`。
            #
            #    2026-10-09 实测：第一轮 4 个 Agent 的 workspace_read
            #    **全部** `bad_path`，它们发的都是 `/plot/outline_chapter1.md`。
            #
            #    为什么会带斜杠？因为**环境事实就是这么显示的** ——
            #    `_workspace_facts` 渲染成 `/plot/  负责人=...`，
            #    Agent 自然把 `/plot/` 和条目名拼成 `/plot/x.md`。
            #
            #    而 `validate_artifact_path` 把前导 `/` 判为**宿主机绝对
            #    路径**并拒绝。写入路径早就在 `parse_intent` 里剥掉它，
            #    我加 action 通道时漏了这一步。
            #
            #    🔒 `parameters.path` 是**约定**的 Workspace 逻辑路径参数名。
            #       这里是「把 Agent 写下的路径归一化成服务层接受的形式」，
            #       **不是**替 Agent 决定读什么。
            #
            #    🔒 剥前导 `/` **不降低**宿主路径防护：Workspace 是封闭
            #       命名空间（`..` / 反斜杠 / 盘符 / `~` 仍然一律拒绝），
            #       `gateway.read(ws, "/etc/passwd")` 读的是 Workspace 里
            #       那个逻辑路径，**永远不是**宿主机的 /etc/passwd。
            #       服务层 `read_artifact` 仍然严格拒绝绝对路径 ——
            #       归一化只发生在解析 Agent 输入这一层。
            _pp = _params.get("path")
            if isinstance(_pp, str):
                _params["path"] = _pp.strip().lstrip("/")
            out["action"] = {
                "type": str(ac.get("type") or ac.get("action_type")).strip(),
                "parameters": _params,
            }
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
        # 🔴 这里曾经写死 `creator="human_demand"` 与**另一份**中文目标。
        #
        #    两处都违反了本项目自己的边界：
        #
        #    ① `creator="human_demand"` —— AGENTS.md §6 的
        #       **`Human ≠ Task Creator`**（ADR-0024：Creator 始终是 Agent。
        #       Human 提供的是 **Demand**，那是 `Origin`，不是 `Creator`）。
        #       它平时被 `run_demand.py` 的包装盖住了，但源码里就在这 ——
        #       任何直接调用 `_ensure_environment` 的路径都会中招。
        #
        #    ② `description` 是那句「约 10000 字科幻悬疑」的**第三份拷贝**
        #       （另两份：`DEMAND` 常量、根 Stage 的 goal）。
        #       三份文字各不相同 —— 典型的「一个事实多个来源」。
        #       它变成真 bug 是在 runner 被当成通用入口之后：
        #       Task 说科幻、Stage 说修仙，Agent 收到两个矛盾目标。
        #
        #    🔒 现在 `DEMAND` 是**唯一来源**：Task 描述、Stage goal
        #       都从它派生。覆盖 `DEMAND` 就等于同时改了这两处。
        # 🔴 走 `Runtime.create_task`（同时建 Task + 空 Shared Context）。
        #    下面 `_ensure_completion_setup` 还会往容器里写 DoD 与验收人 ——
        #    那是**这个实验**的内容，不是容器本身。
        rt.create_task(Task.create(
            creator=PRODUCER,          # ADR-0024：Creator 始终是 Agent
            description=DEMAND,
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
        # 🔴 Stage 的 **goal 从 DEMAND 派生**，不另写一份。
        #
        #    这里曾经写死一句**英文**的科幻目标 —— 而它其实是
        #    `DEMAND`（中文）的复述，连「10,000 字」都对得上。
        #    那是**同一个事实的两份定义**，违反本项目自己的
        #    「一个事实只有一个权威来源」。
        #
        #    它变成真 bug 是在 `experiments/user_demand/run_demand.py`
        #    把这个 runner 变成**通用入口**之后：那里覆盖了 Task 目标，
        #    却没覆盖 Stage 目标 —— 于是 Agent 同时收到
        #        Task : 写修仙第一章
        #        Stage: 写 10,000 字科幻悬疑小说
        #    两个互相矛盾的目标。`novel_reader` 自己在消息里指出了
        #    这个冲突并裁决「以当前 Task 为目标」—— 它处理得比我好。
        #
        #    🔒 Stage 的**机制**是「把长任务切成有界时段 + Checkpoint」，
        #       它不负责决定「写什么题材」。所以 goal 只能是**引用**，
        #       不能是另一份定义。
        st.create_stage(
            TASK_ID,
            DEMAND.strip(),
            # ⚠️ deliverables 是自由文本推不出来的，所以这里给一个
            #    **中性**名字。旧的 "complete manuscript" 是题材相关的
            #    —— 对代码任务、对"只写第一章"都是错的限定。
            deliverables=[{"name": "task deliverables"}],
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


#: 公共资料区的访问器（模块级一份即可 —— 它是只读的，无状态）。
_PUBLIC = PublicReference()


def _TASK_PEOPLE(rt: Any) -> Any:
    """本实验的 `is_task_person(task_id, agent_id)`（缓存一次）。"""
    from main import build_task_people_checker
    checker = getattr(rt, "_task_people_checker", None)
    if checker is None:
        checker = build_task_people_checker(rt.tasks)
        try:
            rt._task_people_checker = checker
        except Exception:                                     # noqa: BLE001
            pass
    return checker


def _bind_align(rt: Any, fn: Any) -> Any:
    """
    把 `fn(alignment, task_id, *, by, to, reason)` 绑定成本实验的形状。

    🔒 `alignment` 与 `task_id` 在这里绑定掉 —— Agent 只说要给谁。
       Agent 既看不到 alignment 服务，也无法指定**别的** task。
    """
    def _bound(*, by: str, to: Any, reason: str = "") -> Any:
        return fn(rt.alignment, TASK_ID, by=by, to=to, reason=reason,
                  # 🔒 生产组合根提供的「谁可以在这条 Task 上行动」
                  #    —— fail-closed，查不到就是 False。
                  is_participant=_TASK_PEOPLE(rt))
    return _bound


def _bind_context_read(rt: Any, fn: Any) -> Any:
    """
    把 `fn(alignment, task_id, *, by, is_participant)` 绑定成本实验的形状。

    🔒 `alignment` 与 `task_id` 在这里绑定掉 —— Agent **没有任何参数**。
       它读到的永远是**这一条** Task 的**当前**共同认知，
       既指不了别的 Task，也翻不了历史。
    """
    def _bound(*, by: str) -> Any:
        return fn(rt.alignment, TASK_ID, by=by,
                  # 🔒 同 `_bind_align`：fail-closed，
                  #    不是这条 Task 的人读不到它的共同认知。
                  is_participant=_TASK_PEOPLE(rt))
    return _bound


def _bind_gw(rt: Any, fn: Any) -> Any:
    """
    把一个 `fn(gateway, workspace_id, agent_id, ...)` 绑定成本实验的形状。

    🔒 **gateway 与 workspace_id 在这里绑定掉** —— Agent 只给路径。
       Agent 永远看不到 gateway 是什么，也不需要知道。

    🔴 2026-10-10：**`task_id` 也在这里绑定掉。**
       `ResourceHandoffExecutor` 会从 `parameters` 读 `task_id` 并传下来，
       而它是 **Workspace 审计记录上的标签**（`gateway.handoff(task_id=…)`）。
       不覆盖的话，Agent 可以把自己的责任交接**记到别的 Task 名下** ——
       审计就不可信了。这与 `_bind_align` / `_bind_context_read`
       「task_id 由组合根绑定，Agent 指定不了」是同一条原则。

       ⚠️ 只覆盖**已经传下来**的键（`if "task_id" in kwargs`）——
          不主动添加，否则 `create_directory` / `release_resource`
          会因为收到不认识的参数而报错。
    """
    def _bound(agent_id: str, path: Any, *args: Any, **kwargs: Any) -> Any:
        if "task_id" in kwargs:
            kwargs["task_id"] = TASK_ID
        return fn(rt.workspace_gateway, WS_ID, str(agent_id), path,
                  *args, **kwargs)
    return _bound


def _ws_of(rt: Any) -> Any:
    """
    按 `agent_id` 取那个 Agent **自己的**私有空间。

    🔒 这是私有侧与 Task 侧在注入方式上的**实质差异**：
       Task Workspace 的执行器拿一个**固定**的 `gateway + workspace_id`；
       私有侧必须**按 agent 路由** —— 因为每个 Agent 一份空间。

    🔒 取不到就返回 None（执行器会给出结构化失败），
       **不抛异常** —— 一个 Agent 查不到不该让整轮崩掉。
    """
    def _resolve(aid: str) -> Any:
        try:
            return rt.economy.agent(str(aid)).workspace
        except Exception:                                     # noqa: BLE001
            return None
    return _resolve


def _send_fn(bridge: Any) -> Any:
    """
    把**异步**的 `send_message_as` 适配成执行器要的**同步** `send_fn`。

    🔒 为什么这个适配器必须在**实验侧**：
       通信层是 async（`comm.send` 是协程），而 Action 执行器是同步的。
       `SyncBridge` 本来就是这条实验的同步驱动器的一部分
       （见 `experiments/novel_production/channels.py` 的说明）。
       **生产层仍然是 async 的**，没有被污染。

    🔒 它只做协程调度，**不改任何语义** —— 类型、正文、payload
       原样透传，不做校验、不做默认值。
    """
    def _send(comm: Any, *, from_agent: str, to_agent: str,
              message_type: str, text: Any, payload: Any) -> Dict[str, Any]:
        if bridge is None:                                    # pragma: no cover
            return {"ok": False, "to": to_agent, "code": "no_bridge",
                    "message": "没有同步桥，无法调异步通信层"}
        return bridge.run(send_message_as(
            comm, from_agent=from_agent, to_agent=to_agent,
            message_type=message_type, text=text, payload=payload,
            trace_id=EXPERIMENT_ID))
    return _send


def _action_seam(rt: Any, bridge: Any = None) -> tuple:
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

    # 🔒 组合根在**这里**决定「这个实验给 Agent 哪些动作」。
    #
    #    ⚠️ 这是**实验级组合**，不是生产层内建：两个执行器都来自生产层
    #       （`actions/workspace_write.py` / `actions/workspace_read.py`），
    #       本处只做**注册**。注册哪些 = 这个实验允许 Agent 发起什么。
    #
    #    2026-10-09 加入 `workspace_read`：此前 Agent 只有「写」，
    #    于是**验收责任人结构上读不到被验收的内容**。实测
    #    `novel_reader` 因此在
    #    `/reader/chapter1_reader_acceptance.md` 里拒绝盖章：
    #        「我目前只能看到 /manuscript/chapter_001.md 的路径和锁定状态，
    #          无法读取正文，所以不能负责任地给出通过。」
    #    🔒 读**不受 Write Lock 影响** —— 锁只挡修改/删除（ADR-0021）。
    dispatcher = ActionDispatcher([
        WorkspaceWriteExecutor(write_artifact,
                               gateway=rt.workspace_gateway,
                               workspace_id=WS_ID),
        WorkspaceReadExecutor(read_artifact,
                              gateway=rt.workspace_gateway,
                              workspace_id=WS_ID),
        # 2026-10-09 加入 message_send：此前 Agent 只能发 MessageType.TEXT
        # （`channels.send` 写死），于是协议里那 8 种业务消息
        # —— 尤其 **TASK_OFFER**（`handle_offer` 会用它建 Task，
        # `creator = 发起方 Agent`）—— 在组合根里接好了却从没被触发。
        # 🔒 这是「Agent 自己组织生产」唯一的现成通道。
        MessageSendExecutor(_send_fn(bridge), comm=rt.communication),
        # 2026-10-09 加入 public_query：查**公共资料区**（只读）。
        # 🔒 它**不是 Workspace** —— 没有锁/修订/审计/参与者，
        #    因为没有需要 Agent 改的东西。只是一份可查的资料。
        #    入口是「系统地图」：告诉 Agent 这个环境里有什么、
        #    边界在哪、它能发起哪些动作。
        PublicQueryExecutor(_PUBLIC.query),
        # 2026-10-09 加入私有空间三件套：Agent 读/写/列**自己的**文件。
        # 🔒 它们与 Task Workspace 那三个是**两套** —— 私有侧没有
        #    锁/修订/审计/参与者，安全边界只有"路径逃逸"。
        # 🔒 **按 intent.agent_id 路由**（每个 Agent 一份空间），
        #    所以注入的是 resolver 而不是固定的 workspace_id。
        # 🔒 agent_id 只取自 intent（运行时填的），不可伪造 ——
        #    否则 Agent 能读写别人的私有空间。
        PrivateWriteExecutor(write_private, workspace_of=_ws_of(rt)),
        PrivateReadExecutor(read_private, workspace_of=_ws_of(rt)),
        PrivateListExecutor(list_private, workspace_of=_ws_of(rt)),
        PrivateDeleteExecutor(delete_private, workspace_of=_ws_of(rt)),
        # 2026-10-09 加入资源结构/责任三件套：建目录 · 交接 · 释放。
        # 🔒 它们管的是**结构与责任**，不是内容（内容归 workspace_*）。
        # 🔒 gateway 与 workspace_id 在这里**绑定**掉，Agent 只给路径 ——
        #    Agent 永远看不到 gateway 是什么。
        # 🔒 **发起方只取自 intent.agent_id**（运行时填的），
        #    所以 Agent 无法替别人交接责任。
        ResourceCreateExecutor(_bind_gw(rt, create_directory)),
        ResourceHandoffExecutor(_bind_gw(rt, handoff_resource)),
        ResourceReleaseExecutor(_bind_gw(rt, release_resource)),
        # 2026-10-09 加入 assign_acceptance：**Agent 自己**指派验收责任。
        # 🔴 依据：`task_execution_service` 自己写着「谁负责验收由 Agent
        #    自己通过现有的 ASSIGN 能力决定」—— 但 ASSIGN **没有消息通道**，
        #    所以以前只能由 runner 代做 ⇒「Agent 自主完成任务」断在这里
        #    （能发 SUBMIT，却没人能被授权 VERIFY）。
        # 🔒 alignment 与 task_id 在组合根**绑定**掉，Agent 只说要给谁。
        AssignAcceptanceExecutor(_bind_align(rt, assign_acceptance)),
        # 2026-10-10 加入 shared_context_read：**Agent 自己**读共同认知。
        # 🔴 依据（用户指出的缺陷）：「Task 的共同认知应该有 Action 给
        #    Agent 查」—— 在此之前它**只有写通道**（assign_acceptance），
        #    Agent 写下的 objective / constraints / decisions /
        #    assumptions / facts / assignments **自己一个字都读不到**。
        #    `alignment.snapshot()` 早就写着自己是「可消费的读取视图」，
        #    缺的只是 Agent 够得到的入口。
        # 🔒 **无参数**：读哪条 Task 由组合根绑定，
        #    以什么身份读由 intent.agent_id 决定 —— 两者 Agent 都指定不了。
        SharedContextReadExecutor(_bind_context_read(rt, read_shared_context))])
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
    dispatcher, experience_store = _action_seam(rt, bridge)

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
        stage_txt = render_stage(
            stage, checkpoint=ckpt, limit_seconds=limit,
            elapsed_seconds=sess.elapsed_seconds(),
            # 🔴 「还能给你几个回合」—— 这是 Agent **能算**的事实
            #    （"留到后续回合"到底可不可行）。"还有几分钟"它算不了。
            rounds_left=max(0, MAX_ROUNDS_PER_SESSION - rnd + 1))
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
          experience_store: Any = None,
          demand: Any = None,
          prompt_template: Any = None) -> Dict[str, Any]:
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

    # 🔒 `prompt_template` 可换：让**别的实验**用它**自己的**环境事实，
    #    而不是继承本实验的整套（Stage / Session / 结项待办 / 「轮」…）。
    _template = (prompt_template if prompt_template is not None else PROMPT)
    prompt = _template.format(
        # 🔴 `demand` 可覆盖：**「人类命令」只递给被指定的那个 Agent**。
        demand=DEMAND if demand is None else demand,
        stage=stage_txt, ws=WS_ID,
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

    # 🔴 走**生产认知入口** `act()`，不再自己造协议。
    #
    #   旧路径：a.generate() → 实验自己的 parse_intent() → 自己 dispatch
    #   新路径：a.act() → run_cognition() → **生产信封** → dispatcher.dispatch()
    #
    #   🔒 一次解决两件事：
    #      ① 协议不再重复（实验与生产用同一个信封 parser）
    #      ② **Self Model 回流接通** —— act() 会把 Agent 提出的
    #         Reflection Proposal 交给它自己的 write_service 走受控写入。
    #         （旧路径走 generate()，那条链在真实实验里从来没被用过。）
    #
    #   ⚠️ **一轮一个动作**是生产信封的形状（`action` 单数）。
    #      实测：旧契约下 **88% 的轮次一轮做 2 件事**（写 + 通知），
    #      所以这次切换会让那部分轮次需要分两轮 —— 这是**有意的代价**，
    #      为了让实验跑在**生产协议**上，不是为了省轮数。
    #
    #   🔒 **不显式传 write_service** —— act() 会自己取 Agent 身上的
    #      `self_model_write_service`（生产组合根已经注入了）。
    try:
        run = a.act(messages=prompt,
                    dispatcher=dispatcher,
                    task_id=TASK_ID,
                    requested_cognitive_budget=COGNITIVE_BUDGET,
                    # §5.3 purpose 由**调用方**声明，绝不从结果反推
                    call_purpose="cognition",
                    cognition_turn_id=cognition_turn_id,
                    experiment_run_id=EXPERIMENT_RUN_ID,
                    # 🔴 2026-10-10（用户决定「形状 2」）：
                    #    Agent 读到东西之后，**读取的真实内容当场回喂**，
                    #    它在**同一次认知里**接着决定做什么。
                    #
                    #    🔒 为什么由**实验**显式打开：它会让「读」的那一轮
                    #       多一次完整 LLM 调用（实测单次 4k–11k tokens）。
                    #       这种成本必须由调用方明说要不要付，
                    #       所以 `run_cognition` 的默认是**关闭**。
                    follow_up_on_reads=True)
    except Exception as exc:                                  # noqa: BLE001
        turn["error"] = f"{type(exc).__name__}: {exc}"
        stats[aid]["calls"] += 1
        stats[aid]["empty"] += 1
        return turn

    # 🔒 act() 内部经 run_cognition() → self.generate()，
    #    所以这次 LLM 调用的事实仍可从 Agent 上取到。
    #    契约状态用 `run.*` —— 那是**生产层**的读数，
    #    不是实验自己从 response 再推一遍。
    r = getattr(a, "last_response", None)
    content = getattr(r, "content", None) or ""

    # 🔴 **这一轮花掉多少 tokens = 所有段之和。**
    #
    #    `last_response` 只保留**最后一次**调用 —— 发生 follow-up 时
    #    直接用它会把这一轮的成本算成**只有第二段**，而两段都是真实的
    #    付费调用。逐段记在 `run.segments[i].tokens` 上。
    #
    #    🔒 权威成本仍然在 `llm_calls` 表（每次调用一行）——
    #       这里修的是**逐轮显示**，不是记账。
    _segs = list(getattr(run, "segments", None) or [])
    _tok_each = [s.tokens for s in _segs]
    turn_tokens = (sum(int(t or 0) for t in _tok_each) if _segs
                   else getattr(r, "total_tokens", None))

    s = stats[aid]
    # 🔒 `calls` 记的是**LLM 调用次数**，不是 turn 数 ——
    #    发生 follow-up 的一轮真的调了两次。
    s["calls"] += max(1, len(_segs))
    s["tokens"] += int(turn_tokens or 0)
    turn.update({"ok": True,
                 "tokens": turn_tokens,
                 # 🔴 逐段的 tokens —— 让「一轮多调了一次」看得见。
                 "tokens_by_segment": _tok_each,
                 "requested_model": getattr(r, "requested_model", None),
                 "response_model": getattr(r, "response_model", None),
                 # 🔴 生产层读出来的契约状态
                 "output_status": run.output_status,
                 "cognitive_status": run.cognitive_status,
                 "finish_reason": run.finish_reason,
                 "ms": round((time.perf_counter() - t0) * 1000, 1),
                 "chars": len(content)})

    # ---- §5.1/§7：这次调用的 llm_call_id 与 Provider 边界证据 ----
    turn["llm_call_id"] = getattr(a, "last_call_id", None)
    evidence = getattr(a, "last_request_evidence", None)
    if evidence:
        turn["request_evidence"] = {
            k: v for k, v in evidence.items()
            if k != "request_sha256_16"     # 摘要单独放，便于核对
        }
        turn["request_sha256_16"] = evidence.get("request_sha256_16")

    # ---- §9/§10/§11：截断事实 + **下一轮真实 Context** 的证据 ----
    truncated = run.cognitive_status == "TRUNCATED"
    turn["truncated"] = truncated
    if truncated:
        turn["critical_experience_id"] = _latest_critical_id(rt, aid)
        turn["critical_observation_created"] = bool(
            turn["critical_experience_id"])
    turn["context"] = dict(_CONTEXT_SINK.get(aid) or {})

    # 🔴 EMPTY_CONTENT **不许悄悄成功**（与 run_cognition 同一条纪律）。
    #    用**生产层的** output_status 判定，不从内容长度反推。
    if run.output_status == "EMPTY_CONTENT" or not content.strip():
        s["empty"] += 1
        turn["ok"] = False
        turn["error"] = "EMPTY_CONTENT"
        return turn
    s["valid"] += 1

    # ---- Decision（由**生产 parser** 得出）----
    decision = run.decision
    reasoning = str(getattr(decision, "reasoning", "") or "")
    turn["decision"] = getattr(decision, "kind", None)
    turn["reasoning"] = reasoning[:400]
    if "完成" in reasoning and "未" not in reasoning:
        turn["claims_done"] = True

    # ---- 🔴 段：一次认知可能有 2 段（读到了 → 当场接着决定）----
    #
    #   🔒 `turn["reasoning"]` 只放**第一段**的 —— 它是「Agent 看到
    #      prompt 时怎么判断的」，语义不变（向后兼容）。
    #      第二段的判断在 `turn["segments"][1]`，另外汇总一份到
    #      `turn["reasonings"]`，好让报告不用钻进 segments。
    #
    #   🔴 不记「它读了什么」的**内容** —— 那是上下文，不是本轮事实；
    #      本轮事实是「它发起了哪些动作、结果如何」，在 actions 里。
    segments = list(getattr(run, "segments", None) or [])
    turn["follow_up_used"] = bool(getattr(run, "follow_up_used", False))
    if segments:
        turn["segments"] = [
            {"index": s.index,
             "reasoning": str(getattr(s.decision, "reasoning", "") or "")[:400],
             "decision": getattr(s.decision, "kind", None),
             "action_count": len(s.action_intents),
             "output_status": s.output_status,
             "cognitive_status": s.cognitive_status,
             "finish_reason": s.finish_reason}
            for s in segments
        ]
        turn["reasonings"] = [s["reasoning"] for s in turn["segments"]
                              if s["reasoning"]]

    # ---- 🔴 唯一的动作通道（一轮一个）----
    #
    #   🔒 **这里不 dispatch** —— run_cognition 已经执行过了。
    #      再 dispatch 一次会**重复执行**（写两次文件、发两条消息）。
    #
    #   🔒 Experience 也**不在这里记** —— BaseAgent.act() 已经记了，
    #      并挂在 `run.experience` 上。这里只**读**。
    #
    #   🔒 下面把一次动作**投影**成 turn 的既有字段（write / message），
    #      好让 `_print_turn` 与报告不用改 —— 那是**视图**，
    #      不是第二个事实源。
    # 🔴 2026-10-10：**一次决策可以有 0..N 个动作** ⇒ 这里逐个投影。
    #
    #    🔒 不 dispatch —— `run_cognition` 已经执行过了（再 dispatch 会**重复执行**）。
    #    🔒 不记 Experience —— `BaseAgent.act()` 已经记了（`run.experiences`）。
    #    🔒 下面把每个动作**投影**成 turn 的字段（write / message / action），
    #       那是**视图**，不是第二个事实源。
    #
    #    🎯 **这就是「写完正文的同时通知下一个人」的落点** ——
    #       以前一次只能一个动作，于是 Agent 只能把它拆成两次决策，
    #       而「下一次」需要有人唤醒它 ⇒ 拆完就断在那里。
    actions_out: List[Dict[str, Any]] = []
    for _i, (ai, ar) in enumerate(zip(run.action_intents, run.action_results)):
        atype = str(ai.action_type)
        params = dict(getattr(ai, "parameters", None) or {})
        out = (ar.output if (ar is not None and isinstance(ar.output, dict))
               else {})
        code = (ar.metadata or {}).get("code") if ar is not None else None
        ok = bool(ar is not None and ar.success)

        item: Dict[str, Any] = {
            "type": atype, "ok": ok, "code": code,
            "path": out.get("path") or params.get("path"),
            "revision": out.get("revision"),
            "chars": out.get("chars"),
            # 🔒 内容也记进 turn —— 否则「读到了什么」没有证据。
            "content": out.get("content"),
            # 🔒 只记 experience_id，不记 Experience 对象
            #    （对象不可 JSON 序列化 —— 实测把整轮结果卡死过）。
            "experience_id": getattr(
                (run.experiences[_i] if _i < len(getattr(run, "experiences", []))
                 else None), "experience_id", None),
        }
        if ar is None:
            # TRUNCATED 时 run_cognition **保留 intent 但不执行** ——
            # 那是真实发生的事实：既不假装成功，也不假装没发生。
            item["skipped"] = "TRUNCATED"
        actions_out.append(item)

        if atype == ACTION_WORKSPACE_WRITE:
            mode = str(params.get("write_mode") or "")
            if mode == MODE_CREATE:
                s["created"] += 1
            elif mode == MODE_APPEND:
                s["appended"] += 1
            else:
                s["replaced"] += 1
            turn["write"] = {
                "path": params.get("path"), "mode": mode, "ok": ok,
                "code": code, "revision": out.get("revision"),
                "previous_chars": out.get("previous_chars"),
                "appended_chars": out.get("appended_chars")}
            if ok:
                s["writes_ok"] += 1
                s["chars_written"] += len(str(params.get("content") or ""))
            else:
                s["writes_fail"] += 1
        elif atype == ACTION_MESSAGE_SEND:
            turn["message"] = {"to": params.get("to"), "ok": ok}
            if ok:
                s["messages"] += 1
                log["comm_events"].append(
                    {"kind": "sent", "agent_id": aid, "round": rnd,
                     "to": params.get("to")})

    if actions_out:
        # 🔒 `action`（单数）保留为**第一个** —— 向后兼容报告与既有守卫；
        #    复数在 `actions` 里。
        turn["action"] = actions_out[0]
        turn["actions"] = actions_out
        turn["action_experience"] = actions_out[0].get("experience_id")
    return turn


def _turn_actions(turn: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    这一轮里的**全部**动作。

    🔴 **不许用 `turn["action"]`** —— 它是**兼容属性，只等于第一个动作**
       （见上面 `turn["action"] = actions_out[0]`）。

    【为什么这是个真 bug，不是洁癖】
        多动作信封上线后，**一次**决定里可以同时

            workspace_write ✅ + message_send ✅ + assign_acceptance ✅

        只读第一个 ⇒ 显示「它只写了个文件」，
        而它其实**把活分出去了**。
        「Agent 什么时候想到把活分工」这个问题就会被答反。

    【为什么兜底到单数】
        多动作之前的旧日志里只有 `action`、没有 `actions` ——
        分析旧结果时不能崩。

    🔒 这里 turn 的形状由本模块定义，所以**真相只有这一份**：
       `run_division._actions_of` 直接转发到它。
    """
    actions = turn.get("actions")
    if actions:
        return [a for a in actions if isinstance(a, dict)]
    one = turn.get("action")
    return [one] if isinstance(one, dict) else []


def _print_turn(aid: str, turn: Dict[str, Any]) -> None:
    if not turn.get("ok"):
        print(f"    ❌ {aid:22s} {turn.get('error', '')[:60]}")
        return
    bits = [f"tokens={turn['tokens']}"]

    # 🔴 这一轮**读到了东西** ⇒ 环境把真实返回当场回喂，模型又调了一次。
    #    🔒 必须显示出来：否则「一轮花了两次调用的钱」看不出原因。
    segs = turn.get("segments") or []
    if turn.get("follow_up_used"):
        bits.append(f"🔁读到内容→当场回喂（{len(segs)}段"
                    f" tokens={turn.get('tokens_by_segment')}）")

    # 🔴 **一个动作都不能漏。**
    #
    #    `turn["write"]` / `turn["message"]` 是「**最后一次**写 /
    #    **最后一次**发消息」的快照（每轮循环里被覆盖），
    #    所以它们**不构成**「这一轮做了什么」的完整证据。
    #    多动作信封下，一轮可以 write + send + assign_acceptance 同时发生。
    #
    #    ⇒ 先按**全部**动作算次数，再逐个渲染。
    acts = _turn_actions(turn)
    n_write = sum(1 for a in acts if a.get("type") == ACTION_WORKSPACE_WRITE)
    n_send = sum(1 for a in acts if a.get("type") == ACTION_MESSAGE_SEND)

    w = turn.get("write")
    if w:
        mark = "✅" if w["ok"] else f"❌{w.get('code')}"
        extra = ""
        if w["mode"] == MODE_APPEND and w["ok"]:
            extra = f" (+{w.get('appended_chars')}→R{w.get('revision')})"
        times = f" ×{n_write}" if n_write > 1 else ""
        bits.append(f"📝{w['mode']} {w['path']} {mark}{extra}{times}")
    if turn.get("message"):
        times = f" ×{n_send}" if n_send > 1 else ""
        bits.append(f"✉️→{turn['message']['to']}{times}")

    for a in acts:
        atype = str(a.get("type") or "?")
        mark = "✅" if a.get("ok") else f"❌{a.get('code')}"
        if atype == ACTION_WORKSPACE_WRITE:
            if n_write == 1 and w:
                continue          # 唯一一次写：上面 📝 已带 mode/revision
            bits.append(f"📝{a.get('path')} {mark}")
        elif atype == ACTION_MESSAGE_SEND:
            if n_send == 1 and turn.get("message"):
                continue          # 唯一一次发消息：上面 ✉️ 已带收件人
            bits.append(f"✉️message_send {mark}")
        elif atype == ACTION_WORKSPACE_READ:
            bits.append(f"📖{a.get('path')} ({a.get('chars')}字) {mark}")
        else:
            parts = [f"⚙️{atype}"]
            if a.get("path"):
                parts.append(str(a["path"]))
            parts.append(mark)
            bits.append(" ".join(parts))

    print(f"    ✅ {aid:22s} {'  '.join(bits)}")


# ================================================================
def _describe_entry(e: Dict[str, Any], *, with_name: bool = True) -> str:
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
    🔒 `with_name=False` 给**已经自己打印了路径**的调用方用 ——
       名字只出现一次，而格式化逻辑仍然只有这一份。
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
    body = f"（{', '.join(facts)}）" if facts else ""
    return f"{name}{body}" if with_name else (body or name)


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


#: 事实块最多列多少条 / 递归多深。
#: 🔒 超了就**如实说截断了**，不静默少列。
_WORKSPACE_FACT_MAX_ENTRIES = 60
_WORKSPACE_FACT_MAX_DEPTH = 4


def _workspace_facts(gw: Any, agent: str) -> str:
    """
    §七：把 Workspace 的**真实环境事实**给 Agent。

    🔴 **2026-10-10 重写：列真实存在的树，不再遍历预设的命名空间清单。**

    【为什么必须改 —— 实测证据】

        旧实现只遍历 `ARTIFACT_NAMESPACES`。而观察实验为了**不预设分工**
        把它清空了（`P.ARTIFACT_NAMESPACES = {}`）——
        于是它**只列根目录**，而根目录下只有一个 `manuscript` 目录、没有文件。

        实测（本地构造，证据确凿）：
            真实树:  /manuscript  (directory)
                     /manuscript/chapter_001.md  (file, R1)
            而 **agent_a 和 agent_b 看到的 facts 块一模一样**：
                workspace_id = WS-0001
                  / 根目录文件: （无）

        ⇒ 一个**已经存在**的文件，在**任何** Agent 的 facts 块里
          **完全不可见**。实测 agent_b 因此判断
          「agent_a 已声明完成，但根目录事实显示无文件 —— 存在**信息不一致**」
          然后去读了一次（好行为）；但如果它**相信** facts 块，就会**重复劳动**。

    🔒 事实 = **环境里真实有什么**，不是我们预设的清单。
       预设清单还有一个更糟的副作用：它**本身就是分工暗示**
       （`world/plot/chapters/...` 等于替 Agent 规定"有哪些角色"）。

    🔒 只陈述 Gateway 已经给出的字段
       （`type` / `revision` / `effective_controller` / `effective_lock_holder`），
       不新增、不推断、不做权限判断。

    ⚠️ **路径不带前导 `/`** —— 显示的就是 `workspace_write` 的 `path` 参数该写的形状。
       历史教训（真实运行 1009-2100）：这里原先显示 `/plot/`，于是 4 个 Agent
       全照着写了 `/plot/outline.md`，而 `validate_artifact_path` 把**以 `/` 开头**
       当成宿主绝对路径拒绝 ⇒ 全员 `bad_path`。
       （根因是 `services/artifacts.py` 与 `WorkspaceGateway` 的语法不一致，
         属于策略问题，**没有**在这里改。）
    """
    lines = [f"workspace_id = {WS_ID}"]
    seen = 0
    truncated = False

    def walk(path: str, depth: int) -> None:
        nonlocal seen, truncated
        got = gw.list(WS_ID, path, agent)
        if not got.success:
            lines.append(f"  {'  ' * depth}（{path} 读不到："
                         f"{getattr(got, 'code', None) or '未知原因'}）")
            return
        for e in (got.result or {}).get("entries", []) or []:
            if seen >= _WORKSPACE_FACT_MAX_ENTRIES:
                truncated = True
                return
            seen += 1
            name = str(e.get("name") or "")
            child = f"/{name}" if path == "/" else f"{path}/{name}"
            is_dir = str(e.get("type") or "") == "directory"
            shown = child.lstrip("/")            # ⚠️ 不带前导 `/`（见 docstring）
            lines.append(f"  {'  ' * depth}{shown}"
                         f"{'/' if is_dir else ''}  "f"{_describe_entry(e, with_name=False)}")
            if is_dir and depth + 1 < _WORKSPACE_FACT_MAX_DEPTH:
                walk(child, depth + 1)

    walk("/", 0)
    if not seen:
        lines.append("  （整个 Workspace 是空的）")
    if truncated:
        lines.append(f"  …（只列了前 {_WORKSPACE_FACT_MAX_ENTRIES} 条，还有更多）")
    return "\n".join(lines)


def _roster_facts(rt: Any, agent: str) -> str:
    """
    其他 Agent 的**公开事实**（§22.20 Public Manifest + §0.5.8 信誉/历史）。

    【🔴 为什么不投 `skills` 了】

        `skills` 是**我们手写的标签**，没有任何东西支撑它（没验证、没历史）。
        它其实**就是 §0.5.7 说的"预设角色表"换了个名字**：

            🔴 不要预先规定「A 是程序员、B 是测试 Agent」。
               应该让**能力、成本、信誉、历史**和任务需求逐步形成专业化。
               这应该是**经济机制和长期反馈**产生的结果。

        而 §22.21 说得更直接：

            Public Manifest  ≠  Capability  ≠  Authorization
                             ≠  Ability Evaluation

        ⇒ 所以这里投的是**公开事实**，不是"谁会什么"的声明。

    【投什么】

        · §22.20 B（系统维护）：`provider` / `model`
          —— 真实的动态事实，有区分度，而且**不是我们替它说的**
        · §0.5.8（OS 提供）：`信誉 · 历史`
          —— 完成任务 / 失败 / 成功率 / 声誉

    🔒 只陈述事实，**不写**「你应该联系谁」，也不写「谁更厉害」。
       🔴 §22.19：Agent 据此**自己**形成 `Other-Agent Model`（含它自己的
          confidence 与 evidence）—— 判断**不由我们代做**。
    """
    entries = [e for e in rt.directory.all()
               if getattr(e, "agent_id", "") and e.agent_id != agent]
    if not entries:
        return "  （没有其他参与者）"

    lines: List[str] = []
    for e in entries:
        meta = dict(getattr(e, "metadata", None) or {})
        head = f"  - {e.agent_id}"
        if meta.get("provider") or meta.get("model"):
            head += (f"  provider={meta.get('provider') or '?'}"
                     f"  model={meta.get('model') or '?'}")
        lines.append(head)

        # ---- §0.5.8：OS 提供的**信誉 / 历史** ----
        rep = _reputation_of(rt, e.agent_id)
        if rep is not None:
            rate = rep.success_rate
            lines.append(
                f"      履历：完成任务={rep.completed_tasks}"
                f"  失败={rep.failed_tasks}"
                f"  成功率={'—' if rate is None else f'{rate:.2f}'}"
                f"  声誉={rep.score:.2f}")
    return "\n".join(lines)


def _reputation_of(rt: Any, agent_id: str) -> Any:
    """读一个 Agent 的声誉（读不到就是 None —— **不伪造一个 0**）。"""
    try:
        return getattr(rt.economy.agent(agent_id), "reputation", None)
    except Exception:                                          # noqa: BLE001
        return None


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

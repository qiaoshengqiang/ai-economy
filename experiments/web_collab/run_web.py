#!/usr/bin/env python3
"""
实验：人类一句话 → 多 Agent 协作完成一个网页
================================================

**Human Command**（唯一任务输入）：

    完成一个网页扫雷小游戏。

没有细纲、没有角色表、没有任务专用 DoD —— Agent 自己决定产物、
分工和实现方式。实验层只提供：

    Demand（人类原话）/ 参与者 / 命名空间 / **通用**验收契约 / 墙钟

复用的一整条真实链路：

    build_runtime()                 ← 真实 Runtime（配置 / 经济 / 通信 / 工作区）
        ↓
    Task + Workspace + 根 Stage
        ↓
    轮次循环：每个参与者一轮
        · prompt = Human Command + Stage + 真实 Workspace 事实 + 花名册 + 信箱
        · LLM → 结构化动作 → WorkspaceGateway（锁 / revision / 审计）
        · 消息 → CommunicationManager（对端 wakeup）
        ↓
    Task Execution：START / SUBMIT / VERIFY / COMPLETE

🔒 结项由 **Agent 自己**发消息推进（`AGENT_DRIVEN_COMPLETION=True`）；
   runner 只观察 Task Core 状态，不代发 VERIFY / COMPLETE。

🔒 `_ensure_completion_setup` 现在会补 **已存在但为空** 的 Shared Context
   （objective / DoD / 验收责任），不再因为 `Runtime.create_task` 先建了
   空容器就跳过。

用法
----
    # 默认：完成一个网页扫雷小游戏
    ./.venv/bin/python experiments/web_collab/run_web.py --minutes 15

    # 换一句人类命令
    ./.venv/bin/python experiments/web_collab/run_web.py \
        --command "完成一个网页扫雷小游戏。"

⚠️ 会花真实 API 的钱。默认 15 分钟硬墙钟、20 AIC 预算。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")


# ======================================================================
# 实验参数
# ======================================================================
#: 人类命令 —— **这是唯一的任务输入**。
#:
#: 用户要求走 human command：只给一句话，不附细纲、不附角色表、
#: 不附任务专用 DoD。Agent 自己决定要产出什么、怎么分工。
HUMAN_COMMAND = "完成一个网页扫雷小游戏。"

#: 参与者：**两个**就够。
#:
#: 🔴 这是为了把「Agent 协作」与「模型稳定性」分开：
#:    · agent_b（豆包）负责实现，关键路径放在已验证稳定的模型上；
#:    · agent_a（DeepSeek）负责内容/协调/独立验收。
#:    第一次 4-Agent 尝试遇到 DeepSeek 503 + 百炼大上下文卡住，
#:    那次失败是**运行可靠性**问题，不是协作能力问题。
DEFAULT_AGENTS = ["agent_a", "agent_b"]

#: 预分配的生产区域。
#:
#: 🔴 **这是脚手架，不是自主分工。**
#:    每个区域有一个 Controller，Agent 才有地方可写；
#:    「谁写哪块」由实验层声明，不是系统硬编码。
NAMESPACES = {
    "brief": "agent_a",       # 文案 / 需求理解 / 参考实现
    "site": "agent_b",        # 实现（HTML/CSS/JS）
    "qa": "agent_a",          # 独立验收（非实现者）
}

#: 生产者与验收人。
#: 🔒 必须不同 —— 否则「生产者不能自验」这条边界无从体现。
PRODUCER = "agent_b"
ACCEPTOR = "agent_a"
OWNER = DEFAULT_AGENTS[0]


DEMAND = HUMAN_COMMAND

#: **通用**验收契约 —— 不是 Minesweeper 的详细规格。
#:
#: 🔒 人类只说了「完成一个网页扫雷小游戏」；下面四条只回答
#:    「什么叫完成」，不替 Agent 决定玩法、视觉、代码结构。
#:    Agent 仍然要自己把「扫雷」翻译成可验收的功能。
DEFINITION_OF_DONE = (
    "交付物存在且非空",
    "交付物是一个可以在浏览器中直接打开的网页",
    "交付物实现了人类命令要求的功能",
    "至少一个非实现者写出独立验收结论",
)


# ======================================================================
# 交付物投影
# ======================================================================
def _deliverable_ref(rt):
    """
    把 `site/index.html` 变成 TaskResultRef。

    🔒 这只是**实验层的投影**：`production.py` 原来的
       `_manuscript_ref` 找的是名字含 manuscript 的手稿；
       网页任务的交付物是代码，不是手稿。
    """
    from core.task_execution import TaskResultRef

    ws = "site/index.html"
    try:
        got = rt.workspace_gateway.read(
            _STATE["workspace_id"], "/" + ws, _STATE["producer"])
    except Exception:                                         # noqa: BLE001
        return None
    if not got.success:
        return None
    content = str((got.result or {}).get("content") or "")
    if not content.strip():
        return None
    return TaskResultRef(
        result_ref="web-landing-page",
        result_type="code",
        summary=f"静态网页交付物 {ws}",
        metadata={
            "artifact": ws,
            "revision": (got.result or {}).get("revision"),
            "chars": len(content),
        },
    )


_STATE = {
    "workspace_id": "",
    "producer": PRODUCER,
}


# ======================================================================
def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="run_web.py",
        description="多 Agent 协作完成一个简单网页任务（真实 API）",
    )
    ap.add_argument("--command", default=HUMAN_COMMAND,
                    help=f"人类命令（一句话），默认：{HUMAN_COMMAND}")
    ap.add_argument("--minutes", type=float, default=15.0,
                    help="总墙钟硬上限（分钟），默认 15")
    ap.add_argument("--session-minutes", type=float, default=5.0,
                    help="单个 Session 上限（分钟），默认 5")
    ap.add_argument("--rounds", type=int, default=3,
                    help="每个 Session 最多几轮（每个 Agent 每轮一次），默认 3")
    ap.add_argument("--budget", type=float, default=20.0,
                    help="Task 预算（AIC），默认 20")
    ap.add_argument("--cognitive-budget", type=int, default=16384,
                    help="单次响应输出上限（tokens），默认 16384")
    ap.add_argument("--out", default=None, help="输出目录")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    run_id = uuid.uuid4().hex[:8]
    outdir = (Path(args.out).expanduser().resolve() if args.out
              else HERE / "runs" / f"{datetime.now():%m%d-%H%M}-{run_id}")
    outdir.mkdir(parents=True, exist_ok=True)
    results = outdir / "results"
    results.mkdir(parents=True, exist_ok=True)
    db = outdir / "web.db"

    from experiments.heterogeneous_production import production as P

    task_id = f"task_web_{run_id}"
    ws_id = f"WS-WEB-{run_id}"

    # ---- 覆盖实验参数（只改进程内模块全局，不碰源码）----
    P.DEMAND = str(args.command).strip() or HUMAN_COMMAND
    P.AGENTS = list(DEFAULT_AGENTS)
    P.OWNER = OWNER
    P.PRODUCER = PRODUCER
    P.ACCEPTOR = ACCEPTOR
    P.TASK_ID = task_id
    P.WS_ID = ws_id
    P.DB = db
    P.RESULTS = results
    P.EXPERIMENT_ID = f"web_collab_{run_id}"
    P.ROOT_STAGE_ID = f"stage_{task_id}_root"
    P.ARTIFACT_NAMESPACES = dict(NAMESPACES)
    P.DEFINITION_OF_DONE = DEFINITION_OF_DONE
    # 🔴 结项必须由 Agent 自己走协议；runner 只观察 Task Core 状态。
    P.AGENT_DRIVEN_COMPLETION = True
    P.COMPLETION_QA_ARTIFACT = "qa/review.md"
    P.BUDGET_AIC = float(args.budget)
    P.TOTAL_WALL_LIMIT = max(60.0, args.minutes * 60.0)
    P.SESSION_LIMIT = max(30.0, args.session_minutes * 60.0)
    P.MAX_ROUNDS_PER_SESSION = max(1, args.rounds)
    P.COGNITIVE_BUDGET = int(args.cognitive_budget)
    # 网页任务不需要小说实验的 400 字符最低产出；调低一点。
    P.MIN_PRODUCTIVE_CHARS_PER_SESSION = 200

    _STATE["workspace_id"] = ws_id
    _STATE["producer"] = PRODUCER
    P._manuscript_ref = _deliverable_ref

    print("=" * 68)
    print("  多 Agent 协作网页任务")
    print("=" * 68)
    print(f"  Human  : {P.DEMAND.strip().splitlines()[0]}")
    print(f"  参与者 : {', '.join(DEFAULT_AGENTS)}")
    print(f"  分区   : " + " · ".join(f"{k}→{v}" for k, v in NAMESPACES.items()))
    print(f"  生产者 : {PRODUCER}   验收人: {ACCEPTOR}")
    print(f"  墙钟   : {P.TOTAL_WALL_LIMIT/60:.1f} 分钟 "
          f"（单 Session {P.SESSION_LIMIT/60:.1f} 分钟，"
          f"每 Session {P.MAX_ROUNDS_PER_SESSION} 轮）")
    print(f"  预算   : {P.BUDGET_AIC} AIC")
    print(f"  输出   : {outdir}")
    print("=" * 68)
    print()

    try:
        rc = P.main()
    finally:
        print()
        print("=" * 68)
        print("  产物位置")
        print("=" * 68)
        print(f"  数据库     : {db}")
        print(f"  结果 JSON  : {results / 'heterogeneous_production_result.json'}")
        print(f"  查看产物   : ./.venv/bin/python experiments/user_demand/view_result.py "
              f"--path {outdir}")
        print("=" * 68)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

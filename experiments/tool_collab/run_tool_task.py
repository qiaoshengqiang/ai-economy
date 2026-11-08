#!/usr/bin/env python3
"""
实验：人类一句话 → Agent 写代码 / 写测试 → **真实执行** → 验收
================================================================

这是 Tool Runtime V1 的端到端实验入口。

    Human Command
        ↓
    Agent 写代码 / 测试到 Task Workspace
        ↓
    run_tool（公共工具库：python / pytest / node / playwright）
        ↓
    独立 run 目录执行；stdout / stderr / exit_code 回喂
        ↓
    Agent 自行修复并重跑
        ↓
    独立验收 + VERIFY / COMPLETE

用法
----
    # 后端：Python + pytest
    ./.venv/bin/python experiments/tool_collab/run_tool_task.py \
        --task-type backend \
        --command "实现一个 Python 模块，提供 add(a,b) 和 subtract(a,b)，写 pytest 测试并运行到通过。"

    # 网页：单文件 HTML + Playwright
    ./.venv/bin/python experiments/tool_collab/run_tool_task.py \
        --task-type web \
        --command "完成一个网页待办小游戏（单文件），写 Playwright 测试并运行到通过。"

⚠️ 会花真实 API 的钱；也会真实执行 Agent 写的代码（在独立 run 目录中）。
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

HUMAN_COMMAND = (
    "实现一个 Python 模块，提供 add(a,b) 和 subtract(a,b)，"
    "写 pytest 测试并运行到通过。"
)

#: 两个 Agent：一个实现，一个测试/验收。
AGENTS = ["agent_a", "agent_b"]
PRODUCER = "agent_b"
ACCEPTOR = "agent_a"
OWNER = "agent_a"

NAMESPACES = {
    "backend": {"src": "agent_b", "tests": "agent_a", "qa": "agent_a"},
    "web": {"site": "agent_b", "tests": "agent_a", "qa": "agent_a"},
}

DEFINITION_OF_DONE = (
    "交付物存在且非空",
    "至少有可执行代码或网页",
    "测试文件存在，且 run_tool 实际执行过测试",
    "测试 exit_code = 0（通过）",
    "至少一个非实现者写出独立验收结论",
)


def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="run_tool_task.py",
        description="人类一句话 → Agent 写代码/测试 → 真实执行 → 验收",
    )
    ap.add_argument("--task-type", choices=["backend", "web"], default="backend")
    ap.add_argument("--command", default=HUMAN_COMMAND)
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--session-minutes", type=float, default=5.0)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--budget", type=float, default=20.0)
    ap.add_argument("--cognitive-budget", type=int, default=16384)
    ap.add_argument(
        "--db", default=None,
        help="复用已有的 tool.db（用于跨任务短期记忆实验）；不传则新建")
    ap.add_argument("--out", default=None)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    run_id = uuid.uuid4().hex[:8]
    outdir = (Path(args.out).expanduser().resolve() if args.out
              else HERE / "runs" / f"{datetime.now():%m%d-%H%M}-{run_id}")
    outdir.mkdir(parents=True, exist_ok=True)
    results = outdir / "results"
    results.mkdir(parents=True, exist_ok=True)
    db = (Path(args.db).expanduser().resolve() if args.db
          else outdir / "tool.db")

    from experiments.heterogeneous_production import production as P

    task_id = f"task_tool_{run_id}"
    ws_id = f"WS-TOOL-{run_id}"
    namespaces = dict(NAMESPACES[args.task_type])

    P.DEMAND = str(args.command).strip() or HUMAN_COMMAND
    P.AGENTS = list(AGENTS)
    P.OWNER = OWNER
    P.PRODUCER = PRODUCER
    P.ACCEPTOR = ACCEPTOR
    P.TASK_ID = task_id
    P.WS_ID = ws_id
    P.DB = db
    P.RESULTS = results
    P.EXPERIMENT_ID = f"tool_collab_{run_id}"
    P.ROOT_STAGE_ID = f"stage_{task_id}_root"
    P.ARTIFACT_NAMESPACES = namespaces
    P.DEFINITION_OF_DONE = DEFINITION_OF_DONE
    P.AGENT_DRIVEN_COMPLETION = True
    P.COMPLETION_QA_ARTIFACT = "qa/review.md"
    P.BUDGET_AIC = float(args.budget)
    P.TOTAL_WALL_LIMIT = max(60.0, args.minutes * 60.0)
    P.SESSION_LIMIT = max(30.0, args.session_minutes * 60.0)
    P.MAX_ROUNDS_PER_SESSION = max(1, args.rounds)
    P.COGNITIVE_BUDGET = int(args.cognitive_budget)
    P.MIN_PRODUCTIVE_CHARS_PER_SESSION = 100

    print("=" * 68)
    print("  人类一句话 → Agent 代码 / 测试 → 真实执行")
    print("=" * 68)
    print(f"  类型   : {args.task_type}")
    print(f"  Human  : {P.DEMAND}")
    print(f"  参与者 : {', '.join(AGENTS)}")
    print(f"  分区   : " + " · ".join(f"{k}→{v}" for k, v in namespaces.items()))
    print(f"  生产者 : {PRODUCER}   验收人: {ACCEPTOR}")
    print(f"  墙钟   : {P.TOTAL_WALL_LIMIT/60:.1f} 分钟 "
          f"（单 Session {P.SESSION_LIMIT/60:.1f} 分钟，"
          f"每 Session {P.MAX_ROUNDS_PER_SESSION} 轮）")
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
        print(f"  工具运行   : data/tool_runs/（每次 run 独立目录）")
        print(f"  查看产物   : ./.venv/bin/python experiments/user_demand/view_result.py "
              f"--path {outdir}")
        print("=" * 68)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

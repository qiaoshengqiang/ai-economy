#!/usr/bin/env python3
"""
用户 Demand → 多 Agent 协作生产（**测试入口**）
=================================================

🔒 **这是一个入口脚本，不修改任何项目主代码。**
   它复用现有的生产机制（`experiments/heterogeneous_production/production.py`），
   只是在**外部**覆盖它的几个模块级参数（Demand / Agent 集 / 墙钟 / 数据库路径）。

用法
----

    # 最简：直接给一句 Demand
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand "写一个 500 字的科幻短篇：一个 Agent 发现自己的记忆被改过。"

    # 从文件读（Demand 很长时方便）
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand-file my_demand.txt --minutes 6

    # 只想先看看会不会动起来（1 轮，5 次调用，很便宜）
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand "..." --rounds 1 --minutes 3

    # 换一组参与者（默认是 5 个小说 Agent）
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand "..." --agents novel_author,novel_editor,novel_reader

发生了什么
----------
完全走**现有机制**，没有任何专用旁路：

    build_runtime()                 ← 真实 Runtime（配置 / 经济 / 通信 / 工作区）
        ↓
    Task.create(creator=第一个 Agent, description=<你的 Demand>)
        ↓
    Workspace + 根 Stage            ← 共享 Task Workspace
        ↓
    轮次循环：每个 Agent 一轮
        · 收到 prompt：Demand + Stage + **真实 Workspace 事实** + 花名册 + 信箱
        · LLM 返回结构化 JSON：assessment / artifact / message
        · 写文件 → WorkspaceGateway（受锁与 revision 约束）
        · 发消息 → CommunicationManager（对端会被 wakeup）
        · Experience / CognitiveContext / 成本记账 全部照常
        ↓
    结果 JSON + 退出原因

🔒 会花真实 API 的钱。墙钟有硬上限（`--minutes`，默认 6 分钟）。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # 项目根（ai-economy/）

# 🔒 runpy / 直接跑时 sys.path[0] 是脚本目录 —— 必须显式加项目根，
#    否则 `import communication` 之类会找不到。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 🔒 必须在 import 配置体系**之前**把 .env 装进 os.environ。
#    生产脚本自己**不**加载 .env（与 main.py 的分工一致）。
from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")


#: 默认参与者 —— 就是那 5 个已经验证过能协作的 Agent。
#: 它们与 `production.py` 里的 PRODUCER / ACCEPTOR 常量一致。
DEFAULT_AGENTS = ["novel_world_builder", "novel_plot_architect",
                  "novel_author", "novel_editor", "novel_reader"]


def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="run_demand.py",
        description="给一个 Demand，让多个 Agent 按现有机制协作生产。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--demand", help="直接给一句 Demand")
    src.add_argument("--demand-file", help="从文本文件读 Demand")

    ap.add_argument("--agents", default=",".join(DEFAULT_AGENTS),
                    help=f"参与者，逗号分隔（默认：{','.join(DEFAULT_AGENTS)}）")
    ap.add_argument("--minutes", type=float, default=6.0,
                    help="总墙钟硬上限（分钟），默认 6")
    ap.add_argument("--session-minutes", type=float, default=3.0,
                    help="单个 Session 上限（分钟），默认 3")
    ap.add_argument("--rounds", type=int, default=3,
                    help="每个 Session 最多几轮，默认 3")
    ap.add_argument("--sessions", type=int, default=1,
                    help="最多跑几个 Session，默认 1（精确生效，见下方说明）")
    ap.add_argument("--budget", type=float, default=20.0,
                    help="生产预算（AIC），默认 20")
    ap.add_argument("--cognitive-budget", type=int, default=None,
                    help="单次响应的输出上限（tokens）。默认沿用实验默认值"
                         "（现为 16384；见 production.py 的 COGNITIVE_BUDGET）")
    ap.add_argument("--out", default=None,
                    help="输出目录（默认 experiments/user_demand/runs/<run_id>）")
    ap.add_argument("--reuse-db", action="store_true",
                    help="复用同一份 DB（默认每次新开，避免污染历史数据）")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    demand = args.demand
    if args.demand_file:
        demand = Path(args.demand_file).read_text(encoding="utf-8").strip()
    if not demand:
        print("❌ Demand 不能为空")
        return 2

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not agents:
        print("❌ 至少要有一个参与者")
        return 2

    # ---- 输出目录：每次运行一个独立目录（默认不复用 DB）----
    run_id = uuid.uuid4().hex[:8]
    outdir = (Path(args.out).expanduser().resolve() if args.out
              else HERE / "runs" / (f"{datetime.now():%m%d-%H%M}-{run_id}"))
    outdir.mkdir(parents=True, exist_ok=True)
    db_path = (outdir / "demand.db" if not args.reuse_db
               else HERE / "runs" / "demand.db")
    results_dir = outdir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # ---- 覆盖实验参数（**只改这个进程内的模块全局，不碰源码**）----
    from experiments.heterogeneous_production import production as P

    P.DEMAND = demand
    P.AGENTS = list(agents)
    P.OWNER = agents[0]
    P.TASK_ID = f"task_demand_{run_id}"
    P.WS_ID = f"WS-DEMAND-{run_id}"
    P.DB = db_path
    P.RESULTS = results_dir
    P.EXPERIMENT_ID = f"user_demand_{run_id}"
    P.TOTAL_WALL_LIMIT = max(60.0, args.minutes * 60.0)
    P.SESSION_LIMIT = max(30.0, args.session_minutes * 60.0)
    P.MAX_ROUNDS_PER_SESSION = max(1, args.rounds)
    P.BUDGET_AIC = float(args.budget)
    # 🔒 None = 不改，沿用 production.py 的默认（单一来源）。
    if args.cognitive_budget is not None:
        P.COGNITIVE_BUDGET = int(args.cognitive_budget)

    # 生产者 / 验收责任人必须是参与者。默认集合本来就包含它们；
    # 用户换 Agent 集时按位置对齐，避免完成链因为「不在参与者里」而失败。
    P.PRODUCER = agents[0]
    P.ACCEPTOR = agents[-1] if len(agents) > 1 else agents[0]

    # ---- 精确限制 session 数（§：外层循环只看总墙钟，没有 session 上限）----
    #
    # 背景：`production.main()` 的外层循环**唯一**的「不再开新 session」
    # 条件就是总墙钟：
    #
    #       budget_left = TOTAL_WALL_LIMIT - elapsed_total
    #       limit       = min(SESSION_LIMIT, budget_left)
    #       while True:
    #           if elapsed_total >= TOTAL_WALL_LIMIT: break
    #
    # 所以只给 `--minutes == --session-minutes` 是**脆的**：
    # session 1 若因为轮数用尽而**提前**结束，总墙钟还没到 → 会再开一个。
    #
    # 🔒 这里包一层 `_run_session`：跑满 N 个之后把总墙钟置 0，
    #    下一次循环判断必然成立 → 立刻退出。
    #    **不改 production.py**，只在本进程内替换模块全局。
    target_sessions = max(1, int(args.sessions))
    _orig_run_session = P._run_session
    _session_calls = {"n": 0}

    def _counting_run_session(rt_, bridge_, index, limit, log_, stats_):
        _session_calls["n"] += 1
        sess, bridge_, s_log = _orig_run_session(
            rt_, bridge_, index, limit, log_, stats_)
        if _session_calls["n"] >= target_sessions:
            # 让外层循环下一轮立刻退出（它读的是模块全局）
            P.TOTAL_WALL_LIMIT = 0.0
        return sess, bridge_, s_log

    P._run_session = _counting_run_session

    # ---- Task 用**用户的 Demand** 作为 description ----
    #
    # `_ensure_environment` 里的 description 是写死的（那部 10000 字小说）。
    # 这里先把 Task 建好，它就会发现「已存在」而跳过创建 ——
    # 于是用户 Demand 成为任务描述，而 Workspace / Stage / DoD 仍然由
    # **原函数**照常建立（我们只是包一层，不改它的任何逻辑）。
    _orig_ensure = P._ensure_environment

    def _ensure_environment(rt):
        from core.task import Task

        if rt.tasks.get(P.TASK_ID) is None:
            rt.tasks.save(Task.create(
                creator=P.PRODUCER,              # ADR-0024：Creator 始终是 Agent
                description=P.DEMAND,
                workspace_id=P.WS_ID,
                task_id=P.TASK_ID,
                participants=list(P.AGENTS),
                note=f"user_demand:{run_id}",
            ))
        _orig_ensure(rt)

    P._ensure_environment = _ensure_environment

    print("=" * 68)
    print("  用户 Demand → 多 Agent 协作生产")
    print("=" * 68)
    print(f"  Demand  : {demand[:100]}{'…' if len(demand) > 100 else ''}")
    print(f"  参与者  : {', '.join(agents)}")
    print(f"  墙钟上限: {P.TOTAL_WALL_LIMIT / 60:.1f} 分钟 "
          f"（单 Session {P.SESSION_LIMIT / 60:.1f} 分钟，最多 "
          f"{P.MAX_ROUNDS_PER_SESSION} 轮/Session）")
    print(f"  Session : 最多 {target_sessions} 个（精确生效）")
    print(f"  预算    : {P.BUDGET_AIC} AIC")
    print(f"  认知预算: {P.COGNITIVE_BUDGET} tokens/次（输出上限，"
          f"reasoning 与正文共享）")
    print(f"  输出    : {outdir}")
    print(f"  数据库  : {db_path}")
    print("=" * 68)
    print()

    rc = P.main()

    print()
    print("=" * 68)
    print("  产物位置")
    print("=" * 68)
    print(f"  结果 JSON : {results_dir / 'heterogeneous_production_result.json'}")
    print(f"  数据库    : {db_path}")
    print(f"  Demand    : {outdir / 'demand.txt'}")
    (outdir / "demand.txt").write_text(demand, encoding="utf-8")

    # 让用户能直接看到「Agent 到底写了什么」
    try:
        import sqlite3
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "SELECT created_at, agent_id, finish_reason, total_tokens "
            "FROM llm_calls ORDER BY created_at").fetchall()
        con.close()
        print(f"  模型调用  : {len(rows)} 次")
        for r in rows[:10]:
            print(f"    {r[0][11:19]}  {r[1]:22s} {r[2]:8s} {r[3]} tok")
        if len(rows) > 10:
            print(f"    …（共 {len(rows)} 次）")
    except Exception as exc:                                  # noqa: BLE001
        print(f"  （读取调用记录失败：{exc}）")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())

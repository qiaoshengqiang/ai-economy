#!/usr/bin/env python3
"""
5-Agent 协作实验：深空空间站管理控制台
=========================================

Human Command 是唯一任务输入。五个 Agent 各自有一个**中性工作区**
（work_a … work_e，不预设“前端/后端/测试”角色），可以：

    · 读写 Task Workspace（各自区域 + 读取全 Task）
    · 互相发消息 / 指派验收
    · 用 run_tool 实际执行 python / pytest / node / playwright
    · 自行决定项目结构、分工、技术方案

此脚本只提供环境与人类命令，不规定团队结构。

用法：
    ./.venv/bin/python experiments/tool_collab/run_space_station.py \
        --minutes 20 --session-minutes 5 --rounds 3 --budget 30
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

HUMAN_COMMAND = """# 项目任务：深空空间站管理控制台

制作一个完整、可实际操作的科幻空间站管理网页。

网页需要具有专业的科幻控制中心视觉风格，采用深色界面，包含空间站运行总览、
能源与氧气监控、资源库存管理、设备控制和事件日志等功能。

这不是一个静态展示页面，而是一个具有真实交互和数据联动的管理系统。
例如，操作设备会影响能源消耗，资源变化会同步更新库存数据，设备异常会产生事件记录。

项目需要包含前端页面、必要的后端服务和数据接口，并能够实际运行。

请自行组织团队、分配任务、协商技术方案，充分利用可用的代码执行和浏览器测试工具，
最终交付一个完整、稳定、可演示的网页项目。"""

AGENTS = ["agent_a", "agent_b", "agent_c", "agent_d", "agent_e"]

#: 中性工作区：不给“前端/后端/测试”预设语义。
#: 每个 Agent 在自己的区域里有写权限；读取整个 Task 不受锁限制。
NAMESPACES = {
    "work_a": "agent_a",
    "work_b": "agent_b",
    "work_c": "agent_c",
    "work_d": "agent_d",
    "work_e": "agent_e",
}

#: 生产/验收角色是**流程需要**，不是任务角色分配。
#: 其他 Agent 仍然可以自行协商、改写分工或重新指派验收。
PRODUCER = "agent_b"
ACCEPTOR = "agent_a"
OWNER = "agent_a"

#: 对照条件：显式角色脚手架（不是自主分工）。
GUIDED_NAMESPACES = {
    "docs": "agent_a",
    "backend": "agent_b",
    "frontend": "agent_c",
    "tests": "agent_d",
    "qa": "agent_e",
}

GUIDED_ROLE_PROMPTS = {
    "agent_a": (
        "你是本次项目的协调者/架构师。优先在 docs/ 写出最小架构、接口契约、"
        "分工与验收清单，并主动给其他 Agent 发消息推动落地。"
        "契约必须写明：后端入口文件路径、`/api/state` 的字段形状、"
        "前端读取哪些字段。契约一旦冻结，其他人只读。"),
    "agent_b": (
        "你是后端实现者，也是本 Task 的**生产者**。严格按 docs/ 契约的入口路径"
        "与字段名实现可启动的 HTTP 服务；不要另建一套平行目录。"
        "用 run_tool 验证。交付物就绪后，**必须由你自己**按结项协议发最小 SUBMIT"
        "（runner 不会代发）；先发最小合法 payload，再补说明。"),
    "agent_c": (
        "你是前端实现者。在 frontend/ 实现控制台页面，字段名严格按 docs/ 契约；"
        "后端字段缺失时在代码注释/验收材料里如实写明，不要用占位 0 伪装成功。"
        "需要浏览器测试时配合 tests/。"),
    "agent_d": (
        "你是测试实现者。在 tests/ 写测试，并**实际启动契约指定的入口**；"
        "断言 `/api/state` 的字段形状（power/life/resources/devices/events），"
        "只断言 HTTP 200 不算通过。通过 run_tool 实际执行；"
        "测试必须覆盖前后端联动。"),
    "agent_e": (
        "你是独立验收人。读取交付物与测试结果，写 qa/review.md；"
        "实际运行测试并打开页面核对数据，不能只看“测试通过”。"
        "若契约字段缺失或页面数据是占位 0，不能发 VERIFY(passed=true)。"
        "通过 VERIFY(passed=true) 然后是 COMPLETE 推进结项（两步都由你发）；"
        "不实现页面。"),
}

DEFINITION_OF_DONE = (
    "前端页面存在且可在浏览器打开",
    "后端服务/数据接口存在且可启动（启动的是契约指定的入口）",
    "`/api/state` 包含契约字段（power/life/resources/devices/events）",
    "页面与后端数据接口有真实联动（不是占位 0）",
    "至少一次 run_tool 实际执行测试",
    "测试断言契约字段形状，而不只是 HTTP 200",
    "测试通过（exit_code=0）或由验收人明确记录未通过项",
    "至少一个非实现者写出独立验收结论",
)


def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="run_space_station.py",
        description="5 个 Agent 协作完成深空空间站管理控制台",
    )
    ap.add_argument(
        "--minutes", type=float, default=30.0,
        help="总墙钟；最后 --final-window-ratio 进入收尾窗口")
    ap.add_argument("--session-minutes", type=float, default=6.0)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--budget", type=float, default=30.0)
    ap.add_argument("--cognitive-budget", type=int, default=16384)
    ap.add_argument(
        "--fast-fallback", action="store_true",
        help="把 agent_d / agent_e 的 brain 临时切到 agent_b 的 doubao 端点；"
             "用于它们的已配置 provider 在本任务上超时的场景")
    ap.add_argument(
        "--guided", action="store_true",
        help="提供最小角色脚手架（协调/后端/前端/测试/QA + 语义命名空间）；"
             "用于对照自主分工失败的情况")
    ap.add_argument(
        "--no-follow-up", action="store_true",
        help="关闭“读完当场回喂”。仅在所有 provider 都能快速返回时使用；"
             "关闭后 Agent 下一轮只看到读取元数据，容易重复读")
    ap.add_argument(
        "--final-window-ratio", type=float, default=0.2,
        help="墙钟最后这一比例进入收尾窗口：实现者停写，只留生产者/验收人/协调者")
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
    db = outdir / "space.db"

    from experiments.heterogeneous_production import production as P

    task_id = f"task_space_{run_id}"
    ws_id = f"WS-SPACE-{run_id}"

    P.DEMAND = HUMAN_COMMAND.strip()
    P.AGENTS = list(AGENTS)
    P.OWNER = OWNER
    P.PRODUCER = PRODUCER
    P.ACCEPTOR = ACCEPTOR
    P.TASK_ID = task_id
    P.WS_ID = ws_id
    P.DB = db
    P.RESULTS = results
    P.EXPERIMENT_ID = f"space_station_{run_id}"
    P.ROOT_STAGE_ID = f"stage_{task_id}_root"
    namespaces = dict(GUIDED_NAMESPACES if args.guided else NAMESPACES)
    P.ARTIFACT_NAMESPACES = namespaces
    P.DEFINITION_OF_DONE = DEFINITION_OF_DONE
    P.AGENT_DRIVEN_COMPLETION = True
    # 自主条件：验收人自行决定写在哪里。
    # 脚手架条件：固定 qa/review.md，便于观察验收是否真的发生。
    P.COMPLETION_QA_ARTIFACT = "qa/review.md" if args.guided else ""
    if args.guided:
        P.PRODUCER = "agent_b"
        P.ACCEPTOR = "agent_e"
    P.BUDGET_AIC = float(args.budget)
    P.TOTAL_WALL_LIMIT = max(60.0, args.minutes * 60.0)
    P.SESSION_LIMIT = max(30.0, args.session_minutes * 60.0)
    P.MAX_ROUNDS_PER_SESSION = max(1, args.rounds)
    P.COGNITIVE_BUDGET = int(args.cognitive_budget)
    # 收尾窗口：最后 20% 墙钟只留生产者 / 验收人 / 协调者。
    # 自主模式没有角色映射，保持空（不跳过任何人）。
    P.FINAL_WINDOW_RATIO = max(0.0, min(0.5, args.final_window_ratio))
    P.FINAL_WINDOW_SKIP_AGENTS = (
        ("agent_c", "agent_d") if args.guided else ())
    # 🔴 默认**开启**读取回喂。关闭后 Agent 下一轮只看到“读过”的元数据，
    #    看不到文档内容，会反复读同一批文件 —— 实测 5-Agent guided 卡死。
    #    慢 provider 由 --fast-fallback 处理，而不是靠关闭回喂。
    P.FOLLOW_UP_ON_READS = not args.no_follow_up
    P.MIN_PRODUCTIVE_CHARS_PER_SESSION = 200

    # 可选：把已配置 provider 在本任务上持续超时的 Agent 临时切到
    # agent_b 的 doubao 端点；可选注入角色脚手架。
    if args.fast_fallback or args.guided:
        import main as M

        _orig_build = M.build_runtime

        def _build_with_overrides(*a, **kw):
            rt = _orig_build(*a, **kw)
            if args.fast_fallback:
                fast = rt.economy.agent("agent_b").brain.settings
                for aid in ("agent_d", "agent_e"):
                    brain = rt.economy.agent(aid).brain
                    brain.settings.provider = fast.provider
                    brain.settings.model = fast.model
                    brain.settings.credential_ref = fast.credential_ref
                    brain._credential_config = {
                        "api_key_env": "DOUBAO_API_KEY"}
                    brain._credential_cache.clear()
            if args.guided:
                for aid, role in GUIDED_ROLE_PROMPTS.items():
                    brain = rt.economy.agent(aid).brain
                    brain.settings.system_prompt = (
                        f"{brain.settings.system_prompt}\n\n{role}")
            return rt

        M.build_runtime = _build_with_overrides

    print("=" * 72)
    print("  5-Agent 协作实验：深空空间站管理控制台")
    print("=" * 72)
    print(f"  Human Command: {HUMAN_COMMAND.splitlines()[0]}")
    print(f"  参与者: {', '.join(AGENTS)}")
    print(f"  模式: {'GUIDED 角色脚手架' if args.guided else 'AUTONOMY 无角色预设'}")
    print(f"  工作区: " + " · ".join(f"{k}→{v}" for k, v in namespaces.items()))
    print(f"  生产者: {P.PRODUCER}   验收人: {P.ACCEPTOR}")
    print(f"  墙钟: {P.TOTAL_WALL_LIMIT/60:.1f} 分钟 "
          f"（Session {P.SESSION_LIMIT/60:.1f} 分钟，{P.MAX_ROUNDS_PER_SESSION} 轮）")
    print(f"  收尾窗口: 最后 {P.FINAL_WINDOW_RATIO*100:.0f}%"
          f"（跳过: {', '.join(P.FINAL_WINDOW_SKIP_AGENTS) or '无'}）")
    print(f"  预算: {P.BUDGET_AIC} AIC")
    if args.fast_fallback:
        print("  fallback: agent_d / agent_e → agent_b 的 doubao 端点"
              "（仅推理端点；身份/prompt 不变）")
    print(f"  输出: {outdir}")
    print("=" * 72)
    print()

    try:
        rc = P.main()
    finally:
        print()
        print("=" * 72)
        print("  产物位置")
        print("=" * 72)
        print(f"  数据库     : {db}")
        print(f"  结果 JSON  : {results / 'heterogeneous_production_result.json'}")
        print(f"  工具运行   : data/tool_runs/")
        print(f"  查看产物   : ./.venv/bin/python experiments/user_demand/view_result.py "
              f"--path {outdir}")
        print("=" * 72)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

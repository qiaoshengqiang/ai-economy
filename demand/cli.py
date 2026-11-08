"""
Demand CLI —— 人类直接给 Agent A 一句话
=========================================

用法：

    ./dev.sh demand --file /tmp/prompt.txt --minutes 20 --budget 20

    # 或直接给一句话
    ./dev.sh demand --demand "做一个 ..." --minutes 10

它会：
    1. 启动真实 Runtime（Economy / Task / Workspace / Communication / Tools）
    2. 创建 DemandRun（不是 Task）
    3. 让 front agent 收到 Demand + Agent Directory
    4. Agent 自己调用 task_create
    5. RunScheduler 串行跑 participants
    6. 输出 run 目录 / Task 状态 / 成本 / 交付物
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")

from demand.models import DemandRun                               # noqa: E402
from demand.runner import DemandRunner                            # noqa: E402


class _Tee:
    """把 stdout 同时写到终端和 `run.log`（不改变原有输出）。"""

    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            try:
                stream.write(text)
                stream.flush()
            except Exception:                                 # noqa: BLE001
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:                                 # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False


def _read_demand(args: argparse.Namespace) -> str:
    if args.demand and args.file:
        raise SystemExit("--demand 和 --file 只能二选一")
    if args.demand:
        return str(args.demand).strip()
    if args.file:
        return Path(args.file).expanduser().read_text(encoding="utf-8").strip()
    raise SystemExit("必须提供 --demand 或 --file")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="demand",
        description="人类 Demand → Agent 自己建 Task → 串行执行")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--demand", help="直接给一段 Demand")
    src.add_argument("--file", help="从文本文件读 Demand")
    ap.add_argument("--minutes", type=float, default=20.0,
                    help="总墙钟上限（分钟），默认 20")
    ap.add_argument("--budget", type=float, default=20.0,
                    help="生产预算（AIC，观测用），默认 20")
    ap.add_argument("--max-turns", type=int, default=30,
                    help="最多 Agent turn 数，默认 30")
    ap.add_argument("--max-agents", type=int, default=5,
                    help="参与者上限，默认 5")
    ap.add_argument("--front-agent", default="agent_a",
                    help="接收 Human Demand 的 Agent，默认 agent_a")
    ap.add_argument("--cognitive-budget", type=int, default=32768,
                    help="单次响应的**硬上限**（tokens）；"
                         "Agent 可以自己选择更低，默认 32768")
    ap.add_argument("--db", default=None,
                    help="Runtime 数据库路径；默认放在 run 目录下")
    ap.add_argument("--out", default=None,
                    help="run 输出目录；默认 runs/demand/<timestamp>-<id>")
    ap.add_argument("--quiet", action="store_true",
                    help="只打印最终结果")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    demand = _read_demand(args)
    if not demand:
        raise SystemExit("Demand 不能为空")
    if int(args.cognitive_budget) <= 0:
        raise SystemExit("--cognitive-budget 必须是正整数")

    run_id = f"demand_{uuid.uuid4().hex[:8]}"
    if args.out:
        run_dir = Path(args.out).expanduser().resolve()
    else:
        run_dir = ROOT / "runs" / "demand" / (
            f"{datetime.now():%Y%m%d-%H%M%S}-{run_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = (Path(args.db).expanduser().resolve()
               if args.db else run_dir / "demand.db")
    log_path = run_dir / "run.log"

    from main import build_runtime

    log_file = log_path.open("a", encoding="utf-8", buffering=1)
    file_handler = None
    try:
        # 🔒 终端输出 + 日志同时进 `run.log`：
        #    logging 的 console handler 可能已经绑定旧 stdout，
        #    所以下面按需再挂一个文件 handler（避免重复写）。
        with contextlib.redirect_stdout(_Tee(sys.stdout, log_file)):
            runtime = build_runtime(db_path=str(db_path))
            logger = logging.getLogger("ai_economy")
            already_teeing = any(
                isinstance(h, logging.StreamHandler)
                and getattr(h, "stream", None) is sys.stdout
                for h in logger.handlers)
            if not already_teeing:
                file_handler = logging.FileHandler(
                    log_path, encoding="utf-8")
                file_handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"))
                logger.addHandler(file_handler)
            try:
                try:
                    front = runtime.economy.agent(args.front_agent)
                except Exception:                             # noqa: BLE001
                    front = None
                if front is None or not getattr(front, "has_brain", False):
                    print(f"❌ front agent {args.front_agent!r} "
                          f"不存在或没有 Brain")
                    return 2

                run = DemandRun(
                    run_id=run_id,
                    demand=demand,
                    budget_aic=float(args.budget),
                    wall_limit_seconds=max(60.0,
                                           float(args.minutes) * 60.0),
                    max_turns=max(1, int(args.max_turns)),
                    max_agents=max(2, int(args.max_agents)),
                    front_agent=str(args.front_agent),
                    run_dir=str(run_dir),
                )
                if not args.quiet:
                    print("=" * 68)
                    print("  AI Economy OS — Demand")
                    print("=" * 68)
                    print(f"  Run        : {run_id}")
                    print(f"  Front Agent: {args.front_agent}")
                    print(f"  Wall       : "
                          f"{run.wall_limit_seconds / 60:.1f} min")
                    print(f"  Budget     : {run.budget_aic} AIC (observe)")
                    print(f"  Cognitive  : "
                          f"{args.cognitive_budget} tokens (hard cap)")
                    print(f"  Output     : {run_dir}")
                    print("=" * 68)
                    print()

                result = DemandRunner(
                    runtime, run,
                    cognitive_budget=int(args.cognitive_budget),
                    quiet=bool(args.quiet)).execute()
                run_state = (result or {}).get("run", {})
                return 0 if run_state.get("status") == "COMPLETED" else 1
            finally:
                try:
                    runtime.close()
                except Exception:                             # noqa: BLE001
                    pass
    finally:
        if file_handler is not None:
            try:
                logging.getLogger("ai_economy").removeHandler(file_handler)
                file_handler.close()
            except Exception:                                 # noqa: BLE001
                pass
        log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())

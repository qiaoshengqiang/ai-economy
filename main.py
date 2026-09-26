#!/usr/bin/env python3
"""
AI Economy OS v0.1 —— 启动入口
============================================================

运行方式：

    python3 main.py

本阶段（Step 1）它会做这些事：

    1. 读取全局配置 config/config.yaml
    2. 读取 .env（如果有的话）
    3. 连接 / 创建 SQLite 数据库 data/economy.db
    4. 建表
    5. 创建 Agent A（读取它自己的配置、建钱包、发 100 AI）
    6. 创建 Agent B（同上）
    7. 对账，检查账本没被写坏
    8. 打印启动横幅

注意：本文件里【没有任何模型厂商的名字】。
Agent 用哪家模型，是配置文件里的事，不是启动程序的事。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ------------------------------------------------------------
# 让 Python 能找到项目内的模块
#
# 为什么需要这个？
#   直接运行 `python3 main.py` 时，Python 把 main.py 所在的目录
#   加进搜索路径。但如果我们从别的地方运行，或者用 pytest，
#   就要手动确保项目根在路径里。
# ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_global_config                     # noqa: E402
from core.economy import Economy                                # noqa: E402
from core.logging_setup import get_logger, setup_logging        # noqa: E402
from core.paths import ensure_runtime_dirs                      # noqa: E402

logger = get_logger(__name__)

# 本版本要创世的两个 Agent。
# 以后要加 Agent C，只需要在这里加一行 + 建一个目录：
#     DEFAULT_AGENTS = ["agent_a", "agent_b", "agent_c"]
DEFAULT_AGENTS = ["agent_a", "agent_b"]


# ============================================================
# 横幅
# ============================================================
BANNER_WIDTH = 32


def print_banner(name: str, version: str) -> None:
    """打印系统标题。"""
    print()
    print("=" * BANNER_WIDTH)
    print(f"  {name} v{version}".center(BANNER_WIDTH))
    print("=" * BANNER_WIDTH)
    print()


def print_agents(economy: Economy) -> None:
    """打印每个 Agent 的钱包状况。"""
    for agent in economy.all_agents():
        print(agent.name)
        print(f"Wallet: {agent.balance:g} {economy.currency}")
        print()


def print_status(economy: Economy) -> None:
    """打印系统状态。"""
    audit = economy.audit()
    ready = economy.is_ready()

    status_text = "READY" if ready else "NOT READY"
    print(f"System status: {status_text}")

    # 调试信息放到下面，不干扰主输出
    print()
    print("-" * BANNER_WIDTH)
    print(f"Database:  {economy.db.path}")
    print(f"Currency:  {economy.currency}")
    print(
        f"Exchange:  1 {economy.currency} = "
        f"{economy.tokens_per_ai_coin:,} tokens"
    )
    print(f"Ledger:    {audit['transactions']} transactions")
    print(
        f"Audit:     {'OK' if audit['balanced'] else 'FAILED'} "
        f"(supply={audit['total_supply']:g}, "
        f"wallets={audit['wallets_total']:g})"
    )
    print("-" * BANNER_WIDTH)
    print()


# ============================================================
# 主流程
# ============================================================
def build_economy(db_path: str | None = None) -> Economy:
    """
    构建经济系统并创建两个 Agent。

    这个函数被 main() 和 pytest 共用，
    所以它不打印任何东西 —— 打印是 main() 的事。
    这是一个好习惯：让核心逻辑和展示逻辑分开。
    """
    ensure_runtime_dirs()

    config = load_global_config()
    setup_logging(level=str(config.get("system.log_level", "INFO")))

    economy = Economy.bootstrap(db_path=db_path, config=config)

    for agent_id in DEFAULT_AGENTS:
        economy.create_agent(agent_id)

    return economy


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AI Economy OS v0.1",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="数据库文件路径（默认读 config/config.yaml 里的 storage.database_path）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只打印最简信息（给脚本和测试用）",
    )
    args = parser.parse_args(argv)

    try:
        economy = build_economy(db_path=args.db)
    except Exception as exc:  # noqa: BLE001
        print()
        print("!! 启动失败 !!")
        print(f"错误类型: {type(exc).__name__}")
        print(f"错误信息: {exc}")
        print()
        print("请把上面这几行完整复制给我，我来修。")
        print()
        return 1

    if args.quiet:
        for agent in economy.all_agents():
            print(f"{agent.name}: {agent.balance:g} {economy.currency}")
        return 0

    system_name = str(economy.config.get("system.name", "AI Economy OS"))
    system_version = str(economy.config.get("system.version", "0.1"))

    print_banner(system_name, system_version)
    print_agents(economy)
    print_status(economy)

    economy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
AI Economy OS v0.1 —— 启动入口 & 命令行工具
============================================================

【基本运行】

    python3 main.py
        → 创建 / 加载 Agent，打印启动横幅

【命令行工具】

    python3 main.py transfer agent_a agent_b 20
        → 让 Agent A 付 20 AI 给 Agent B（这是经济系统的执行入口）

    python3 main.py balances
        → 查看所有 Agent 的余额

    python3 main.py ledger
        → 查看交易流水

    python3 main.py audit
        → 对账，检查账本有没有被写坏

更短的写法（推荐，不用管虚拟环境）：
    ./dev.sh transfer agent_a agent_b 20
    ./dev.sh balances
    ./dev.sh ledger

【本文件里没有任何模型厂商的名字】
    Agent 用哪家模型是配置文件的事，不是启动程序的事。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

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
from core.economy import AgentNotFoundError, Economy, EconomyError  # noqa: E402
from core.ledger import LedgerError, format_amount              # noqa: E402
from core.logging_setup import get_logger, setup_logging        # noqa: E402
from core.paths import ensure_runtime_dirs                      # noqa: E402

logger = get_logger(__name__)

# 本版本要创世的两个 Agent。
# 以后要加 Agent C，只需要在这里加一行 + 建一个目录：
#     DEFAULT_AGENTS = ["agent_a", "agent_b", "agent_c"]
DEFAULT_AGENTS = ["agent_a", "agent_b"]


# ============================================================
# 金额格式化
# ============================================================
BANNER_WIDTH = 32


def fmt(value) -> str:
    """格式化金额。统一入口，保证显示风格一致，且永远不会变成科学计数法。"""
    return format_amount(value)


def print_banner(name: str, version: str) -> None:
    """打印系统标题。"""
    print()
    print("=" * BANNER_WIDTH)
    print(f"  {name} v{version}".center(BANNER_WIDTH))
    print("=" * BANNER_WIDTH)
    print()


def print_agents(economy: Economy) -> None:
    """打印每个 Agent 的钱包状况（启动横幅用）。"""
    for agent in economy.all_agents():
        print(agent.name)
        print(f"Wallet: {fmt(agent.balance)} {economy.currency}")
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
        f"(supply={fmt(audit['total_supply'])}, "
        f"wallets={fmt(audit['wallets_total'])})"
    )
    print("-" * BANNER_WIDTH)
    print()


# ============================================================
# 经济系统构建
# ============================================================
def build_economy(db_path: Optional[str] = None) -> Economy:
    """
    构建经济系统并创建两个 Agent。

    这个函数被命令行和 pytest 共用，
    所以它不打印任何东西 —— 打印是命令处理函数的事。
    这是一个好习惯：让核心逻辑和展示逻辑分开。
    """
    ensure_runtime_dirs()

    config = load_global_config()
    setup_logging(level=str(config.get("system.log_level", "INFO")))

    economy = Economy.bootstrap(db_path=db_path, config=config)

    for agent_id in DEFAULT_AGENTS:
        economy.create_agent(agent_id)

    return economy


# ============================================================
# 命令：start（默认）
# ============================================================
def cmd_start(economy: Economy, args: argparse.Namespace) -> int:
    """打印启动横幅。这是不加任何参数时的默认行为。"""
    if getattr(args, "quiet", False):
        for agent in economy.all_agents():
            print(f"{agent.name}: {fmt(agent.balance)} {economy.currency}")
        return 0

    system_name = str(economy.config.get("system.name", "AI Economy OS"))
    system_version = str(economy.config.get("system.version", "0.1"))

    print_banner(system_name, system_version)
    print_agents(economy)
    print_status(economy)
    return 0


# ============================================================
# 命令：transfer
# ============================================================
def cmd_transfer(economy: Economy, args: argparse.Namespace) -> int:
    """
    让一个 Agent 向另一个 Agent 转钱。

    这是「控制 Agent 花钱」的入口。
    注意它调用的不是钱包，而是 Economy.transfer() —— OS 级校验。
    """
    # 允许用 "a" / "Agent A" / "agent_a" 任意一种写法
    from_id = economy.resolve_agent_id(args.from_agent)
    to_id = economy.resolve_agent_id(args.to_agent)

    from_agent = economy.agent(from_id)
    to_agent = economy.agent(to_id)

    # 转账前先记下余额，好在输出里展示变化
    before_from = from_agent.balance
    before_to = to_agent.balance

    # ---- 真正执行 ----
    tx = economy.transfer(
        from_agent_id=from_id,
        to_agent_id=to_id,
        amount=args.amount,
        reason=args.reason,
        task_id=args.task_id,
    )

    after_from = from_agent.balance
    after_to = to_agent.balance
    currency = economy.currency

    # ---- 展示结果 ----
    print()
    print("转账成功")
    print("-" * 46)
    print(f"  付款方:  {from_agent.name} ({from_id})")
    print(f"  收款方:  {to_agent.name} ({to_id})")
    print(f"  金额:    {fmt(tx.amount)} {currency}")
    print(f"  原因:    {tx.reason}")
    if tx.task_id:
        print(f"  任务:    {tx.task_id}")
    print(f"  交易号:  {tx.tx_id}")
    print(f"  时间:    {tx.created_at}")
    print()
    print("余额变化")
    print("-" * 46)
    print(
        f"  {from_agent.name}:  "
        f"{fmt(before_from)} -> {fmt(after_from)} {currency}"
    )
    print(
        f"  {to_agent.name}:  "
        f"{fmt(before_to)} -> {fmt(after_to)} {currency}"
    )
    print()

    # 每次经济动作后都对一次账。做错了立刻能发现。
    audit = economy.audit()
    audit_text = "OK" if audit["balanced"] else "FAILED"
    print(
        f"账本审计: {audit_text}   "
        f"(货币总量 {fmt(audit['total_supply'])} {currency}, "
        f"共 {audit['transactions']} 笔交易)"
    )
    print()

    return 0


# ============================================================
# 命令：balances
# ============================================================
def cmd_balances(economy: Economy, args: argparse.Namespace) -> int:
    """查看所有 Agent 的余额。"""
    agents = economy.all_agents()
    currency = economy.currency

    print()
    print("Agent 余额")
    print("-" * 56)
    print(f"  {'Agent':<14} {'ID':<12} {'余额':>12}  {'状态'}")
    print("-" * 56)

    total = 0.0
    for agent in agents:
        total += agent.balance
        print(
            f"  {agent.name:<14} {agent.agent_id:<12} "
            f"{fmt(agent.balance):>12} {currency}  {agent.status}"
        )

    print("-" * 56)
    print(f"  {'合计':<28} {fmt(total):>12} {currency}")
    print()

    return 0


# ============================================================
# 命令：ledger
# ============================================================
def cmd_ledger(economy: Economy, args: argparse.Namespace) -> int:
    """查看交易流水。"""
    agent_id = None
    if args.agent:
        agent_id = economy.resolve_agent_id(args.agent)

    transactions = economy.ledger.history(agent_id=agent_id, limit=args.limit)

    print()
    if agent_id:
        print(f"交易流水 —— {economy.agent(agent_id).name}")
    else:
        print("交易流水 —— 全部")
    print("-" * 78)

    if not transactions:
        print("  （还没有任何交易）")
        print()
        return 0

    print(f"  {'时间':<20} {'类型':<14} {'从 → 到':<26} {'金额':>10}")
    print("-" * 78)

    for tx in transactions:
        source = tx.from_agent or "∅铸币"
        target = tx.to_agent or "∅销毁"
        flow = f"{source} → {target}"
        time_short = tx.created_at[:19].replace("T", " ")

        print(
            f"  {time_short:<20} {tx.tx_type:<14} "
            f"{flow:<26} {fmt(tx.amount):>10} {tx.asset}"
        )
        if tx.reason:
            print(f"      原因: {tx.reason}   交易号: {tx.tx_id}")

    print("-" * 78)
    print(f"  共 {len(transactions)} 笔")
    print()

    return 0


# ============================================================
# 命令：audit
# ============================================================
def cmd_audit(economy: Economy, args: argparse.Namespace) -> int:
    """
    对账：检查「账本里的货币总量」是否对得上。

    这是发现经济系统 bug 最重要的工具。
    任何绕过账本直接改余额的代码，都会让这里 FAILED。
    """
    audit = economy.audit()
    currency = economy.currency

    print()
    print("账本对账")
    print("-" * 46)
    print(f"  交易笔数:      {audit['transactions']}")
    print(f"  货币总量:      {fmt(audit['total_supply'])} {currency}")
    print(f"  Agent 钱包合计: {fmt(audit['wallets_total'])} {currency}")
    print(f"  系统金库:      {fmt(audit['treasury'])} {currency}")
    print(f"  差额:          {fmt(audit['difference'])} {currency}")
    print("-" * 46)

    if audit["balanced"]:
        print("  结果: OK —— 账本一致，没有凭空多出或消失的货币")
        print()
        return 0

    print("  结果: FAILED —— 账本对不上！")
    print()
    print("  这说明有代码绕过了账本直接修改余额，")
    print("  或者是铸币/转账逻辑有 bug。请立刻检查。")
    print()
    return 1


# ============================================================
# 命令行参数
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    """
    构建命令行解析器。

    设计说明：
        --db / --quiet 放在一个"共享父解析器"里，
        并给它们 default=argparse.SUPPRESS。

        为什么要 SUPPRESS？
            这样「没写这个参数」时属性根本不会出现，
            于是无论你把 --db 写在子命令前面还是后面，都能生效：

                python main.py --db test.db transfer a b 20   ✓
                python main.py transfer a b 20 --db test.db   ✓

            如果不用 SUPPRESS，子解析器会用默认值把前面设好的值覆盖掉，
            这就是 argparse 里著名的 parents 陷阱。
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="数据库文件路径（默认读 config/config.yaml 里的 storage.database_path）",
    )
    shared.add_argument(
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="只打印最简信息",
    )

    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AI Economy OS v0.1 —— Agent 经济系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[shared],
        epilog="""
例子:
  python3 main.py                                  启动系统，打印横幅
  python3 main.py transfer agent_a agent_b 20      让 A 付 20 AI 给 B
  python3 main.py transfer a b 20 --reason 定金     简写，带原因
  python3 main.py balances                         查看余额
  python3 main.py ledger --agent a                 查看 A 的流水
  python3 main.py audit                            对账
        """,
    )

    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    # --- start ---
    subparsers.add_parser(
        "start",
        parents=[shared],
        help="启动系统，打印横幅（不加命令时的默认行为）",
    ).set_defaults(func=cmd_start)

    # --- transfer ---
    transfer = subparsers.add_parser(
        "transfer",
        parents=[shared],
        help="让一个 Agent 向另一个 Agent 转钱",
    )
    transfer.add_argument(
        "from_agent",
        metavar="付款方",
        help="付款方的 agent_id、名字或简写（例如 agent_a / 'Agent A' / a）",
    )
    transfer.add_argument(
        "to_agent",
        metavar="收款方",
        help="收款方（同上）",
    )
    transfer.add_argument(
        "amount",
        metavar="金额",
        type=float,
        help="转账金额，必须大于 0",
    )
    transfer.add_argument(
        "--reason",
        default="manual_transfer",
        help="转账原因，会写进账本（默认 manual_transfer）",
    )
    transfer.add_argument(
        "--task-id",
        default=None,
        help="关联的任务 ID（可选）",
    )
    transfer.set_defaults(func=cmd_transfer)

    # --- balances ---
    subparsers.add_parser(
        "balances",
        parents=[shared],
        help="查看所有 Agent 的余额",
    ).set_defaults(func=cmd_balances)

    # --- ledger ---
    ledger = subparsers.add_parser(
        "ledger",
        parents=[shared],
        help="查看交易流水",
    )
    ledger.add_argument(
        "--agent",
        default=None,
        help="只看某个 Agent 的流水（可选）",
    )
    ledger.add_argument(
        "--limit",
        type=int,
        default=50,
        help="最多显示几笔（默认 50）",
    )
    ledger.set_defaults(func=cmd_ledger)

    # --- audit ---
    subparsers.add_parser(
        "audit",
        parents=[shared],
        help="对账，检查账本有没有被写坏",
    ).set_defaults(func=cmd_audit)

    return parser


# ============================================================
# 入口
# ============================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 不加任何命令时，默认执行 start（保持 `python main.py` 可用）
    if getattr(args, "command", None) is None:
        args.func = cmd_start

    # --quiet 在 start 之外的命令里没有意义，忽略掉即可
    db_path = getattr(args, "db", None)

    try:
        economy = build_economy(db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        print()
        print("!! 启动失败 !!")
        print(f"错误类型: {type(exc).__name__}")
        print(f"错误信息: {exc}")
        print()
        print("请把上面这几行完整复制给我，我来修。")
        print()
        return 1

    try:
        return args.func(economy, args)

    except (AgentNotFoundError, EconomyError, LedgerError) as exc:
        # 这些是「经济规则拒绝了这次操作」，属于预期内的失败，
        # 不是程序 bug。所以要给出清晰、友好的提示，而不是抛堆栈。
        print()
        print("操作被经济系统拒绝")
        print("-" * 46)
        print(f"  {exc}")
        print()
        return 2

    except Exception as exc:  # noqa: BLE001
        print()
        print("!! 执行出错 !!")
        print(f"错误类型: {type(exc).__name__}")
        print(f"错误信息: {exc}")
        print()
        print("请把上面这几行完整复制给我，我来修。")
        print()
        return 1

    finally:
        economy.close()


if __name__ == "__main__":
    raise SystemExit(main())

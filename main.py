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
import asyncio
import sys
from dataclasses import dataclass
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

from agents.brain import Brain, BrainSettings                      # noqa: E402
from core.config import load_global_config                     # noqa: E402
from core.economy import AgentNotFoundError, Economy, EconomyError  # noqa: E402
from core.ledger import LedgerError, format_amount              # noqa: E402
from core.logging_setup import get_logger, setup_logging        # noqa: E402
from core.paths import ensure_runtime_dirs                      # noqa: E402
from llm import LLMCallRecorder, LLMRouter, LLMError            # noqa: E402
from communication import (                                     # noqa: E402
    CommunicationManager,
    LocalTransport,
    MessageType,
    ReceiveStatus,
    Transport,
)

logger = get_logger(__name__)

# 本版本要创世的两个 Agent。
# 以后要加 Agent C，只需要在这里加一行 + 建一个目录：
#     DEFAULT_AGENTS = ["agent_a", "agent_b", "agent_c"]
DEFAULT_AGENTS = ["agent_a", "agent_b"]


# ============================================================
# 运行时容器
# ============================================================
@dataclass
class Runtime:
    """
    把一次运行需要的东西装在一起。

    【为什么要有这个容器】

        程序启动时要组装这些部件：

            Economy              经济系统（钱包、账本）
            LLMRouter            智力资源路由
            Recorder             调用记录
            CommunicationManager 通信门面    ← Step 3
            Transport            通信实现     ← Step 3（当前是 LocalTransport）

        这些部件由 main.py（组合根）装配好，再交给各个命令。

        为什么要显式装起来，而不是让 Agent 自己去创建？
            因为「谁来组装」这件事很重要。
            Agent 不该知道 Router 是怎么来的、用的哪个 Provider，
            更不该知道消息是走内存还是走网络。

            组装是组合根的职责 —— 这样以后：
                · 换模型 → 只改 main.py
                · 换传输 → 只改 main.py（LocalTransport → P2PTransport）
            而 Agent / Brain / Economy 全都不用动。
    """

    economy: Economy
    router: LLMRouter
    recorder: LLMCallRecorder
    communication: CommunicationManager
    transport: Transport

    def agent(self, agent_id: str):
        return self.economy.agent(agent_id)

    def close(self) -> None:
        """
        关掉所有资源。

        为什么这里是同步的？
            因为 main() 的 finally 块是同步的。
            本地传输不持有任何需要释放的系统资源（没有 socket、没有文件句柄），
            所以用 asyncio.run 跑一下它的 close() 就够了。

            等将来接入 P2PTransport（真的持有连接）时，
            应该改成显式的 async 关闭流程，而不是硬塞进同步方法里。
        """
        self.economy.close()

        try:
            asyncio.run(self.communication.close())
        except RuntimeError:
            # 已经处在事件循环里，或循环已关闭 —— 两种情况都不该崩。
            # 本地传输没有系统资源要释放，跳过是安全的。
            logger.debug("跳过异步关闭（当前处于事件循环中）")


# ============================================================
# 横幅
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
    """
    打印每个 Agent 的钱包与大脑状况（启动横幅用）。

    Step 2 新增了 Provider / Model 两行 —— 它们直接来自 Agent 的配置，
    证明「每个 Agent 可以独立选择用哪个模型」这件事是配得上的。
    """
    for agent in economy.all_agents():
        print(agent.name)
        print(f"Wallet: {fmt(agent.balance)} {economy.currency}")

        if agent.has_brain:
            print(f"Provider: {agent.brain.provider}")
            print(f"Model: {agent.brain.model or '(默认)'}")
        else:
            print("Provider: (未装大脑)")

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
def build_runtime(db_path: Optional[str] = None) -> Runtime:
    """
    组装一次运行的完整环境：经济系统 + LLM 路由 + 调用记录 + 给 Agent 装大脑。

    【这就是「组合根」】
        注意这个函数的职责：它把各个互相不知道对方存在的部件接起来。

            Economy  不知道 LLM 的存在
            LLMRouter 不知道 Economy / Agent 的存在
            Brain     知道 Router，但不知道 Economy

        只有这里同时认识它们。所以「换 Provider」「换记录方式」
        这类改动只会落在这个函数里，不会污染任何一层。

    注意它不打印任何东西 —— 打印是命令处理函数的事。
    """
    ensure_runtime_dirs()

    config = load_global_config()
    setup_logging(level=str(config.get("system.log_level", "INFO")))

    economy = Economy.bootstrap(db_path=db_path, config=config)

    # --- LLM 层 ---
    router = LLMRouter(config)
    # 记录器需要知道当前的兑换比例，好在每条记录里存下汇率快照。
    # 注意：这里只【存快照】，不算成本 —— 算成本是 Step 6 的事。
    recorder = LLMCallRecorder(
        db=economy.db,
        tokens_per_ai_coin=economy.tokens_per_ai_coin,
    )

    # --- 通信层（Step 3）---
    #
    # 注意这里创建的是 LocalTransport 这个【具体实现】。
    # 它是整个项目里唯一一处写死传输方式的地方 ——
    # 将来换成 P2PTransport 只需要改这一行。
    transport = LocalTransport()
    communication = CommunicationManager(transport=transport)

    # --- 创建 Agent，给每个装上大脑，并接入通信层 ---
    for agent_id in DEFAULT_AGENTS:
        agent = economy.create_agent(agent_id)

        # 每个 Agent 的大脑配置来自它自己的 config.yaml，
        # 所以 A 和 B 可以用不同的 provider，
        # 而 Agent 的代码里一行厂商名字都不会出现。
        brain = Brain(
            router=router,
            settings=BrainSettings.from_agent_config(agent.config),
            recorder=recorder,
        )
        agent.attach_brain(brain)

        # 把 Agent 接入通信层。
        # capabilities 直接取自配置里的技能标签 ——
        # 本步骤只是把它存进注册表，Step 4 的「按能力找人」会用到。
        communication.register(
            agent,
            capabilities=tuple(agent.config.skills),
        )

        # Agent 拿到的是【门面】，不是 Transport。
        # 它因此永远不知道消息是走内存还是走网络。
        agent.attach_communication(communication)

    return Runtime(
        economy=economy,
        router=router,
        recorder=recorder,
        communication=communication,
        transport=transport,
    )


def build_economy(db_path: Optional[str] = None) -> Economy:
    """
    向后兼容的入口：只要 Economy。

    Step 1 的测试和外部脚本用的是这个函数，所以保留它。
    现在它会顺带把大脑也装好 —— 也就是说通过这些测试创建出来的
    Agent 同样能调用模型，行为和真实运行一致。
    """
    return build_runtime(db_path=db_path).economy


# ============================================================
# 命令：start（默认）
# ============================================================
def cmd_start(runtime: Runtime, args: argparse.Namespace) -> int:
    """打印启动横幅。这是不加任何参数时的默认行为。"""
    economy = runtime.economy
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
def cmd_transfer(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    让一个 Agent 向另一个 Agent 转钱。

    这是「控制 Agent 花钱」的入口。
    注意它调用的不是钱包，而是 Economy.transfer() —— OS 级校验。
    """
    economy = runtime.economy
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
def cmd_balances(runtime: Runtime, args: argparse.Namespace) -> int:
    """查看所有 Agent 的余额。"""
    economy = runtime.economy
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
def cmd_ledger(runtime: Runtime, args: argparse.Namespace) -> int:
    """查看交易流水。"""
    economy = runtime.economy
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
def cmd_audit(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    对账：检查「账本里的货币总量」是否对得上。

    这是发现经济系统 bug 最重要的工具。
    任何绕过账本直接改余额的代码，都会让这里 FAILED。
    """
    economy = runtime.economy
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
# 命令：providers（看 LLM 层现在有什么）
# ============================================================
def cmd_providers(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    列出所有 LLM provider 的状态。

    这个命令直接体现了注册表的好处：
    你加一个新厂商，这里的输出会自动多一行，不需要改这段代码。
    """
    info = runtime.router.describe()

    print()
    print("LLM Providers")
    print("-" * 58)
    print(f"  默认 provider: {info['default_provider']}")
    print()

    print("  可直接使用")
    if info["implemented"]:
        for name in info["implemented"]:
            enabled = "启用" if runtime.router.is_enabled(name) else "已关闭"

            # 注意：这里要问 provider【实例】拿模型名，而不是直接读 config。
            # 因为模型名可能来自 .env（环境变量优先级高于配置文件），
            # 只读 config 会显示成 "(无默认模型)"，误导人以为没配。
            try:
                model = runtime.router.get_provider(name).default_model
            except Exception:  # noqa: BLE001 - 显示用，不该因此崩掉
                model = ""

            model = model or "(未配置模型)"
            mark = "  ← 当前默认" if name == info["default_provider"] else ""
            print(f"    {name:<10} {enabled:<4} model={model}{mark}")
    else:
        print("    （无）")

    print()
    print("  已登记但尚未实现")
    if info["unimplemented"]:
        for name in info["unimplemented"]:
            print(f"    {name:<10} 需要新建 llm/{name}.py")
    else:
        print("    （无）")

    print()
    print("-" * 58)
    print("  每个 Agent 当前用的大脑：")
    for agent in runtime.economy.all_agents():
        if agent.has_brain:
            brain = agent.brain
            print(
                f"    {agent.agent_id:<10} provider={brain.provider:<10} "
                f"model={brain.model or '(默认)'}"
            )
        else:
            print(f"    {agent.agent_id:<10} (未装大脑)")
    print()

    return 0


# ============================================================
# 命令：ask（单问一句）
# ============================================================
def cmd_ask(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    向某个 Agent 问一句话，看它怎么回答。

    这个命令是 Step 2 最直接的成果展示：
    它走的是 Agent → Brain → Router → Provider 整条链路。
    """
    economy = runtime.economy
    agent_id = economy.resolve_agent_id(args.agent)
    agent = economy.agent(agent_id)

    if not agent.has_brain:
        print()
        print(f"!! {agent.name} 还没有大脑，无法回答 !!")
        print()
        return 1

    # --provider 可以临时换一家，不改配置文件
    override = resolve_provider_option(runtime, getattr(args, "provider", None))

    shown_provider = override or agent.brain.provider
    if override:
        # 临时换 provider 时，模型名要按新 provider 重新解析 ——
        # 否则会显示出旧 provider 的模型，误导人。
        shown_model = runtime.router.provider_model(override) or "默认"
    else:
        shown_model = agent.brain.model or "默认"

    print()
    print(f"{agent.name}  [{shown_provider}/{shown_model}]")
    print("-" * 58)
    print(f"  你: {args.prompt}")
    print()

    try:
        response = agent.generate(messages=args.prompt, provider=override)
    except LLMError as exc:
        print("  模型调用失败")
        print(f"  {exc}")
        print()
        return 2

    print(f"  {agent.name}: {response.content}")
    print()

    print_usage(response)

    # 顺手显示这个 Agent 累计用了多少
    summary = runtime.recorder.summary(agent_id)
    print(
        f"  累计: {summary['calls']} 次调用, "
        f"{summary['total_tokens']} tokens"
    )
    print()

    return 0


# ============================================================
# 命令：demo（Step 2 完整链路演示）
# ============================================================
def cmd_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    跑一遍 Step 2 的完整链路，把每一环都显示出来。

         User
          ↓
        Agent A → Brain → Router → MockProvider
          ↓
        LLMResponse（含 token 用量）
          ↓
        记进 llm_calls 表

    这个命令存在的意义：
        证明 Agent【完全不知道】自己在用哪个模型 ——
        因为它拿到的只有一个 LLMResponse。
    """
    economy = runtime.economy
    agent_id = economy.resolve_agent_id(args.agent)
    agent = economy.agent(agent_id)

    if not agent.has_brain:
        print()
        print(f"!! {agent.name} 还没有大脑 !!")
        print()
        return 1

    brain = agent.brain
    prompt = args.prompt

    # --provider 可以临时换一家
    override = resolve_provider_option(runtime, getattr(args, "provider", None))
    effective_provider = override or brain.provider

    banner_name = str(economy.config.get("system.name", "AI Economy OS"))
    banner_version = str(economy.config.get("system.version", "0.1"))

    print()
    print("=" * BANNER_WIDTH)
    print(f"  {banner_name} v{banner_version}".center(BANNER_WIDTH))
    # 注意：这里用纯 ASCII 做居中。
    # str.center() 是按【字符数】补空格的，而中文字符在终端里
    # 占 2 个字符宽，所以含中文的行用 center() 一定会歪。
    print("Step 2 Demo".center(BANNER_WIDTH))
    print("=" * BANNER_WIDTH)
    print()

    # --- 证明 Agent 与厂商解耦 ---
    print("调用链")
    print("-" * BANNER_WIDTH)
    print(f"  {agent.name}")
    print("    ↓  agent.generate(messages=...)")
    print("  Brain")
    origin = "命令行 --provider" if override else "来自 Agent 配置"
    print(f"    ↓  provider = {effective_provider!r}（{origin}）")
    print("  Router")
    print(f"    ↓  按名字查注册表")
    print("  Provider")
    print(f"    ↓  {type(runtime.router.get_provider(effective_provider)).__name__}")
    print("  LLMResponse")
    print()

    # --- 真正调用 ---
    calls_before = runtime.recorder.count(agent_id)

    try:
        response = agent.generate(messages=prompt, provider=override)
    except LLMError as exc:
        print("!! 模型调用失败 !!")
        print(f"  {exc}")
        print()
        return 2

    print("对话")
    print("-" * BANNER_WIDTH)
    print("User:")
    print(f"  {prompt}")
    print()
    print(f"{agent.name}:")
    print(f"  {response.content}")
    print()

    print_usage(response)

    # --- 证明记账真的落库了 ---
    calls_after = runtime.recorder.count(agent_id)
    summary = runtime.recorder.summary(agent_id)

    print("调用记录")
    print("-" * BANNER_WIDTH)
    print(f"  llm_calls 新增: {calls_after - calls_before} 条")
    print(f"  {agent.agent_id} 累计: {calls_after} 次")
    print(
        f"  累计 tokens: {summary['total_tokens']} "
        f"(输入 {summary['input_tokens']} / 输出 {summary['output_tokens']})"
    )
    if summary["simulated_calls"]:
        print(
            f"  其中 {summary['simulated_calls']} 次是模拟数据"
            f"（MockProvider 估算，不是真实计费）"
        )
    print()

    # --- 证明经济层没被动过 ---
    audit = economy.audit()
    print("系统状态")
    print("-" * BANNER_WIDTH)
    print(f"  Provider: {effective_provider}")
    print(
        f"  Model:    {runtime.router.provider_model(effective_provider) or '默认'}"
    )
    print(f"  {agent.name} 余额: {fmt(agent.balance)} {economy.currency}（未扣费）")
    print(
        f"  账本审计: {'OK' if audit['balanced'] else 'FAILED'}"
        f"  (货币总量 {fmt(audit['total_supply'])} {economy.currency})"
    )
    print("  System status: READY")
    print()

    return 0


def print_usage(response) -> None:
    """
    打印一次调用的 token 用量。

    【为什么要小心处理 None】
        token 有三个层次：
            具体数字  API 真的给了
            0        确实用了 0 个 token
            None     不知道（API 没返回 usage）

        None 绝对不能显示成 0 —— 那会让人以为「这次调用没花钱」。
        所以这里明确显示「未知」，并把来源说清楚。
    """
    provider = response.provider
    model = response.model

    def show(value) -> str:
        if value is None:
            return "未知"
        return str(value)

    source = response.token_source
    source_note = {
        "api": "来自 API 的真实用量，可以拿去对账单",
        "estimated": "估算值，不是真实计费数据",
        "unknown": "API 未返回 usage —— 这里没有估算顶替，就是不知道",
    }.get(source, source)

    print("Token usage")
    print("-" * BANNER_WIDTH)
    print(f"  Provider:      {provider}")
    print(f"  Model:         {model}")
    print(f"  Input tokens:  {show(response.input_tokens)}")
    print(f"  Output tokens: {show(response.output_tokens)}")
    print(f"  Total tokens:  {show(response.total_tokens)}")
    print(f"  Latency:       {response.latency_ms:.2f} ms")
    print(f"  Simulated:     {response.simulated}")
    print(f"  Token source:  {source}  ({source_note})")
    print()


def resolve_provider_option(runtime: Runtime, requested: Optional[str]) -> Optional[str]:
    """
    处理命令行传进来的 --provider。

    为什么要单独处理？
        因为用户很容易打错 provider 名字，或者选了一个
        还没实现 / 没配 Key 的。与其让它一路走到调用时才炸，
        不如在这里就给出清晰提示。

    返回 None 表示「用 Agent 自己配置里的 provider」。
    """
    if not requested:
        return None

    name = requested.strip().lower()
    registry = runtime.router.registry

    if not registry.is_known(name):
        available = ", ".join(registry.known) or "（无）"
        raise LLMError(
            f"未知的 provider：{requested!r}\n"
            f"可用的名字：{available}\n"
            f"（用 `./dev.sh providers` 看完整列表）"
        )

    if name not in registry.implemented:
        hint = registry.hint_for(name) or ""
        raise LLMError(
            f"provider {name!r} 还没有实现。\n" + hint
        )

    return name


# ============================================================
# 命令：communication-demo（Step 3 通信链路演示）
# ============================================================
def cmd_communication_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    跑一遍 Step 3 的完整通信链路。

        Agent A → CommunicationManager → Message
                → Transport → LocalTransport → Agent B
        Agent B → 回复 → Agent A

    【这个 Demo 刻意不做的事】
        · 不涉及任何 AI 币
        · 不涉及任何交易
        · 不调用任何模型

        因为通信层和这些都无关。它能单独跑通，
        正说明这层抽象是干净的。
    """
    return asyncio.run(_communication_demo_async(runtime, args))


async def _communication_demo_async(
    runtime: Runtime, args: argparse.Namespace
) -> int:
    economy = runtime.economy
    manager = runtime.communication
    transport = runtime.transport

    sender_id = economy.resolve_agent_id(args.sender)
    receiver_id = economy.resolve_agent_id(args.receiver)

    sender = economy.agent(sender_id)
    receiver = economy.agent(receiver_id)

    banner_name = str(economy.config.get("system.name", "AI Economy OS"))
    version = str(economy.config.get("system.version", "0.1"))

    print()
    print("=" * BANNER_WIDTH)
    print(f"  {banner_name} v{version}".center(BANNER_WIDTH))
    print("Step 3 Communication Demo".center(BANNER_WIDTH))
    print("=" * BANNER_WIDTH)
    print()

    # ---------------- 链路结构 ----------------
    print("通信链路")
    print("-" * 46)
    print(f"  {sender.name}")
    print(f"    ↓  send {MessageType.TEXT}")
    print("  CommunicationManager")
    print("    ↓  造 Message / 查地址 / 去重")
    print(f"  Transport（{type(transport).__name__}, scheme={transport.scheme}）")
    print("    ↓  投递到信箱")
    print(f"  {receiver.name}")
    print()

    # ---------------- 让 B 会回复 ----------------
    #
    # 注意：这个处理函数【没有调用任何模型】。
    # 通信本身不需要智力 —— 这正是通信层与 LLM 解耦的体现。
    def receiver_handler(message):
        return f"{receiver.name} 收到了：「{message.text}」"

    receiver.on_message(receiver_handler)

    # ---------------- ① A → B ----------------
    print("① 发送")
    print("-" * 46)
    sent = await sender.send(receiver_id, text=args.text)
    for key, value in sent.summary().items():
        print(f"  {key:<12}: {value}")
    print(f"  {'scheme':<12}: {transport.scheme}")
    print()

    # ---------------- ② B 接收 ----------------
    result = await manager.receive(receiver_id, timeout=args.timeout)

    print("② 接收")
    print("-" * 46)
    print(f"  状态        : {result.status}")
    if result.message is not None:
        print(f"  {receiver.name} 收到: {result.message.text}")
    if result.reply is not None:
        print(
            f"  自动回复    : {result.reply.message_id} "
            f"→ {result.reply.to_agent}"
        )
    print()

    if not result.delivered:
        print(f"  ⚠️  没有正常投递：{result.detail}")
        print()
        return 1

    # ---------------- ③ 回复到达发送方 ----------------
    print("③ 回复到达发送方")
    print("-" * 46)
    reply_result = await manager.receive(sender_id, timeout=args.timeout)

    if reply_result.delivered and reply_result.message is not None:
        reply = reply_result.message
        print(f"  {sender.name} 收到: {reply.text}")
        print(f"  这是对哪条消息的回复 : {reply.reply_to}")
        print(f"  与原始消息 ID 一致   : {reply.reply_to == sent.message_id}")
    else:
        print(f"  ⚠️  没有收到回复：{reply_result.status} {reply_result.detail}")
    print()

    # ---------------- 往返记录 ----------------
    print("往返消息")
    print("-" * 46)
    print(f"  {'方向':<24} {'类型':<8} 消息 ID")
    print(
        f"  {sent.from_agent + ' → ' + sent.to_agent:<24} "
        f"{sent.message_type:<8} {sent.message_id}"
    )
    if reply_result.message is not None:
        reply = reply_result.message
        print(
            f"  {reply.from_agent + ' → ' + reply.to_agent:<24} "
            f"{reply.message_type:<8} {reply.message_id}"
        )
    print()

    # ---------------- 统计 ----------------
    print("统计")
    print("-" * 46)
    stats = manager.stats()
    print(f"  发出      : {stats['sent']}")
    print(f"  已处理    : {stats['delivered']}")
    print(f"  重复跳过  : {stats['duplicates']}")
    print(f"  超时      : {stats['timeouts']}")
    print(f"  注册表    : {stats['registry']['agents']}")
    print(
        f"  传输层    : {stats['transport']['transport']} "
        f"（信箱 {stats['transport']['mailboxes']}）"
    )
    print()

    print("说明")
    print("-" * 46)
    print("  · 全程没有调用任何模型 —— 通信层与 LLM 完全解耦")
    print("  · 全程没有涉及任何 AI 币 —— 通信层与 Economy 完全解耦")
    print("  · 收发双方只认识 CommunicationManager，不认识 LocalTransport")
    print()

    return 0


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
  python3 main.py demo                             Step 2 完整链路演示
  python3 main.py ask "你好"                       问 Agent A 一句话
  python3 main.py ask "你好" --provider deepseek    用真实 DeepSeek 问一句
  python3 main.py providers                        看有哪些 LLM provider
  python3 main.py communication-demo               跑一遍 Agent 间通信
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

    # --- providers（Step 2）---
    subparsers.add_parser(
        "providers",
        parents=[shared],
        help="列出所有 LLM provider 的状态",
    ).set_defaults(func=cmd_providers)

    # --- ask（Step 2）---
    ask = subparsers.add_parser(
        "ask",
        parents=[shared],
        help="向某个 Agent 问一句话",
    )
    ask.add_argument(
        "prompt",
        metavar="内容",
        help="你要说的话",
    )
    ask.add_argument(
        "--agent",
        default="agent_a",
        help="问哪个 Agent（默认 agent_a，可用 a / 'Agent A' 等写法）",
    )
    ask.add_argument(
        "--provider",
        default=None,
        help="临时指定 LLM provider（默认用 Agent 配置里的，例如 deepseek）",
    )
    ask.set_defaults(func=cmd_ask)

    # --- demo（Step 2）---
    demo = subparsers.add_parser(
        "demo",
        parents=[shared],
        help="跑一遍 Step 2 的完整 LLM 调用链路",
    )
    demo.add_argument(
        "--agent",
        default="agent_a",
        help="用哪个 Agent 演示（默认 agent_a）",
    )
    demo.add_argument(
        "--prompt",
        default="Hello, who are you?",
        help="要发给 Agent 的话",
    )
    demo.add_argument(
        "--provider",
        default=None,
        help="临时指定 LLM provider（默认用 Agent 配置里的，例如 deepseek）",
    )
    demo.set_defaults(func=cmd_demo)

    # --- communication-demo（Step 3）---
    comm = subparsers.add_parser(
        "communication-demo",
        parents=[shared],
        help="跑一遍 Step 3 的 Agent 间通信链路",
    )
    comm.add_argument(
        "--sender",
        default="agent_a",
        help="发送方（默认 agent_a）",
    )
    comm.add_argument(
        "--receiver",
        default="agent_b",
        help="接收方（默认 agent_b）",
    )
    comm.add_argument(
        "--text",
        default="Hello Agent B",
        help="要发送的内容",
    )
    comm.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="等待接收的超时秒数（默认 2.0）",
    )
    comm.set_defaults(func=cmd_communication_demo)

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
        runtime = build_runtime(db_path=db_path)
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
        return args.func(runtime, args)

    except (AgentNotFoundError, EconomyError, LedgerError) as exc:
        # 这些是「经济规则拒绝了这次操作」，属于预期内的失败，
        # 不是程序 bug。所以要给出清晰、友好的提示，而不是抛堆栈。
        print()
        print("操作被经济系统拒绝")
        print("-" * 46)
        print(f"  {exc}")
        print()
        return 2

    except LLMError as exc:
        # 模型层的问题（provider 未实现、配置不对、调用失败）。
        # 同样属于「可预期的失败」，不该甩堆栈给用户。
        print()
        print("模型调用被拒绝")
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
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

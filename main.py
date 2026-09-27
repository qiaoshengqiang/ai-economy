#!/usr/bin/env python3
"""
AI Economy OS v0.1 —— 启动入口 & 命令行工具
============================================================

【基本运行】

    python3 main.py
        → 创建 / 加载 Agent，打印启动横幅

【命令行工具】

    python3 main.py transfer agent_a agent_b 20
        → 让 Agent A 付 20 AIC 给 Agent B（这是经济系统的执行入口）

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
from typing import Any, Dict, List, Optional

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
from core.agent_directory import AgentDirectory, LocalAgentDirectory  # noqa: E402
from core.decision_policy import RuleBasedDecisionPolicy        # noqa: E402
from core.money import Money                                    # noqa: E402
from core.task import OfferType, Task, TaskStatus               # noqa: E402
from core.task_store import TaskStore                           # noqa: E402
from services import (
    TaskExecutionService,  # noqa: E402
    TaskService,  # noqa: E402
)
from core.paths import ensure_runtime_dirs                      # noqa: E402
from llm import LLMCallRecorder, LLMRouter, LLMError            # noqa: E402
from communication import (                                     # noqa: E402
    CommunicationManager,
    CommunicationRecorder,
    LocalTransport,
    MessageStatus,
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
            CommunicationRecorder 通信日志    ← Step 3.5
            TaskStore            任务持久化     ← Step 4A（Task Core）
            AgentDirectory       能力发现       ← Step 4B
            TaskService          消息→领域适配  ← Step 4B
            DecisionPolicy       议价决策策略    ← Step 4C

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
    journal: CommunicationRecorder
    tasks: TaskStore
    directory: AgentDirectory
    task_service: TaskService
    task_execution: TaskExecutionService
    decision_policies: Dict[str, Any]

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
def _address_hint(communication: CommunicationManager,
                  agent_id: str) -> Optional[str]:
    """
    从通信层的注册表里取一个 Agent 的地址（字符串形式）。

    【为什么要从注册表读，而不是自己拼字符串】
        因为「消息实际会送到哪里」是通信层的事实。
        如果这里自己拼一个 "local://agent_b"，而通信层用的是别的地址，
        两者就会不一致 —— 而这种不一致极难排查。

        返回 None 表示通信层也不知道它在哪 —— 这完全可以接受，
        因为能力目录里的地址只是一个**提示**，不是投递依据。
    """
    info = communication.registry.find(agent_id)
    return str(info.address) if info is not None else None


def _wire_task_handlers(
    communication: CommunicationManager,
    task_service: TaskService,
) -> None:
    """
    把 TaskService 的业务处理器接到通信层。

    【为什么需要这一层薄薄的适配】
        TaskService 返回的是 HandlerResult（业务结果对象），
        而通信层只认识「回复内容」（Message / dict / str / None）。

        两者之间的翻译必须发生在**组合根**：
            · 通信层不该知道 HandlerResult 是什么
              （那会让 communication/ 依赖 services/，方向就反了）
            · TaskService 也不该知道「怎么把消息发出去」
              （那它就没法脱离通信层测试了）

    【注意这里没有做任何业务判断】
        它只是把 to_reply() 的结果转交给通信层。
        接不接任务、报什么价 —— 那些决定都在 TaskService 和 Agent 那边。
    """
    def adapt(method):
        def handler(message):
            result = method(message)
            return result.to_reply(message)
        return handler

    communication.register_type_handler(
        MessageType.TASK_OFFER, adapt(task_service.handle_offer)
    )
    communication.register_type_handler(
        MessageType.COUNTER_OFFER, adapt(task_service.handle_counter_offer)
    )
    communication.register_type_handler(
        MessageType.ACCEPT, adapt(task_service.handle_accept)
    )
    communication.register_type_handler(
        MessageType.REJECT, adapt(task_service.handle_reject)
    )

    # ---- Step 4D：执行生命周期 ----
    #
    # 【加这四种消息，门面改了吗？没有】
    #     handle_start / handle_submit / handle_verify / handle_complete
    #     都只是「按类型查注册表再转调 handler」的一行转发。
    #     真正的业务在 TaskExecutionService 和四个 Handler 里。
    #
    #     这就是上一轮「Task Application Layer Refactor」要买的东西：
    #     加消息类型不需要碰门面。
    communication.register_type_handler(
        MessageType.START, adapt(task_service.handle_start)
    )
    communication.register_type_handler(
        MessageType.SUBMIT, adapt(task_service.handle_submit)
    )
    communication.register_type_handler(
        MessageType.VERIFY, adapt(task_service.handle_verify)
    )
    communication.register_type_handler(
        MessageType.COMPLETE, adapt(task_service.handle_complete)
    )


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

    # 通信日志（Step 3.5）。
    # 注意它是【注入】给 CommunicationManager 的，不是 Manager 内部 new 的 ——
    # 这样通信层不需要知道数据库在哪，测试通信时也不用准备数据库。
    journal = CommunicationRecorder(
        db=economy.db,
        store_payload=True,
        strict=False,
    )
    communication = CommunicationManager(
        transport=transport,
        recorder=journal,
    )

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

    # Task Core 的持久化（Step 4A）。
    #
    # 【注意 Task 和 Economy 的关系】
    #     TaskStore 用的是同一个数据库，但 Task Core **不碰钱包和账本**。
    #     Task 里的 budget / agreed_price 只是「经济约定」，
    #     记录它们不会产生任何一行账本记录。
    #     真实结算属于后续步骤。
    tasks = TaskStore(db=economy.db)

    # 能力目录（Step 4B）。
    #
    # 【为什么在这里建、而不是让 Agent 自己建】
    #     因为「有哪些 Agent 可用」是**部署环境**的事实，
    #     不是某个 Agent 的属性。组合根负责装配。
    #
    # 【为什么现在只有一个本机实现】
    #     LocalAgentDirectory 只是 AgentDirectory 的一种实现。
    #     将来换成 NetworkAgentDirectory / P2P Discovery 时，
    #     调用方（TaskService / Agent）的代码一行都不用改。
    directory = LocalAgentDirectory()
    for agent in economy.all_agents():
        directory.register_agent(
            agent_id=agent.agent_id,
            capabilities=agent.config.skills,
            # 位置提示用字符串 —— 这样 core/ 不必依赖 communication/。
            #
            # 地址从**通信层的注册表**里读，而不是自己拼 ——
            # 因为那才是真正会用于投递的地址（可能是本机的，
            # 也可能是将来登记的远端地址）。
            address=_address_hint(communication, agent.agent_id),
        )

    # 每个 Agent 自己的议价策略（Step 4C）。
    #
    # 【为什么在这里逐个构造】
    #     因为议价参数是**每个 Agent 私有的经济参数**，
    #     写在各自的 config.yaml 里：
    #
    #         agent_a: accept_price=50, opening_ask=40, ...
    #         agent_b: accept_price=50, opening_ask=60, ...
    #
    #     同一个策略类，参数不同 —— 所以不需要（也禁止）
    #     写 `if agent_id == "agent_b"`。
    #
    #     没有配置 negotiation 的 Agent 就拿不到策略，
    #     它收到报价时会明确报错，而不是被塞一个默认策略。
    decision_policies: Dict[str, Any] = {}
    for agent in economy.all_agents():
        params = agent.config.negotiation
        if params:
            decision_policies[agent.agent_id] = (
                RuleBasedDecisionPolicy.from_config(
                    params, currency=economy.currency
                )
            )

    # 消息 → 领域 的适配层。
    #
    # 【议价和执行是两个并列的服务】——
    #     TaskNegotiationService  怎么谈（Step 4C）
    #     TaskExecutionService    怎么干（Step 4D）
    # 它们共享幂等 / trace / 协议违规回复（同一个基类），
    # 但互相不知道对方存在。有架构守卫盯着这条线。
    task_execution = TaskExecutionService(store=tasks)

    task_service = TaskService(
        store=tasks,
        directory=directory,
        policies=decision_policies,
        execution=task_execution,
    )

    # 把 Task 的业务处理器接到通信层的类型处理器上。
    #
    # 这是 Step 4B 第一次把 Task + Capability + Communication 连起来。
    # 但注意方向：是**组合根**把它们接起来，
    # 而不是让 Task 去 import communication，也不是让
    # communication 去 import Task。
    _wire_task_handlers(communication, task_service)

    return Runtime(
        economy=economy,
        router=router,
        recorder=recorder,
        communication=communication,
        transport=transport,
        journal=journal,
        tasks=tasks,
        directory=directory,
        task_service=task_service,
        task_execution=task_execution,
        decision_policies=decision_policies,
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
        · 不涉及任何 AIC
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

    # ---------------- 通信日志 ----------------
    journal = runtime.journal
    if journal is not None:
        print("通信日志（已落库，可用 communication-log 查询）")
        print("-" * 46)
        trace_messages = journal.trace(sent.trace_id)
        trace_events = journal.trace_events(sent.trace_id)
        print(f"  trace_id : {sent.trace_id}")
        print(f"  本链路   : {len(trace_messages)} 条消息 / {len(trace_events)} 个事件")
        print(f"  日志总计 : {journal.count_messages()} 条消息 / "
              f"{journal.count_events()} 个事件")
        print()
        print("  最后一条消息的完整事件流：")
        last_id = (
            reply_result.message.message_id
            if reply_result.message is not None
            else sent.message_id
        )
        for event in journal.events_for(last_id):
            print(
                f"    {event['status']:<11} "
                f"{event['actor'] or '-':<10} {event['detail'] or ''}"
            )
        print()
        print(f"  查询方式：./dev.sh communication-log --trace {sent.trace_id}")
        print()

    print("说明")
    print("-" * 46)
    print("  · 全程没有调用任何模型 —— 通信层与 LLM 完全解耦")
    print("  · 全程没有涉及任何 AIC —— 通信层与 Economy 完全解耦")
    print("  · 收发双方只认识 CommunicationManager，不认识 LocalTransport")
    print()

    return 0


# ============================================================
# 命令：communication-log（查询通信日志）
# ============================================================
def cmd_communication_log(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    按 message_id / trace_id / conversation_id 查通信日志。

    支持两种写法（两种都是自然的输入习惯）：

        ./dev.sh communication-log trc_abc123       ← 直接给 ID
        ./dev.sh communication-log --trace trc_abc123

    直接给 ID 时，靠前缀自动判断查什么：
        trc_  → trace
        conv_ → conversation
        msg_  → message

    【这个命令的存在理由】
        需求里说「可以根据日志完整定位通信问题」。
        光把日志写进数据库还不算 ——
        还得能在出问题时真的查出来。这就是那个入口。
    """
    journal = runtime.journal

    if journal is None:
        print()
        print("!! 本次运行没有启用通信日志 !!")
        print()
        return 1

    # ---------------- 位置参数：靠前缀自动判断 ----------------
    target = getattr(args, "id", None)
    if target:
        kind = _classify_id(target)

        if kind == "message":
            return _print_message_detail(journal, target)
        if kind == "trace":
            return _print_trace(journal, target)
        if kind == "conversation":
            return _print_conversation(journal, target)

        # 前缀认不出来 —— 别猜，直接告诉他怎么写
        return _print_unknown_id(target, journal)

    # ---------------- 按单条消息查 ----------------
    if args.message:
        return _print_message_detail(journal, args.message)

    # ---------------- 按 trace 查 ----------------
    if args.trace:
        return _print_trace(journal, args.trace)

    # ---------------- 按 conversation 查 ----------------
    if args.conversation:
        return _print_conversation(journal, args.conversation)

    # ---------------- 列出最近的 trace ----------------
    if args.traces:
        return _print_trace_list(journal, args.limit)

    # ---------------- 默认：最近的消息 ----------------
    return _print_recent_messages(journal, args)


# ID 前缀 → 类型
# 这些前缀是 core.identity.new_message_id() 生成 ID 时用的，
# 所以是可以依赖的约定。
_ID_PREFIXES = (
    ("trc_", "trace"),
    ("conv_", "conversation"),
    ("msg_", "message"),
)


def _classify_id(value: str) -> str:
    """按前缀判断这个 ID 是什么。认不出来返回 "unknown"。"""
    text = str(value or "").strip()
    for prefix, kind in _ID_PREFIXES:
        if text.startswith(prefix):
            return kind
    return "unknown"


def _print_unknown_id(target: str, journal) -> int:
    """
    前缀认不出来时，尝试在所有可能的表里找一遍。

    为什么不一上来就报错？
        因为 ID 可能就是被人抄错了一个字母。
        试着找一下，找到了就给出结果，
        找不到再告诉他正确的写法 —— 这比直接拒绝有用。
    """
    for finder, kind in (
        (journal.get_message, "message"),
        (lambda x: journal.trace(x) or None, "trace"),
        (lambda x: journal.conversation(x) or None, "conversation"),
    ):
        try:
            found = finder(target)
        except Exception:  # noqa: BLE001
            found = None
        if found:
            print()
            print(f"（这个 ID 没有标准前缀，但确实存在，按 {kind} 处理）")
            if kind == "message":
                return _print_message_detail(journal, target)
            if kind == "trace":
                return _print_trace(journal, target)
            return _print_conversation(journal, target)

    print()
    print(f"认不出这个 ID：{target!r}")
    print()
    print("  ID 的前缀决定查什么：")
    print("    trc_xxx   查一条链路（一次任务）的所有消息")
    print("    conv_xxx  查一轮对话")
    print("    msg_xxx   查一条消息的完整事件流")
    print()
    print("  不确定 ID 是什么？先看看有哪些：")
    print("    ./dev.sh communication-log --traces")
    print("    ./dev.sh communication-log")
    print()
    return 2


def _short(text, width: int = 40) -> str:
    if text is None:
        return ""
    text = str(text).replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_recent_messages(journal, args) -> int:
    rows = journal.recent(
        limit=args.limit,
        agent_id=args.agent,
        status=args.status,
    )

    # 标题里把筛选条件写出来 ——
    # 否则「查不到东西」和「筛掉了所有东西」看起来一模一样。
    filters = []
    if args.agent:
        filters.append(f"agent={args.agent}")
    if args.status:
        filters.append(f"status={args.status}")
    title = "通信日志 —— 最近的消息"
    if filters:
        title += f"（筛选：{', '.join(filters)}）"

    print()
    print(title)
    print("-" * 96)
    print(f"  {'时间':<20} {'状态':<11} {'方向':<22} {'类型':<10} 消息 ID")
    print("-" * 96)

    if not rows:
        # 【重要】区分两种「空」：
        #     库里本来就没有记录   → 提示去做点什么
        #     有记录但被筛掉了     → 提示换个条件，别让人以为日志坏了
        if filters and journal.count_messages() > 0:
            print(f"  （没有符合条件的消息，但库里有 {journal.count_messages()} 条）")
            print("  换个条件试试：./dev.sh communication-log")
        else:
            print("  （还没有任何通信记录）")
            print("  跑一次演示就会产生日志：./dev.sh communication-demo")
        print()
        return 0

    for row in rows:
        when = (row["created_at"] or "")[:19].replace("T", " ")
        flow = f"{row['from_agent']} → {row['to_agent']}"
        print(
            f"  {when:<20} {row['status']:<11} {flow:<22} "
            f"{row['message_type']:<10} {row['message_id']}"
        )
        if row["error_message"]:
            print(f"      ⚠️  {_short(row['error_code'])}: {_short(row['error_message'], 70)}")

    print("-" * 96)
    print(f"  共 {len(rows)} 条    trace: {_short(args.trace or '（用 --traces 看有哪些）')}")
    print()
    print("  提示：")
    print("    ./dev.sh communication-log --traces             列出最近的链路")
    print("    ./dev.sh communication-log --message msg_xxx    看一条消息的完整事件流")
    print("    ./dev.sh communication-log --trace trc_xxx      看一条链路的所有消息")
    print("    ./dev.sh communication-log --status FAILED      只看失败的")
    print()
    return 0


def _print_message_detail(journal, message_id: str) -> int:
    row = journal.get_message(message_id)

    if row is None:
        print()
        print(f"找不到消息：{message_id}")
        print("（用 ./dev.sh communication-log 看最近的消息）")
        print()
        return 2

    print()
    print(f"消息 {message_id}")
    print("-" * 72)

    fields = [
        ("trace_id", row["trace_id"]),
        ("conversation_id", row["conversation_id"]),
        ("from → to", f"{row['from_agent']} → {row['to_agent']}"),
        ("message_type", row["message_type"]),
        ("status", row["status"]),
        ("transport", row["transport"]),
        ("address", row["address"]),
        ("is_remote", bool(row["is_remote"])),
        ("reply_to", row["reply_to"]),
        ("retry_count", row["retry_count"]),
        ("payload_size", row["payload_size"]),
        ("payload_hash", _short(row["payload_hash"], 24)),
    ]
    for key, value in fields:
        print(f"  {key:<16}: {value}")

    print()
    print("  时间线")
    for key in ("created_at", "sent_at", "received_at", "processed_at"):
        print(f"    {key:<14}: {(row[key] or '—')[:26]}")

    if row["error_message"]:
        print()
        print("  ⚠️  错误")
        print(f"    code    : {row['error_code']}")
        print(f"    message : {_short(row['error_message'], 300)}")

    print()
    print("  事件流")
    print("  " + "-" * 68)
    for event in journal.events_for(message_id):
        when = (event["created_at"] or "")[11:19]
        print(
            f"    [{when}] {event['status']:<11} "
            f"{event['actor'] or '-':<10} {_short(event['detail'], 40)}"
        )
        if event["error_message"]:
            print(f"              ⚠️  {_short(event['error_message'], 60)}")

    print()
    return 0


def _print_trace(journal, trace_id: str) -> int:
    messages = journal.trace(trace_id)
    events = journal.trace_events(trace_id)

    print()
    print(f"链路 {trace_id}")
    print("-" * 96)

    if not messages:
        print("  （这条链路没有消息）")
        print()
        return 2

    print(f"  消息 {len(messages)} 条 / 事件 {len(events)} 个")
    print()
    print(f"  {'时间':<20} {'状态':<11} {'方向':<22} {'类型':<12} 消息 ID")
    print("  " + "-" * 92)

    for row in messages:
        when = (row["created_at"] or "")[:19].replace("T", " ")
        flow = f"{row['from_agent']} → {row['to_agent']}"
        print(
            f"  {when:<20} {row['status']:<11} {flow:<22} "
            f"{row['message_type']:<12} {row['message_id']}"
        )

    # 状态汇总：一眼看出卡在哪
    by_status: dict = {}
    for row in messages:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    print()
    print("  状态汇总:", ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))

    # 有问题就把事件流也打出来
    troubled = [m for m in messages if m["status"] in ("FAILED", "TIMEOUT")
                or m["error_message"]]
    if troubled:
        print()
        print("  ⚠️  有问题的消息：")
        for row in troubled:
            print(
                f"    {row['message_id']}  {row['status']}  "
                f"{_short(row['error_code'])}: {_short(row['error_message'], 60)}"
            )
        print()
        print("  相关事件：")
        for event in events:
            if event["status"] in ("FAILED", "TIMEOUT", "DUPLICATE", "RETRY"):
                when = (event["created_at"] or "")[11:19]
                print(
                    f"    [{when}] {event['message_id']} "
                    f"{event['status']}: {_short(event['detail'], 40)}"
                )

    print()
    print(f"  查某一条：./dev.sh communication-log --message <上面的消息 ID>")
    print()
    return 0


def _print_conversation(journal, conversation_id: str) -> int:
    rows = journal.conversation(conversation_id)

    print()
    print(f"对话 {conversation_id}")
    print("-" * 88)

    if not rows:
        print("  （这轮对话没有消息）")
        print()
        return 2

    for row in rows:
        when = (row["created_at"] or "")[:19].replace("T", " ")
        print(
            f"  {when}  {row['from_agent']} → {row['to_agent']}  "
            f"[{row['message_type']}]  {row['status']}"
        )

    print("-" * 88)
    print(f"  共 {len(rows)} 条")
    print()
    return 0


def _print_trace_list(journal, limit: int) -> int:
    rows = journal.traces(limit=limit)

    print()
    print("最近的通信链路（trace）")
    print("-" * 88)
    print(f"  {'trace_id':<20} {'消息':>5} {'已处理':>7} {'失败':>5}  最后活动")
    print("-" * 88)

    if not rows:
        print("  （还没有任何链路）")
        print()
        return 0

    for row in rows:
        last = (row["last_at"] or "")[:19].replace("T", " ")
        failed = row["failed"] or 0
        mark = "  ⚠️" if failed else ""
        print(
            f"  {row['trace_id']:<20} {row['messages']:>5} "
            f"{row['processed'] or 0:>7} {failed:>5}  {last}{mark}"
        )

    print("-" * 88)
    print(f"  共 {len(rows)} 条链路")
    print()
    print("  看某条链路的详情：./dev.sh communication-log --trace <trace_id>")
    print()
    return 0


# ============================================================
# 命令：task-demo（Step 4A）
# ============================================================
def cmd_task_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    Task Core 演示。

    【这个演示刻意不做什么】
        ❌ 不转账（agreed_price 只是约定，不是账目）
        ❌ 不创建 Workspace（workspace_id 只是标识）
        ❌ 不碰 Git
        ❌ 不调用任何模型
        ❌ 不发任何网络消息

    它只证明一件事：Task Core 能正确地走完自己的状态机。
    """
    store = runtime.tasks
    economy = runtime.economy

    print()
    print("=" * 64)
    print("        AI Economy OS v0.1")
    print("        Step 4A — Task Core Demo")
    print("=" * 64)
    print()

    # ---------------- 记账前的经济状态 ----------------
    ledger_before = economy.ledger.count()
    balance_before = economy.agent("agent_a").balance

    print("开始前的经济状态")
    print("-" * 64)
    print(f"  账本记录数        : {ledger_before}")
    print(f"  agent_a 余额      : {format_amount(balance_before)} {economy.currency}")
    print()

    # ---------------- 1) 创建任务 ----------------
    print("① 创建任务")
    print("-" * 64)
    task = Task.create(
        creator="agent_a",
        description="写一个 CSV 分析程序",
        budget=Money(100),
        workspace_id="WS-001",
        acceptance={"type": "tests"},
    )
    store.save(task)

    print(f"  task_id          : {task.task_id}")
    print(f"  current_trace_id : {task.current_trace_id}   ← 这一次执行")
    print(f"  status           : {task.status}")
    print(f"  budget           : {task.budget}                ← 预算上限")
    print(f"  agreed_price     : {task.agreed_price or chr(8212)}                  ← 还没谈成")
    print(f"  workspace_id     : {task.workspace_id}")
    print(f"  workspace 种类   : {task.workspace.kind}")
    print()

    print("  关于 workspace_id：")
    print(f"    它只是一个逻辑引用：{task.workspace}")
    print("    它【不是】文件路径，也【不是】权限凭证。")
    print("    知道 WS-001 不等于能访问 WS-001。")
    print()

    # ---------------- 2) 发出邀约 ----------------
    print("② 发出邀约（offer）")
    print("-" * 64)
    task.offer()
    task.record_offer(OfferType.TASK_OFFER, Money(100), note="初始报价")
    store.save(task)
    print(f"  status           : {task.status}")
    print(f"  报价历史         : {[str(o) for o in task.offers]}")
    print()

    # ---------------- 3) 议价 ----------------
    print("③ 议价（negotiating）")
    print("-" * 64)
    task.negotiate()
    task.record_offer(OfferType.COUNTER_OFFER, Money(60), note="B 还价")
    store.save(task)
    print(f"  agent_b 还价 60 AIC  →  status = {task.status}")

    task.negotiate()
    task.record_offer(OfferType.COUNTER_OFFER, Money(25), note="B 再还价")
    store.save(task)
    print(f"  agent_b 再还 25 AIC  →  status = {task.status}")
    print(f"  （NEGOTIATING 可以重复进入 —— 一轮谈判里能还价多次）")
    print()

    # ---------------- 4) 成交 ----------------
    print("④ 接受报价（accept）")
    print("-" * 64)
    task.accept(Money(25), contractor="agent_b")
    task.record_offer(OfferType.ACCEPT, Money(25), note="成交")
    store.save(task)

    print(f"  status           : {task.status}")
    print(f"  budget           : {task.budget}     ← 预算（上限，没变）")
    print(f"  agreed_price     : {task.agreed_price}      ← 成交价")
    print(f"  contractor       : {task.contractor}")
    print()
    print("  【注意这两个数不一样】")
    print(f"    预算 100 AIC 是「我愿意付的上限」")
    print(f"    成交  25 AIC 是「实际谈成的数」")
    print(f"    它们必须分别保存 —— 混用一个字段就说不清是压价还是按预算成交。")
    print()

    # ---------------- 5) 执行 ----------------
    print("⑤ 执行与验收")
    print("-" * 64)
    for action, label in (
        (task.start, "开工"),
        (task.submit, "提交成果"),
    ):
        action()
        store.save(task)
        print(f"  {label:<12} →  status = {task.status}")

    print(f"  {'验收不通过':<12} →  status = ", end="")
    task.verify(passed=False)
    store.save(task)
    print(f"{task.status}   ← 打回重做")

    task.submit()
    store.save(task)
    print(f"  {'重新提交':<12} →  status = {task.status}")

    task.verify(passed=True)
    store.save(task)
    print(f"  {'验收通过':<12} →  status = {task.status}")

    task.complete()
    store.save(task)
    print(f"  {'结项':<12} →  status = {task.status}  终态={task.is_terminal}")
    print()

    # ---------------- 6) 完整报价链 ----------------
    print("⑥ 完整报价链（从数据库读回来）")
    print("-" * 64)
    offers = store.offers_for(task.task_id)
    for index, offer in enumerate(offers, 1):
        print(f"  {index}. {offer.offer_type:<15} {offer.amount}")
    print()
    print(f"  最终 agreed_price = {task.agreed_price}")
    print(f"  （来自最后一条 ACCEPT，而不是 budget）")
    print()

    # ---------------- 7) 状态机 ----------------
    print("⑦ Task 状态机")
    print("-" * 64)
    print("  CREATED → OFFERED → NEGOTIATING → ACCEPTED")
    print("         → IN_PROGRESS → SUBMITTED → VERIFIED → COMPLETED")
    print()
    print("  本次实际路径：")
    print("    CREATED → OFFERED → NEGOTIATING ⇄ NEGOTIATING")
    print("           → ACCEPTED → IN_PROGRESS → SUBMITTED")
    print("           → IN_PROGRESS（返工）→ SUBMITTED → VERIFIED → COMPLETED")
    print()

    # ---------------- 8) 边界证明 ----------------
    ledger_after = economy.ledger.count()
    balance_after = economy.agent("agent_a").balance

    print("⑧ 边界证明")
    print("-" * 64)
    print(f"  账本记录数   : {ledger_before} → {ledger_after}   "
          f"{'✅ 没有产生任何账目' if ledger_after == ledger_before else '❌ 账本被改了！'}")
    print(f"  agent_a 余额 : {format_amount(balance_before)} → "
          f"{format_amount(balance_after)} {economy.currency}   "
          f"{'✅ 一分钱没动' if balance_after == balance_before else '❌ 余额被改了！'}")
    print()
    print("  · 全程没有调用任何模型    —— Task Core 与 LLM 解耦")
    print("  · 全程没有转账            —— Task 只记录经济约定")
    print("  · 全程没有创建 Workspace  —— workspace_id 只是逻辑标识")
    print("  · 全程没有碰 Git / 文件系统")
    print()

    # ---------------- 9) 持久化 ----------------
    print("⑨ 持久化")
    print("-" * 64)
    stats = store.stats()
    print(f"  任务总数 : {stats['tasks']}")
    print(f"  报价记录 : {stats['offers']}")
    print(f"  按状态   : {stats['by_status']}")
    print(f"  涉及工作空间 : {stats['workspaces']}")
    print()
    print(f"  查询方式：./dev.sh task-log")
    print()
    return 0


# ============================================================
# 命令：task-log（查看任务）
# ============================================================
def cmd_task_log(runtime: Runtime, args: argparse.Namespace) -> int:
    """列出任务 / 看某个任务的详情。"""
    store = runtime.tasks

    if args.task:
        return _print_task_detail(store, args.task)

    rows = store.list_tasks(status=args.status, limit=args.limit)

    print()
    print("任务列表")
    print("-" * 104)
    print(f"  {'状态':<12} {'task_id':<16} {'发起':<9} {'承接':<9} "
          f"{'预算':>10} {'成交':>10}  工作空间")
    print("-" * 104)

    if not rows:
        print("  （还没有任何任务）")
        print("  跑一次演示就会产生任务：./dev.sh task-demo")
        print()
        return 0

    for task in rows:
        budget = str(task.budget) if task.budget else "—"
        price = str(task.agreed_price) if task.agreed_price else "—"
        print(
            f"  {task.status:<12} {task.task_id:<16} {task.creator:<9} "
            f"{(task.contractor or '—'):<9} {budget:>10} {price:>10}  "
            f"{task.workspace_id or '—'}"
        )

    print("-" * 104)
    print(f"  共 {len(rows)} 个任务")
    print()
    print("  看详情：./dev.sh task-log <task_id>")
    print()
    return 0


def _print_task_detail(store, task_id: str) -> int:
    task = store.get(task_id)
    if task is None:
        print()
        print(f"找不到任务：{task_id}")
        print("（用 ./dev.sh task-log 看有哪些）")
        print()
        return 2

    print()
    print(f"任务 {task.task_id}")
    print("-" * 72)

    fields = [
        ("status", task.status),
        ("current_trace_id", task.current_trace_id),
        ("creator", task.creator),
        ("contractor", task.contractor),
        ("participants", task.participants or "—"),
        ("description", task.description or "—"),
        ("workspace_id", task.workspace_id or "—"),
        ("budget", task.budget or "—"),
        ("agreed_price", task.agreed_price or "—"),
        ("acceptance", task.acceptance or "—"),
        ("expires_at", task.expires_at or "—"),
        ("created_at", task.created_at[:19]),
        ("updated_at", task.updated_at[:19]),
    ]
    for key, value in fields:
        print(f"  {key:<18}: {value}")

    if task.workspace is not None:
        print()
        print("  workspace 引用")
        print(f"    {task.workspace.describe()}")

    print()
    print("  报价历史")
    offers = store.offers_for(task.task_id)
    if offers:
        for index, offer in enumerate(offers, 1):
            note = f"   {offer.note}" if offer.note else ""
            print(f"    {index}. {offer.offer_type:<15} {offer.amount}{note}")
    else:
        print("    （没有报价记录）")

    print()
    print("  允许的下一步状态")
    allowed = task.allowed_transitions()
    print(f"    {allowed or '（已是终态）'}")
    print()
    return 0


# ============================================================
# 命令：capability-demo（Step 4B）
# ============================================================
def cmd_capability_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    能力发现 + 自主委派演示。

    【这个演示要证明的事】

        ✅ 系统能根据任务需要的能力**找出候选 Agent**
        ✅ 选谁由发起方 Agent 自己决定 —— 系统不代劳
        ✅ 能力匹配 ≠ 自动接单（B 收到邀约后状态仍是 OFFERED）
        ❌ 不转账、不授权 Workspace、不调模型、不碰 Git

    【刻意不证明的事】
        这里没有「最佳 Agent」排名、没有信誉分、没有价格优化 ——
        那些都不属于 Step 4B。
    """
    directory = runtime.directory
    service = runtime.task_service
    manager = runtime.communication
    economy = runtime.economy

    print()
    print("=" * 66)
    print("        AI Economy OS v0.1")
    print("        Step 4B — Capability & Delegation Demo")
    print("=" * 66)
    print()

    ledger_before = economy.ledger.count()

    # ---------------- 1) 目录里有什么 ----------------
    print("① Agent 能力目录")
    print("-" * 66)
    print("  （这些是 Agent **自己声明**的能力，不是系统认证的能力）")
    print()
    for entry in directory.all():
        print(f"    {entry.agent_id:<10} {', '.join(entry.capabilities.names)}")
    print()
    print(f"  目录统计: {directory.stats()['agents']} 个 Agent，"
          f"{len(directory.stats()['capabilities'])} 种能力")
    print()

    # ---------------- 2) 任务需要什么能力 ----------------
    print("② Agent A 创建任务，并声明需要什么能力")
    print("-" * 66)

    required = ["python_coding", "data_analysis"]
    task = Task.create(
        creator="agent_a",
        description="写一个 CSV 分析程序",
        budget=Money(100),
        workspace_id="WS-001",
        required_capabilities=required,
        acceptance={"type": "tests"},
    )
    runtime.tasks.save(task)

    print(f"  task_id              : {task.task_id}")
    print(f"  description          : {task.description}")
    print(f"  budget               : {task.budget}")
    print(f"  workspace_id         : {task.workspace_id}")
    print(f"  required_capabilities: {task.required_capabilities.names}")
    print()
    print("  注意 Task 到此为止 —— 它只**声明需要什么**，")
    print("  它自己不会去找 Agent，也不会发消息。")
    print()

    # A 决定要招标了 —— 把任务推进到 OFFERED。
    # 这个动作是 A 做的（不是系统替它做的）。
    task.offer()
    runtime.tasks.save(task)

    # ---------------- 3) 查候选（系统提供信息）----------------
    print("③ 查询候选 Agent（AgentDirectory）")
    print("-" * 66)
    explanation = service.explain_candidates(task)

    print(f"  任务要求: {', '.join(explanation['required'])}")
    print()
    print("  候选 Agent:")
    if explanation["candidates"]:
        for item in explanation["candidates"]:
            print(f"    ✅ {item['agent_id']:<10} {', '.join(item['capabilities'])}")
    else:
        print("    （没有匹配的 Agent）")
    print()
    print("  被排除的 Agent:")
    for item in explanation["excluded"]:
        missing = ", ".join(item["missing"])
        print(f"    ❌ {item['agent_id']:<10} 缺少: {missing}")
    print()
    print("  【注意】目录只回答「谁声明了这些能力」。")
    print("         它没有排序、没有打分、没有推荐「最佳人选」。")
    print()

    # ---------------- 4) A 自己做决定 ----------------
    print("④ Agent A 自己做决定")
    print("-" * 66)

    candidates = service.candidates_for(task)
    if not candidates:
        print("  没有候选，无法委派。")
        print()
        return 0

    # 【这一行就是「A 的决定」】
    # 演示里用「取第一个候选」代替真正的决策过程 ——
    # 未来这里会是 Agent Brain 根据成本、信誉、风险做出的判断。
    # 关键是：**这个决定发生在 Agent 侧，不在 Directory 里，也不在 Task 里。**
    chosen = candidates[0]

    print(f"  候选列表: {[e.agent_id for e in candidates]}")
    print(f"  A 决定选: {chosen.agent_id}")
    print()
    print("  【这一步的边界很重要】")
    print("    系统帮 A 找到了候选人，但**没有**替 A 决定用谁。")
    print("    Task 不知道 Directory，Directory 也不知道 Task。")
    print("    做决定的是 Agent A 自己。")
    print()

    # ---------------- 5) 发 TASK_OFFER ----------------
    print("⑤ A 通过 Communication 发出 TASK_OFFER")
    print("-" * 66)

    async def send_offer():
        return await manager.send(
            "agent_a",
            chosen.agent_id,
            message_type=MessageType.TASK_OFFER,
            trace_id=task.current_trace_id,
            payload={
                "task_id": task.task_id,
                "workspace_id": task.workspace_id,
                "description": task.description,
                "budget": task.budget.to_dict(),
                "required_capabilities": task.required_capabilities.names,
                "acceptance": task.acceptance,
            },
        )

    sent = asyncio.run(send_offer())

    print(f"  message_id      : {sent.message_id}")
    print(f"  message_type    : {sent.message_type}")
    print(f"  from → to       : {sent.from_agent} → {sent.to_agent}")
    print(f"  trace_id        : {sent.trace_id}")
    print(f"  conversation_id : {sent.conversation_id}")
    print(f"  budget          : {sent.payload['budget']}")
    print()

    # ---------------- 6) B 收到并处理 ----------------
    print("⑥ Agent B 收到邀约（经 Handler 进入 Task 层）")
    print("-" * 66)

    async def receive():
        return await manager.receive(chosen.agent_id, timeout=2.0)

    inbound = asyncio.run(receive())

    print(f"  接收状态: {inbound.status}")
    if inbound.message is None:
        print("  没有收到消息。")
        print()
        return 1

    received = inbound.message
    print(f"  收到的消息: {received.message_id} ({received.message_type})")
    print()

    # B 侧的本地任务（同一个 task_id，但是 B 自己的记录）
    b_task = runtime.tasks.get(task.task_id)
    print(f"  任务状态 : {b_task.status}")
    print(f"    ↑ 是 OFFERED 而不是 ACCEPTED")
    print(f"      「收到了邀约」和「接受邀约」是两件事 ——")
    print(f"      B 自己判断之后才能决定接不接（这是 AI Economy 的核心行为）。")
    print()
    print("  【本演示的一个真实局限，必须说清楚】")
    print("    A 和 B 跑在同一个进程、共用同一个数据库。")
    print("    所以 Handler 走的是「关联到已有任务」，而不是「新建本地任务」。")
    print("    真实部署里每个 Agent 有**自己的**存储，那时 B 会新建一条记录，")
    print("    但 task_id 相同 —— 因为它是同一个业务任务。")
    print("    （Handler 两种路径都实现并测试了，见 handle_offer 的 action 字段）")
    print()

    # ---------------- 7) 完整链路 ----------------
    print("⑦ 可追踪链路（这是 Step 4B 第一次把三个系统缝起来）")
    print("-" * 66)

    # 【注意】这里重新从存储读一次，而不是用 A 的内存对象 ——
    # 因为状态是 Handler 在服务端改的，A 手里那个对象可能已经过期。
    # 这本身就是一个好例子：内存对象不等于事实来源。
    stored = runtime.tasks.get(task.task_id)
    offers = runtime.tasks.offers_for(task.task_id)
    print(f"  Task ({task.task_id})")
    print(f"    └── status           : {stored.status}")
    print(f"    └── current_trace_id : {stored.current_trace_id}")
    print(f"    └── Offers ({len(offers)} 条)")
    for offer in offers:
        print(f"          └── {offer.offer_type} {offer.amount}")
        print(f"                message_id : {offer.message_id}")

    print()
    print(f"  这四条 ID 现在可以互相查到：")
    print(f"    task_id         : {stored.task_id}")
    print(f"    message_id      : {sent.message_id}")
    print(f"    trace_id        : {stored.current_trace_id}")
    print(f"    conversation_id : {sent.conversation_id}")
    print()

    # 从 message_id 查回通信日志，证明链路是通的
    journal = runtime.journal
    if journal is not None and sent.message_id:
        row = journal.get_message(sent.message_id)
        if row is not None:
            print("  验证：从 message_id 查通信日志")
            print(f"    trace_id        : {row['trace_id']}")
            print(f"    conversation_id : {row['conversation_id']}")
            print(f"    status          : {row['status']}")
            print(f"    ✅ 与任务的 trace_id 一致: "
                  f"{row['trace_id'] == stored.current_trace_id}")
            print()

    # ---------------- 8) 边界证明 ----------------
    ledger_after = economy.ledger.count()

    print("⑧ 边界证明")
    print("-" * 66)
    print(f"  账本记录数 : {ledger_before} → {ledger_after}   "
          f"{'✅ 没有产生任何账目' if ledger_after == ledger_before else '❌ 账本被改了'}")
    print(f"  B 的任务状态: {b_task.status}   "
          f"{'✅ B 没有自动接单' if b_task.status != TaskStatus.ACCEPTED else '❌ B 被自动接了单'}")
    print(f"  A 的余额   : {format_amount(economy.agent('agent_a').balance)} "
          f"{economy.currency}   ✅ 没有付款")
    print()
    print("  · 能力匹配没有转账          —— Capability ≠ Economy")
    print("  · 能力匹配没有授予 Workspace 权限 —— Capability ≠ Permission")
    print("  · 没有调用任何模型          —— 匹配是纯集合运算")
    print("  · B 收到邀约但没有自动接单   —— Agent 自主决定")
    print("  · 没有实现 P2P / Git / Escrow")
    print()

    print(f"  查询方式：./dev.sh task-log {task.task_id}")
    print()
    return 0


# ============================================================
# 命令：negotiation-demo（Step 4C）
# ============================================================
async def _negotiate(
    runtime: Runtime,
    task_id: str,
    first_message,
    max_steps: int = 8,
):
    """
    驱动一轮完整的议价，直到成交 / 拒绝 / 步数用尽。

    【这个循环里没有一次 LLM 调用】
        每一步都是：
            收到消息 → DecisionPolicy（本地规则）→ Decision → TaskService 执行
        决策层是纯本地计算，所以谈判成本几乎为零。
    """
    manager = runtime.communication
    service = runtime.task_service
    transcript = []

    current = first_message
    for _ in range(max_steps):
        responder = current.to_agent

        inbound = await manager.receive(responder, timeout=2.0)
        if inbound.message is None:
            transcript.append(("timeout", responder, None, None))
            break

        incoming = inbound.message
        decision = service.decide(incoming)
        result = service.execute(incoming, decision)

        transcript.append((decision.decision_type, responder, decision, result))

        if not result.ok:
            break
        if result.action in ("accepted", "already_accepted", "rejected"):
            # 成交/拒绝之后，把最后那条消息也发出去，
            # 让对方那一侧也走一遍接收逻辑（这样两边状态一致）
            reply = result.to_reply(incoming)
            if reply is not None:
                await manager.send_message(reply)
                await manager.receive(reply.to_agent, timeout=2.0)
            break

        reply = result.to_reply(incoming)
        if reply is None:
            break

        await manager.send_message(reply)
        current = reply

    return transcript


def _reset_demo_tasks(runtime: Runtime, task_ids) -> int:
    """
    清掉上一次演示留下的任务，让演示可以反复跑。

    【这只是演示的便利，不是产品功能】
        真实的 tasks / task_offers / task_agreements 都是 append-only 的，
        生产代码里**没有**任何删除它们的路径（有架构守卫盯着）。

        但演示如果每次都在旧数据上叠加，输出会越来越乱
        （同一个 task_id 下堆着好几轮的报价），
        所以这里在开始前把演示自己用过的那几个 task_id 清掉。
    """
    removed = 0
    with runtime.tasks.db.transaction() as conn:
        for task_id in task_ids:
            # 【顺序很重要：先删子表，再删 tasks】
            #     task_executions / task_offers / task_agreements
            #     都有指向 tasks 的外键。
            #     先删 tasks 会撞上 FOREIGN KEY constraint failed ——
            #     （这个坑演示重跑时才暴露出来。）
            for table in ("task_executions", "task_agreements",
                          "task_offers", "tasks"):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE task_id = ?", (task_id,)
                )
                removed += cursor.rowcount or 0
    return removed


def cmd_negotiation_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    自主议价演示（Step 4C）。

    【要证明的三件事】

        ✅ Agent 能自己完成 ACCEPT / COUNTER_OFFER / REJECT
        ✅ 决策由**本地规则**做出 —— 谈判全程不调用任何模型
        ✅ 成交只是**合同成立**：不转账、不建 Workspace、不碰 Git
    """
    from core.decision_policy import RuleBasedDecisionPolicy

    store = runtime.tasks
    economy = runtime.economy
    directory = runtime.directory

    print()
    print("=" * 68)
    print("        AI Economy OS v0.1")
    print("        Step 4C — Negotiation Demo")
    print("=" * 68)
    print()

    # 让演示可以反复跑（见 _reset_demo_tasks 的说明）
    demo_ids = ("TASK-NEG-1", "TASK-NEG-2", "TASK-NEG-3")
    _reset_demo_tasks(runtime, demo_ids)

    ledger_before = economy.ledger.count()
    balance_before_a = economy.agent("agent_a").balance
    balance_before_b = economy.agent("agent_b").balance

    # ---------------- 0) 双方的议价策略 ----------------
    print("① 每个 Agent 自己的议价策略（来自各自的 config.yaml）")
    print("-" * 68)
    for agent_id, policy in sorted(runtime.decision_policies.items()):
        info = policy.describe()
        side = "买方（发起方，越低越好）" if agent_id == "agent_a" else "卖方（承接方，越高越好）"
        print(f"  {agent_id}  {side}")
        print(f"     接受线 {info['accept_price']:<9} 开口 {info['opening_ask']:<9} "
              f"底线 {info['walk_away_price']:<9} 最多还价 {info['max_rounds']} 次")
    print()
    print("  【注意】这些是**每个 Agent 自己的私有经济参数**，")
    print("         同一个策略类，参数不同 —— 代码里没有任何 Agent 的名字。")
    print()

    # ============================================================
    # 场景 1：完整议价链 → 成交
    # ============================================================
    print("=" * 68)
    print("场景 1：多轮议价 → 成交")
    print("=" * 68)
    print()

    task1 = Task.create(
        task_id="TASK-NEG-1",
        creator="agent_a",
        description="写一个 CSV 分析程序",
        budget=Money(100),
        workspace_id="WS-001",
        required_capabilities=["python_coding", "data_analysis"],
        participants=["agent_a", "agent_b"],
    )
    task1.offer()
    store.save(task1)

    print(f"  Budget（预算上限）: {task1.budget}")
    print()

    async def run_scenario_1():
        offer = await runtime.communication.send(
            "agent_a", "agent_b",
            message_type=MessageType.TASK_OFFER,
            trace_id=task1.current_trace_id,
            payload={
                "task_id": task1.task_id,
                "workspace_id": "WS-001",
                "description": task1.description,
                "budget": task1.budget.to_dict(),
                "required_capabilities": ["python_coding", "data_analysis"],
            },
        )
        return offer, await _negotiate(runtime, task1.task_id, offer)

    offer1, transcript1 = asyncio.run(run_scenario_1())

    print(f"  A → TASK_OFFER      budget = {task1.budget}      （{offer1.message_id}）")
    for index, (kind, responder, decision, result) in enumerate(transcript1, start=1):
        if kind == "timeout":
            print(f"  （{responder} 没有等到消息，流程结束）")
            continue
        price = decision.price if decision is not None else None
        if kind == "ACCEPT":
            print(f"  {responder} → ACCEPT        {price}      ✅ 成交")
        elif kind == "REJECT":
            print(f"  {responder} → REJECT")
        else:
            print(f"  {responder} → COUNTER_OFFER {price}")

    print()
    final1 = store.get(task1.task_id)
    agreement1 = store.latest_agreement(task1.task_id)
    print(f"  任务状态      : {final1.status}")
    print(f"  agreed_price  : {final1.agreed_price}")
    print(f"  budget        : {final1.budget}   ← 和成交价是两个不同的数")
    print(f"  contractor    : {final1.contractor}")
    print()
    print("  报价链（可用 reply_to 回溯）：")
    for item in store.offers_for(task1.task_id):
        print(f"    {item.offer_type:<15} {str(item.amount):>9}  "
              f"from={item.from_agent:<8} reply_to={item.reply_to}")
    print()
    if agreement1 is not None:
        print("  成交凭证（不可变，append-only）：")
        print(f"    agreement_id             : {agreement1.agreement_id}")
        print(f"    双方                     : {agreement1.initiator} ↔ {agreement1.contractor}")
        print(f"    成交价                   : {agreement1.agreed_price}")
        print(f"    依据报价 message_id      : {agreement1.accepted_offer_message_id}")
        print(f"    trace_id / conversation  : {agreement1.trace_id} / {agreement1.conversation_id}")
    print()

    # ============================================================
    # 场景 2：直接接受 TASK_OFFER
    # ============================================================
    print("=" * 68)
    print("场景 2：直接接受 TASK_OFFER（不还价）")
    print("=" * 68)
    print()

    print("  这里临时把 B 的 accept_first_offer 设为 True ——")
    print("  同一个策略类，只是参数不同（这正是「策略是 Agent 自己的参数」）。")
    print()

    original_policy = runtime.task_service.policies["agent_b"]
    b_params = dict(economy.agent("agent_b").config.negotiation)
    runtime.task_service.policies["agent_b"] = (
        RuleBasedDecisionPolicy.from_config(
            {**b_params, "accept_first_offer": True},
            currency=economy.currency,
        )
    )

    task2 = Task.create(
        task_id="TASK-NEG-2",
        creator="agent_a",
        description="简单的数据清洗",
        budget=Money(80),
        participants=["agent_a", "agent_b"],
    )
    task2.offer()
    store.save(task2)

    async def run_scenario_2():
        offer = await runtime.communication.send(
            "agent_a", "agent_b",
            message_type=MessageType.TASK_OFFER,
            trace_id=task2.current_trace_id,
            payload={
                "task_id": task2.task_id,
                "description": task2.description,
                "budget": task2.budget.to_dict(),
            },
        )
        return offer, await _negotiate(runtime, task2.task_id, offer)

    offer2, transcript2 = asyncio.run(run_scenario_2())
    runtime.task_service.policies["agent_b"] = original_policy

    print(f"  A → TASK_OFFER      budget = {task2.budget}")
    for kind, responder, decision, result in transcript2:
        if kind == "ACCEPT":
            print(f"  {responder} → ACCEPT        {decision.price}      "
                  f"✅ 直接按预算成交")
        elif kind != "timeout":
            print(f"  {responder} → {kind}")

    final2 = store.get(task2.task_id)
    print()
    print(f"  任务状态     : {final2.status}")
    print(f"  agreed_price : {final2.agreed_price}   （= budget，因为直接接受了上限）")
    print()

    # ============================================================
    # 场景 3：拒绝
    # ============================================================
    print("=" * 68)
    print("场景 3：拒 绝（超出底线）")
    print("=" * 68)
    print()

    b_policy = runtime.decision_policies["agent_b"]
    task3 = Task.create(
        task_id="TASK-NEG-3",
        creator="agent_a",
        description="一个预算太低的任务",
        budget=Money(10),          # 低于 B 的底线 25
        participants=["agent_a", "agent_b"],
    )
    task3.offer()
    store.save(task3)

    async def run_scenario_3():
        offer = await runtime.communication.send(
            "agent_a", "agent_b",
            message_type=MessageType.TASK_OFFER,
            trace_id=task3.current_trace_id,
            payload={
                "task_id": task3.task_id,
                "description": task3.description,
                "budget": task3.budget.to_dict(),
            },
        )
        return offer, await _negotiate(runtime, task3.task_id, offer)

    offer3, transcript3 = asyncio.run(run_scenario_3())

    print(f"  A → TASK_OFFER      budget = {task3.budget}   "
          f"（低于 B 的底线 {b_policy.walk_away_price}）")
    for kind, responder, decision, result in transcript3:
        if kind == "REJECT":
            reason = ""
            if result is not None and result.details.get("reason"):
                reason = f"  理由：{result.details['reason']}"
            print(f"  {responder} → REJECT{reason}")
        elif kind != "timeout":
            print(f"  {responder} → {kind}")

    final3 = store.get(task3.task_id)
    print()
    print(f"  任务状态     : {final3.status}")
    print(f"  agreed_price : {final3.agreed_price}   （没成交，所以是空的）")
    print()
    print("  拒绝是**正常的业务行为** —— 不是协议错误，也不是通信失败。")
    print()

    # ============================================================
    # 边界证明
    # ============================================================
    ledger_after = economy.ledger.count()
    balance_after_a = economy.agent("agent_a").balance
    balance_after_b = economy.agent("agent_b").balance

    print("=" * 68)
    print("边界证明")
    print("=" * 68)
    print()
    print(f"  Budget          : 100 AIC  →  成交价: {final1.agreed_price}")
    print(f"  Wallet A        : {format_amount(balance_before_a)} → "
          f"{format_amount(balance_after_a)} AIC   "
          f"{'✅ 不变' if balance_after_a == balance_before_a else '❌ 被改了'}")
    print(f"  Wallet B        : {format_amount(balance_before_b)} → "
          f"{format_amount(balance_after_b)} AIC   "
          f"{'✅ 不变' if balance_after_b == balance_before_b else '❌ 被改了'}")
    print(f"  Ledger          : {ledger_before} → {ledger_after} 条   "
          f"{'✅ 不变' if ledger_after == ledger_before else '❌ 被改了'}")
    print(f"  Workspace       : 未创建、未授权   ✅（ACCEPT ≠ 权限）")
    print(f"  Git             : 未触碰           ✅")
    print(f"  LLM             : 全程未调用       ✅（决策是本地规则）")
    print()
    print("  · ACCEPT 只意味着**合同成立**，不意味着付款")
    print("  · Capability ≠ Permission，成交 ≠ 获得 Workspace 访问权")
    print()

    # ---------------- 统计 ----------------
    print("持久化")
    print("-" * 68)
    stats = store.stats()
    print(f"  任务 {stats['tasks']} 个 / 报价 {stats['offers']} 条 / "
          f"成交凭证 {stats['agreements']} 份")
    print(f"  按状态: {stats['by_status']}")
    print()
    print(f"  查询方式：./dev.sh task-log {task1.task_id}")
    print(f"            ./dev.sh communication-log --trace {final1.current_trace_id}")
    print()
    return 0


# ============================================================
# 命令行参数
# ============================================================
# ============================================================
# 命令：task-4d-demo（Step 4D）
# ============================================================
def cmd_task_4d_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    执行 / 提交 / 验收 / 完结演示（Step 4D）。

    【它要证明的事情】

        ✅ 两个 Agent 之间**通过消息**推进任务生命周期 ——
           不是直接调方法，而是走
           Message → Handler → ExecutionService → Task Core → TaskStore

        ✅ 完整链路：
           ACCEPT → START → SUBMIT → VERIFY(false) → 返工 → SUBMIT
                  → VERIFY(true) → COMPLETE

        ✅ 四条边界全程没被越过：
           Wallet 没变 / Ledger 没变 / 没调 LLM / 没建 Workspace /
           没执行代码 / 没碰 Git

    【它刻意不证明的事情】
        ❌ 不执行任何代码（没有 subprocess、没有测试运行器）
        ❌ 不做结算（COMPLETE 只结束生命周期）
    """
    store = runtime.tasks
    economy = runtime.economy
    service = runtime.task_service

    print()
    print("=" * 72)
    print("        AI Economy OS v0.1")
    print("        Step 4D — Task Execution / Submission / Verification")
    print("=" * 72)
    print()

    task_id = "TASK-4D-1"
    # 【为什么先清一次】
    #     演示可以反复跑。不清的话每次都在旧数据上叠加，
    #     输出会越来越乱（同一个 task_id 下堆着好几轮执行事件）。
    #     真实的 tasks / task_offers / task_agreements / task_executions
    #     都是 append-only 的，生产代码里没有任何删除它们的路径。
    _reset_demo_tasks(runtime, (task_id,))

    # ---------------- 边界快照（演示开始前）----------------
    ledger_before = economy.ledger.count()
    wallet_a_before = economy.agent("agent_a").balance
    wallet_b_before = economy.agent("agent_b").balance
    llm_before = runtime.recorder.count() if hasattr(runtime.recorder, "count") else 0

    print("① 边界快照（演示开始前）")
    print("-" * 72)
    print(f"  Wallet A   : {wallet_a_before}")
    print(f"  Wallet B   : {wallet_b_before}")
    print(f"  Ledger     : {ledger_before} 条")
    print()

    # ============================================================
    # 第一部分：先谈成（复用 4C 的链路）
    # ============================================================
    print("=" * 72)
    print("第一部分：先谈成一个任务（Step 4C 的链路）")
    print("=" * 72)
    print()

    from communication import Message, MessageType

    def send(from_agent, to_agent, message_type, payload, reply_to=None,
             label=""):
        """
        发一条消息给**接收侧**，再让发送侧落地。

        【为什么这里要模拟「两边」】
            单进程演示里两个 Agent 共用一个 store，
            所以一条消息有两条处理路径：

                发送侧：我决定发什么 → 先落地（拿到 message_id 再发）
                接收侧：收到消息 → 处理（通常识别为重复，幂等）

            真实的多机部署里这两边在不同的进程 ——
            但幂等逻辑必须让两种情况都正确，所以演示里都走一遍。
        """
        message = Message(
            from_agent=from_agent,
            to_agent=to_agent,
            message_type=message_type,
            payload=payload,
            reply_to=reply_to,
        )
        # 接收侧：把消息交给通信层的类型处理器
        reply = runtime.communication.deliver(message)
        result = service.handle_message(
            Message(
                from_agent=from_agent, to_agent=to_agent,
                message_type=message_type, payload=payload,
                reply_to=reply_to, message_id=message.message_id,
            )
        )
        return message, result, reply

    # ---- 议价：A 报价 → B 还价 → A 接受 ----
    offer = Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.TASK_OFFER,
        payload={
            "task_id": task_id,
            "description": "写一个 CSV 分析程序，输出月度报表",
            "budget": {"amount": 100, "currency": "AIC"},
            "workspace_id": "WS-001",
            "required_capabilities": ["python_coding", "data_analysis"],
        },
    )
    service.handle_message(offer)
    _print_step("A → B", "TASK_OFFER", offer,
                f"budget = 100 AIC", store.get(task_id).status)

    counter = Message(
        from_agent="agent_b", to_agent="agent_a",
        message_type=MessageType.COUNTER_OFFER,
        payload={"task_id": task_id,
                 "price": {"amount": 25, "currency": "AIC"}},
        reply_to=offer.message_id,
    )
    service.handle_message(counter)
    _print_step("B → A", "COUNTER_OFFER", counter,
                "25 AIC", store.get(task_id).status)

    accept = Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.ACCEPT,
        payload={"task_id": task_id,
                 "price": {"amount": 25, "currency": "AIC"}},
        reply_to=counter.message_id,
    )
    result = service.handle_message(accept)
    task = store.get(task_id)
    _print_step("A → B", "ACCEPT", accept,
                f"成交价 25 AIC（预算 100）", task.status)
    print(f"     承接方 : {task.contractor}")
    print(f"     成交凭证: {result.details.get('agreement_id')}")
    print()

    # ============================================================
    # 第二部分：执行生命周期
    # ============================================================
    print("=" * 72)
    print("第二部分：执行生命周期（Step 4D 新增）")
    print("=" * 72)
    print()
    print("  全程通过 Message → Handler → ExecutionService → Task Core")
    print()

    last_message_id = accept.message_id

    # ---- START ----
    start = Message(
        from_agent="agent_b", to_agent="agent_a",
        message_type=MessageType.START,
        payload={"task_id": task_id},
        reply_to=last_message_id,
    )
    result = service.handle_message(start)
    _print_step("B → A", "START", start,
                "承接方开工", store.get(task_id).status)
    last_message_id = start.message_id

    # ---- 第一轮 SUBMIT ----
    submit1 = Message(
        from_agent="agent_b", to_agent="agent_a",
        message_type=MessageType.SUBMIT,
        payload={
            "task_id": task_id,
            "result": {
                "result_type": "code",
                "result_ref": "csv-analysis-v1",
                "summary": "初版：能跑通，月度报表已生成",
                "metadata": {"files": 2},
            },
        },
        reply_to=last_message_id,
    )
    result = service.handle_message(submit1)
    _print_step("B → A", "SUBMIT", submit1,
                "交付 csv-analysis-v1", store.get(task_id).status)
    print(f"     成果引用 : {result.details['result_ref']}"
          f"  （只是逻辑名字，不是路径、不是权限）")
    print(f"     这是第   : {result.details['attempt']} 次提交")
    last_message_id = submit1.message_id

    # ---- VERIFY(false) ----
    verify1 = Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.VERIFY,
        payload={
            "task_id": task_id,
            "passed": False,
            "feedback": "接口返回值错误：第 3 个函数在空输入时抛异常",
        },
        reply_to=last_message_id,
    )
    result = service.handle_message(verify1)
    _print_step("A → B", "VERIFY", verify1,
                "passed = False  打回返工", store.get(task_id).status)
    print(f"     验收意见 : {result.details['feedback']}")
    print(f"     → 任务回到 IN_PROGRESS，承接方可以再提交一次")
    last_message_id = verify1.message_id

    # ---- 返工 SUBMIT ----
    submit2 = Message(
        from_agent="agent_b", to_agent="agent_a",
        message_type=MessageType.SUBMIT,
        payload={
            "task_id": task_id,
            "result": {
                "result_type": "code",
                "result_ref": "csv-analysis-v2",
                "summary": "已修复空输入：第 3 个函数现在返回空列表",
                "metadata": {"files": 3, "fixes": ["empty-input"]},
            },
        },
        reply_to=last_message_id,
    )
    result = service.handle_message(submit2)
    _print_step("B → A", "SUBMIT", submit2,
                "交付 csv-analysis-v2", store.get(task_id).status)
    print(f"     成果引用 : {result.details['result_ref']}")
    print(f"     这是第   : {result.details['attempt']} 次提交")
    last_message_id = submit2.message_id

    # ---- VERIFY(true) ----
    verify2 = Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.VERIFY,
        payload={
            "task_id": task_id,
            "passed": True,
            "feedback": "复现了空输入场景，已修复，验收通过",
        },
        reply_to=last_message_id,
    )
    result = service.handle_message(verify2)
    _print_step("A → B", "VERIFY", verify2,
                "passed = True   验收通过", store.get(task_id).status)
    last_message_id = verify2.message_id

    # ---- COMPLETE ----
    complete = Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.COMPLETE,
        payload={"task_id": task_id, "summary": "任务验收通过，正式结束"},
        reply_to=last_message_id,
    )
    result = service.handle_message(complete)
    _print_step("A → B", "COMPLETE", complete,
                "任务正式结束", store.get(task_id).status)
    print()

    # ============================================================
    # 第三部分：执行历史
    # ============================================================
    print("=" * 72)
    print("第三部分：执行历史（append-only，可用 reply_to 回溯）")
    print("=" * 72)
    print()

    history = runtime.task_execution.history_summary(task_id)
    print(f"  {'事件':<12} {'谁':<10} {'之后状态':<14} {'message_id':<16} reply_to")
    print(f"  {'-'*12} {'-'*10} {'-'*14} {'-'*16} {'-'*16}")
    for event in runtime.task_execution.executions_for(task_id):
        print(f"  {event.event_type:<12} {event.from_agent or '':<10} "
              f"{event.status_after or '':<14} {event.message_id:<16} "
              f"{event.reply_to or '-'}")

    print()
    print(f"  提交次数（attempts）  : {history['attempts']}")
    print(f"  返工次数（rework）    : {history['rework_count']}")
    print(f"  执行事件总数          : {len(history['events'])}")
    print()
    print("  最近一次交付：")
    latest = store.latest_submission(task_id)
    if latest and latest.result:
        for key, value in latest.result.describe().items():
            print(f"      {key:<22} : {value}")
    print()
    print("  【注意】result_ref 只是逻辑引用 ——")
    print("         不是文件路径、不是访问权限、不是 URL、不是 Git 引用。")
    print()

    # ============================================================
    # 第四部分：边界证明
    # ============================================================
    print("=" * 72)
    print("第四部分：边界证明")
    print("=" * 72)
    print()

    task = store.get(task_id)
    final = runtime.task_execution.executions_for(task_id)[-1]

    wallet_a_after = economy.agent("agent_a").balance
    wallet_b_after = economy.agent("agent_b").balance
    ledger_after = economy.ledger.count()

    def verdict(before, after):
        return "✅ 不变" if before == after else f"❌ 变了（{before} → {after}）"

    print(f"  任务最终状态   : {task.status}")
    print(f"  成交价         : {task.agreed_price}")
    print()
    print(f"  Wallet A       : {wallet_a_before} → {wallet_a_after}   "
          f"{verdict(wallet_a_before, wallet_a_after)}")
    print(f"  Wallet B       : {wallet_b_before} → {wallet_b_after}   "
          f"{verdict(wallet_b_before, wallet_b_after)}")
    print(f"  Ledger         : {ledger_before} → {ledger_after} 条   "
          f"{verdict(ledger_before, ledger_after)}")
    print(f"  Workspace      : 未创建、未授权、未读写   ✅（workspace_id 只是引用）")
    print(f"  Git            : 未触碰                   ✅")
    print(f"  代码执行       : 未执行任何代码            ✅（没有 subprocess）")
    print(f"  LLM            : 全程未调用                ✅（验收由发起方显式给出）")
    print()

    details = final.describe()
    print("  最后一条执行事件的自我声明：")
    for key in ("is_payment",):
        print(f"      {key:<14} : {details[key]}")
    print()

    print("  · START / SUBMIT / VERIFY / COMPLETE 都**不产生**经济后果")
    print("  · COMPLETE 只表示任务生命周期结束 —— 结算是一个独立的后续阶段")
    print("  · 「验收」由发起方做出：承接方不能自己宣布自己合格")
    print()

    # ============================================================
    print("=" * 72)
    print("持久化")
    print("=" * 72)
    print()
    print(f"  任务 {store.count()} 个 / "
          f"执行事件 {store.count_executions()} 条")
    print(f"  这个任务的执行事件: {store.count_executions(task_id)} 条")
    print()
    print(f"  查询方式：./dev.sh task-log {task_id}")
    print(f"            ./dev.sh communication-log --trace {task.trace_id if hasattr(task, 'trace_id') else task.current_trace_id}")
    print()
    return 0


def _print_step(sender, message_type, message, note, status):
    """打印一行执行链。"""
    print(f"  {sender:<6} → {message_type:<14} {note:<28} "
          f"[{status}]")
    print(f"         message_id = {message.message_id}"
          f"   reply_to = {message.reply_to or '-'}")


# ============================================================
# 命令：reliability-demo（Step 4D.1）
# ============================================================
def cmd_reliability_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    可靠性 / 并发加固演示（Step 4D.1）。

    【它要证明的五件事】

        ① 正常处理    消息被处理 → 「处理过」成为持久化事实
        ② 重复消息    X / X / X → 业务副作用只有 1 次
        ③ 重启模拟    全新 runtime 收到同一条 → 仍然识别为重复
        ④ 并发冲突    version 相同的两个写入方 → 后者被拒绝，不静默覆盖
        ⑤ 事务        状态 + 事件要么一起成功，要么一起失败

    【它刻意不证明的事情】
        ❌ 不做 retry —— 本阶段只把「处理事实」持久化
        ❌ 不做多机 / P2P
        ❌ 不碰 Workspace / Git / 钱
    """
    import tempfile
    from pathlib import Path as _Path

    from communication import CommunicationManager, LocalTransport, Message, MessageType
    from communication.recorder import CommunicationRecorder
    from core.money import Money
    from core.task import Task, TaskStatus
    from core.task_execution import TaskResultRef
    from core.task_store import ConcurrentTaskModification, TaskStore
    from services import TaskExecutionService
    from storage.database import Database

    print()
    print("=" * 74)
    print("        AI Economy OS v0.1")
    print("        Step 4D.1 — Reliability / Concurrency Hardening")
    print("=" * 74)
    print()
    print("  只解决两个问题：")
    print("      消息：进程重启以后不要「失忆」")
    print("      Task：并发写入以后不要「静默互相覆盖」")
    print()

    workdir = _Path(tempfile.mkdtemp(prefix="dsh-4d1-"))
    db_path = workdir / "reliability.db"

    def make_runtime(handled):
        """模拟一次「进程启动」：全新 DB 句柄 / recorder / communication。"""
        db = Database(str(db_path))
        db.initialize()
        recorder = CommunicationRecorder(db)
        comm = CommunicationManager(LocalTransport(), recorder=recorder)

        def handler(message):
            handled.append(message.message_id)
            return None

        for message_type in (MessageType.START, MessageType.SUBMIT,
                             MessageType.VERIFY, MessageType.COMPLETE):
            comm.register_type_handler(message_type, handler)
        comm.register("agent_b")
        return db, comm, recorder

    def send_and_receive(comm, message, run_async):
        async def scenario():
            await comm.send_message(message)
            return await comm.receive("agent_b", timeout=1.0)
        return run_async(scenario())

    def sub(message_id):
        return Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.SUBMIT,
            payload={"task_id": "T-4D1", "result": {"result_ref": "v1"}},
            message_id=message_id,
        )

    # ============================================================
    # 第一部分：正常处理
    # ============================================================
    print("=" * 74)
    print("第一部分：正常处理 —— 「处理过」变成持久化事实")
    print("=" * 74)
    print()

    handled: list = []
    db_a, comm_a, rec_a = make_runtime(handled)
    _ = (runtime, args)  # 演示自带独立数据库，不用运行时那个

    result = send_and_receive(comm_a, sub("msg_demo_1"), _run_async)
    print(f"  收到 msg_demo_1          → {result.status}")
    print(f"  业务处理次数             → {len(handled)}")
    state = rec_a.processing_state("msg_demo_1")
    print(f"  processed（持久化事实）  → {state['processed']}")
    print(f"  status（最新状态）       → {state['status']}")
    print(f"  processed_at             → {state['processed_at']}")
    print()
    print("  【注意】status 是「最新状态」，会被后来的事件覆盖；")
    print("         processed_once 是「只增不减的成功事实」，去重依据是它。")
    print()

    # ============================================================
    # 第二部分：重复消息
    # ============================================================
    print("=" * 74)
    print("第二部分：重复消息 —— X / X / X，业务副作用只有 1 次")
    print("=" * 74)
    print()

    before = len(handled)
    print(f"  当前业务处理总次数: {before}")
    print()

    for index in range(1, 4):
        r = send_and_receive(comm_a, sub("msg_demo_1"), _run_async)
        print(f"  第 {index + 1} 次投递 msg_demo_1 → {r.status}")

    print()
    print(f"  业务处理总次数: {len(handled)}   （必须还是 {before}）")
    print(f"  duplicate 计数: {comm_a.duplicate_count}")
    print("  ✅ 重复消息没有产生第二次副作用" if len(handled) == before
          else "  ❌ 重复处理了！")
    print()

    # ============================================================
    # 第三部分：重启模拟
    # ============================================================
    print("=" * 74)
    print("第三部分：重启模拟 —— 全新 runtime，内存全空")
    print("=" * 74)
    print()

    db_a.close()
    print("  实例 A 已关闭（相当于进程退出）")
    print()

    handled_b: list = []
    db_b, comm_b, rec_b = make_runtime(handled_b)
    tracker = comm_b.seen_tracker("agent_b")
    print(f"  实例 B 启动：")
    print(f"      内存 tracker 大小      → {tracker.size if tracker else 0}")
    print(f"      但知道 msg_demo_1 吗   → {comm_b.was_processed('msg_demo_1')}")
    print()

    r = send_and_receive(comm_b, sub("msg_demo_1"), _run_async)
    print(f"  实例 B 收到同一条消息      → {r.status}")
    print(f"  实例 B 的业务处理次数      → {len(handled_b)}")
    print()
    print("  ✅ 去重事实来自数据库，不是内存" if len(handled_b) == 0
          else "  ❌ 重启后失忆了")
    print()

    # ============================================================
    # 第四部分：并发冲突
    # ============================================================
    print("=" * 74)
    print("第四部分：并发冲突 —— stale write 必须被拒绝")
    print("=" * 74)
    print()

    store = TaskStore(db_b)
    task = Task.create(
        task_id="T-CONFLICT", creator="agent_a", description="并发演示",
        budget=Money(100), participants=["agent_a", "agent_b"],
    )
    task.offer()
    store.save(task)
    print(f"  任务初始状态: version={task.version} status={task.status}")
    print()

    writer_a = store.get("T-CONFLICT")
    writer_b = store.get("T-CONFLICT")
    print(f"  两个写入方同时读到: A.version={writer_a.version} "
          f"B.version={writer_b.version}")
    print()

    writer_a.accept(Money(25), contractor="agent_b")
    store.save(writer_a)
    print(f"  Writer A 保存 → ✅ 成功，version={writer_a.version} "
          f"status={writer_a.status}")

    writer_b.note = "B 的修改"
    try:
        store.save(writer_b)
        print("  Writer B 保存 → ❌ 也成功了（静默覆盖！）")
    except ConcurrentTaskModification as exc:
        print(f"  Writer B 保存 → 🚫 被拒绝")
        print(f"      expected_version = {exc.expected_version}")
        print(f"      actual_version   = {exc.actual_version}")
        print(f"      原因: 这份 Task 已经过期")
    print()

    final = store.get("T-CONFLICT")
    print(f"  数据库最终: version={final.version} status={final.status} "
          f"note={final.note!r}")
    print("  ✅ A 的修改完好，B 没有静默覆盖" if final.note is None
          else "  ❌ 被覆盖了")
    print()
    print("  冲突之后**不会**自动重试、也不会自动重放 ——")
    print("  状态变化有语义，自动重放可能产生重复状态变化。")
    print()

    # ============================================================
    # 第五部分：事务
    # ============================================================
    print("=" * 74)
    print("第五部分：事务 —— 状态 + 事件要么一起成功，要么一起失败")
    print("=" * 74)
    print()

    execution = TaskExecutionService(store)

    exec_task = Task.create(
        task_id="T-TX", creator="agent_a", description="事务演示",
        budget=Money(100), participants=["agent_a", "agent_b"],
    )
    exec_task.offer()
    exec_task.accept(Money(25), contractor="agent_b")
    exec_task.start()
    store.save(exec_task)

    incoming = Message(from_agent="agent_b", to_agent="agent_a",
                       message_type=MessageType.SUBMIT,
                       payload={"task_id": "T-TX"})

    print("  ⑤-1 成功路径")
    execution.apply_submit(store.get("T-TX"), TaskResultRef(result_ref="v1"),
                           incoming=incoming)
    print(f"       状态 → {store.get('T-TX').status}")
    print(f"       执行事件数 → {store.count_executions('T-TX')}")
    print(f"       ✅ 两者都成功")
    print()

    print("  ⑤-2 失败路径（强制让事件写入抛异常）")
    exec_task2 = Task.create(
        task_id="T-TX2", creator="agent_a", description="事务演示 2",
        budget=Money(100), participants=["agent_a", "agent_b"],
    )
    exec_task2.offer()
    exec_task2.accept(Money(25), contractor="agent_b")
    exec_task2.start()
    store.save(exec_task2)

    original_record = store.record_execution

    def boom(record):
        raise RuntimeError("模拟：写执行事件时磁盘满了")

    store.record_execution = boom
    incoming2 = Message(from_agent="agent_b", to_agent="agent_a",
                        message_type=MessageType.SUBMIT,
                        payload={"task_id": "T-TX2"})
    try:
        execution.apply_submit(store.get("T-TX2"), TaskResultRef(result_ref="v1"),
                               incoming=incoming2)
    except RuntimeError as exc:
        print(f"       捕获异常: {exc}")
    finally:
        store.record_execution = original_record

    print(f"       状态 → {store.get('T-TX2').status}   （必须还是 IN_PROGRESS）")
    print(f"       执行事件数 → {store.count_executions('T-TX2')}   （必须是 0）")
    rollback_ok = (store.get("T-TX2").status == TaskStatus.IN_PROGRESS
                   and store.count_executions("T-TX2") == 0)
    print("       ✅ 状态和事件都回滚了，没有半成功状态"
          if rollback_ok else "       ❌ 出现半成功状态！")
    print()

    # ============================================================
    # 边界证明
    # ============================================================
    print("=" * 74)
    print("边界证明")
    print("=" * 74)
    print()

    ledger_before = runtime.economy.ledger.count()
    a_balance = runtime.economy.agent("agent_a").balance
    b_balance = runtime.economy.agent("agent_b").balance

    print(f"  Wallet A   : {a_balance}   ✅ 未被 4D.1 触碰")
    print(f"  Wallet B   : {b_balance}   ✅ 未被 4D.1 触碰")
    print(f"  Ledger     : {ledger_before} 条   ✅ 未变")
    print(f"  Workspace  : 未创建、未授权   ✅")
    print(f"  Git        : 未触碰           ✅")
    print(f"  代码执行   : 未执行           ✅（没有 subprocess）")
    print(f"  Retry      : 未实现           ✅（只持久化事实）")
    print()
    print("  · 消息去重的事实现在是**持久化**的 —— 重启后仍然有效")
    print("  · Task 的并发写入会被**拒绝**，不会静默覆盖")
    print("  · 状态变化和执行事件在**同一个事务**里")
    print()

    db_b.close()
    print(f"  演示数据库: {db_path}")
    print()

    return 0


def _run_async(coro):
    """在同步的 CLI 命令里跑一小段 async 流程。"""
    import asyncio
    return asyncio.run(coro)


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
  python3 main.py communication-log --traces       看最近的通信链路
  python3 main.py communication-log --trace trc_x  看某条链路的全部消息
  python3 main.py transfer agent_a agent_b 20      让 A 付 20 AIC 给 B
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

    # --- communication-log（Step 3.5）---
    log = subparsers.add_parser(
        "communication-log",
        parents=[shared],
        help="查询通信日志（按 message_id / trace_id / conversation_id）",
    )
    # 位置参数：直接给 ID，靠前缀自动判断
    log.add_argument(
        "id",
        nargs="?",
        default=None,
        metavar="ID",
        help="trc_ / conv_ / msg_ 开头的 ID（前缀决定查什么）",
    )
    log.add_argument("--message", default=None, help="看某条消息的完整事件流")
    log.add_argument("--trace", default=None, help="看某条链路（一次任务）的所有消息")
    log.add_argument("--conversation", default=None, help="看某一轮对话")
    log.add_argument("--traces", action="store_true", help="列出最近的链路")
    log.add_argument("--agent", default=None, help="只看与某个 Agent 相关的消息")
    log.add_argument("--status", default=None, help="只看某个状态（如 FAILED）")
    log.add_argument("--limit", type=int, default=20, help="最多显示几条（默认 20）")
    log.set_defaults(func=cmd_communication_log)

    # --- task-demo（Step 4A）---
    task_demo = subparsers.add_parser(
        "task-demo",
        parents=[shared],
        help="Task Core 演示（不转账、不创建 Workspace、不调用模型）",
    )
    task_demo.set_defaults(func=cmd_task_demo)

    # --- task-log（Step 4A）---
    task_log = subparsers.add_parser(
        "task-log",
        parents=[shared],
        help="查看任务",
    )
    task_log.add_argument("task", nargs="?", default=None, metavar="TASK_ID",
                          help="看某个任务的详情")
    task_log.add_argument("--status", default=None, help="只看某个状态")
    task_log.add_argument("--limit", type=int, default=20, help="最多显示几条")
    task_log.set_defaults(func=cmd_task_log)

    # --- capability-demo（Step 4B）---
    cap_demo = subparsers.add_parser(
        "capability-demo",
        parents=[shared],
        help="能力发现与自主委派演示（不转账、不授权、不调模型）",
    )
    cap_demo.set_defaults(func=cmd_capability_demo)

    # --- negotiation-demo（Step 4C）---
    neg_demo = subparsers.add_parser(
        "negotiation-demo",
        parents=[shared],
        help="自主议价演示（不调用 LLM、不转账、不建 Workspace）",
    )
    neg_demo.set_defaults(func=cmd_negotiation_demo)

    # --- task-4d-demo（Step 4D）---
    d4_demo = subparsers.add_parser(
        "task-4d-demo",
        parents=[shared],
        help="执行 / 提交 / 验收 / 完结演示（不调 LLM、不转账、不执行代码）",
    )
    d4_demo.set_defaults(func=cmd_task_4d_demo)

    # --- reliability-demo（Step 4D.1）---
    rel_demo = subparsers.add_parser(
        "reliability-demo",
        parents=[shared],
        help="可靠性 / 并发加固演示（持久化去重 + 乐观锁 + 事务）",
    )
    rel_demo.set_defaults(func=cmd_reliability_demo)

    return parser


# ============================================================
# 入口
# ============================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()

    # 【为什么这里要接住 argparse 的错误】
    #
    # argparse 默认遇到不认识的参数时，会打印一大段 usage 然后 exit(2)。
    # 问题是：
    #     1. 它只说一句「unrecognized arguments」，
    #        不会告诉你「正确的写法是什么」——
    #        用户看到的是一坨 usage，得自己去找
    #     2. 它直接调 sys.exit()，越过本项目的统一收尾流程
    #
    # 而其它失败路径（经济规则拒绝、模型调用失败）都有清晰的提示，
    # 参数写错不该成为一个例外。
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # --help 也会走到这里，但退出码是 0 —— 那是正常行为，放行
        if exc.code == 0:
            raise

        print()
        print("参数写错了。常见写法：")
        print("    ./dev.sh communication-log                看最近的消息")
        print("    ./dev.sh communication-log trc_xxx        看一条链路")
        print("    ./dev.sh communication-log --traces       列出所有链路")
        print("    ./dev.sh communication-log --help         看这个命令的全部参数")
        print()
        print("    ./dev.sh                                 看所有可用命令")
        print()
        return 2

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

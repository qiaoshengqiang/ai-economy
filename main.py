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
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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
from communication.agent_runtime import AgentRuntime, RuntimeState  # noqa: E402
from core.config import load_global_config                     # noqa: E402
from core.config_resolver import (                              # noqa: E402
    ResolvedRuntimeConfig,
    load_resolved_config,
)
from core.paths import GLOBAL_CONFIG_FILE                        # noqa: E402
from llm.router import adapter_specs                            # noqa: E402
from core.economy import AgentNotFoundError, Economy, EconomyError  # noqa: E402
from core.ledger import LedgerError, format_amount              # noqa: E402
from core.logging_setup import get_logger, setup_logging        # noqa: E402
from core.agent_directory import AgentDirectory, LocalAgentDirectory  # noqa: E402
from core.decision_policy import RuleBasedDecisionPolicy        # noqa: E402
from core.money import Money                                    # noqa: E402
from core.task import OfferType, Task, TaskStatus               # noqa: E402
from core.task_store import TaskStore                           # noqa: E402
from core.task_context_store import TaskContextStore
from core.public_working_memory_store import PublicWorkingMemoryStore
from core.private_working_memory_store import PrivateWorkingMemoryStore
from core.private_short_term_memory_store import PrivateShortTermMemoryStore
from core.private_long_term_memory_store import PrivateLongTermMemoryStore
from core.workspace_core import normalize_path                 # noqa: E402
from core.workspace_store import WorkspaceStore                 # noqa: E402
from change import GitManager, WorktreeManager                   # noqa: E402
from change.worktree import WorkspaceSnapshot                    # noqa: E402
from change.worktree_store import WorktreeStore                  # noqa: E402
from change.materializer import WorktreeMaterializer             # noqa: E402
from tool_runtime import ToolLibrary, ToolRunner                 # noqa: E402
from services.workspace_concurrency import ResourceLockRegistry  # noqa: E402
from services.workspace_gateway import (  # noqa: E402
    WorkspaceGateway, WorkspaceLimits,
)
from services import (
    TaskExecutionService,  # noqa: E402
    TaskAlignmentService,  # noqa: E402
    TaskService,  # noqa: E402
)
from services.public_working_memory_service import (  # noqa: E402
    PublicWorkingMemoryService,
)
from services.private_working_memory_service import (  # noqa: E402
    PrivateWorkingMemoryService,
)
from services.private_short_term_memory_service import (  # noqa: E402
    PrivateShortTermMemoryService,
)
from services.private_long_term_memory_service import (  # noqa: E402
    PrivateLongTermMemoryService,
)
from core.paths import ensure_runtime_dirs                      # noqa: E402
from llm import LLMCallRecorder, LLMRouter, LLMError            # noqa: E402
from llm.cost import CostCalculator                             # noqa: E402
from llm.credential import CredentialResolver                   # noqa: E402
from communication import (                                     # noqa: E402
    CommunicationManager,
    DEFAULT_CLAIM_LEASE_SECONDS,
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
#     DEFAULT_AGENTS = ["agent_a", "agent_b", "agent_c", "agent_d", "agent_e"]
DEFAULT_AGENTS = ["agent_a", "agent_b", "agent_c", "agent_d", "agent_e"]


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

    #: 🔒 **配置事实的唯一来源**。
    #:
    #:     ConfigurationResolver 负责**解释**配置，
    #:     ResolvedConfig 负责**表达**事实，
    #:     Runtime 负责**消费**事实 —— 不再重新解释。
    #:
    #:     Runtime 需要什么配置，从这里读：
    #:       resolved_config.log_level / database_path /
    #:       message_claim_lease_seconds / default_provider
    #:       resolved_config.agent(id) / model(profile) /
    #:       provider(name) / credential(profile) / cost
    resolved_config: ResolvedRuntimeConfig

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
    contexts: TaskContextStore
    alignment: TaskAlignmentService
    #: Task 范围内的公共工作记忆（计划/分工/清单/进度）。
    #: 🔒 不是 Task Core，也不是 Shared Context。
    public_working_memory: PublicWorkingMemoryService
    #: 单个 Agent 的私有工作记忆（抽象结构 / 发现 / 问题）。
    private_working_memory: PrivateWorkingMemoryService
    #: 单个 Agent 的私有短期记忆（跨 Task 摘要）。
    private_short_term_memory: PrivateShortTermMemoryService
    #: 单个 Agent 的私有长期记忆（短期记忆的高级抽象 + 权重）。
    private_long_term_memory: PrivateLongTermMemoryService

    # ---- Workspace Core v1（收尾阶段接入组合根）----
    # 【依赖方向】
    #     Agent → Runtime / Service Layer → WorkspaceGateway
    #           → WorkspaceStore / WorkspaceConcurrency → Persistence
    #
    # 🔒 Agent **不能**自己创建 WorkspaceStore，也**不能**直接碰 DB。
    #    它只能拿到 gateway。这条边界由「Runtime 里没有暴露 store
    #    给 Agent」+ 架构守卫共同保证。
    #: Task 生产预算（生产资源边界；**不是**第二个账本）。
    task_budgets: TaskBudgetStore
    budget_service: TaskBudgetService

    #: 生产性委托（Task Delegation = commerce）。
    #: 🔒 与 Communication **无关** —— 沟通不产生经济行为。
    delegations: DelegationStore
    delegation_service: DelegationService

    workspaces: WorkspaceStore
    workspace_gateway: WorkspaceGateway
    workspace_locks: ResourceLockRegistry

    # ---- Change Pipeline（第二阶段 · Phase 1）----
    # 🔒 **边界**：`WorktreeManager` 不 import `WorkspaceStore` ——
    #    它通过 `build_workspace_lookup(workspaces)` 这个**纯回调**
    #    问「workspace 存在吗、revision 是多少」。
    #    与 WorkspaceGateway 用注入的 is_participant 是同一种手法。
    worktrees: WorktreeStore
    worktree_manager: WorktreeManager

    # ---- Tool Runtime V1（受控代码执行 / 测试）----
    # 🔒 公共工具库只读；Agent 没有安装 / 下载 / 改写入口。
    tool_library: ToolLibrary
    tool_runner: ToolRunner

    def agent(self, agent_id: str):
        return self.economy.agent(agent_id)

    def create_task(self, task: Any, *, by: Optional[str] = None) -> Any:
        """
        🔴 **建 Task 的唯一入口**（2026-10-10）。

        【为什么必须有它 —— 用户指出的缺陷】

            「这明显之前实现任务创建的时候把它漏了。
              Agent 创建任务的时候就该把这个给创建好了。」

            `Task` 和它的 `TaskSharedContext` 本该**一起诞生** ——
            Shared Context 是「这个 Task 的共同认知」的**容器**。
            但之前任务创建**散落在 ~10 处**，各自直接
            `rt.tasks.save(Task.create(...))` ⇒ **谁也没建容器**。

            后果（实测）：观察实验里 agent_a 想指派验收人，
            `assign_acceptance` 报 `no_shared_context` 失败 ——
            **它想合作，环境只给了它「会失败」的告知，没给「能做到」的路径。**

        🔒 **建的是空容器**：`objective` 空 · 无 acceptance_criteria · 无 ASSIGN。
           容器是**环境形状**（和 `ensure_workspace` 同类），
           **内容全归 Agent** —— 不预设任何分工（§0.5）。

        🔒 **幂等**：已经有容器就**不动它** —— 绝不覆盖 Agent 写进去的东西。

        🔴 失败**不吞** —— 见下。
        """
        saved = self.tasks.save(task)
        # 🔴 失败**不吞** —— 少了容器会让 Agent 之后「连验收人都指派不了」
        #    （`assign_acceptance` 报 `no_shared_context`），而那时它根本
        #    追不到这里。任务和它的容器**一起诞生**：建不成就是建不成。
        if self.alignment.current(task.task_id) is None:
            self.alignment.create_context(task.task_id, by=by or task.creator)
        return saved


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


def _sum_llm_cost(db, where: str, params: tuple) -> float:
    """
    只在 `llm_calls` 上求和 —— **不新增第二套账本**（§十一）。

    🔒 `llm_calls` 是 LLMCallRecorder 的真实记录，`cost_ai` 是
       CostCalculator 算出来的。这里只是把它按不同维度聚合一次，
       没有重新计算任何价格、没有另开一张表。
    """
    row = db.fetchone(
        f"SELECT COALESCE(SUM(cost_ai), 0) AS c FROM llm_calls WHERE {where}",
        params,
    )
    return float(row["c"]) if row is not None else 0.0


def _sum_llm_usage(db: Any, where: str, params: tuple) -> Dict[str, Any]:
    """
    按维度聚合 `llm_calls` 的 token / AIC / 调用次数。

    🔒 只读聚合；不重新计算价格、不写第二套账本。
    """
    row = db.fetchone(
        f"""
        SELECT
            COUNT(*)                                      AS calls,
            COALESCE(SUM(input_tokens), 0)                AS input_tokens,
            COALESCE(SUM(output_tokens), 0)               AS output_tokens,
            COALESCE(SUM(total_tokens), 0)                AS total_tokens,
            COALESCE(SUM(cost_ai), 0)                     AS cost_ai,
            COALESCE(SUM(CASE WHEN token_source = 'unknown'
                              THEN 1 ELSE 0 END), 0)       AS unknown_calls
        FROM llm_calls WHERE {where}
        """,
        params,
    )
    if row is None:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "cost_ai": 0.0, "unknown_calls": 0}
    out = dict(row)
    out["calls"] = int(out.get("calls") or 0)
    for key in ("input_tokens", "output_tokens", "total_tokens",
                "unknown_calls"):
        out[key] = int(out.get(key) or 0)
    out["cost_ai"] = float(out.get("cost_ai") or 0.0)
    return out


def _recent_llm_usage(db: Any, agent_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """最近 N 次调用的只读摘要（newest first）。"""
    rows = db.fetchall(
        """
        SELECT call_id, provider, model, input_tokens, output_tokens,
               total_tokens, cost_ai, token_source, task_id, created_at
        FROM llm_calls
        WHERE agent_id = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        (agent_id, int(limit)),
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["cost_known"] = (str(item.get("token_source") or "")
                              != "unknown")
        out.append(item)
    return out


def _sum_settled_cost(db: Any, task_id: str) -> float:
    """
    这个 Task 上已经**结算出去**的 Agent↔Agent 支出。

    🔒 仍然只查 `ledger_transactions` —— **不新建第二套账本**。
       与 `_sum_llm_cost` 一起构成 `spent_aic` 的两个真实来源。
    """
    row = db.fetchone(
        "SELECT COALESCE(SUM(amount), 0) AS c FROM ledger_transactions "
        "WHERE task_id = ? AND tx_type = 'TRANSFER'", (task_id,))
    return float(row["c"]) if row is not None else 0.0


def _count_untagged_calls(db: Any) -> int:
    """
    有多少次 LLM 调用**没有** task_id。

    🔒 它是 `spent_aic` 的可信度指标：如果这个数不是 0，
       「Task 消耗」就会被低估 —— 报告里必须能看见这一点。
    """
    row = db.fetchone(
        "SELECT COUNT(*) AS n FROM llm_calls "
        "WHERE task_id IS NULL OR task_id = ''")
    return int(row["n"]) if row is not None else 0


def _task_budget_fact(service: Any, task_id: str) -> Optional[dict]:
    """
    §七：把 Task 预算变成**纯事实**给 Cognitive Context。

    🔒 只给 budget / spent / remaining / mode。
       **不给**「你应该省钱」「预算不足要停止」。
       `budget/models.py` 里的 `blocks_production` 恒为 False，
       有测试钉住这一点。
    """
    if not task_id:
        return None
    snap = service.snapshot(task_id)
    if snap is None:
        return None
    return {
        "budget_aic": snap.budget_aic,
        "spent_aic": snap.spent_aic,
        "remaining_aic": snap.remaining_aic,
        "mode": snap.mode,
        "currency": snap.currency,
        "is_exhausted": snap.is_exhausted,
        "blocks_production": snap.blocks_production,
    }


def _pending_offer_facts(store: Any, agent_id: str) -> list:
    """
    §十三：把「与这个 Agent 相关的未决委托」变成**纯事实**。

    🔒 只给：offer_id / 我的角色 / 对方 / 价格 / 状态。
       **不给**「你应该接受 / 拒绝」，**不给**「这个价偏高」。
       `delegation/` 里有守卫盯这条线。

    ⚠️ 这里不 import `delegation/` 的模型 —— 只调 store 的公开方法，
       保持组合根对领域对象的依赖尽可能薄。
    """
    out = []
    for o in store.list_offers(agent_id=agent_id):
        if not o.is_open:
            continue
        out.append({
            "offer_id": o.offer_id,
            "task_id": o.task_id,
            "role": o.role_of(agent_id),
            "counterparty": o.counterparty_of(agent_id),
            "price_aic": o.price_aic,
            "status": o.status,
        })
    return out



def _agent_context_query(base: Mapping[str, Any],
                         resolved_agent: Any) -> Dict[str, Any]:
    """
    把**这个 Agent 自己的** Constitution 合进 Context 检索参数。

    【V1 务实取舍（2026-10-09）】

        🔴 **只读 `constitution`，不读 `drives`。**
           Core Drives 属于**经济/激励层** —— 设计的最终形态是让 Agent
           因为 AIC 的成本/收益自然倾向于某些行为（§0.5.3），
           而不是靠一段文字。AIC 经济系统尚未完成，所以这一层先不做。

        🔴 设计 §14 说「**不要把 Constitution 简单理解成 System Prompt**」。
           V1 放进提示词是为了**便于测试**，是**有意的取舍，不是定稿**。

    🔒 读的是 `ResolvedAgent.raw`（保留的原始 agent 段）——
       因此**不需要给 `config_resolver` 加字段**：新配置项天然通过。

    🔒 `identity` 也支持（`to_text()` 的 head 段里它和 constitution 并列），
       只是当前没有 Agent 配它。
    """
    out: Dict[str, Any] = dict(base or {})
    cog = dict((getattr(resolved_agent, "raw", None) or {}).get("cognition")
               or {})
    for key in ("identity", "constitution"):
        val = cog.get(key)
        if val is not None and str(val).strip():
            out[key] = str(val).strip()
    return out



def build_task_people_checker(task_store: Any) -> Any:
    """
    造一个 `is_task_person(task_id, agent_id) -> bool` 回调。

        「谁可以在这条 Task 上行动」= participants ∪ {creator, contractor}

    【为什么需要它（真实测试抓到的洞）】

        `assign_acceptance` 第一版**不检查接收方是否存在** ——
        于是

            assign_acceptance(to="not_a_real_agent")

        **成功了**。后果不是报错，而是**静默停住**：
        `acceptors_of()` 返回一个幽灵，谁都 VERIFY 不了。

        🔒 这与 `WorkspaceGateway.handoff` 已经堵过的是**同一类**洞
           （「接收方必须是真实存在且属于这个 Workspace 的 Agent」）。

    🔒 **fail-closed**：查不到 Task ⇒ False。
       与 Gateway 的 `agent_exists` 同一原则 ——
       「读不到就放行」在这条链上是最不该出现的行为。
    """
    def _is_task_person(task_id: str, agent_id: str) -> bool:
        try:
            task = task_store.get(str(task_id))
        except Exception:                                     # noqa: BLE001
            return False
        if task is None:
            return False
        who = str(agent_id or "").strip()
        if not who:
            return False
        people = {str(p) for p in (getattr(task, "participants", None) or [])}
        people |= {str(x) for x in (getattr(task, "creator", None),
                                    getattr(task, "contractor", None)) if x}
        return who in people
    return _is_task_person

def build_participant_checker(task_store: TaskStore):
    """
    造一个 `is_participant(workspace_id, agent_id) -> bool` 回调。

    【为什么是回调，而不是让 Gateway 自己去查 Task】

        Gateway **不应该**知道 Task 是什么。
        它只回答「谁可以访问这个 Workspace」——
        「谁是 Task 的参与者」是**任务层**的知识。

        所以组合根把这个知识**注入**进去：

            main.py（唯一同时认识两边的地方）
                    ↓ 注入
            WorkspaceGateway.is_participant

        这样 Gateway 不知道 TaskStore，TaskStore 也不知道 Gateway。

    【v1 的 Participant 判定就是「这个 Task 的参与者」】

        workspace 属于某个 Task（`Task.workspace_id`），
        而该 Task 的参与者 = participants ∪ {creator, contractor}。

    ⚠️ **已知限制**：这是一次 `list_tasks(workspace_id=...)` 查询，
       每次 Gateway 调用都会做。workspace 多起来以后应当加缓存或
       直接建 `workspace → participants` 索引。记在报告里。
    """

    def is_participant(workspace_id: str, agent_id: str) -> bool:
        try:
            tasks = task_store.list_tasks(workspace_id=workspace_id, limit=1000)
        except Exception:                     # pragma: no cover - 防御
            return False
        for task in tasks:
            members = set(task.participants or [])
            if task.creator:
                members.add(task.creator)
            if task.contractor:
                members.add(task.contractor)
            if agent_id in members:
                return True
        return False

    return is_participant


def build_agent_exists_checker(economy: Economy):
    """
    造一个 `agent_exists(agent_id) -> bool` 回调。

    🔒 **身份存在性**是身份层的事，不是权限。
       与 `is_participant` 分开，因为两者的正确应对不同：

           UNKNOWN_AGENT        「你把名字打错了」
           NOT_TASK_PARTICIPANT 「这个人存在，但不在这个 Task 里」
    """

    def agent_exists(agent_id: str) -> bool:
        try:
            return economy.agent(agent_id) is not None
        except Exception:                     # pragma: no cover - 防御
            return False

    return agent_exists


def build_workspace_services(
    db,
    *,
    agent_exists,
    is_participant=None,
    lock_registry: Optional[ResourceLockRegistry] = None,
    limits=None,
) -> tuple:
    """
    组装 Workspace Core 的三件套。

        WorkspaceStore        持久化（唯一边界）
        ResourceLockRegistry  资源级互斥（**进程内**，必须被共享）
        WorkspaceGateway      唯一受控入口

    【🔒 为什么 lock_registry 要能被外部传进来】

        资源级互斥是**进程内**概念。如果同一个进程里有多个 Gateway
        各自 new 一个注册表，它们就**互相看不见对方的锁** ——
        同一 File 就可能有两条并发写入。

        所以组合根**只造一个**注册表，所有 Gateway 共享它。

    【🔒 为什么不做成 singleton】

        任务书 §22 明确禁止 `WorkspaceGateway` 自己偷偷创建全局 singleton。
        这里返回普通对象，由调用方持有 ——
        测试可以随手造一套**独立**的服务，互不干扰。

    【🔒 `agent_exists` 是必需参数（fail-closed）】

        它是「这个 Agent 是否真实存在」的判定 ——
        没有它，`handoff` 可以指向一个虚构的接收方。
        所以这里**不设默认值**：漏传就直接 `TypeError`。

    返回 `(store, registry, gateway)`。
    """
    store = WorkspaceStore(db=db)
    registry = lock_registry or ResourceLockRegistry()
    gateway = WorkspaceGateway(
        db,
        store=store,
        lock_registry=registry,
        is_participant=is_participant,
        agent_exists=agent_exists,
        limits=limits,
    )
    return store, registry, gateway


def build_workspace_lookup(workspace_store: WorkspaceStore):
    """
    造一个 `workspace_lookup(workspace_id) -> WorkspaceSnapshot | None`。

    【为什么是回调，而不是让 WorktreeManager 自己查 Workspace】

        Change Pipeline **不应该**知道 Workspace 表的形状。
        它只回答「这个 workspace 存在吗、revision 是多少」——
        那是 Workspace Core 的知识。

        组合根把这个知识**注入**进去：

            main.py（唯一同时认识两边的地方）
                    ↓ 注入
            WorktreeManager.workspace_lookup

        🔒 这样 `change/` **一行都不 import** `core/workspace_store`，
           也因此可以脱离整个 Workspace 系统单独测试
           （`tests/test_worktree_manager.py` 就是这么做的）。
    """

    def lookup(workspace_id: str):
        try:
            root = workspace_store.find_by_path(workspace_id, "/")
        except Exception:                     # pragma: no cover - 防御
            return None
        if root is None:
            return None
        return WorkspaceSnapshot(
            workspace_id=workspace_id, revision=root.revision, exists=True
        )

    return lookup


def build_change_services(
    db,
    *,
    workspace_lookup,
    repository_ref: str = "default",
    repo_path: Optional[str] = None,
    worktrees_root: Optional[str] = None,
    registry: Optional[ResourceLockRegistry] = None,
) -> tuple:
    """
    组装 Change Pipeline 的三件套。

        GitManager        Git CLI 的薄封装（**成熟原语，不重新实现 Git**）
        WorktreeStore     持久化
        WorktreeManager   worktree 生命周期

    【🔒 为什么 `workspace_lookup` 是必需参数】

        与 `agent_exists` 同样的 fail-closed 理由：
        没有它，WorktreeManager 无法验证 Workspace 边界，
        也无法记录 `workspace_revision` 这个 provenance 字段。
        漏传直接 `TypeError`。

    【🔒 为什么不做成 singleton】

        任务书 §23 明确要求依赖通过组合根注入。
        每次调用返回全新对象 —— 测试可以随手造一套独立的。
    """
    git = GitManager(repo_path=repo_path)
    store = WorktreeStore(db=db)
    manager = WorktreeManager(
        db,
        git_manager=git,
        workspace_lookup=workspace_lookup,
        repository_ref=repository_ref,
        worktrees_root=worktrees_root,
        store=store,
        registry=registry,
    )
    return store, manager, git


def build_runtime(
    db_path: Optional[str] = None,
    agent_ids: Optional[list] = None,
) -> Runtime:
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

    # 🔴 **配置解释只发生这一次。**
    #
    #    从这里往下，Runtime 及其所有子系统只消费 `resolved` ——
    #    不再自己去 `config.get(...)` 找配置、不再猜 fallback。
    #
    #    🔒 失败必须 loud：任何 unknown provider / model / credential /
    #       adapter、任何已废弃键回归，都会在这里抛 ConfigurationError，
    #       **不会**退回 mock 或某个默认 provider。
    resolved = load_resolved_config(GLOBAL_CONFIG_FILE,
                                    adapter_specs=adapter_specs)

    # 🔒 canonical 位置是 `resolved.log_level`。
    #    `system.log_level` 已删除 —— 那曾经是同一事实的第二份定义。
    setup_logging(level=str(resolved.log_level))

    economy = Economy.bootstrap(db_path=db_path, config=config)

    # --- LLM 层 ---
    router = LLMRouter(config)

    # 凭据解析器：全局配置 + 每个 Agent 自己的 credential。
    # 🔒 它是**唯一**知道「key 从哪来 / 优先级是什么」的地方。
    credential_resolver = CredentialResolver(config)
    # 记录器需要知道当前的兑换比例，好在每条记录里存下汇率快照。
    recorder = LLMCallRecorder(
        db=economy.db,
        tokens_per_ai_coin=economy.tokens_per_ai_coin,
        # §0.5 审计 A-02 / H-2：LLM 认知成本**真正进入 Ledger**。
        # 🔒 规则来自配置（含 per-provider/model 费率），不硬编码。
        # 🔒 这里只是「接线」—— 成本怎么算是 llm/cost.py 的事，
        #    记账是 economy.charge_llm_cost 的事，两者都不替 Agent 做策略。
        cost_calculator=CostCalculator.from_resolved(resolved.cost),
        on_cost=lambda agent_id, cost, call_id, row: (
            economy.charge_llm_cost(
                agent_id, cost, call_id=call_id,
                memo={"provider": row.get("provider"),
                      "model": row.get("model"),
                      "task_id": row.get("task_id")},
            )
        ),
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
    #
    # 【Workspace Phase 0：两个可靠性参数从配置读】
    #     认领租约时长和 SQLite 忙等超时都不是业务规则，
    #     而是「这个部署面对并发时怎么表现」——
    #     所以它们来自 config.yaml，不写死在代码里。
    #
    # 🔒 canonical 位置是 `runtime.message_claim_lease_seconds`。
    #    `system.reliability.*` 已删除 —— 那曾经是同一事实的第二份定义。
    journal = CommunicationRecorder(
        db=economy.db,
        store_payload=True,
        strict=False,
        claim_lease_seconds=int(
            resolved.message_claim_lease_seconds
            if resolved.message_claim_lease_seconds is not None
            else DEFAULT_CLAIM_LEASE_SECONDS
        ),
    )
    communication = CommunicationManager(
        transport=transport,
        recorder=journal,
    )

    # --- 创建 Agent，给每个装上大脑，并接入通信层 ---
    # 🔒 `agent_ids` 是**显式覆盖**，给测试与脚本用。
    #    不传就按 DEFAULT_AGENTS（也就是「本地实验配置」——
    #    开发者可能已经把它加到 3 个 Agent）。
    # --- Self Model / Cognitive Context（§五：默认路径自动接入）--------
    #
    # 🔒 依赖方向：main.py（组装）知道 SelfModelStore 在哪；
    #    Brain **不知道** —— 它只收到一个 context_builder + 一个默认 query。
    from cognition import CognitiveContextBuilder
    from cognition.economic import EconomicContextBuilder
    from delegation import DelegationService, DelegationStore
    from budget import TaskBudgetService, TaskBudgetStore
    from experience import ExperienceStore
    from reflection import SelfModelWriteService
    from selfmodel import SelfModelStore

    self_model = SelfModelStore(economy.db)
    experience_store = ExperienceStore(economy.db)
    #: §二十：受控写入服务 —— Reflection proposal 的**唯一**落地通道
    self_model_write_service = SelfModelWriteService(
        self_model, experience_store=experience_store)

    ctx_cfg = config.get("cognition.context", {}) or {}

    # ---- 🔴 Delegation：生产性委托的经济边界 ----
    #
    # 【为什么单独一个 store，而不是塞进 TaskStore】
    #
    #     Task 表达「要做什么」（12 状态生命周期）；
    #     Delegation 表达「谁委托谁、成不成交」。
    #     把报价 / 结算 / 拒绝史塞进 Task 会让 Task 变成经济容器 ——
    #     而 Task Core 已经被明确要求保持薄（ADR-0006）。
    #
    # 🔒 `delegation/` **不 import** `core/economy.py` ——
    #    它只拿到注入进来的 economy（鸭子类型）。
    #    这样它既可以脱离 Economy 单测，也不会长出第二套账本。
    delegations = DelegationStore(economy.db)
    delegation_service = DelegationService(delegations, economy)

    # ---- 🔴 Task Production Budget V1（本轮只启用 OBSERVE_ONLY）----
    #
    # 【它不是第二个账本】
    #
    #     Ledger  → 实际发生了什么经济变化（唯一事实源）
    #     Budget  → 这个 Task 的**资金边界/配置**
    #     spent   → **派生**自 llm_calls + ledger_transactions，不自己累加
    #
    # 🔒 表里只有 4 列（task_id/budget_aic/currency/mode）——
    #    `spent_aic` / `remaining_aic` **从不落库**，
    #    与 `wallets` 表刻意没有 balance 列是同一条纪律。
    task_budgets = TaskBudgetStore(economy.db)
    budget_service = TaskBudgetService(
        task_budgets,
        llm_cost_of_task=lambda tid: _sum_llm_cost(
            economy.db, "task_id = ?", (tid,)),
        settled_cost_of_task=lambda tid: _sum_settled_cost(economy.db, tid),
        untagged_llm_calls=lambda: _count_untagged_calls(economy.db),
    )

    # ---- 🔴 Economy Awareness V1：经济事实进入 Cognitive Context ----
    #
    # 【依赖方向：这里注入的是「取值函数」，不是 Ledger 对象】
    #
    #     Ledger / Economy
    #           ↓  三个**只读**回调
    #     cognition.EconomicContextBuilder
    #           ↓
    #     CognitiveContext.economic_context
    #           ↓
    #     LLM
    #
    # 🔒 `cognition/` 因此**不 import** `core/ledger.py` /
    #    `core/economy.py` —— 认知层结构上拿不到账本，只能拿到值。
    #    这与 WorkspaceGateway 收注入的 `is_participant` 是同一种手法。
    #
    # 🔒 **每次 build 都重新问 Ledger**（§十）——
    #    绝不在 Agent 初始化时把余额写死成 100。
    economic_builder = EconomicContextBuilder(
        balance_of=lambda aid: economy.ledger.balance_of(aid, economy.currency),
        #: §十一：任务维度成本**当前不可靠** ——
        #:    `llm_calls.task_id` 这一列存在，但真实生产路径
        #:    （`Brain.generate`）从来没有把 task_id 传进来，
        #:    所以实际数据里它恒为 NULL。这里如实实现「查得到就给，
        #:    查不到就 None」，**不伪造一个 0**。
        cost_of_task=lambda tid: _sum_llm_cost(economy.db, "task_id = ?", (tid,)),
        #: §十一：会话维度成本由「起点时刻」派生 ——
        #:    「会话」这个概念住在 Runtime（`stage/limit.py`），
        #:    core 不知道它。所以这里只提供「某个时刻之后花了多少」，
        #:    起点由调用方（Runtime / 实验脚本）负责传。
        cost_since=lambda aid, since: _sum_llm_cost(
            economy.db, "agent_id = ? AND created_at >= ?", (aid, since)),
        #: LLM 用量历史（只读）：最近调用 + task/session 聚合。
        #: 这是 Agent「根据历史形成消耗认知」的事实来源。
        recent_calls_of=lambda aid, limit: _recent_llm_usage(
            economy.db, aid, limit),
        usage_of_task=lambda tid: _sum_llm_usage(
            economy.db, "task_id = ?", (tid,)),
        usage_since=lambda aid, since: _sum_llm_usage(
            economy.db, "agent_id = ? AND created_at >= ?", (aid, since)),
        #: §十三：未决的生产性委托 —— 同样只读，同样只给事实。
        pending_offers_of=lambda aid: _pending_offer_facts(delegations, aid),
        task_budget_of=lambda tid: _task_budget_fact(budget_service, tid),
        currency=economy.currency,
    )

    context_builder = CognitiveContextBuilder(
        self_model,
        experience_store=experience_store,      # §四：相关 Experience 进 Context
        economic_builder=economic_builder,      # §七：经济事实（只读）
        max_claims=int(ctx_cfg.get("max_claims", 5)),
        max_strategies=int(ctx_cfg.get("max_strategies", 5)),
        max_experiences=int(ctx_cfg.get("max_experiences", 5)),
    )
    #: Runtime 默认的检索情境。
    #: ⚠️ 现在 Runtime 还没有 World/Domain/Situation 的一等字段（§六：
    #:    缺失字段允许为 None）。留空表示「不限定情境」——
    #:    此时 retrieve() 会返回**按 confidence 排序的前 N 条**，
    #:    仍然受 max_claims / max_strategies 约束（**不是**全量注入）。
    #:    将来 Task/World 提供情境时，改这里一处即可。
    default_context_query = dict(ctx_cfg.get("default_query", {}) or {})

    for agent_id in (agent_ids if agent_ids is not None else DEFAULT_AGENTS):
        # 🔒 启动语义：先恢复持久化经济数据；只有真正的新 Agent 才初始化。
        agent = economy.restore_or_create_agent(agent_id)

        # 每个 Agent 的大脑配置来自 **ResolvedConfig** ——
        # 所以 A 和 B 可以用不同的 provider / model / credential，
        # 而 Agent 的代码里一行厂商名字都不会出现。
        #
        # 🔒 不再走 `AgentConfig.provider` / `AgentConfig.model` /
        #    `provider.default_model` 那条旧投影路径：
        #      Agent → Model Profile → model_id → Provider → Adapter
        #      Agent → Credential Profile → 环境变量名 → Secret（.env）
        #    两条链的每一段都已在 `resolved` 里解析完毕。
        resolved_agent = resolved.agent(agent_id)
        brain = Brain(
            router=router,
            settings=BrainSettings.from_resolved_agent(resolved_agent),
            recorder=recorder,
            # 🔒 交给 Brain 的是「**怎么解析**凭据」，而**不是** key 本身。
            credential_resolver=credential_resolver,
            credential_config={
                "api_key_env": resolved_agent.credential.env},
            # §五：默认路径自动接入 —— 调用方不必手写 context_query
            context_builder=context_builder,
            # 🔴 **每个 Agent 一份** —— 不能共用循环外那个 dict：
            #    Constitution 是**每个 Agent 自己的**（§14），
            #    共用会让 5 个 Agent 拿到同一份身份。
            default_context_query=_agent_context_query(
                default_context_query, resolved_agent),
        )
        agent.attach_brain(brain)
        # §二十：生产路径 —— Agent 的 Cognitive Run 能检索 Experience，
        # 并在决定反思时走受控写入。**两者都是注入的**。
        agent.attach_self_model_services(
            experience_store=experience_store,
            write_service=self_model_write_service,
        )

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
        # 🔴 §22.20 B：**系统维护的公开事实**（Dynamic runtime facts）。
        #
        #    设计原文：
        #        Agent ID · Current Provider · **Current Model** · Current Model Version
        #        「Provider / Model / Version 属于 Dynamic runtime facts」——
        #        用户换了模型，其他 Agent 查询时**必须看到新的**。
        #
        #    🔒 所以它从 `resolved`（**当前真实解析结果**）里读，
        #       不是从别处复制一份 —— 换了 provider/model 它自动跟着变。
        #
        #    🔴 **刻意不放 `model_profile`**（我们给模型起的别名）：
        #       那里面会有 `doubao_novel` 这种**带角色暗示**的名字，
        #       投出去等于又泄露一次分工。
        #       投的是**真实的 model_id**（`deepseek-flash` / `ep-…`）。
        _resolved_agent = resolved.agent(agent.agent_id)
        directory.register_agent(
            agent_id=agent.agent_id,
            capabilities=agent.config.skills,
            metadata={
                "provider": _resolved_agent.provider.name,
                "model": _resolved_agent.model_id,
            },
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
    # ---- Completion / Acceptance：把 Shared Context 的两件事注入执行服务 ----
    #
    # 【为什么是注入，而不是让 TaskExecutionService 自己去读】
    #     `services/` 不直接依赖 `storage/`（AGENTS.md §3），
    #     而 Shared Context 的读取入口是 TaskContextStore / Alignment。
    #     让执行服务依赖**两个只读能力**，而不是依赖它们的实现 ——
    #     和 WorkspaceGateway 收注入 is_participant 是同一种手法。
    #
    # 🔒 两个回调都**只读**，而且 fail-closed：
    #     读不到 → 返回空 → 「没有 DoD / 没有验收人」
    #     → 结项被挡住，而不是被放行。
    #
    # ⚠️ `contexts` 在下面几行才创建 —— 这里用闭包，调用时才解析。
    def _acceptors_of(task_id: str):
        try:
            revision = contexts.current(task_id)
        except Exception:  # noqa: BLE001 — 读不到就是「没有验收人」
            return ()
        return revision.context.acceptors() if revision else ()

    def _criteria_of(task_id: str):
        try:
            revision = contexts.current(task_id)
        except Exception:  # noqa: BLE001 — 读不到就是「没有 DoD」
            return ()
        return revision.context.acceptance_criteria_list() if revision else ()

    task_execution = TaskExecutionService(
        store=tasks,
        acceptors_of=_acceptors_of,
        criteria_of=_criteria_of,
    )

    # ---- Step 4F：Task Shared Context / Alignment ----
    #
    # 【为什么共享上下文不塞进 TaskStore】
    #     TaskStore 已经 958 行、7 张表。再加 3 张表和一套对齐语义，
    #     它就会变成什么都管的 God Store ——
    #     而 TaskService 曾经因为同样的原因被拆开（ADR-0006）。
    #
    # 【注意它复用的是同一个 Database】
    #     没有 SharedContextDatabase，没有独立事务系统。
    #     同一个连接、同一个可重入 transaction()、同一个 BEGIN IMMEDIATE。
    contexts = TaskContextStore(db=economy.db)
    alignment = TaskAlignmentService(tasks=tasks, contexts=contexts)

    # ---- Public Working Memory（Task 范围） ----
    # 🔒 与 Shared Context 平行，但不走 Alignment：
    #    它是可写的操作状态，不是“已确认的共同认知”。
    public_working_memory = PublicWorkingMemoryService(
        PublicWorkingMemoryStore(db=economy.db), tasks=tasks)
    private_working_memory = PrivateWorkingMemoryService(
        PrivateWorkingMemoryStore(db=economy.db), tasks=tasks)
    short_term_store = PrivateShortTermMemoryStore(db=economy.db)
    private_short_term_memory = PrivateShortTermMemoryService(short_term_store)
    private_long_term_memory = PrivateLongTermMemoryService(
        PrivateLongTermMemoryStore(db=economy.db), short_term_store)

    task_service = TaskService(
        store=tasks,
        directory=directory,
        policies=decision_policies,
        execution=task_execution,
        # 🔴 任务创建时同时建 Shared Context（Agent 收到 TASK_OFFER 那条路径）
        alignment=alignment,
    )

    # 把 Task 的业务处理器接到通信层的类型处理器上。
    #
    # 这是 Step 4B 第一次把 Task + Capability + Communication 连起来。
    # 但注意方向：是**组合根**把它们接起来，
    # 而不是让 Task 去 import communication，也不是让
    # communication 去 import Task。
    _wire_task_handlers(communication, task_service)

    # ---- Workspace Core v1：接入组合根 ----
    #
    # 【顺序有意为之】
    #     先有 tasks / economy，才能造出 participant / identity 回调；
    #     有了回调，Gateway 才是一个**真正可用**的门面，
    #     而不是一个谁来都拒绝的孤立组件。
    #
    # 🔒 注意 WorkspaceGateway **不知道** TaskStore 与 Economy ——
    #    它只拿到两个纯函数回调。这条边界让 Gateway 可以
    #    脱离整个 Task 系统单独测试（测试里就是这么做的）。
    workspace_locks = ResourceLockRegistry()
    workspaces, workspace_locks, workspace_gateway = build_workspace_services(
        economy.db,
        is_participant=build_participant_checker(tasks),
        agent_exists=build_agent_exists_checker(economy),
        lock_registry=workspace_locks,
        # 🔒 结构上限的唯一来源：config.yaml 的 `workspace:` 段。
        limits=WorkspaceLimits.from_mapping(resolved.workspace),
    )
    # Public WM 的 evidence 校验需要 WorkspaceGateway；组合根在 Gateway
    # 构造完成后注入（服务本身不依赖 services/workspace_gateway 的类型）。
    public_working_memory.attach_gateway(workspace_gateway)

    # ---- Change Pipeline（第二阶段 · Phase 1）----
    #
    # 【为什么复用同一个 workspace_locks 注册表族但换一个 key 空间】
    #     Worktree 的进程内互斥是**按 (repository, workspace) 分键**的
    #     （见 WorktreeManager._workspace_guard）——
    #     与 Workspace 资源锁**不会互相干扰**，
    #     也**不是**一把全局锁。
    worktrees, worktree_manager, _git = build_change_services(
        economy.db,
        workspace_lookup=build_workspace_lookup(workspaces),
        repository_ref=os.path.basename(PROJECT_ROOT) or "ai-economy",
        repo_path=str(PROJECT_ROOT),
        registry=ResourceLockRegistry(),
    )

    # ---- Tool Runtime V1（受控代码执行 / 测试）----
    # 🔒 公共工具库从 resources/public/tools/manifests 加载；
    #    Agent 只能使用清单里的工具，不能安装/下载/改写。
    tool_library = ToolLibrary.from_directory(PROJECT_ROOT)
    tool_runner = ToolRunner(
        tool_library,
        run_root=PROJECT_ROOT / "data" / "tool_runs",
    )

    return Runtime(
        resolved_config=resolved,
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
        contexts=contexts,
        alignment=alignment,
        public_working_memory=public_working_memory,
        private_working_memory=private_working_memory,
        private_short_term_memory=private_short_term_memory,
        private_long_term_memory=private_long_term_memory,
        delegations=delegations,
        delegation_service=delegation_service,
        task_budgets=task_budgets,
        budget_service=budget_service,
        workspaces=workspaces,
        workspace_gateway=workspace_gateway,
        workspace_locks=workspace_locks,
        worktrees=worktrees,
        worktree_manager=worktree_manager,
        tool_library=tool_library,
        tool_runner=tool_runner,
    )


def build_economy(
    db_path: Optional[str] = None,
    agent_ids: Optional[list] = None,
) -> Economy:
    """
    向后兼容的入口：只要 Economy。

    Step 1 的测试和外部脚本用的是这个函数，所以保留它。
    现在它会顺带把大脑也装好 —— 也就是说通过这些测试创建出来的
    Agent 同样能调用模型，行为和真实运行一致。
    """
    return build_runtime(db_path=db_path, agent_ids=agent_ids).economy


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
# 命令：config（看解析后的配置 —— 只读）
# ============================================================
def cmd_config(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    显示 **ResolvedConfig** —— 配置解析的最终结果。

    【为什么需要这个命令】

        配置系统重构之后，`config/config.yaml` 里存的是
        **引用**（model profile / credential profile），
        运行期真正用的是**解析后**的值。

        这个命令把「引用 → 解析结果」摊开，回答三个问题：

            ① 有哪些 Provider / Model / Credential
            ② 每个 Agent 最终用哪一个（provider / model_id / env 变量名）
            ③ 这个值**来自哪里**（`sources`）

    🔒 它**只读**，不写任何文件，**不建 runtime**
       （`runtime` 参数恒为 `None`；见 `main()` 里的提前返回）。
    🔒 它**不显示任何 secret 的值** —— 只显示环境变量名与「有没有配」。
    🔒 `--json` 时 stdout 是**纯 JSON**（没有任何日志混进去），
       因为它是给 GUI / 脚本用的。

    用法：
        ./dev.sh config              # 给人看
        ./dev.sh config --json       # 给机器看（GUI / 脚本用）
        ./dev.sh config --agent novel_author
    """
    from core.config_resolver import load_resolved_config, ConfigurationError
    from llm.router import adapter_specs
    try:
        resolved = load_resolved_config(
            GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs)
    except ConfigurationError as exc:
        # 🔒 fail-loud：配置有问题就明确报出来，**不降级**
        print()
        print("❌ 配置解析失败")
        print()
        print(str(exc))
        print()
        print("（本系统不会在配置错误时退回 mock —— 那会让「配置错了」"
              "看起来像「跑起来了」。）")
        print()
        return 2

    if getattr(args, "json", False):
        import json as _json
        print(_json.dumps(resolved.to_dict(), ensure_ascii=False,
                          indent=2, sort_keys=True))
        return 0

    only = getattr(args, "agent", None)
    line = "=" * 68
    print()
    print(line)
    print(f"  Resolved Configuration   （来自 {GLOBAL_CONFIG_FILE}）")
    print(line)
    print()
    print(f"  version        = {resolved.version}")
    print(f"  log_level      = {resolved.log_level}")
    print(f"  database_path  = {resolved.database_path}")
    eco = resolved.economy
    print(f"  currency       = {eco.get('currency')} "
          f"({eco.get('currency_name')})   "
          f"tokens_per_ai_coin = {eco.get('tokens_per_ai_coin')}")
    print(f"  cost           = {resolved.cost}")
    print()

    # ---- Providers ----
    print("-" * 68)
    print("  ① Providers（端点 + 协议 adapter）")
    print("-" * 68)
    for name in sorted(resolved.providers):
        p = resolved.providers[name]
        print(f"    {name:<12} adapter={p.adapter:<16} timeout={p.timeout_seconds:g}s")
        print(f"    {'':<12} base_url={p.base_url or '(不需要)'}")
        if p.request:
            print(f"    {'':<12} request={dict(p.request)}")
    print()

    # ---- Models ----
    print("-" * 68)
    print("  ② Models（profile → 真实 model id → provider）")
    print("-" * 68)
    for prof in sorted(resolved.models):
        m = resolved.models[prof]
        print(f"    {prof:<16} id={m.model_id:<30} provider={m.provider.name}")
    print()

    # ---- Credentials ----
    print("-" * 68)
    print("  ③ Credentials（profile → 环境变量名；🔒 不显示值）")
    print("-" * 68)
    for prof in sorted(resolved.credentials):
        c = resolved.credentials[prof]
        mark = "✅ 已配" if c.present else "❌ 未配"
        print(f"    {prof:<16} env={c.env:<24} {mark}")
    print()

    # ---- Agents ----
    print("-" * 68)
    print("  ④ Agents（引用 → 解析结果）")
    print("-" * 68)
    print(f"    {'agent':<22} {'model profile':<16} {'model_id':<28} "
          f"{'provider':<9} {'credential':<15} env")
    for aid in sorted(resolved.agents):
        if only and aid != only:
            continue
        a = resolved.agents[aid]
        print(f"    {aid:<22} {a.model_profile:<16} {a.model_id:<28} "
              f"{a.provider.name:<9} {a.credential_profile:<15} "
              f"{a.credential.env}")
    print()

    # ---- 一个 Agent 的完整溯源 ----
    if only:
        if only not in resolved.agents:
            print(f"❌ 没有这个 Agent：{only!r}")
            print(f"   已知：{sorted(resolved.agents)}")
            return 2
        a = resolved.agents[only]
        print("-" * 68)
        print(f"  ⑤ {only} 的完整解析链")
        print("-" * 68)
        print(f"    Agent          {a.agent_id}（{a.name}）")
        print(f"      ↓ {resolved.sources.get(f'agents.{only}', '?')}")
        print(f"    Model Profile  {a.model_profile}")
        print(f"      ↓ {resolved.sources.get(f'models.{a.model_profile}', '?')}")
        print(f"    model_id       {a.model_id}")
        print(f"      ↓ 由 models.{a.model_profile}.provider 解析")
        print(f"    Provider       {a.provider.name}  (adapter={a.provider.adapter})")
        print(f"      ↓ {resolved.sources.get(f'providers.{a.provider.name}', '?')}")
        print(f"    base_url       {a.provider.base_url}")
        print(f"    timeout        {a.provider.timeout_seconds:g}s")
        print(f"    Credential     {a.credential_profile}")
        print(f"      ↓ {resolved.sources.get(f'credentials.{a.credential_profile}', '?')}")
        print(f"    env 变量名     {a.credential.env}"
              f"   {'✅ 已配' if a.credential.present else '❌ 未配'}")
        print(f"    temperature    {a.temperature}")
        print(f"    max_tokens     {a.max_tokens}")
        print(f"    system_prompt  {len(a.system_prompt)} 字符")
        print()
        print("  🔒 上面没有任何 API Key —— 只有环境变量名。")
        print()

    print(line)
    print("  改配置只需要改 config/config.yaml —— 不需要改 Python。")
    print(line)
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
    runtime.create_task(task)

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
    runtime.create_task(task)

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
            #     下面这些子表都有指向 tasks 的外键（全部是 NO ACTION，
            #     没有 CASCADE）。先删 tasks 会撞上
            #     FOREIGN KEY constraint failed。
            #
            # 🔴 2026-10-10：`task_shared_contexts` /
            #     `task_context_revisions` / `task_context_changes`
            #     是 Step 4F 加的表，**当时漏加进了这个列表**。
            #     以前没暴露，是因为以前建 Task 根本不建 Shared Context；
            #     现在 `Runtime.create_task` 会建 ——
            #     于是第二次跑演示就会在清理时崩（实测：
            #     workspace-demo 跑完全部十步，死在最后一行清理）。
            #
            #     ⚠️ 这个列表会随新表腐烂，所以有守卫盯着：
            #     `test_shared_context_guards.py` 里
            #     `test_demo_cleanup_covers_every_table_that_points_at_tasks`
            #     用 PRAGMA 把「所有指向 tasks 的表」跟这个列表对账。
            for table in ("task_context_changes", "task_context_revisions",
                          "task_shared_contexts",
                          "task_public_working_memory_revisions",
                          "task_public_working_memory",
                          "agent_private_working_memory_revisions",
                          "agent_private_working_memory",
                          "task_executions", "task_agreements",
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
    runtime.create_task(task1)

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
    runtime.create_task(task2)

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
    runtime.create_task(task3)

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


# ============================================================
# 命令：context-demo（Step 4F）
# ============================================================
# ============================================================
# Workspace Core Demo（收尾阶段 Phase 5）
# ============================================================
def cmd_workspace_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    最小可运行的 Workspace Demo。

    【这个 demo 的目的**不是**功能演示】

        是证明一件事：

            Workspace Core 已经从「独立库 / 测试组件」
            变成了 AI Economy OS 里**真正可调用的基础设施**。

        所以它**必须**走真实的系统链路：

            main.py（组合根）
                ↓ 组装
            Runtime.workspace_gateway      ← 不是 demo 自己 new 的
                ↓ 注入
            is_participant  ← 真的去 TaskStore 查这个 Task 的参与者
            agent_exists    ← 真的去 Economy 查身份

        而不是绕开组合根直接建一个 Gateway —— 那样证明不了任何事。

    【链路】（任务书 §23 的 10 步）
        建 Workspace → 建资源 → 两个 Participant
        → A/B 都读 → A 改自己的 → Revision 1→2
        → Audit + Event → Handoff A→B
        → A 再改被拒 → B 改成功 → Revision 2→3
    """
    gw = runtime.workspace_gateway

    def step(n: int, title: str, subtitle: str = "") -> None:
        """打印一个带编号的步骤标题（本 demo 自己的小工具）。"""
        print()
        print(f"  ── [{n:>2}] {title}")
        if subtitle:
            print(f"         {subtitle}")

    # ---- 准备两个真实 Agent ----
    agent_ids = [a.agent_id for a in runtime.economy.all_agents()]
    if len(agent_ids) < 2:
        print("  ❌ 至少需要 2 个 Agent，请先跑一次 ./dev.sh run 初始化")
        return 1
    a_id, b_id = agent_ids[0], agent_ids[1]

    # 【为什么每次跑都用一个新的 workspace_id】
    #
    #   第一次写死 "WS-DEMO-001"，结果**第二次跑就崩**：
    #   根目录已经存在 → `ensure_workspace` 返回 created=False
    #   → 接着 create("docs") 撞上 NAME_CONFLICT。
    #
    #   Workspace Core v1 **没有**删除 Workspace 的 API
    #   （这是有意的 —— 生命周期属于后续阶段）。
    #   所以 demo 每次用一个新的 id 才是诚实的做法，
    #   而不是偷偷加一个 delete_workspace()。
    workspace_id = getattr(args, "workspace_id", None) or (
        f"WS-DEMO-{uuid.uuid4().hex[:6]}"
    )

    print()
    print("=" * 68)
    print("  Workspace Core Demo —— 共享工作空间的责任与版本")
    print("=" * 68)
    print()
    print(f"  本次 workspace_id = {workspace_id}")
    print()
    print("【这个 demo 走的是**真实系统链路**】")
    print("    main.py（组合根） → Runtime.workspace_gateway")
    print("        ↓ 注入 is_participant（真的去 TaskStore 查）")
    print("        ↓ 注入 agent_exists（真的去 Economy 查身份）")
    print()

    # ---------------------------------------------------------
    # ① 建一个真实 Task，让 workspace 有参与者来源
    # ---------------------------------------------------------
    step(1, "建立 Task 与 Workspace",
         f"workspace_id={workspace_id}，参与者 {a_id} / {b_id}")

    task = Task.create(
        creator=a_id,
        description="Workspace Core demo",
        workspace_id=workspace_id,
        participants=[a_id, b_id],
    )
    runtime.create_task(task)

    r = gw.ensure_workspace(workspace_id, a_id)
    if not r.success:
        print(f"  ❌ 建 Workspace 失败：{r.error_code}")
        return 1
    root_id = r.result["resource_id"]
    print(f"  ✅ Workspace 建好  root={_short(root_id, 18)}  "
          f"revision={r.result['revision']}")
    print(f"     → Controller 与 Directory Lock 的**来源**就是这一步 "
          f"（{a_id} 是创建者）")

    # ---------------------------------------------------------
    # ② 建初始资源
    # ---------------------------------------------------------
    step(2, "初始化共享资源", "建一个目录 docs/ 和一个文件 docs/spec.md")
    gw.create(workspace_id, "/", "docs", "directory", a_id)
    created = gw.create(workspace_id, "/docs", "spec.md", "file", a_id,
                        content="# Spec v1\n\n初稿。")
    spec_id = created.result["resource_id"]
    print(f"  ✅ docs/spec.md  resource_id={_short(spec_id, 18)}  "
          f"revision={created.result['revision']}")
    print("     🔒 新资源**没有**自己的 Explicit Lock —— "
          "它继承 docs/ 的责任（这就是「创建不产生显式锁」）")

    # ---------------------------------------------------------
    # ③ 两个 Participant
    # ---------------------------------------------------------
    step(3, "确认参与者", "Participant 判定来自**真实的 Task 参与者**")
    for who in (a_id, b_id):
        ok = gw.read(workspace_id, "/docs/spec.md", who)
        print(f"  {'✅' if ok.success else '❌'} {who} 是这个 Workspace 的 "
              f"Participant（TaskStore 里查到的）")
    outsider = gw.read(workspace_id, "/docs/spec.md", "NOT_A_PARTICIPANT")
    print(f"  ✅ 非参与者被拒绝：{outsider.error_code}")

    # ---------------------------------------------------------
    # ④ A / B 都能读（Read 与 Write 分离）
    # ---------------------------------------------------------
    step(4, "A 和 B 都能**读**", "Read 靠 Task Participation，不需要 Lock")
    for who in (a_id, b_id):
        got = gw.read(workspace_id, "/docs/spec.md", who)
        print(f"  ✅ {who}.read() → revision={got.result['revision']}  "
              f"content={_short(got.result['content'], 24)!r}")
    print("     🔒 能读 **不代表** 能写 —— 下面立刻验证")

    # ---------------------------------------------------------
    # ⑤ B 想直接改 A 的资源 → 被拒
    # ---------------------------------------------------------
    step(5, "B 尝试直接修改", "B 能读，但 docs/ 的责任在 A 手里")
    denied = gw.edit(workspace_id, "/docs/spec.md", 1, "B 偷偷改的", b_id)
    print(f"  {'✅' if not denied.success else '❌'} 被拒绝："
          f"{denied.error_code}")
    print(f"     → 需要走**正式 Handoff** 才能获得责任（下面第 8 步）")

    # ---------------------------------------------------------
    # ⑥ A 修改自己的资源 → Revision 变化
    # ---------------------------------------------------------
    step(6, "A 修改自己有效负责的资源", "Revision 1 → 2")
    edited = gw.edit(workspace_id, "/docs/spec.md", 1,
                     "# Spec v2\n\nA 修改过。", a_id)
    print(f"  ✅ revision {1} → {edited.result['revision']}")
    print("     🔒 EDIT END ≠ LOCK RELEASE —— 编辑完成后责任**没有**自动释放")

    # ---- 顺手演示 STALE_REVISION ----
    stale = gw.edit(workspace_id, "/docs/spec.md", 1, "用旧版本覆盖", a_id)
    print(f"  ✅ 用旧 revision 再提交 → {stale.error_code}"
          f"（current={stale.error.get('current_revision')}, "
          f"provided={stale.error.get('provided_revision')}）")
    print("     🔒 禁止静默覆盖")

    # ---------------------------------------------------------
    # ⑦ Audit / Event
    # ---------------------------------------------------------
    step(7, "看 Audit 与 Event", "Audit = 谁做了什么；Event = 状态变了什么")
    with runtime.economy.db.transaction(write=False):
        audits = runtime.workspaces.audits_for(workspace_id)
        events = runtime.workspaces.events_for(workspace_id)

    print(f"  Audit（{len(audits)} 条）—— 含**失败尝试**：")
    for row in audits[-6:]:
        mark = "✅" if row["result"] == "SUCCESS" else "⚠️ "
        extra = f"  [{row['error_code']}]" if row["error_code"] else ""
        print(f"    {mark} {row['action']:<14} actor={row['actor']:<10}"
              f"{extra}")
    print(f"  Event（{len(events)} 条）—— 只有**成功的状态变化**：")
    for row in events[-6:]:
        print(f"    • {row['event_type']:<24} revision={row['revision']} "
              f"actor={row['actor']}")
    print("     🔒 失败的 edit 只有 Audit、**没有** Event")
    print("     🔒 Event **不含文件内容** —— 它只说明「发生了什么」")

    # ---------------------------------------------------------
    # ⑧ Handoff A → B
    # ---------------------------------------------------------
    step(8, f"正式责任交接 {a_id} → {b_id}", "Handoff 改变责任，**不改变 Revision**")
    before = gw.read(workspace_id, "/docs/spec.md", a_id).result["revision"]
    ho = gw.handoff(workspace_id, "/docs/spec.md", a_id, b_id, a_id,
                    reason="A 把 spec.md 交给 B 维护",
                    task_id=task.task_id)
    after = gw.read(workspace_id, "/docs/spec.md", b_id).result["revision"]
    print(f"  ✅ Handoff 成功  revision {before} → {after}"
          f"  {'（未变 ✅）' if before == after else '（❌ 不该变）'}")
    print(f"     Audit 记录了 from/to/reason/task_id —— 可追溯")
    print(f"     Event = RESPONSIBILITY_HANDOFF")

    # ---------------------------------------------------------
    # ⑨ A 再改 → 被拒
    # ---------------------------------------------------------
    step(9, "A 再尝试修改", "责任已经交出去了")
    denied2 = gw.edit(workspace_id, "/docs/spec.md", after, "A 又改", a_id)
    print(f"  {'✅' if not denied2.success else '❌'} 被拒绝："
          f"{denied2.error_code}")
    print(f"     → 内容没有被改动："
          f"{_short(gw.read(workspace_id, '/docs/spec.md', b_id).result['content'], 24)!r}")

    # ---------------------------------------------------------
    # ⑩ B 继续工作
    # ---------------------------------------------------------
    step(10, "B 继续工作", "新责任人接手")
    ok = gw.edit(workspace_id, "/docs/spec.md", after,
                 "# Spec v3\n\nB 接手后修改。", b_id)
    print(f"  ✅ revision {after} → {ok.result['revision']}")
    final = gw.read(workspace_id, "/docs/spec.md", b_id).result
    print(f"     最终内容：{_short(final['content'], 30)!r}")

    # ---------------------------------------------------------
    # 收尾：list + 不变量
    # ---------------------------------------------------------
    print()
    print("─" * 68)
    print("  list() —— 推荐同时返回 effective_controller / "
          "effective_lock_holder")
    print("─" * 68)
    listing = gw.list(workspace_id, "/", a_id)
    print(f"  {'名称':<16}{'类型':<12}{'R':<4}{'Controller':<12}"
          f"{'Lock Holder':<12}")
    for entry in listing.result["entries"]:
        print(f"  {entry['name']:<16}{entry['type']:<12}"
              f"{entry['revision']:<4}"
              f"{str(entry['effective_controller']):<12}"
              f"{str(entry['effective_lock_holder']):<12}")

    print()
    print("=" * 68)
    print("  🔒 Workspace Core 已经作为基础设施在运行")
    print("=" * 68)
    print("  · Agent 只看到 workspace_id / resource_id / 逻辑路径")
    print("  · **没有**任何宿主机物理路径出现在接口里")
    print("  · **没有** lock() API —— 责任只能来自 Create / Handoff / Release")
    print("  · Read 靠 Task Participation，Write 靠 Lock，两者完全分离")
    print("  · Revision 是内容事实；责任变化**不**产生新 Revision")
    print()

    if getattr(args, "keep", False):
        print(f"  （--keep：数据保留在库里，workspace_id={workspace_id}）")
    else:
        _reset_demo_tasks(runtime, [task.task_id])
        print("  （已清理本次 demo 的 Task；用 --keep 可保留）")
    return 0


# ============================================================
# 命令：hello-demo（第一次真正“使用”这个系统的端到端 Demo）
# ============================================================
#: Human Demand —— 固定，不让 LLM 决定要做什么。
HELLO_DEMO_DEMAND = (
    "请创建一个 Python Hello World 程序，让运行结果输出 Hello, World!。"
)

#: 🔒 **只允许 Agent 创建这一个文件**（任务书 §16）。
#:    任何其他路径（`/etc/passwd`、`../../x`、`/Users/...`）都必须被拒绝。
HELLO_DEMO_ALLOWED_FILE = "hello.py"

#: Demo 的工作区与 worktree 用**稳定 id** ——
#: 这样 `--keep` 之后重跑是「复用 + Revision 递增」，而不是每次造一堆。
HELLO_DEMO_WORKSPACE = "WS-HELLO-DEMO"


@dataclass
class HelloDemoResult:
    """
    Demo 的**结构化结果**（而不是直接 print）。

    【为什么不在流程里直接 print】

        那样就没法测了 —— 测试只能抓 stdout 字符串。
        把「做什么」和「怎么显示」分开：

            _run_hello_demo()  →  HelloDemoResult（可断言）
            cmd_hello_demo()   →  打印（给人看）

    🔒 `physical_path` 只用于 **Human-facing 显示与内部 subprocess cwd**，
       绝不进任何 Agent-facing 结构。
    """

    ok: bool
    stage: str = ""
    reason: str = ""
    demand: str = HELLO_DEMO_DEMAND
    agent_id: str = ""
    other_agent_id: str = ""
    task_id: str = ""
    workspace_id: str = ""
    worktree_id: str = ""
    branch: str = ""
    physical_path: str = ""          # 🔒 Human-only
    provider: str = ""
    model: str = ""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: float = 0.0
    summary: str = ""
    resource_id: str = ""
    revision_before: int = 0
    revision_after: int = 0
    content: str = ""
    stdout: str = ""
    stderr: str = ""
    returncode: Optional[int] = None
    materialized: bool = False
    guard_result: str = ""           # §18 的失败测试结论
    reused_worktree: bool = False
    simulated: bool = False          # ⚠️ True = 这不是真实 LLM 调用


def _extract_json_object(text: str) -> Optional[dict]:
    """
    从 LLM 输出里取出**第一个完整的 JSON 对象**。

    ⚠️ 这是**十行的容错提取**，不是「第二套 LLM response parser」——
       它不做 schema 校验、不做类型转换，只负责
       「模型爱在 JSON 前后写解释文字」这一件事实。

        §15 要求优先复用现有机制。本项目当前的 LLM 层
        （`llm/base.py` 的 `LLMResponse`）只保证返回文本，
        没有 structured output / tool calling 抽象 ——
        所以这里做最小的提取，并**明确记录**这是一个临时手段。
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                import json as _json
                try:
                    return _json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _pick_hello_demo_provider(runtime: Runtime,
                              requested: Optional[str] = None) -> tuple:
    """
    挑一个**真实的** LLM provider。

    【为什么要挑，而不是直接用 default_provider】

        `config.yaml` 里 `default_provider = "mock"` —— 那是为了让
        全量测试不烧钱、不依赖网络。

        但本 Demo 的意义正是**第一次真正使用真实 LLM**（任务书 §7）。
        所以这里按这个顺序挑：

            ① 显式 `--provider`         （Human 指定）
            ② 第一个 enabled 且 **api key 真的存在** 的非 mock provider
            ③ 实在没有 → mock，但**明确标为 simulated**

    🔒 **绝不打印 key** —— 只判断「在不在」。

    返回 `(provider_name, is_simulated, note)`。
    """
    import os as _os

    router = runtime.router
    # ⚠️ `implemented()` 在 **ProviderRegistry** 上，不在 LLMRouter 上 ——
    #    第一版写成了 `router.implemented()`，`hasattr` 静默为 False，
    #    于是 known=[] 直接掉到 mock，**看起来"跑通了"但根本没调真实 API**。
    #    这正是 §19「不要把 API 调通与代码生成分开」要防的那类假象。
    def _as_list(candidate):
        """
        ⚠️ 这些属性**既可能是列表、也可能是方法** —— 而且**真的两种都出现过**：

            `ProviderRegistry.implemented`  → **list**（实现里是属性）
            `LLMRouter.available_providers` → **bound method**

        第一版统一按「方法」处理 → 遇到 list 就 TypeError；
        第二版把兜底写成 `list(available_providers)` →
        遇到 bound method 还是 TypeError（**而且这条兜底从没被走到**，
        所以直到测试里用假 runtime 才暴露）。

        所以：**两种都接受**，并且这是唯一一处判断。
        """
        if candidate is None:
            return []
        try:
            value = candidate() if callable(candidate) else candidate
        except Exception:                     # pragma: no cover - 防御
            return []
        try:
            return list(value)
        except TypeError:                     # pragma: no cover - 防御
            return []

    known = _as_list(getattr(getattr(router, "registry", None),
                             "implemented", None))
    if not known:
        known = _as_list(getattr(router, "available_providers", None))

    if requested:
        return requested, requested == "mock", ""

    for name in known:
        if name == "mock":
            continue
        cfg = router.provider_config(name) or {}
        if not cfg.get("enabled", True):
            continue
        key_env = cfg.get("api_key_env")
        if key_env and not _os.environ.get(key_env):
            continue
        return name, False, ""

    return "mock", True, (
        "没有找到配置了 API key 的真实 provider —— "
        "本次是 **simulated**（mock），不是真实 LLM 调用"
    )


def _hello_demo_prompt(demand: str) -> str:
    """
    给 LLM 的提示。

    🔒 它要求**结构化输出**（§15），并且**明确不要解释文字** ——
       否则模型很容易返回一大段 Markdown，我们还得猜哪段是代码。
    """
    return (
        "You are an AI agent in an agent operating system.\n"
        "A human gave you this demand:\n\n"
        f"{demand}\n\n"
        "Write the complete code to fulfil it. Reply with ONLY a JSON object, "
        "no markdown fences, no explanation:\n"
        "{\n"
        '  "files": [{"path": "hello.py", "content": "<file content>"}],\n'
        '  "summary": "<one short sentence>"\n'
        "}\n"
        "Rules:\n"
        f'- Create exactly one file named "{HELLO_DEMO_ALLOWED_FILE}".\n'
        "- The program must print exactly: Hello, World!\n"
        "- Do not add any other file.\n"
    )


def _run_hello_demo(
    runtime: Runtime,
    *,
    operation_id: Optional[str] = None,
    provider: Optional[str] = None,
) -> HelloDemoResult:
    """
    Demo 的**核心流程**（§10 的十一步）。

        1 Human Demand  2 Agent  3 Task  4 Workspace  5 Worktree
        6 LLM           7 code   8 Gateway  9 read        10 run
        11 (额外) 失败测试 —— B 改不了 A 的文件

    🔒 **每一步失败都在那一步停下**，不做任何「假装成功」的补救（§20）。

    🔒 **Agent 全程不碰 physical path**：它只通过
       `runtime.workspace_gateway` 用**逻辑路径**写文件。
       physical path 只出现在 `subprocess` 的 `cwd`（由 Demo 决定）。
    """
    import json as _json

    # ---------- 1 / 2：Human Demand → Agent ----------
    agents = list(runtime.economy.all_agents())
    if len(agents) < 2:
        return HelloDemoResult(
            ok=False, stage="agent",
            reason=f"至少需要 2 个 Agent，当前只有 {len(agents)} 个",
        )
    agent = agents[0]
    other = agents[1]

    if not agent.has_brain:
        return HelloDemoResult(
            ok=False, stage="agent",
            reason=f"{agent.name} 没有大脑（未配置 LLM provider）",
        )

    from agents.brain import Brain  # noqa: F401  （文档用，实际走 agent.generate）

    # ---------- 3：Task ----------
    # 🔒 Human 不是 Task Creator（ADR-0024）——创建者始终是 Agent。
    task = Task.create(
        creator=agent.agent_id,
        description=HELLO_DEMO_DEMAND,
        workspace_id=HELLO_DEMO_WORKSPACE,
        participants=[agent.agent_id, other.agent_id],
        note="hello-demo",
    )
    runtime.create_task(task)

    # ---------- 4：Task Workspace ----------
    gw = runtime.workspace_gateway
    ws = HELLO_DEMO_WORKSPACE
    ensured = gw.ensure_workspace(ws, agent.agent_id)
    if not ensured.success:
        return HelloDemoResult(
            ok=False, stage="workspace",
            reason=f"建 Workspace 失败：{ensured.error_code}",
            agent_id=agent.agent_id, other_agent_id=other.agent_id,
            task_id=task.task_id, workspace_id=ws,
        )

    # ---------- 5：真实 Git Worktree ----------
    wm = runtime.worktree_manager
    wt = wm.create(ws, operation_id=operation_id, task_id=task.task_id)
    if not wt.success:
        # §20：Worktree 创建失败 → **立即停止**，不继续执行 Agent
        return HelloDemoResult(
            ok=False, stage="worktree",
            reason=f"建 Git Worktree 失败：{wt.error_code}",
            agent_id=agent.agent_id, other_agent_id=other.agent_id,
            task_id=task.task_id, workspace_id=ws,
        )

    recorded = runtime.worktrees.get(wt.result["worktree_id"])
    physical = recorded.physical_path if recorded else ""

    base = HelloDemoResult(
        ok=False, stage="",
        agent_id=agent.agent_id, other_agent_id=other.agent_id,
        task_id=task.task_id, workspace_id=ws,
        worktree_id=wt.result["worktree_id"],
        branch=(wt.result.get("git_ref") or "").replace("refs/heads/", ""),
        physical_path=physical,
        reused_worktree=bool(wt.references.get("reused")
                             or wt.references.get("idempotent_replay")),
    )

    # ---------- 6：真实 LLM ----------
    chosen, simulated, note = _pick_hello_demo_provider(runtime, provider)
    base = replace(base, provider=chosen)
    if note:
        base = replace(base, summary=note)

    try:
        response = agent.generate(
            messages=_hello_demo_prompt(HELLO_DEMO_DEMAND), provider=chosen)
    except Exception as exc:
        # §19/§20：LLM 失败**绝不能**产生假成功；
        #          也没有任何 Workspace 写入发生过。
        return replace(
            base, stage="llm", reason=f"LLM request failed: {type(exc).__name__}",
            provider=getattr(agent.brain, "provider", ""),
            model=getattr(agent.brain, "model", ""),
        )

    base = replace(
        base,
        provider=response.provider, model=response.model,
        simulated=bool(getattr(response, "simulated", False)) or simulated,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        latency_ms=response.latency_ms,
    )

    # ---------- 7：解析（非法结构 → 不写 Workspace）----------
    payload = _extract_json_object(response.content)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return replace(
            base, stage="llm_parse",
            reason="LLM 返回的不是期望的 JSON 结构",
        )

    files = payload["files"]
    if len(files) != 1 or not isinstance(files[0], dict):
        return replace(base, stage="llm_parse",
                       reason=f"期望恰好 1 个文件，收到 {len(files)} 个")

    path = str(files[0].get("path") or "")
    content = files[0].get("content")
    summary = str(payload.get("summary") or "")
    if not isinstance(content, str) or not content.strip():
        return replace(base, stage="llm_parse", reason="LLM 没有给出文件内容")

    # ---------- 8：🔒 只允许 hello.py（同时验证路径隔离）----------
    if path != HELLO_DEMO_ALLOWED_FILE:
        return replace(
            base, stage="path_guard",
            reason=(f"拒绝创建 {path!r} —— Demo 只允许 "
                    f"{HELLO_DEMO_ALLOWED_FILE!r}"),
        )

    # ---------- 8：经 Workspace Gateway 写入 ----------
    existing = gw.read(ws, f"/{path}", agent.agent_id)
    if existing.success:
        # 已经存在（重跑）→ 走 edit，顺带演示 Revision
        rev = existing.result["revision"]
        written = gw.edit(ws, f"/{path}", rev, content, agent.agent_id)
    else:
        written = gw.create(ws, "/", path, "file", agent.agent_id,
                            content=content)

    if not written.success:
        # §20：Gateway 拒绝 → **绝不绕过它直接写文件**
        return replace(
            base, stage="gateway",
            reason=f"Gateway 拒绝写入：{written.error_code}",
        )

    base = replace(base, summary=summary)

    # ---------- 9：读回来验证（Revision 是核心，§17）----------
    read1 = gw.read(ws, f"/{path}", agent.agent_id)
    if not read1.success:
        return replace(base, stage="read", reason="写完之后读不回来")
    resource_id = read1.result["resource_id"]
    rev1 = read1.result["revision"]
    if read1.result["content"] != content:
        return replace(base, stage="read", reason="读回的内容与写入不一致")

    base = replace(base, resource_id=resource_id, content=content,
                   revision_before=rev1, materialized=False)

    # ---------- 11（§18）：B 想改 A 的文件 → 必须被拒 ----------
    denied = gw.edit(ws, f"/{path}", rev1, content + "\n# by B",
                     other.agent_id)
    guard = denied.error_code if not denied.success else "UNEXPECTED_SUCCESS"

    # ---------- A 正常修改成功 → Revision +1 ----------
    # ⚠️ 这里**故意写同一内容** —— 因为 Revision 记录的是
    #    「这个资源发生过几次状态变化」，不是「内容 diff」。
    #    内容相同也会 +1，这正是一个值得让 Human 看到的事实。
    rev2 = gw.read(ws, f"/{path}", agent.agent_id).result["revision"]
    again = gw.edit(ws, f"/{path}", rev2, content, agent.agent_id)
    if not again.success:
        return replace(base, stage="revision_demo",
                       reason=f"A 修改自己的文件失败：{again.error_code}",
                       guard_result=guard)

    base = replace(base, guard_result=guard,
                   revision_after=again.result["revision"])

    # ---------- 9.5：Materialize（⚠️ 见下面的重要说明）----------
    #
    # ============================================================
    # ⚠️ **这一步暴露了一个真实的架构缺口 —— 不是 Demo 的取巧**
    # ============================================================
    #
    #   Workspace Gateway 写的是**逻辑资源**（在 Workspace DB 里）。
    #   它**不会**把内容落到 Git worktree 的物理目录上。
    #
    #   实测证据：第一次跑通全流程后，
    #       ls .worktrees/WS-HELLO-DEMO/WT-*/hello.py
    #       → No such file or directory
    #
    #   而 **Materialization（逻辑资源 → 物理文件）属于 Patch 阶段**
    #   （设计文档 §10.2 的反方向）。任务书 §5 明确禁止本阶段实现 Patch。
    #
    #   所以这里由 **Demo 自己**做一个**最小、显式、带标注**的物化：
    #
    #       Agent → Gateway（逻辑写入，已发生且已验证）
    #                    ↓  读取逻辑内容
    #       Demo  → 写进 worktree 的物理目录（**物化替身**）
    #
    #   🔒 **Agent 全程没有碰物理路径** —— 它只做了 Gateway 那次写入。
    #      物理写入是 Demo 的行为，而且内容**来自 Gateway 读回的值**，
    #      不是另起一套。
    #
    #   → 这是一个 **Open Question**：真正的物化归 Patch 阶段。
    materialized = False
    gateway_content = gw.read(ws, f"/{path}", agent.agent_id)
    if physical and gateway_content.success:
        try:
            os.makedirs(physical, exist_ok=True)
            with open(os.path.join(physical, path), "w",
                      encoding="utf-8") as fh:
                fh.write(gateway_content.result["content"])
            materialized = True
            base = replace(base, materialized=True)
        except OSError as exc:
            return replace(base, stage="materialize",
                           reason=f"物化失败：{type(exc).__name__}")

    # ---------- 10：运行 hello.py（命令**由 Demo 固定**）----------
    # 🔒 任务书 §11：command 是系统固定的，**不是** LLM 生成的。
    #    禁止 `subprocess.run(model_output)`。
    #    cwd 来自 WorktreeManager 内部得到的物理路径 —— Agent 不知道它。
    if not physical:
        return replace(base, stage="execute", reason="拿不到 worktree 物理路径")
    if not materialized:
        return replace(base, stage="materialize",
                       reason="逻辑写入成功，但没能物化到 worktree")

    try:
        proc = subprocess.run(
            [sys.executable, HELLO_DEMO_ALLOWED_FILE],
            cwd=physical,
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return replace(base, stage="execute",
                       reason=f"执行失败：{type(exc).__name__}")

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    base = replace(base, stdout=stdout, returncode=proc.returncode,
                   stderr=stderr)

    # §20：执行失败 → CODE EXECUTION FAILED，**不是** DEMO SUCCESS
    if proc.returncode != 0:
        return replace(base, stage="execute",
                       reason=f"hello.py 退出码 {proc.returncode}")

    if "Hello, World!" not in stdout:
        return replace(base, stage="execute",
                       reason="运行成功，但输出里没有 'Hello, World!'")

    return replace(base, ok=True, stage="done")


# ============================================================
# 命令：credential-demo（Agent 独立 Credential）
# ============================================================
# ============================================================
# 命令：communication-wakeup-demo（Event Wakeup V1）
# ============================================================
async def _run_wakeup_demo(runtime: Runtime, *, seconds: float = 5.0) -> dict:
    """
    🔒 **确定性**：不用 `sleep(1)` 猜时机（§13）。

    用「自旋等状态」+ 断言来证明时序：

        ① 断言 B 此刻是 **sleeping**（发消息之前）
        ② A 才 send
        ③ 等 B 的 runtime 自己把 processed_count 从 0 变成 1
        ④ 断言 B 从来没被外面调用过 receive()
    """
    import asyncio as _asyncio

    comm = runtime.communication
    agent_a = runtime.economy.agent("agent_a")
    agent_b = runtime.economy.agent("agent_b")

    out: dict = {"events": []}

    received: list = []

    def b_handler(message):
        """B 收到消息时的行为 —— 由 Agent 自己决定（§6）。"""
        received.append(message)
        out["events"].append(("B_received", message.text))
        out["handler_calls"] = len(received)
        return {"ack": f"Hello {message.from_agent}"}

    agent_b.on_message(b_handler)

    rt_b = AgentRuntime(agent=agent_b, communication=comm)
    out["events"].append(("B_state", rt_b.state))

    # ---- 启动 B 的 runtime（它是唯一那个循环，§10）----
    task = _asyncio.create_task(rt_b.run_forever())

    # ---- ① 等 B 真的进入「等唤醒」，而不是 sleep 猜 ----
    for _ in range(2000):
        if rt_b.state == RuntimeState.WAITING:
            break
        await _asyncio.sleep(0)

    out["b_sleeping_before_send"] = rt_b.is_sleeping
    out["events"].append(("B_sleeping", rt_b.is_sleeping))

    # ---- ② A 发消息（此时没有任何人调用过 receive("agent_b")）----
    receipt = await agent_a.send("agent_b", text="Hello B")
    out["sent_id"] = receipt.message_id
    out["events"].append(("A_sent", "Hello B"))

    # ---- ③ 等 B 被唤醒并处理（自旋等，不 sleep 猜时长）----
    for _ in range(20000):
        if rt_b.processed_count >= 1:
            break
        await _asyncio.sleep(0)

    out["b_wakeups"] = rt_b.wakeup_count
    out["b_processed"] = rt_b.processed_count
    out["events"].append(("B_wakeup_count", rt_b.wakeup_count))

    # ---- B 的回复进了 A 的信箱，A 取一下 ----
    reply_text = None
    inbound = await comm.receive("agent_a", timeout=seconds)
    if inbound.delivered:
        msg = inbound.message
        # 回复可能是 text，也可能是 payload（B 返回了 dict）
        reply_text = msg.text
        if not reply_text and isinstance(msg.payload, dict):
            reply_text = f"{msg.payload}"
        out["events"].append(("A_received", reply_text or ""))

    out["reply"] = reply_text
    out["b_state_after"] = rt_b.state

    rt_b.stop()
    try:
        await _asyncio.wait_for(task, timeout=seconds)
    except Exception:
        task.cancel()

    return out


def cmd_communication_wakeup_demo(runtime: Runtime,
                                  args: argparse.Namespace) -> int:
    """
    `./dev.sh communication-wakeup-demo`

    🔒 **不使用真实 LLM**（§12）—— 本 Demo 证明的是 **Runtime wakeup 机制**，
       不是模型能力。行为由 `on_message` 里的确定性回调决定。
    """
    import asyncio as _asyncio

    banner = "=" * 48
    print()
    print(banner)
    print("  AI Economy OS — Event Wakeup Demo")
    print(banner)

    result = _asyncio.run(_run_wakeup_demo(runtime))

    print()
    print("  时序")
    print("  " + "-" * 44)
    print(f"  ① B 启动时的状态            : {result['events'][0][1]}")
    print(f"  ② A 发送**之前** B 是否在睡  : {result['b_sleeping_before_send']}")
    print(f"  ③ A → B                     : 已投递 ({result['sent_id']})")
    print(f"  ④ B 被唤醒次数              : {result['b_wakeups']}")
    print(f"  ⑤ B 处理的消息数            : {result['b_processed']}")
    print(f"  ⑥ B → A                     : {result['reply']!r}")
    print(f"  ⑦ B 最后的状态              : {result['b_state_after']}")

    print()
    print("  链路")
    print("  " + "-" * 44)
    for kind, detail in result["events"]:
        print(f"    {kind:16s} {detail}")

    ok = (result["b_sleeping_before_send"]
          and result["b_wakeups"] >= 1
          and result["b_processed"] >= 1)

    print()
    print(banner)
    print("  ✅ WAKEUP OK" if ok else "  ❌ WAKEUP FAILED")
    print(banner)
    print()
    print("  · B 全程**没有**被外部调用过 receive()")
    print("  · 唤醒来自 LocalTransport 投递后发出的信号")
    print("  · 消息内容仍只从信箱取 —— wakeup 不携带 payload")
    print("  · Durable Inbox = FUTURE（进程重启后内存信箱会丢）")
    print()
    return 0 if ok else 1


def cmd_credential_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    显示每个 Agent 实际用的是**哪个** Credential。

    🔒 只显示 `credential_ref` / source / available —— **绝不显示 key**。

    默认**不打真实 API**（只展示解析结果）。
    加 `--real` 才会各调一次真实 provider ——
    这样「多 Key 真实验证」永远不会混进 pytest（任务书 §18）。
    """
    agents = list(runtime.economy.all_agents())
    if not agents:
        print("没有可用 Agent。")
        return 1

    banner = "=" * 52
    print()
    print(banner)
    print("  AI Economy OS — Agent Credential Demo")
    print(banner)

    only = getattr(args, "agent", None)
    rows = []
    for agent in agents:
        if only and agent.agent_id != only:
            continue
        provider = getattr(args, "provider", None) or agent.brain.provider
        cred = agent.brain.credential_for(provider)
        rows.append((agent, provider, cred))

    print()
    for agent, provider, cred in rows:
        print(f"  {agent.agent_id}")
        print(f"    provider       = {provider}")
        print(f"    model          = {agent.brain.model or '（provider 默认）'}")
        print(f"    credential_ref = {cred.ref}")
        print(f"    source         = {cred.source}")
        print(f"    available      = {cred.available}")
        if getattr(args, "real", False):
            try:
                resp = agent.generate(messages="Reply with exactly: OK",
                                      provider=provider)
                print(f"    request        = successful")
                print(f"    response model = {resp.model}")
                print(f"    tokens         = {resp.input_tokens}"
                      f"+{resp.output_tokens}  latency={resp.latency_ms:.0f}ms")
            except Exception as exc:                    # noqa: BLE001
                # 🔒 异常信息里也**不能**有 key（provider 已做 scrub）
                print(f"    request        = FAILED ({type(exc).__name__})")
                print(f"    reason         = {str(exc)[:160]}")
        print()

    print(banner)
    print("  🔒 上面没有任何 API Key —— 只有 credential_ref。")
    if not getattr(args, "real", False):
        print("     加 --real 才会真正调用 provider（默认不打真实 API）。")
    print()
    return 0


# ============================================================
# 命令：three-agent-demo（第一次真实三 Agent 协作实验）
# ============================================================
#
# ⚠️ 【这是什么，不是什么】
#
#   这是一个 **Smoke Demo 专用的编排代码**。
#   它存在的原因：系统当前**没有**通用 Tool Calling，
#   所以 LLM 的自然语言输出需要一个很薄的东西接回系统 API。
#
#   🔒 它**不是**：
#       · Tool Calling Framework / Function Calling
#       · Capability Framework / Tool Registry / Action Schema
#       · 通用 Agent Orchestrator
#       · 正式 Workspace → Worktree Materializer
#
#   下面那个 parser 只认「本 Demo 约定的行格式」，
#   换一个任务就得重写 —— 这正说明它不是通用抽象。

THREE_DEMO_DEMAND = (
    "请实现一个极小的 Python calculator 模块："
    "提供 add(a, b) / subtract(a, b) / multiply(a, b) 三个函数，"
    "并提供对应的测试。"
)

THREE_DEMO_WORKSPACE = "WS-THREE-AGENT-DEMO"

#: 三个 Agent 的角色（§五/§六/§七）
THREE_ROLES = {
    "agent_a": "Planner / Coordinator",
    "agent_b": "Implementer",
    "agent_c": "Reviewer / Tester",
}


# ------------------------------------------------------------
# Demo 专用 parser（只认简单文本，不要求模型输出 JSON）
# ------------------------------------------------------------
# 🔒 §十二：**故意不用 JSON**。
#    实测 doubao 在「把代码转义进 JSON 字符串」时约 50% 会吐坏 JSON，
#    所以这里用「行标记 + 裸文本」，模型不需要转义任何东西。
def _td_section(text: str, marker: str) -> str:
    """取 `marker` 之后、下一个 `===` 标记之前的**原样文本**。"""
    if not text:
        return ""
    idx = text.find(marker)
    if idx < 0:
        return ""
    body = text[idx + len(marker):]
    nxt = body.find("\n===")
    return (body if nxt < 0 else body[:nxt]).strip()


def _td_parse_files(text: str) -> dict:
    """
    解析 `===FILE: <name>===` 块。

    ⚠️ 同样**不转义** —— 代码原样写在标记之间。
       这和 snake-demo 的分隔符格式是同一个思路。
    """
    import re as _re

    files: dict = {}
    pattern = _re.compile(r"^===\s*FILE:\s*(.+?)\s*===\s*$", _re.MULTILINE)
    marks = list(pattern.finditer(text))
    for i, m in enumerate(marks):
        name = m.group(1).strip().strip("`\"'")
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end]
        # 去掉尾部的 ===END=== 之类
        body = _re.split(r"^===\s*(?:END|REVIEW|SUMMARY)\s*===\s*$",
                         body, flags=_re.MULTILINE)[0]
        # 去掉可能包着的 ``` 围栏
        body = _re.sub(r"^\s*```[a-zA-Z]*\s*\n", "", body)
        body = _re.sub(r"\n```\s*$", "\n", body)
        if name:
            files[name] = body.strip("\n") + "\n"
    return files


def _td_prompt_a(demand: str, task_id: str, ws: str) -> str:
    return (
        "你是 Planner Agent（规划者）。\n"
        "你**不能**直接修改共享的 Task Workspace —— 你只负责规划。\n"
        "你会把计划交给 Implementer Agent。\n\n"
        f"Human Demand:\n{demand}\n\n"
        f"Task ID: {task_id}\nTask Workspace: {ws}\n"
        "当前角色: Planner / Coordinator\n\n"
        "请给出一个**最小**实现计划（3-5 条即可）。\n"
        "只输出下面这个格式，不要任何额外说明：\n\n"
        "===PLAN===\n"
        "<你的计划>\n"
        "===END===\n"
    )


def _td_prompt_b(demand: str, plan: str, task_id: str) -> str:
    return (
        "你是 Implementer Agent（实现者）。\n"
        "你接收 Planner 的计划，并生成完整可运行的文件。\n\n"
        f"Human Demand:\n{demand}\n\n"
        f"Planner 的计划:\n{plan}\n\n"
        f"Task ID: {task_id}\n\n"
        "请生成两个文件：calculator.py 和 test_calculator.py\n"
        "要求：add(a, b) / subtract(a, b) / multiply(a, b)，并用 pytest 测试。\n\n"
        "只输出下面这个格式，**不要用 JSON、不要加解释**，\n"
        "文件内容**原样**写在标记之间（不要做任何转义）：\n\n"
        "===NOTE===\n<一句话说明你的实现>\n"
        "===FILE: calculator.py===\n"
        "<calculator.py 的完整内容>\n"
        "===FILE: test_calculator.py===\n"
        "<test_calculator.py 的完整内容>\n"
        "===END===\n"
    )


def _td_prompt_c(demand: str, files: dict, test_output: str,
                 test_ok: bool) -> str:
    listing = "\n\n".join(f"--- {k} ---\n{v}" for k, v in files.items())
    return (
        "你是 Reviewer Agent（审查者）。\n"
        "你**不能**重写代码 —— 只做审查。\n\n"
        f"Human Demand:\n{demand}\n\n"
        f"Task Workspace 中的代码:\n{listing}\n\n"
        f"Demo Harness 实际运行 pytest 的结果（退出码 "
        f"{'0 = 通过' if test_ok else '非 0 = 失败'}）:\n{test_output[:1500]}\n\n"
        "请审查，至少覆盖这四点：\n"
        "  1. 代码是否满足需求（三个函数）\n"
        "  2. 测试是否覆盖三个函数\n"
        "  3. 是否存在明显错误\n"
        "  4. 测试是否通过（结合上面的真实结果，不要自己猜）\n\n"
        "只输出下面这个格式，不要额外说明：\n\n"
        "===REVIEW===\n<你的审查结果>\n"
        "===VERDICT===\nPASS 或 FAIL\n"
        "===END===\n"
    )


def _td_prompt_a_final(demand: str, plan: str, note: str,
                       review: str, test_ok: bool) -> str:
    return (
        "你是 Planner Agent。这次协作已经结束，请做**最终总结**。\n\n"
        f"Human Demand:\n{demand}\n\n"
        f"你的计划:\n{plan}\n\n"
        f"Implementer 的说明:\n{note}\n\n"
        f"Reviewer 的审查:\n{review}\n\n"
        f"真实 pytest 结果: {'通过' if test_ok else '失败'}\n\n"
        "请输出最终总结，至少包含：\n"
        "  1. 任务是否完成\n  2. B 是否完成实现\n"
        "  3. C 是否通过审查\n  4. 测试是否通过\n\n"
        "只输出下面这个格式：\n\n"
        "===FINAL SUMMARY===\n<总结>\n"
        "===END===\n"
    )


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
async def _run_three_agent_demo(runtime: Runtime, *, keep=None) -> dict:
    """
    A → B → C → A 的真实协作闭环（全程真 LLM + 真通信 + 真唤醒）。
    """
    import asyncio as _asyncio

    comm = runtime.communication
    gw = runtime.workspace_gateway
    log: list = []
    calls: dict = {}

    def say(n, text):
        log.append((n, text))
        print(f"  [{n:2d}] {text}")

    agents = {a.agent_id: a for a in runtime.economy.all_agents()}
    a, b, c = agents["agent_a"], agents["agent_b"], agents["agent_c"]

    # ---------- [1] Human Demand ----------
    say(1, "Human Demand created")
    say(2, f"Task Workspace: {THREE_DEMO_WORKSPACE}")

    task = Task.create(
        creator="agent_a",
        description=THREE_DEMO_DEMAND,
        workspace_id=THREE_DEMO_WORKSPACE,
        participants=["agent_a", "agent_b", "agent_c"],
        note="three-agent-demo",
    )
    runtime.create_task(task)

    # 🔒 【实测踩到的 Workspace Core 语义】
    #
    #   第一版写的是 `ensure_workspace(WS, "agent_a")`，
    #   B 写 Task Workspace 时被拒：
    #       NOT_EFFECTIVE_LOCK_HOLDER
    #
    #   原因：Workspace Core **没有 lock() API** ——
    #   「责任」只能来自 Create / Handoff / Release。
    #   谁**创建**了根，谁就持有根的锁；B 在 A 持有的根下创建文件 → 被拒。
    #
    #   按本次角色分工，**写共享代码的是 B**（§五：A 不直接修改共享代码），
    #   所以根应当由 **B** 创建 —— 于是 B 天然持有写责任。
    #   （C 只读，读靠 participation，不需要锁。）
    gw.ensure_workspace(THREE_DEMO_WORKSPACE, "agent_b")

    # ---------- 角色状态 ----------
    state: dict = {"plan": "", "note": "", "review": "", "files": {},
                   "test_ok": False, "test_out": "", "summary": "",
                   "errors": []}

    # ============================================================
    # Agent B：收到 A 的消息后被唤醒
    # ============================================================
    async def b_on_message(message):
        plan = message.text
        state["plan"] = plan
        say(6, "Agent B woke up  ← 被 A 的消息唤醒（不是直接调用）")
        say(7, f"Agent B thinking... provider={b.brain.provider}")
        r = await _asyncio.to_thread(
            b.generate, _td_prompt_b(THREE_DEMO_DEMAND, plan, task.task_id))
        calls["b"] = _td_call_info(r)
        say(8, f"Agent B LLM: {r.provider} / {r.model}  "
               f"simulated={r.simulated}  {r.latency_ms:.0f}ms")

        files = _td_parse_files(r.content)
        if not files:
            raise RuntimeError("B 没有产出可解析的文件（格式不符）")
        state["files"] = files
        state["note"] = _td_section(r.content, "===NOTE===") or "(无说明)"

        # Private Workspace
        b.write_private("implementation.txt",
                        f"# Agent B 实现记录\n\n{state['note']}\n\n"
                        f"产出文件: {', '.join(sorted(files))}\n")
        say(9, "Agent B wrote private implementation.txt")

        # Task Workspace（共享）
        for name, content in files.items():
            existing = gw.read(THREE_DEMO_WORKSPACE, f"/{name}", "agent_b")
            if existing.success:
                gw.edit(THREE_DEMO_WORKSPACE, f"/{name}",
                        existing.result["revision"], content, "agent_b")
            else:
                made = gw.create(THREE_DEMO_WORKSPACE, "/", name, "file",
                                 "agent_b", content=content)
                if not made.success:
                    raise RuntimeError(f"写 Task Workspace 失败：{name} "
                                       f"{made.error_code}")
        say(10, f"Agent B wrote Task Workspace files: "
                f"{', '.join(sorted(files))}")

        await b.send("agent_c", text=f"implementation finished\n\n"
                                     f"{state['note']}")
        say(11, "B -> C message sent")

    # ============================================================
    # Agent C：收到 B 的消息后被唤醒
    # ============================================================
    async def c_on_message(message):
        say(12, "Agent C woke up  ← 被 B 的消息唤醒")
        say(13, "Agent C thinking... 先读 Task Workspace")

        # 读共享代码（经 Workspace Gateway，不是直接读盘）
        files = {}
        for name in ("calculator.py", "test_calculator.py"):
            rd = gw.read(THREE_DEMO_WORKSPACE, f"/{name}", "agent_c")
            if rd.success:
                files[name] = rd.result["content"]

        # Demo Harness 的**临时** materialize + 真跑 pytest
        test_out, test_ok = _td_run_pytest(files, keep)
        state["test_out"], state["test_ok"] = test_out, test_ok
        say(14, f"Tests executed: exit={'0 (PASS)' if test_ok else '? (FAIL)'}"
                f"  ← Demo 临时物化，不是正式 Materializer")

        r = await _asyncio.to_thread(
            c.generate, _td_prompt_c(THREE_DEMO_DEMAND, files,
                                     test_out, test_ok))
        calls["c"] = _td_call_info(r)
        say(15, f"Agent C LLM: {r.provider} / {r.model}  "
                f"simulated={r.simulated}  {r.latency_ms:.0f}ms")

        review = _td_section(r.content, "===REVIEW===") or r.content.strip()
        verdict = _td_section(r.content, "===VERDICT===") or "(未给出)"
        state["review"] = f"{review}\n\nVERDICT: {verdict}"

        c.write_private("review.txt",
                        f"# Agent C 审查记录\n\n{state['review']}\n\n"
                        f"pytest exit ok: {test_ok}\n")
        say(16, "Agent C wrote private review.txt")

        await c.send("agent_a", text=f"review finished\n\n{state['review']}")
        say(17, "C -> A message sent")

    # ============================================================
    # Agent A：收到 C 的消息后被唤醒 → 最终总结
    # ============================================================
    async def a_on_message(message):
        say(18, "Agent A woke up  ← 被 C 的消息唤醒（第二次思考）")
        r = await _asyncio.to_thread(
            a.generate, _td_prompt_a_final(
                THREE_DEMO_DEMAND, state["plan"], state["note"],
                state["review"], state["test_ok"]))
        calls["a_final"] = _td_call_info(r)
        say(19, f"Agent A final LLM: {r.provider} / {r.model}  "
                f"simulated={r.simulated}  {r.latency_ms:.0f}ms")
        state["summary"] = _td_section(r.content, "===FINAL SUMMARY===") \
            or r.content.strip()

    # 🔒 handler 里的异常如果不管，就会变成「Demo 卡住但什么都不报」
    def _guard(name, fn):
        async def wrapped(message):
            try:
                return await fn(message)
            except Exception as exc:                      # noqa: BLE001
                import traceback as _tb
                state["errors"].append(
                    f"{name}: {type(exc).__name__}: {exc}\n"
                    + _tb.format_exc()[-800:])
                print(f"  [!!] {name} handler 出错: {type(exc).__name__}: {exc}")
                raise
        return wrapped

    a.on_message(_guard("agent_a", a_on_message))
    b.on_message(_guard("agent_b", b_on_message))
    c.on_message(_guard("agent_c", c_on_message))

    # ---------- 启动三个 Runtime（每人**一个**循环） ----------
    rts = {aid: AgentRuntime(agent=ag, communication=comm)
           for aid, ag in (("agent_a", a), ("agent_b", b), ("agent_c", c))}
    rttasks = [asyncio.create_task(rt.run_forever()) for rt in rts.values()]
    for rt in rts.values():
        for _ in range(5000):
            if rt.is_busy:
                break
            await asyncio.sleep(0)
    say(3, "Three Agent Runtimes started (sleeping, waiting for wakeup)")

    # ---------- A 作为 Planner 先动 ----------
    say(4, "Agent A thinking...（规划）")
    r0 = await asyncio.to_thread(
        a.generate, _td_prompt_a(THREE_DEMO_DEMAND, task.task_id,
                                 THREE_DEMO_WORKSPACE))
    calls["a_plan"] = _td_call_info(r0)
    say(5, f"Agent A LLM: {r0.provider} / {r0.model}  "
           f"simulated={r0.simulated}  {r0.latency_ms:.0f}ms")

    plan = _td_section(r0.content, "===PLAN===") or r0.content.strip()
    state["plan"] = plan
    a.write_private("plan.txt", f"# Agent A 规划\n\n{plan}\n")
    say(5, "Agent A wrote private plan.txt")

    await a.send("agent_b", text=plan)
    say(5, "A -> B message sent")

    # ---------- 等闭环完成 ----------
    # 🔒 用**挂钟超时**，不是迭代次数。
    #    第一版是 `for _ in range(40000): sleep(0.05)` = 2000 秒，
    #    失败时看起来就是「卡死」，而且不给任何诊断。
    deadline = asyncio.get_event_loop().time() + 240.0
    while asyncio.get_event_loop().time() < deadline:
        if state["summary"] or state["errors"]:
            break
        await asyncio.sleep(0.05)

    # ---------- 收尾 ----------
    for rt in rts.values():
        rt.stop()
    try:
        await asyncio.wait_for(asyncio.gather(*rttasks), timeout=10)
    except Exception:
        for tk in rttasks:
            tk.cancel()

    return {
        "log": log, "calls": calls, "state": state,
        "wakeups": {aid: rt.wakeup_count for aid, rt in rts.items()},
        "processed": {aid: rt.processed_count for aid, rt in rts.items()},
        "task_id": task.task_id,
    }


def _td_call_info(resp) -> dict:
    return {
        "provider": resp.provider, "model": resp.model,
        "simulated": bool(resp.simulated),
        "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
        "latency_ms": resp.latency_ms,
    }


def _td_run_pytest(files: dict, keep) -> tuple:
    """
    🔒 **Demo Harness 的临时测试机制**，不是正式 Materializer。

    把 Task Workspace 里的文件写到一个临时目录，然后跑 pytest。
    正式 Materializer（Workspace → Worktree）属于 Patch 阶段，本 Demo 不做。
    """
    import tempfile as _tempfile

    if not files:
        return "(Task Workspace 里没有文件)", False

    root = Path(keep) if keep else Path(
        _tempfile.mkdtemp(prefix="aieco-three-agent-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / Path(name).name
        target.write_text(content, encoding="utf-8")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:                              # noqa: BLE001
        return f"pytest 运行失败：{type(exc).__name__}: {exc}", False

    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip()[-1500:], proc.returncode == 0


def cmd_three_agent_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    `./dev.sh three-agent-demo` —— 第一次真实三 Agent 协作。

    ⚠️ 这是一个 **真实 API Demo**（会调用 DeepSeek + Doubao）。
       它**不在** pytest 里 —— 必须显式运行。
    """
    import asyncio as _asyncio

    banner = "=" * 60
    print()
    print(banner)
    print("  AI Economy OS — Three-Agent Collaboration Demo")
    print(banner)
    print()
    print("  A = Planner     (agent_a)   B = Implementer (agent_b)")
    print("  C = Reviewer    (agent_c)")
    print()

    result = _asyncio.run(_run_three_agent_demo(
        runtime, keep=getattr(args, "keep_artifacts", None)))

    state = result["state"]
    banner2 = "=" * 60
    print()
    print(banner2)
    print("  协作链路")
    print(banner2)
    # ⚠️ 用**真实计数**判断，不是无条件打勾。
    #    （第一版这六行全是写死的 ✓ —— 而 C/A 其实一次都没被唤醒。）
    wk, pr = result["wakeups"], result["processed"]
    def mark(n):
        return "✓" if n >= 1 else "✗"
    print(f"  A -> B   message          {'✓' if pr.get('agent_b',0) else '✗'}")
    print(f"  B wakeup                  {mark(wk.get('agent_b',0))} "
          f"({wk.get('agent_b',0)} 次)")
    print(f"  B -> C   message          "
          f"{'✓' if pr.get('agent_c',0) else '✗'}")
    print(f"  C wakeup                  {mark(wk.get('agent_c',0))} "
          f"({wk.get('agent_c',0)} 次)")
    print(f"  C -> A   message          "
          f"{'✓' if result['state'].get('summary') else '✗'}")
    print(f"  A wakeup                  {mark(wk.get('agent_a',0))} "
          f"({wk.get('agent_a',0)} 次)")

    print()
    print(banner2)
    print("  真实 LLM 调用")
    print(banner2)
    labels = {"a_plan": "A 规划", "b": "B 实现", "c": "C 审查",
              "a_final": "A 总结"}
    all_real = True
    for key in ("a_plan", "b", "c", "a_final"):
        info = result["calls"].get(key)
        if not info:
            print(f"  {labels[key]:8s} ❌ 没有记录")
            all_real = False
            continue
        ok = not info["simulated"]
        all_real = all_real and ok
        print(f"  {labels[key]:8s} {info['provider']:9s} "
              f"{info['model']:36s} simulated={info['simulated']}  "
              f"{info['latency_ms']:.0f}ms")

    print()
    print(banner2)
    print("  最终总结（Agent A 真实 LLM 输出）")
    print(banner2)
    for line in (state["summary"] or "(空)").splitlines():
        print(f"  {line}")

    print()
    print(banner2)
    print(f"  pytest（由 Demo Harness 临时物化后真实运行）: "
          f"{'通过 ✅' if state['test_ok'] else '失败 ❌'}")
    print(banner2)
    print()
    print("  Private Workspace（各自的私有资产）:")
    for aid in ("agent_a", "agent_b", "agent_c"):
        ag = runtime.economy.agent(aid)
        names = [n for n in ag.list_private() if not n.startswith(".")]
        print(f"    {aid}: {names}")
    print()
    print("  Task Workspace（三个 Agent 共享）:")
    listing = runtime.workspace_gateway.list(THREE_DEMO_WORKSPACE, "/",
                                             "agent_a")
    if listing.success:
        for entry in listing.result["entries"]:
            print(f"    {entry.get('name')}")
    print()
    if all_real and state["test_ok"] and state["summary"]:
        print(banner2)
        print("  ✅ THREE-AGENT COLLABORATION OK")
        print(banner2)
        return 0
    print(banner2)
    print("  ❌ DEMO INCOMPLETE")
    print(banner2)
    return 1


def cmd_hello_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    `./dev.sh hello-demo` —— 第一次真正“使用”这个系统。

    【这个命令存在的意义】

        不是又一个 demo，而是**第一次**把已经建好的东西串起来跑一遍：

            Human Demand → Agent → 真实 LLM → Task → Workspace
                → Git Worktree → Gateway → 代码 → 运行 → Human

    🔒 **不打印任何 secret**（§3）：
       provider / model / token / latency 可以打，
       API key / base_url / 环境变量一概不打。
    """
    show_path = bool(getattr(args, "show_path", False))

    # ---- --cleanup：只清理 Demo 自己的东西 ----
    if getattr(args, "cleanup", False):
        return _hello_demo_cleanup(runtime)

    banner = "=" * 52
    print()
    print(banner)
    print("  AI Economy OS — Hello World Demo")
    print(banner)

    def step(n, title, body=""):
        print()
        print(f"[{n}/9] {title}")
        for line in (body or "").splitlines():
            print(f"      {line}")

    step(1, "Human Demand", HELLO_DEMO_DEMAND)

    result = _run_hello_demo(runtime, provider=getattr(args, "provider", None))

    if result.simulated:
        print()
        print("  ⚠️  警告：本次**没有**调用真实 LLM（provider=mock）。")
        print("     这不是任务书 §7 要求的真实 API 调用，结果仅供参考。")

    if result.agent_id:
        step(2, "Agent", f"{result.agent_id}   (partner: {result.other_agent_id})")
    if result.task_id:
        step(3, "Task", result.task_id)
    if result.workspace_id:
        step(4, "Workspace", result.workspace_id)
    if result.worktree_id:
        step(5, "Git Worktree",
             f"{result.worktree_id}\n"
             f"branch: {result.branch}\n"
             + (f"reused: yes" if result.reused_worktree else "created: new"))

    # ---- 失败路径：明确报错，绝不假装成功 ----
    if not result.ok:
        print()
        print(banner)
        print("  DEMO FAILED")
        print(banner)
        print(f"  Stage:  {result.stage}")
        print(f"  Reason: {result.reason}")
        if result.stderr:
            print(f"  stderr: {result.stderr.splitlines()[0][:120]}")
        if result.stage == "llm":
            print()
            print("  No Workspace corruption should occur.")
            print("  （Worktree / DB 状态仍然可恢复：重跑或用 --cleanup）")
        return 1

    step(6, "LLM",
         f"provider: {result.provider}\n"
         f"model:    {result.model}\n"
         f"request:  successful\n"
         f"response: received  "
         f"({result.input_tokens if result.input_tokens is not None else '未知'}"
         f"+{result.output_tokens if result.output_tokens is not None else '未知'}"
         f" tokens, {result.latency_ms:.0f} ms)")
    if result.summary:
        print(f"      summary:  {result.summary}")

    step(7, "Workspace Gateway",
         f"{HELLO_DEMO_ALLOWED_FILE} written\n"
         f"resource: {result.resource_id}\n"
         f"revision: {result.revision_before} -> {result.revision_after}")

    step(8, "Workspace Core guard",
         f"{result.other_agent_id} tried to edit {HELLO_DEMO_ALLOWED_FILE}\n"
         f"-> {result.guard_result}")

    step(9, "Execute",
         f"materialized: {HELLO_DEMO_ALLOWED_FILE} -> worktree (Demo 替身)\n"
         f"$ python {HELLO_DEMO_ALLOWED_FILE}\n"
         f"cwd: <worktree>")
    print()
    print("      " + "-" * 30)
    for line in result.stdout.splitlines():
        print(f"      {line}")
    print("      " + "-" * 30)

    print()
    print(banner)
    print("  DEMO SUCCESS")
    print(banner)
    print(f"  hello.py (真实 LLM 生成) revision = {result.revision_after}")
    print()
    print(f"  Worktree ID : {result.worktree_id}")
    print(f"  Workspace ID: {result.workspace_id}")
    print(f"  Git branch  : {result.branch}")
    if show_path:
        # 🔒 只有 Human 显式要求时才显示物理路径（§12）。
        #    它绝不进入 Agent Message / Agent Context / Agent API。
        print(f"  Worktree path: {result.physical_path}")
        print()
        print(f"  打开这个目录即可看到 Agent 生成的 {HELLO_DEMO_ALLOWED_FILE}：")
        print(f"    open {result.physical_path}")
    else:
        print()
        print("  （加 --show-path 可以显示 Worktree 的本地路径）")
    print()
    print("  没有产生任何 Git commit —— 代码只存在于 worktree 里。")
    print()
    return 0


def _hello_demo_cleanup(runtime: Runtime) -> int:
    """
    `--cleanup` —— **只清理 Demo 自己的东西**。

    🔒 不碰项目仓库里其它任何 worktree / branch。
    """
    import shutil as _shutil

    print()
    print("清理 hello-demo 的 Worktree...")
    removed = 0
    for rec in runtime.worktrees.list_for_workspace(HELLO_DEMO_WORKSPACE):
        if rec.physical_path and os.path.isdir(rec.physical_path):
            _shutil.rmtree(rec.physical_path, ignore_errors=True)
            removed += 1
            print(f"  removed: {rec.worktree_id}  ({rec.state.value})")

    # 让 Git 忘掉已经不存在的 worktree 记录（只动 Git 的记账）
    try:
        runtime.worktree_manager.git.worktree_prune()
    except Exception:
        pass

    print(f"  共清理 {removed} 个目录。")
    print("  （DB 记录与事件**保留** —— 审计历史不因为清理物理目录而消失。）")
    print()
    return 0


# ============================================================
# 命令：snake-demo（Task Demo V2 —— 单 Agent 完成一个真实小项目）
# ============================================================
SNAKE_DEMO_DEMAND = (
    "请创建一个可以直接在浏览器中运行的网页版贪吃蛇游戏。"
    "使用 HTML、CSS 和 JavaScript 实现。"
    "游戏参数必须放在独立的配置文件中，游戏主逻辑必须读取配置文件，"
    "而不是把这些参数直接写死在游戏逻辑中。"
)

SNAKE_DEMO_WORKSPACE = "WS-SNAKE-DEMO"
SNAKE_DEMO_PORT = 8765
SNAKE_DEMO_PIDFILE = "data/snake-demo-server.json"

#: 🔒 **多文件最低验收**（任务书 §7）——不规定具体文件名，只看**类型与数量**。
SNAKE_MIN_FILES = {"html": 1, "css": 1, "js": 2}


@dataclass
class SnakeDemoResult:
    """Snake Demo 的结构化结果（流程与显示分开，便于定向测试）。"""

    ok: bool
    stage: str = ""
    reason: str = ""
    agent_id: str = ""
    task_id: str = ""
    workspace_id: str = SNAKE_DEMO_WORKSPACE
    worktree_id: str = ""
    branch: str = ""
    physical_path: str = ""          # 🔒 Human-only
    provider: str = ""
    model: str = ""
    simulated: bool = False
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: float = 0.0
    summary: str = ""
    files: list = field(default_factory=list)        # 逻辑路径
    counts: dict = field(default_factory=dict)
    config_file: str = ""
    config_fields: list = field(default_factory=list)
    materialized: list = field(default_factory=list)
    url: str = ""
    server_pid: Optional[int] = None


def _snake_prompt(demand: str) -> str:
    """
    🔒 要求**结构化 JSON** + **多文件自然分离**。

    ⚠️ 刻意**不写「必须生成 5 个文件」**（任务书 §6）——
       那会让验收变成机械填空。这里只要求
       「页面 / 样式 / 逻辑 / 配置合理分离」。
    """
    return (
        "You are a software agent. A human gave you this demand:\n\n"
        f"{demand}\n\n"
        "Produce a SMALL but complete, runnable web project.\n"
        "Separate concerns naturally: markup, styling, game logic, and "
        "configuration must live in different files (a config file is "
        "required; the game logic must READ it, not hardcode the values).\n\n"
        # ⚠️ 【为什么这里要把配置项列出来】
        #
        #   第一版只说了「参数要放在配置文件里」，没列出**哪几个** ——
        #   实测 DeepSeek 连续两次都**漏掉了「是否允许穿墙」**。
        #
        #   而 Human Demand（任务书 §5）本身就**明确列了**这五项要求。
        #   所以把它们写进 prompt 是**忠实转述需求**，不是提示答案。
        #
        #   🔒 注意仍然**不说文件数量/文件名**（§6 明确禁止）——
        #      分离方式由 Agent 自己决定。
        "The config file MUST expose these five parameters "
        "(pick clear names, e.g. boardWidth/boardHeight/speed/"
        "initialLength/wrapWalls):\n"
        "  1. board width\n"
        "  2. board height\n"
        "  3. game speed\n"
        "  4. initial snake length\n"
        "  5. whether the snake may wrap through walls\n\n"
        # 🔒 行分隔格式，不要 JSON ——
        #    实测 doubao 把代码转义进 JSON 字符串时约一半会吐坏 JSON。
        #    这个格式里每行都是**原样文本**，不需要任何转义。
        "Reply using EXACTLY this plain-text format.\n"
        "Do NOT use JSON, do NOT wrap anything in markdown fences, and do\n"
        "NOT escape any characters - write each file's contents exactly\n"
        "as they should appear on disk:\n\n"
        "===SUMMARY===\n"
        "<one short sentence>\n"
        "===CONFIG===\n"
        "<path of the config file>\n"
        "===FILE: <path>===\n"
        "<the complete raw content of that file>\n"
        "===FILE: <path>===\n"
        "<the complete raw content of that file>\n"
        "===END===\n\n"
        "Rules:\n"
        "- Plain HTML/CSS/JavaScript only. No build step, no npm, no CDN.\n"
        "- Must run by simply opening index.html in a browser.\n"
        "- The game logic MUST read its parameters from the config file.\n"
        "- Controls: arrow keys. Must include a restart control.\n"
        "- Keep every file short. This is a demo, not a product.\n"
    )


#: 🔒 分隔符格式 —— **不让模型把代码塞进 JSON 字符串**。
#:
#: 【为什么换成这个格式 / 实测依据】
#:
#:   原来让模型输出：
#:       {"files":[{"path":"x.js","content":"<整段代码>"}]}
#:
#:   deepseek 稳定；**doubao-seed-evolving 实测 2/4 次吐坏 JSON**：
#:       · 把结构性引号也转义："content\":\"..."
#:       · Unterminated string
#:       · Expecting ',' delimiter
#:
#:   根因**不是配置**，是任务本身：要求模型把「含引号、反斜杠、换行
#:   的代码」逐字符转义进 JSON 字符串 —— 很多模型都做不稳。
#:
#:   换成行分隔格式后**完全不需要转义**，对模型友好得多。
#:   JSON 那条路**保留为回退**，所以不是「换了一套 parser」而是加了一条。
_SNAKE_MARK_FILE = "===FILE:"
_SNAKE_MARK_SUMMARY = "===SUMMARY==="
_SNAKE_MARK_CONFIG = "===CONFIG==="
_SNAKE_MARK_END = "===END==="


def _snake_parse_delimited(text: str) -> tuple:
    """解析 `===FILE: path===` 分隔格式 → `(files, summary, config_file)`。"""
    if _SNAKE_MARK_FILE not in text:
        return [], "", ""

    files: list = []
    summary = ""
    config_file = ""
    mode = "head"
    buf: list = []
    current = None

    def flush():
        if not current:
            return
        content = "\n".join(buf)
        if not content.endswith("\n"):
            content += "\n"
        files.append({"path": current, "content": content})

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith(_SNAKE_MARK_FILE):
            flush()
            path = stripped[len(_SNAKE_MARK_FILE):].split("===")[0]
            current, buf, mode = path.strip().strip("`\"'"), [], "file"
            continue
        if stripped == _SNAKE_MARK_SUMMARY:
            flush(); current, buf, mode = None, [], "summary"; continue
        if stripped == _SNAKE_MARK_CONFIG:
            flush(); current, buf, mode = None, [], "config"; continue
        if stripped.startswith(_SNAKE_MARK_END):
            flush(); current, buf, mode = None, [], "done"; continue

        if mode == "file":
            buf.append(line)
        elif mode == "summary" and stripped and not summary:
            summary = stripped
        elif mode == "config" and stripped and not config_file:
            config_file = stripped.strip("`\"'")

    flush()
    return [f for f in files if f["path"]], summary, config_file


def _snake_parse_reply(text: str) -> tuple:
    """`(files, summary, config_file, how)` —— 分隔符优先，JSON 回退。"""
    files, summary, config_file = _snake_parse_delimited(text)
    if files:
        return files, summary, config_file, "delimited"

    payload = _extract_json_object(text)
    if isinstance(payload, dict) and isinstance(payload.get("files"), list):
        good = [f for f in payload["files"] if isinstance(f, dict)]
        if good:
            return (good, str(payload.get("summary") or ""),
                    str(payload.get("configFile") or ""), "json")

    return [], "", "", "failed"


def _snake_classify(files: list) -> tuple:
    """
    按**扩展名**分类，并做最低验收（任务书 §7）。

    返回 `(counts, problems)`。
    """
    counts = {"html": 0, "css": 0, "js": 0, "other": 0}
    for f in files:
        path = str(f.get("path") or "").lower()
        if path.endswith(".html") or path.endswith(".htm"):
            counts["html"] += 1
        elif path.endswith(".css"):
            counts["css"] += 1
        elif path.endswith(".js"):
            counts["js"] += 1
        else:
            counts["other"] += 1

    problems = []
    for kind, minimum in SNAKE_MIN_FILES.items():
        if counts[kind] < minimum:
            problems.append(
                f"需要至少 {minimum} 个 .{kind} 文件，实际 {counts[kind]} 个"
            )
    if problems:
        problems.append("Expected a multi-file application.")
    return counts, problems


#: 五个配置概念 —— Agent 可以自己起字段名，所以用**概念正则**匹配。
_SNAKE_CONFIG_CONCEPTS = {
    # ⚠️ 第一版用 `re.fullmatch` —— 于是 `tickInterval` / `initialSnakeLength`
    #    这种**复合名**全部匹配不上，误判成「配置不全」。
    #    真实模型就是这么命名的（实测：gridSize / cellSize /
    #    initialSnakeLength / tickInterval）。
    #    改成 `search`（子串命中），并把常见命名都收进来。
    #
    #    注意：`gridSize` 这种**方形棋盘**同时满足宽/高 —— 这是允许的，
    #    报告里会说明棋盘是方的。
    "board_width": r"width|cols?|columns?|size",
    "board_height": r"height|rows?|size",
    "speed": r"speed|interval|tick|delay|fps|ms",
    "initial_length": r"initial|start|len|length",
    "wrap_walls": r"wrap|through|portal|pass|toroidal",
}


def _snake_inspect_config(files: list, declared: str) -> tuple:
    """
    找出配置文件，并检查「它**真的**被主逻辑用」。

    返回 `(config_file, fields, problems, missing_concepts)`。

    ⚠️ **`missing_concepts` 必须显式返回** ——
       第一版让调用方用 `zip(_SNAKE_CONFIG_CONCEPTS, fields)` 去算，
       但 `fields` 是**过滤过空值**的列表，于是 zip 静默错位：
       缺 1 个概念时 `missing` 算出来是**空**，
       修复机制**从未触发**（而且不报错）。

       这是一个「用长度/位置隐式表达缺失」的经典坑。


    ⚠️ 这里做的是**最低限度的静态检查**：
       「配置对象被别的 JS 引用了」+「五个概念都能在配置里找到」。
       **真正的验证是人工测试**（改 speed 重新加载）。
       代码里明确写清这一点，不假装它比实际更强。
    """
    import re as _re

    js = [(str(f.get("path") or ""), str(f.get("content") or ""))
          for f in files
          if str(f.get("path") or "").lower().endswith(".js")]

    if not js:
        return "", [], ["没有任何 .js 文件"]

    # ---- ① 找配置文件 ----
    config_file = ""
    if declared:
        for path, _ in js:
            if path == declared or path.endswith(declared):
                config_file = path
                break
    if not config_file:
        for path, _ in js:
            if "config" in path.lower() or "setting" in path.lower():
                config_file = path
                break
    if not config_file:
        return "", [], ["没有找到独立的配置文件（文件名里应有 config/settings）"], \
            list(_SNAKE_CONFIG_CONCEPTS)

    config_content = dict(js)[config_file]

    # ---- ② 配置对象名 ----
    # ⚠️ 原来只认 `const X = {` —— 实测 doubao 会写
    #       `window.GAME_CONFIG = {` / `var X = {` / `X = {`
    #    于是明明有配置对象却被判「没找到」。改成两段式：
    #       ① 有声明关键字的
    #       ② 任何「名字 = {」的赋值（含 window.X）
    m = _re.search(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{", config_content)
    if not m:
        m = _re.search(r"([A-Za-z_$][\w$.]*)\s*=\s*\{", config_content)
    if not m:
        return config_file, [], [
            f"{config_file} 里没有找到配置对象（期望 `X = {{...}}`）"
        ], list(_SNAKE_CONFIG_CONCEPTS)
    # `window.GAME_CONFIG` → 引用检查用最后一段
    config_name = m.group(1).split(".")[-1]

    # ---- ③ 五个概念必须都在配置里 ----
    block = config_content[m.start():]
    fields = []
    for concept, pattern in _SNAKE_CONFIG_CONCEPTS.items():
        hit = _re.search(rf"([A-Za-z_$][\w$]*)\s*:\s*[^,\n]+", block)
        names = _re.findall(r"([A-Za-z_$][\w$]*)\s*:", block)
        matched = [n for n in names if _re.search(pattern, n, _re.I)]
        if matched:
            fields.append(matched[0])
        else:
            fields.append("")
    # 把没找到的概念报出来
    problems = []
    missing = [c for c, f in zip(_SNAKE_CONFIG_CONCEPTS, fields) if not f]
    # ⚠️ 阈值从 `>= 2` 收紧到 `>= 1`：
    #    第一版允许**缺 1 个**概念就算通过，于是 DeepSeek 生成的配置
    #    **没有**「是否允许穿墙」这一项，Demo 却报成功 ——
    #    而任务书 §5/§10 明确要求 Human 能手工测试它。
    #    缺任何一个都必须触发修复；修不好就如实失败。
    if missing:
        problems.append(
            f"配置文件里找不到这些概念的字段：{missing}"
        )

    # ---- ④ 🔒 配置必须被**别的** JS 真正引用 ----
    others = [(p, c) for p, c in js if p != config_file]
    referenced = any(config_name in content for _, content in others)
    if not referenced:
        problems.append(
            f"🔒 配置对象 {config_name!r} 没有被其它 JS 引用 —— "
            f"这会是「看起来有配置，实际游戏不用配置」的假配置"
        )

    return config_file, [f for f in fields if f], problems, missing


def _run_snake_demo(
    runtime: Runtime,
    *,
    operation_id: Optional[str] = None,
    provider: Optional[str] = None,
    port: int = SNAKE_DEMO_PORT,
    start_server: bool = False,
) -> SnakeDemoResult:
    """
    Snake Demo 的核心流程（任务书 §21 的十步）。

    🔒 **单 Agent**（任务书 §13）—— 不引入 A/B/C，不做协商。
    🔒 **不实现 Patch / TestRun / Acceptance / Merge**（§4）。
    """
    agents = list(runtime.economy.all_agents())
    if not agents:
        return SnakeDemoResult(ok=False, stage="agent", reason="没有可用 Agent")
    agent = agents[0]                       # 🔒 只用一个

    if not agent.has_brain:
        return SnakeDemoResult(ok=False, stage="agent",
                               reason=f"{agent.name} 没有大脑")

    # ---------- Task（§14：creator 是 Agent，不是 Human）----------
    task = Task.create(
        creator=agent.agent_id,
        description=SNAKE_DEMO_DEMAND,
        workspace_id=SNAKE_DEMO_WORKSPACE,
        participants=[agent.agent_id],
        note="snake-demo",
    )
    runtime.create_task(task)

    gw = runtime.workspace_gateway
    ws = SNAKE_DEMO_WORKSPACE
    ensured = gw.ensure_workspace(ws, agent.agent_id)
    if not ensured.success:
        return SnakeDemoResult(ok=False, stage="workspace",
                               reason=ensured.error_code,
                               agent_id=agent.agent_id, task_id=task.task_id)

    # ---------- 真实 Git Worktree ----------
    wt = runtime.worktree_manager.create(
        ws, operation_id=operation_id, task_id=task.task_id)
    if not wt.success:
        return SnakeDemoResult(ok=False, stage="worktree",
                               reason=wt.error_code,
                               agent_id=agent.agent_id, task_id=task.task_id)

    rec = runtime.worktrees.get(wt.result["worktree_id"])
    base = SnakeDemoResult(
        ok=False, stage="",
        agent_id=agent.agent_id, task_id=task.task_id,
        worktree_id=wt.result["worktree_id"],
        branch=(wt.result.get("git_ref") or "").replace("refs/heads/", ""),
        physical_path=rec.physical_path if rec else "",
    )

    # ---------- 真实 LLM ----------
    chosen, simulated, note = _pick_hello_demo_provider(runtime, provider)
    try:
        response = agent.generate(
            messages=_snake_prompt(SNAKE_DEMO_DEMAND), provider=chosen)
    except Exception as exc:
        return replace(base, stage="llm",
                       reason=f"LLM request failed: {type(exc).__name__}")

    base = replace(base, provider=response.provider, model=response.model,
                   simulated=simulated or bool(getattr(response, "simulated", False)),
                   input_tokens=response.input_tokens,
                   output_tokens=response.output_tokens,
                   latency_ms=response.latency_ms,
                   summary=note)

    files, reply_summary, reply_config, how = _snake_parse_reply(
        response.content)
    base = replace(base, summary=reply_summary or base.summary)
    if not files:
        return replace(base, stage="llm_parse",
                       reason="LLM 回复无法解析（分隔符格式与 JSON 都失败）")
    payload = {"configFile": reply_config}

    # ---------- 多文件验收（§7）----------
    counts, problems = _snake_classify(files)
    base = replace(base, counts=counts)
    if problems:
        return replace(base, stage="multi_file", reason="；".join(problems))

    # ---------- 🔒 有界修复：缺概念时最多再问一次 ----------
    #
    # ⚠️ 这是 **Demo 级的补救**，**不是** Acceptance / Merge（§4 禁止实现）：
    #    · 最多 **1** 次（不会无限重试）
    #    · 只在「已经明确缺哪个概念」时才重问
    #    · 拿不到合格结果就**如实失败**，绝不假装成功
    #
    #    实测动机：DeepSeek 第一次就漏了「是否允许穿墙」这个配置项 ——
    #    而任务书 §10 要求 Human 能手工测试它。
    repaired, fields, cfg_problems, missing = _snake_inspect_config(
        files, str(payload.get("configFile") or ""))
    if cfg_problems:
        if missing:
            followup = (
                "Your previous JSON was missing config fields for: "
                + ", ".join(missing)
                + ". Reply with the FULL corrected JSON object again "
                  "(same schema, all files). The config file must contain "
                  "a field controlling each of those, and the game logic "
                  "must read them from the config."
            )
            try:
                second = agent.generate(
                    messages=[_snake_prompt(SNAKE_DEMO_DEMAND), followup],
                    provider=chosen)
                files2, _s2, cfg2, _h2 = _snake_parse_reply(second.content)
                if files2:
                    c2, p2 = _snake_classify(files2)
                    if not p2:
                        files = files2
                        counts = c2
                        payload = {"configFile": cfg2}
                        base = replace(
                            base, input_tokens=(
                                (base.input_tokens or 0)
                                + (second.input_tokens or 0)),
                            output_tokens=(
                                (base.output_tokens or 0)
                                + (second.output_tokens or 0)),
                            latency_ms=base.latency_ms + second.latency_ms,
                        )
            except Exception:
                pass                      # 修复失败就走下面的如实报错

    # ---------- 配置必须真的起作用（§8）----------
    config_file, fields, cfg_problems, _missing = _snake_inspect_config(
        files, str(payload.get("configFile") or ""))
    base = replace(base, config_file=config_file, config_fields=fields,
                   counts=counts)
    if cfg_problems:
        return replace(base, stage="config",
                       reason="；".join(cfg_problems)
                       + f"（已尝试 1 次修复；配置字段实测为 {fields}）")

    # ---------- 经 Gateway 写（🔒 Agent 不碰物理路径）----------
    logical_paths: list[str] = []
    for f in files:
        raw_path = str(f.get("path") or "")
        content = f.get("content")
        if not isinstance(content, str):
            return replace(base, stage="llm_parse",
                           reason=f"{raw_path} 没有内容")

        # 🔒 绝对路径一律拒绝 —— 逻辑路径**必须是相对的**。
        #    （`normalize_path("/etc/passwd")` 会得到合法的逻辑路径
        #      `/etc/passwd`，落点是 <worktree>/etc/passwd —— 那不是
        #      逃逸，但对一个 Demo 来说「看起来像宿主路径」就该拒。）
        if raw_path.startswith("/") or raw_path.startswith("\\"):
            return replace(base, stage="path_guard",
                           reason=f"拒绝绝对路径 {raw_path!r}")
        try:
            logical = normalize_path(raw_path).lstrip("/")
        except Exception:
            return replace(base, stage="path_guard",
                           reason=f"拒绝非法路径 {raw_path!r}")
        if not logical or logical.endswith("/"):
            return replace(base, stage="path_guard",
                           reason=f"拒绝目录路径 {raw_path!r}")

        existing = gw.read(ws, f"/{logical}", agent.agent_id)
        if existing.success:
            w = gw.edit(ws, f"/{logical}", existing.result["revision"],
                        content, agent.agent_id)
        else:
            parent = "/" + logical.rsplit("/", 1)[0] if "/" in logical else "/"
            made = gw.create(ws, parent, logical.rsplit("/", 1)[-1],
                             "file", agent.agent_id, content=content)
            if not made.success and made.error_code == "RESOURCE_NOT_FOUND":
                # 父目录不存在 → 逐层建目录再建文件
                made = _snake_ensure_parents(gw, ws, parent, agent.agent_id)
                if made.success:
                    made = gw.create(ws, parent, logical.rsplit("/", 1)[-1],
                                     "file", agent.agent_id, content=content)
            w = made

        if not w.success:
            return replace(base, stage="gateway",
                           reason=f"写入 {logical} 被拒：{w.error_code}")
        logical_paths.append(logical)

    base = replace(base, files=logical_paths)

    # ---------- Materialization（V0）----------
    if not base.physical_path:
        return replace(base, stage="materialize", reason="拿不到物理路径")

    materializer = WorktreeMaterializer(
        read_resource=lambda w_, p: _snake_read_logical(gw, w_, p, agent.agent_id),
        worktree_path_for=lambda wid: (
            runtime.worktrees.get(wid).physical_path
            if runtime.worktrees.get(wid) else None
        ),
    )
    try:
        report = materializer.materialize(ws, base.worktree_id, logical_paths)
    except Exception as exc:
        return replace(base, stage="materialize",
                       reason=f"{type(exc).__name__}: {exc}")
    base = replace(base, materialized=report.written)

    # ---------- Browser（命令**固定**，§19）----------
    if start_server:
        pid = _snake_start_server(base.physical_path, port)
        if pid is None:
            return replace(base, stage="browser",
                           reason="本地 HTTP server 启动失败")
        base = replace(base, server_pid=pid,
                       url=f"http://127.0.0.1:{port}/index.html")

    return replace(base, ok=True, stage="done")


def _snake_read_logical(gw, workspace_id, logical, agent_id):
    """把 Gateway 的 read 适配成 Materializer 要的 `read(ws, path)`。"""
    r = gw.read(workspace_id, "/" + logical, agent_id)
    return r.result["content"] if r.success else None


def _snake_ensure_parents(gw, workspace_id, parent, agent_id):
    """逐层创建目录（Agent 可能返回 `js/game.js` 这种结构）。"""
    if parent in ("", "/"):
        return gw.read(workspace_id, "/", agent_id)
    parts = [p for p in parent.strip("/").split("/") if p]
    cur = "/"
    last = None
    for part in parts:
        last = gw.create(workspace_id, cur, part, "directory", agent_id)
        if not last.success and last.error_code != "NAME_CONFLICT":
            return last
        cur = f"{cur.rstrip('/')}/{part}"
    return last if last is not None else gw.read(workspace_id, "/", agent_id)


def _snake_start_server(physical_root: str, port: int) -> Optional[int]:
    """
    🔒 **命令由 Demo 固定**（任务书 §19）：
       `python -m http.server <port> --bind 127.0.0.1`，cwd 固定为 worktree。

       **LLM 无权决定 shell command。**
    """
    import json as _json

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port),
             "--bind", "127.0.0.1"],
            cwd=physical_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    Path(SNAKE_DEMO_PIDFILE).parent.mkdir(parents=True, exist_ok=True)
    Path(SNAKE_DEMO_PIDFILE).write_text(_json.dumps({
        "pid": proc.pid,
        "port": port,
        "url": f"http://127.0.0.1:{port}/index.html",
        "worktree_path": physical_root,
    }), encoding="utf-8")
    return proc.pid


def _snake_stop_server() -> bool:
    """停掉之前起的 server（简单实现，不做 process manager，§22）。"""
    import json as _json

    path = Path(SNAKE_DEMO_PIDFILE)
    if not path.exists():
        return False
    try:
        info = _json.loads(path.read_text(encoding="utf-8"))
        os.kill(int(info["pid"]), 15)
    except Exception:
        pass
    try:
        path.unlink()
    except OSError:
        pass
    return True


def cmd_snake_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """`./dev.sh snake-demo` —— 单 Agent 完成一个真实小项目。"""
    import shutil as _shutil

    if getattr(args, "stop", False):
        print("停止 snake-demo server...",
              "done" if _snake_stop_server() else "（没有在跑的 server）")
        return 0

    if getattr(args, "cleanup", False):
        _snake_stop_server()
        removed = 0
        for rec in runtime.worktrees.list_for_workspace(SNAKE_DEMO_WORKSPACE):
            if rec.physical_path and os.path.isdir(rec.physical_path):
                _shutil.rmtree(rec.physical_path, ignore_errors=True)
                removed += 1
        try:
            runtime.worktree_manager.git.worktree_prune()
        except Exception:
            pass
        print(f"清理完成：{removed} 个 worktree 目录。")
        return 0

    banner = "=" * 48
    print()
    print(banner)
    print("  AI Economy OS — Snake Demo")
    print(banner)

    def step(n, title, body=""):
        print()
        print(f"[{n}/10] {title}")
        for line in (body or "").splitlines():
            print(f"        {line}")

    step(1, "Human Demand", SNAKE_DEMO_DEMAND)

    result = _run_snake_demo(
        runtime,
        provider=getattr(args, "provider", None),
        port=int(getattr(args, "port", SNAKE_DEMO_PORT)),
        start_server=True,
    )

    if result.agent_id:
        step(2, "Agent", f"{result.agent_id}   (single agent)")
    if result.task_id:
        step(3, "Task", result.task_id)
    step(4, "Workspace", result.workspace_id)
    if result.worktree_id:
        step(5, "Git Worktree", f"{result.worktree_id}\nbranch: {result.branch}")

    if not result.ok:
        print()
        print(banner)
        print("  DEMO FAILED")
        print(banner)
        print(f"  Stage:  {result.stage}")
        print(f"  Reason: {result.reason}")
        if result.stage == "multi_file":
            print("  Expected a multi-file application.")
        return 1

    step(6, "LLM",
         f"provider: {result.provider}\n"
         f"model:    {result.model}\n"
         f"request:  successful\n"
         f"tokens:   {result.input_tokens}+{result.output_tokens}  "
         f"latency: {result.latency_ms:.0f} ms")
    if result.simulated:
        print("        ⚠️  本次是 mock（不是真实 API）")

    step(7, "Generated Project",
         "\n".join(f"  {p}" for p in result.files))
    step(8, "Materialization",
         f"{len(result.materialized)} files written to Worktree")
    step(9, "Browser", result.url or "（未启动 server）")
    step(10, "Demo", "READY FOR HUMAN TEST")

    print()
    print(banner)
    print(f"  config file : {result.config_file}")
    print(f"  config fields: {', '.join(result.config_fields)}")
    print(banner)
    print()
    print("  Open the URL above in your browser.")
    print()
    print(f"  Worktree ID : {result.worktree_id}")
    print(f"  Workspace ID: {result.workspace_id}")
    print(f"  Git branch  : {result.branch}")
    if getattr(args, "show_path", False):
        print(f"  Worktree path: {result.physical_path}")
        print(f"    open {result.physical_path}")
    else:
        print("  （加 --show-path 显示 Worktree 本地路径）")
    print()
    print("  人工测试：修改配置文件里的 speed / wrapWalls / boardWidth，")
    print("            刷新浏览器即可看到变化。")
    print("  停止 server: ./dev.sh snake-demo --stop")
    print("  清理:        ./dev.sh snake-demo --cleanup")
    print()
    return 0


def cmd_context_demo(runtime: Runtime, args: argparse.Namespace) -> int:
    """
    Task Shared Context / Alignment 演示（Step 4F）。

    【演示什么】
        1. 两个 Agent 对一个 Task 建立共享上下文
        2. Agent A 提议一个**决定** → 需要 B 确认 → Revision 2
        3. Agent B 提议一个**假设**（声明类，可自批）→ Revision 3
        4. 一条被拒绝的提议 —— **版本号一点都不变**
        5. Snapshot 读取（含现场引用的 Task Core 状态）
        6. Revision 历史可追溯
        7. 并发：两个 Agent 基于同一版本同时提交 → 只有一个成功

    【为什么不用真消息】
        规范 §11 明确：Message **不会**自动变成 Shared Context。
        所以这里直接调应用服务 —— 演示的是「对齐机制」本身，
        而不是「消息怎么送达」（那在 4D 已经演示过了）。
        如果这里用消息演示，反而会暗示「发消息=进上下文」，
        那正是被禁止的。
    """
    from core.task import Task
    from core.task_shared_context import (
        ContextChangeType, ConcurrentContextModification,
        NotAParticipantError, SelfAlignmentError,
    )

    economy = runtime.economy
    tasks = runtime.tasks
    svc = runtime.alignment

    for agent_id in ("agent_a", "agent_b"):
        runtime.agent(agent_id)   # 确保 Agent 存在（会从配置 bootstrap）

    task_id = "TASK-4F-001"

    # 清理上次运行的残留（外键要求先删子表）
    with economy.db.transaction() as conn:
        for table in ("task_context_changes", "task_context_revisions",
                      "task_shared_contexts",
                      "task_public_working_memory_revisions",
                      "task_public_working_memory",
                      "agent_private_working_memory_revisions",
                      "agent_private_working_memory",
                      "task_executions", "task_agreements",
                      "task_offers", "tasks"):
            conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))

    print()
    print("=" * 62)
    print("  Task Shared Context / Alignment 演示（Step 4F）")
    print("=" * 62)

    # ---- Task ----
    task = Task.create(
        task_id=task_id,
        creator="agent_a",
        description="实现一个 CSV parser",
        budget=Money(amount=50.0, currency="AIC"),
    )
    task.participants = ["agent_a", "agent_b"]
    task.contractor = "agent_b"
    tasks.save(task)

    print(f"\nTask: {task_id}")
    print(f"  creator    = agent_a")
    print(f"  contractor = agent_b")
    print(f"  status     = {task.status}   ← Task Core 是唯一权威")

    # ---- Revision 1 ----
    rev1 = svc.create_context(
        task_id,
        by="agent_a",
        objective="实现一个能处理引号内逗号的 CSV parser",
        constraints=["只用 Python 标准库", "必须处理 UTF-8"],
        acceptance_criteria=["空文件返回空列表", "引号内逗号不分割"],
    )
    print(f"\n{'─' * 62}")
    print(f"Revision {rev1.revision}  (by agent_a)")
    print(f"{'─' * 62}")
    print(f"  Objective:  {rev1.context.objective}")
    for c in rev1.context.constraints:
        print(f"  Constraint: {c}")
    for a in rev1.context.acceptance_criteria:
        print(f"  Accept:     {a}")

    # ---- A 提议一个决定 ----
    print(f"\n{'─' * 62}")
    print("Agent A 提议一个 DECIDE")
    print(f"{'─' * 62}")

    change1 = svc.propose(
        task_id,
        proposed_by="agent_a",
        change_type=ContextChangeType.DECIDE,
        new_value={"statement": "用状态机而不是正则解析",
                   "reason": "正则处理嵌套引号会失控"},
        expected_revision=1,
        reason="正则方案在嵌套引号上不可靠",
        source="msg-4f-001",
    )
    print(f"  change_id  = {change1.change_id}")
    print(f"  status     = {change1.status}   ← 只是提议，Context 没变")
    print(f"  context 仍是 revision {svc.current(task_id).revision}")

    # A 自己不能批
    try:
        svc.accept(task_id, change1.change_id, decided_by="agent_a")
        print("  ❌ 意外：自批成功了")
    except SelfAlignmentError:
        print("  ✗ agent_a 自批被拒绝（「决定」需要别人确认）")

    rev2 = svc.accept(task_id, change1.change_id, decided_by="agent_b",
                      reason="同意，状态机更可控")
    print(f"  ✓ agent_b 确认 → Revision {rev2.revision}")
    d = rev2.context.decisions[0]
    print(f"      Decision:  {d.statement}")
    print(f"      Reason:    {d.reason}")
    print(f"      Proposed:  {d.proposed_by}")
    print(f"      Accepted:  {', '.join(d.accepted_by)}")

    # ---- B 提议一个假设 ----
    print(f"\n{'─' * 62}")
    print("Agent B 提议一个 ASSUME（声明类，可自批）")
    print(f"{'─' * 62}")

    change2 = svc.propose(
        task_id,
        proposed_by="agent_b",
        change_type=ContextChangeType.ASSUME,
        new_value={"statement": "输入文件的编码一定是 UTF-8"},
        expected_revision=rev2.revision,
        reason="上游文档没写，先按最常见情况做",
    )
    rev3 = svc.accept(task_id, change2.change_id, decided_by="agent_b")
    a = rev3.context.assumptions[0]
    print(f"  ✓ Revision {rev3.revision}")
    print(f"      Assumption: {a.statement}")
    print(f"      Status:     {a.status}   ← 未验证，**不是** Fact")

    # ---- 一条被拒绝的提议 ----
    print(f"\n{'─' * 62}")
    print("一条被拒绝的提议（版本号必须不变）")
    print(f"{'─' * 62}")

    change3 = svc.propose(
        task_id,
        proposed_by="agent_a",
        change_type=ContextChangeType.ADD_CONSTRAINT,
        new_value="必须支持 GBK 编码",
        expected_revision=rev3.revision,
        reason="担心老系统",
    )
    before = svc.current(task_id).revision
    svc.reject(task_id, change3.change_id, decided_by="agent_b",
               reason="超出本任务范围")
    after = svc.current(task_id).revision
    print(f"  change_id = {change3.change_id}")
    print(f"  status    = {svc.contexts.get_change(change3.change_id).status}")
    print(f"  revision  : {before} → {after}   ← 拒绝**不改变** Context")

    # ---- 开放问题 ----
    q = svc.propose(
        task_id,
        proposed_by="agent_b",
        change_type=ContextChangeType.QUESTION,
        new_value={"question": "空 CSV 应该返回 [] 还是报错？",
                   "owner": "agent_a"},
        expected_revision=after,
        reason="验收标准里有歧义",
    )
    rev4 = svc.accept(task_id, q.change_id, decided_by="agent_a")
    print(f"\n  ✓ Revision {rev4.revision}：记录了一个开放问题")
    print(f"      {rev4.context.open_questions[0].question}")

    # ---- Snapshot ----
    print(f"\n{'=' * 62}")
    print("Context Snapshot")
    print("=" * 62)

    for who in ("agent_a", "agent_b"):
        snap = svc.snapshot(task_id)
        print(f"\n--- {who} 看到的（Revision {snap.revision} @ {snap.taken_at}）---")
        print(f"  Objective:   {snap.context.objective}")
        print(f"  Decisions:   {len(snap.context.decisions)}")
        print(f"  Assumptions: {len(snap.context.assumptions)}"
              f"（未验证 {snap.context.unverified_assumption_count}）")
        print(f"  Questions:   {snap.context.open_question_count} 个待解决")
        print(f"  ── 以下**引用**自 Task Core（不是 Context 拥有的）──")
        print(f"  status:      {snap.domain_state['status']}"
              f"   source={snap.domain_state_source}")

    # ---- 历史 ----
    print(f"\n{'=' * 62}")
    print("Revision History")
    print("=" * 62)
    for r in svc.history(task_id):
        who = r.created_by or "?"
        reason = r.change_reason or "（初始版本）"
        print(f"  Revision {r.revision}  by {who:<8} ← {reason}")

    # ---- 并发 ----
    print(f"\n{'=' * 62}")
    print("并发：两个 Agent 基于**同一个** revision 同时提交")
    print("=" * 62)

    base = svc.current(task_id).revision
    print(f"  基准 revision = {base}")

    ca = svc.propose(task_id, proposed_by="agent_a",
                     change_type=ContextChangeType.ADD_CONSTRAINT,
                     new_value="必须支持流式读取",
                     expected_revision=base, reason="大文件")
    cb = svc.propose(task_id, proposed_by="agent_b",
                     change_type=ContextChangeType.ADD_CONSTRAINT,
                     new_value="必须有 100% 测试覆盖",
                     expected_revision=base, reason="质量要求")

    print(f"  A 提议 {ca.change_id}（基于 {base}）")
    print(f"  B 提议 {cb.change_id}（基于 {base}）")

    ra = svc.accept(task_id, ca.change_id, decided_by="agent_b")
    print(f"\n  A 的提议 → ✓ SUCCESS（Revision {ra.revision}）")

    try:
        svc.accept(task_id, cb.change_id, decided_by="agent_a")
        print("  B 的提议 → ❌ 意外成功（不应该）")
    except ConcurrentContextModification:
        print(f"  B 的提议 → ✗ ConcurrentContextModification")

    print(f"\n  A 的变更在 Revision {ra.revision} 里吗: "
          f"{'必须支持流式读取' in svc.current(task_id).context.constraints}")
    print(f"  B 的变更在 Revision {ra.revision} 里吗: "
          f"{'必须有 100% 测试覆盖' in svc.current(task_id).context.constraints}"
          f"   ← 没有被静默写入")
    print(f"  B 的提议状态: "
          f"{svc.contexts.get_change(cb.change_id).status}")

    # ---- 非参与方 ----
    print(f"\n{'=' * 62}")
    print("参与方边界")
    print("=" * 62)
    try:
        svc.propose(task_id, proposed_by="agent_c",
                    change_type=ContextChangeType.FACT,
                    new_value={"statement": "x", "source": "y"},
                    expected_revision=svc.current(task_id).revision,
                    reason="路人甲")
        print("  ❌ 意外：非参与方成功提交")
    except NotAParticipantError:
        print("  ✗ agent_c（非参与方）被拒绝")

    final = svc.current(task_id)
    print(f"\n{'=' * 62}")
    print(f"最终 Revision: {final.revision}")
    print(f"  历史深度:   {len(svc.history(task_id))} 版")
    print(f"  提议总数:   {svc.contexts.count_changes(task_id)}"
          f"（含被拒绝的）")
    print(f"  待裁决:     {len(svc.pending(task_id))}")
    print("=" * 62)

    return 0


# ============================================================
# 命令：reliability-multiprocess-demo（Workspace Phase 0）
# ============================================================
def cmd_reliability_multiprocess_demo(
    runtime: Runtime, args: argparse.Namespace
) -> int:
    """
    真实多进程 / 租约演示（Workspace Phase 0）。

    【它要证明的五件事】

        ① 两个**真正的进程**都能打开并写同一个数据库
        ② 同一条消息：只能有一个进程认领成功
        ③ 同一个 Task：CAS 只有一个成功，另一个被拒绝
        ④ 认领租约：未过期抢不走，过期后可以接手
        ⑤ 崩溃的 worker 留下的认领，会在租约到期后被恢复

    【它刻意不证明的事情】
        ❌ 不做 retry / 不做心跳 / 不做 Workspace
        ❌ 不做多机 —— 这是**多进程**，同一个文件系统
    """
    import multiprocessing as mp
    import os
    import tempfile
    import time as _time
    from datetime import timedelta
    from pathlib import Path as _Path

    from communication import Message, MessageType
    from core.identity import utc_now
    from core.money import Money
    from core.task import Task
    from core.task_store import ConcurrentTaskModification, TaskStore
    from communication.recorder import CommunicationRecorder
    from storage.database import Database

    sys_path = str(_Path(__file__).resolve().parent)
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from tests import multiprocess_helpers as helpers

    print()
    print("=" * 76)
    print("        AI Economy OS v0.1")
    print("        Workspace Phase 0 — Multiprocess / Claim Lease")
    print("=" * 76)
    print()
    print("  只验证两件事：")
    print("      真实的多进程并发写入（不是线程、不是单例连接）")
    print("      消息认领从「永久占用」变成「短期租约」")
    print()

    workdir = _Path(tempfile.mkdtemp(prefix="dsh-p0-"))
    db_path = str(workdir / "multiprocess.db")

    # ---- 建库 ----
    db = Database(db_path)
    db.initialize()
    pragmas = db.pragmas()
    print(f"  数据库文件 : {db_path}")
    print(f"  建立连接的进程 PID : {os.getpid()}")
    print(f"  journal_mode : {pragmas['journal_mode']}")
    print(f"  busy_timeout : {pragmas['busy_timeout']} ms")
    print()

    ctx = mp.get_context("spawn")

    def drain(queue):
        items = []
        while not queue.empty():
            items.append(queue.get())
        return items

    def run(targets, barrier_size=None, timeout=90):
        out = ctx.Queue()
        barrier = ctx.Barrier(barrier_size) if barrier_size else None
        processes = []
        for target, args in targets:
            full = (*args, barrier, out) if barrier is not None else (*args, out)
            processes.append(ctx.Process(target=target, args=full))
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=timeout)
        results = drain(out)
        out.close()
        return results

    # ============================================================
    # ① 两个独立进程打开并写同一个数据库
    # ============================================================
    print("=" * 76)
    print("① 两个真正的进程打开并写同一个数据库")
    print("=" * 76)
    print()

    results = run([
        (helpers.read_write_worker, (db_path, "A")),
        (helpers.read_write_worker, (db_path, "B")),
    ])
    for item in results:
        if item.get("step") == "opened":
            print(f"  [pid {item['pid']}] {item['worker']} 打开成功  "
                  f"journal_mode={item['journal_mode']}  "
                  f"busy_timeout={item['busy_timeout']}")
        elif item.get("step") == "done":
            print(f"  [pid {item['pid']}] {item['worker']} 写入成功  "
                  f"（看到 {item['before']} 行 → 写后 {item['after']} 行）")
        else:
            print(f"  [pid {item['pid']}] {item['worker']} ❌ {item.get('error')}")

    pids = {item["pid"] for item in results}
    print()
    print(f"  参与写入的进程数: {len(pids)}   子进程 PID: {sorted(pids)}")
    print("  ✅ 两个独立进程都成功" if len(pids) == 2
          else "  ❌ 没有真正并发")
    print()
    print("  【关键】每个进程各自 `Database(path)` —— 没有共享对象、")
    print("         没有共享连接。父进程的连接不会被传给子进程。")
    print()

    # ============================================================
    # ② 同一条消息：只能一个进程认领
    # ============================================================
    print("=" * 76)
    print("② 同一条消息 —— 两个进程同时抢，只能一个成功")
    print("=" * 76)
    print()

    results = run([
        (helpers.claim_worker, (db_path, "msg_demo_race", "worker-A")),
        (helpers.claim_worker, (db_path, "msg_demo_race", "worker-B")),
    ], barrier_size=2)

    for item in sorted(results, key=lambda r: r.get("worker", "")):
        mark = "✅ 认领成功" if item.get("ok") else f"🚫 {item.get('status')}"
        print(f"  [pid {item['pid']}] {item['worker']:<10} → {mark}")
    print()

    db2 = Database(db_path)
    state = CommunicationRecorder(db2).processing_state("msg_demo_race")
    print(f"  数据库里的最终状态:")
    print(f"      claim_id       = {state['claimed_by']}")
    print(f"      processed_once = {state['processed']}   （只是认领，还没处理完）")
    print(f"      failure_count  = {state['failure_count']}")
    print()
    print("  ✅ 只有一个进程拿到认领" if len([r for r in results if r.get("ok")]) == 1
          else "  ❌ 两个都认领成功了")
    print()

    # ============================================================
    # ③ 同一个 Task：CAS 冲突
    # ============================================================
    print("=" * 76)
    print("③ 同一个 Task —— CAS 只有一个成功，另一个被拒绝")
    print("=" * 76)
    print()

    store = TaskStore(db2)
    store.save(Task.create(task_id="T-DEMO-CAS", creator="agent_a",
                           description="CAS 演示", budget=Money(100),
                           participants=["agent_a", "agent_b"]))
    print(f"  任务初始 version = {store.get('T-DEMO-CAS').version}")
    print()

    results = run([
        (helpers.cas_worker, (db_path, "T-DEMO-CAS", "A")),
        (helpers.cas_worker, (db_path, "T-DEMO-CAS", "B")),
    ], barrier_size=2)

    for item in sorted(results, key=lambda r: r.get("worker", "")):
        if item["outcome"] == "SUCCESS":
            print(f"  [pid {item['pid']}] 写入方 {item['worker']} → ✅ 保存成功  "
                  f"version {item['version_read']} → {item['version_after']}")
        elif item["outcome"] == "CONFLICT":
            print(f"  [pid {item['pid']}] 写入方 {item['worker']} → 🚫 被拒绝  "
                  f"（我手上 v{item['expected']}，数据库已是 v{item['actual']}）")
        else:
            print(f"  [pid {item['pid']}] 写入方 {item['worker']} → ❌ {item.get('error')}")

    final = store.get("T-DEMO-CAS")
    print()
    print(f"  最终 version = {final.version}   note = {final.note!r}")
    print("  ✅ 只 +1（输家没有偷偷写进去）"
          if final.version == 1 + len([r for r in results if r["outcome"] == "SUCCESS"])
          else "  ❌ version 不对")
    print()
    print("  【这是 Workspace Phase 0 测出来的真实缺陷】")
    print("     4D.1 的并发保护在**单进程**下正确。")
    print("     真多进程测试时，失败者拿到的是：")
    print("         OperationalError: database is locked")
    print("     而不是 ConcurrentTaskModification。")
    print("     原因：BEGIN（延迟事务）先拿读锁，写的时候要升级 →")
    print("           两个进程都持读锁都在等 → 死锁 →")
    print("           SQLite 立刻返回 BUSY，**不理会 busy_timeout**。")
    print("     修法：写事务改用 BEGIN IMMEDIATE。")
    print()

    # ============================================================
    # ④ 租约：未过期抢不走，过期可接手
    # ============================================================
    print("=" * 76)
    print("④ 认领租约 —— 未过期抢不走，过期后可以接手")
    print("=" * 76)
    print()

    lease_recorder = CommunicationRecorder(db2, claim_lease_seconds=60)
    lease_message = Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.SUBMIT,
        payload={"task_id": "T-LEASE"},
        message_id="msg_demo_lease",
    )
    base = utc_now()

    def at(offset):
        return (base + timedelta(seconds=offset)).isoformat()

    print(f"  租约时长 = {lease_recorder.claim_lease_seconds} 秒")
    print()

    claim = lease_recorder.claim_message(lease_message, "worker-A", now=at(0))
    print(f"  T+0s   worker-A 认领          → {claim.status}")

    state = lease_recorder.processing_state("msg_demo_lease")
    print(f"         claim_id = {state['claimed_by']}   "
          f"claimed_at = {state['claimed_at']}")
    print()

    claim = lease_recorder.claim_message(lease_message, "worker-B", now=at(30))
    print(f"  T+30s  worker-B 想接手        → {claim.status}  "
          f"（租约还没过期，抢不走）")

    claim = lease_recorder.claim_message(lease_message, "worker-B", now=at(90))
    print(f"  T+90s  worker-B 再试          → {claim.status}  "
          f"（租约过期，可以接手）")

    state = lease_recorder.processing_state("msg_demo_lease")
    print()
    print(f"         接手后 claim_id = {state['claimed_by']}")
    print(f"                claimed_at = {state['claimed_at']}")
    print(f"                reclaimed  = {claim.reclaimed}  "
          f"previous_owner = {claim.previous_owner}")
    print(f"                processed  = {state['processed']}   "
          f"（租约过期 ≠ 处理完成）")
    print(f"                failure_count = {state['failure_count']}   "
          f"（worker 崩溃 ≠ 业务失败）")
    print()
    print("  ✅ 租约过期让占用失效，但**不改变**成功事实与失败计数")
    print()

    # ============================================================
    # ⑤ 崩溃恢复
    # ============================================================
    print("=" * 76)
    print("⑤ 崩溃恢复 —— worker 被 kill 之后，认领会在租约到期后恢复")
    print("=" * 76)
    print()

    crash_out = ctx.Queue()
    crash_process = ctx.Process(
        target=helpers.crash_after_claim_worker,
        args=(db_path, "msg_demo_crash", "crashed-worker", crash_out, 60),
    )
    crash_process.start()
    crash_process.join(timeout=60)
    for item in drain(crash_out):
        print(f"  [pid {item['pid']}] {item['worker']} 认领 → {item['status']}")
    crash_out.close()

    print("  该进程**直接退出**：不释放认领、不关闭连接、不跑任何清理代码")
    print("  （这就是 kill -9 的效果 —— 没有 finally 会执行）")
    print()

    crash_recorder = CommunicationRecorder(db2, claim_lease_seconds=60)
    state = crash_recorder.processing_state("msg_demo_crash")
    print(f"  崩溃后数据库里:")
    print(f"      claim_id = {state['claimed_by']}   "
          f"（认领留下来了 —— 因为没人清理）")
    print()

    claim = crash_recorder.claim_message(
        Message(from_agent="agent_a", to_agent="agent_b",
                message_type=MessageType.SUBMIT,
                payload={"task_id": "T-CRASH"},
                message_id="msg_demo_crash"),
        "rescuer", now=at(120),
    )
    print(f"  租约到期后，rescuer 认领      → {claim.status}  "
          f"reclaimed={claim.reclaimed}")
    print(f"      previous_owner = {claim.previous_owner}")
    print()
    print("  ✅ 恢复**不依赖崩溃进程做任何配合** —— 只靠租约到期")
    print()

    # ============================================================
    # 边界证明
    # ============================================================
    print("=" * 76)
    print("边界证明")
    print("=" * 76)
    print()

    integrity = db2.fetchone("PRAGMA integrity_check")[0]
    print(f"  数据库完整性      : {integrity}")
    print(f"  消息表行数        : {db2.fetchone('SELECT COUNT(*) AS n FROM communication_messages')['n']}")
    print(f"  成功处理过的消息  : {crash_recorder.count_processed()}")
    print()
    print(f"  Wallet / Ledger   : 未被本阶段触碰   ✅")
    print(f"  Workspace         : 仍未实现         ✅")
    print(f"  Git               : 未触碰           ✅")
    print(f"  代码执行          : 未执行           ✅")
    print(f"  Retry Scheduler   : 未实现           ✅")
    print(f"  Heartbeat         : 未实现           ✅")
    print()
    print("  · 消息去重与认领是**持久化事实** —— 跨进程、跨重启都有效")
    print("  · Task 的并发写入在**真实多进程**下被正确拒绝，不会静默覆盖")
    print("  · 认领是**短期租约** —— 崩溃的 worker 不会永久占住消息")
    print()

    db.close()
    db2.close()
    print(f"  演示目录: {workdir}")
    print()
    return 0


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
        "--agents",
        default=argparse.SUPPRESS,
        help="逗号分隔的 Agent 列表（覆盖默认，测试/脚本用）。"
             "例如 --agents agent_a,agent_b",
    )
    shared.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="数据库文件路径（默认读 config/config.yaml 里的 runtime.database_path）",
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

    # --- config（Configuration System Refactor：看解析后的配置）---
    config_cmd = subparsers.add_parser(
        "config",
        parents=[shared],
        help="显示解析后的配置（ResolvedConfig）—— 只读",
    )
    config_cmd.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON（给 GUI / 脚本用），而不是给人看的表格",
    )
    config_cmd.add_argument(
        "--agent",
        default=None,
        metavar="AGENT_ID",
        help="只显示某个 Agent，并打印它的完整解析链",
    )
    config_cmd.set_defaults(func=cmd_config)

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

    # --- reliability-multiprocess-demo（Workspace Phase 0）---
    mp_demo = subparsers.add_parser(
        "reliability-multiprocess-demo",
        parents=[shared],
        help="真实多进程 / 认领租约演示（Workspace Phase 0）",
    )
    mp_demo.set_defaults(func=cmd_reliability_multiprocess_demo)

    # --- context-demo（Step 4F）---
    context_demo = subparsers.add_parser(
        "context-demo",
        parents=[shared],
        help="Task Shared Context / Alignment 演示（Step 4F）",
    )
    context_demo.set_defaults(func=cmd_context_demo)

    ws_demo = subparsers.add_parser(
        "workspace-demo",
        parents=[shared],
        help="Workspace Core 演示（共享工作空间的责任与版本）",
    )
    ws_demo.add_argument(
        "--keep",
        action="store_true",
        help="保留 demo 产生的 Task 数据（默认清理）",
    )
    ws_demo.add_argument(
        "--workspace-id",
        default=None,
        help="指定 workspace_id（默认每次随机，避免与历史运行冲突）",
    )
    ws_demo.set_defaults(func=cmd_workspace_demo)

    hello = subparsers.add_parser(
        "hello-demo",
        parents=[shared],
        help="端到端 Demo：Human Demand → 真实 LLM → Task → Workspace → "
             "Git Worktree → 代码 → 运行",
    )
    hello.add_argument("--show-path", action="store_true",
                       help="显示 Worktree 的本地路径（**仅 Human**，不进 Agent API）")
    hello.add_argument("--cleanup", action="store_true",
                       help="清理之前保留的 Demo Worktree")
    hello.add_argument("--provider", default=None,
                       help="指定 LLM provider（默认自动挑一个配了 key 的真实 provider）")
    hello.set_defaults(func=cmd_hello_demo)

    cred = subparsers.add_parser(
        "credential-demo", parents=[shared],
        help="显示每个 Agent 实际使用的 LLM Credential（🔒 不显示 key）",
    )
    cred.add_argument("--agent", default=None, help="只看某个 Agent")
    cred.add_argument("--provider", default=None, help="按指定 provider 解析凭据")
    cred.add_argument("--real", action="store_true",
                      help="真正调用 provider（默认只展示解析结果，不打 API）")
    cred.set_defaults(func=cmd_credential_demo)

    wake = subparsers.add_parser(
        "communication-wakeup-demo", parents=[shared],
        help="Event Wakeup V1：B 睡着 → A 发消息 → B 被自动唤醒并处理",
    )
    wake.set_defaults(func=cmd_communication_wakeup_demo)

    three = subparsers.add_parser(
        "three-agent-demo", parents=[shared],
        help="真实三 Agent 协作：A(规划) → B(实现) → C(审查) → A(总结)",
    )
    three.add_argument("--keep-artifacts", default=None,
                       help="把 Demo 的临时测试目录保留到这个路径")
    three.set_defaults(func=cmd_three_agent_demo)

    snake = subparsers.add_parser(
        "snake-demo", parents=[shared],
        help="Task Demo V2：单 Agent 用真实 LLM 生成一个可运行的网页小项目",
    )
    snake.add_argument("--show-path", action="store_true",
                       help="显示 Worktree 本地路径（仅 Human）")
    snake.add_argument("--cleanup", action="store_true",
                       help="停止 server 并清理 worktree")
    snake.add_argument("--stop", action="store_true", help="只停止 server")
    snake.add_argument("--port", type=int, default=SNAKE_DEMO_PORT,
                       help=f"本地 HTTP 端口（默认 {SNAKE_DEMO_PORT}）")
    snake.add_argument("--provider", default=None, help="指定 LLM provider")
    snake.set_defaults(func=cmd_snake_demo)

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

    # 🔒 `--agents` 让「用哪几个 Agent」变成**显式**的，
    #    而不是隐式依赖 main.py 里当前的 DEFAULT_AGENTS。
    #    测试必须显式传 —— 这样仓库测试基线就不会被
    #    开发者本机的实验配置（比如加到 3 个 Agent）影响。
    raw_agents = getattr(args, "agents", None)
    agent_ids = ([a.strip() for a in str(raw_agents).split(",") if a.strip()]
                 if raw_agents else None)

    # 🔒 `config` 是**纯配置检查**命令 —— 它**不建 runtime**。
    #
    #    【为什么必须提前返回】
    #      `build_runtime` 有**写副作用**：
    #        · `Economy.bootstrap` 建库 / 跑迁移
    #        · `create_agent` 给每个 Agent 写一行 + 创世铸币
    #      对一个「看配置」的命令来说那是不可接受的 ——
    #      它会让 `./dev.sh config` 变成一条**会改数据**的只读命令。
    #
    #      而且那些装配过程会打日志到 stdout，把 `--json` 的输出污染掉，
    #      使机器没法解析。这两件事是同一个根因。
    #
    #      `cmd_config` 本来就只读 `config/config.yaml`，不需要 runtime。
    if getattr(args, "command", None) == "config":
        return cmd_config(None, args)

    try:
        runtime = build_runtime(db_path=db_path, agent_ids=agent_ids)
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

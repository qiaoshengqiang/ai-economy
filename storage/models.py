"""
数据库模型定义（Schema）
============================================================

【原则】不要一上来就用复杂 ORM。
    v0.1 直接用标准库 sqlite3 + 手写 SQL。
    等经济模型稳定了（大概 v0.3 以后）再考虑升级。

这里只放「数据长什么样」的定义：建表语句 + 表名常量。
真正执行数据库操作的在 database.py。

v0.1 建这些表：
    agents              —— 经济主体的身份
    wallets             —— 钱包（注意：不存余额！）
    ledger_transactions —— 交易账本（唯一的真相来源）
    llm_calls           —— 每次调用模型的 token 用量与成本
                      （tasks/bids/contracts/escrows/messages
                        留给 Step 2~5，现在不建，避免过度设计）
"""

from __future__ import annotations

from typing import Final

# ============================================================
# 表名常量
# 用常量而不是散落各处的字符串，避免拼写错误。
# ============================================================
TABLE_AGENTS: Final = "agents"
TABLE_WALLETS: Final = "wallets"
TABLE_LEDGER: Final = "ledger_transactions"
TABLE_LLM_CALLS: Final = "llm_calls"


# ============================================================
# 建表 SQL
# ============================================================
SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (

    # --------------------------------------------------------
    # agents —— 身份表
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS agents (
        agent_id    TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        public_key  TEXT,
        owner       TEXT,
        home_world  TEXT NOT NULL DEFAULT 'local',

        -- 声誉：v0.1 先放最朴素的三个计数，不做复杂评分公式
        reputation_score REAL NOT NULL DEFAULT 0,
        completed_tasks  INTEGER NOT NULL DEFAULT 0,
        failed_tasks     INTEGER NOT NULL DEFAULT 0,

        -- 存活状态：ACTIVE / INSOLVENT
        status      TEXT NOT NULL DEFAULT 'ACTIVE'
    )
    """,

    # --------------------------------------------------------
    # wallets —— 钱包
    #
    # 【关键设计】这张表里【没有 balance 字段】。
    #
    # 为什么？因为「余额是一个数字，随时可以被改」是经济系统最大的坑：
    # 一旦允许直接改余额，就再也说不清钱是怎么来的、有没有凭空多出来。
    #
    # 所以本系统只有一本账（ledger_transactions），
    # 余额永远是「对账本求和算出来的结果」，不是被存储的状态。
    # 这跟真正的会计系统、以及区块链的做法一致。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS wallets (
        wallet_id   TEXT PRIMARY KEY,
        agent_id    TEXT NOT NULL UNIQUE,
        currency    TEXT NOT NULL DEFAULT 'AI',
        created_at  TEXT NOT NULL,

        -- 该钱包允许透支的额度。v0.1 恒为 0：没钱就不能花。
        overdraft_limit REAL NOT NULL DEFAULT 0,

        -- 是否被冻结（未来用于风控/仲裁）
        frozen      INTEGER NOT NULL DEFAULT 0,

        FOREIGN KEY (agent_id) REFERENCES agents (agent_id)
    )
    """,

    # --------------------------------------------------------
    # ledger_transactions —— 交易账本
    #
    # 这是整个经济系统的【唯一真相来源】。
    # 每一条记录都不可变（append-only）：只允许 INSERT，不允许 UPDATE 金额。
    #
    # tx_type 取值（v0.1）：
    #   GENESIS_MINT  —— 创世铸币。新 Agent 出生时系统给它发初始资金。
    #                    这是整个系统里唯一能「凭空产生」货币的入口，
    #                    所以它被单独标识出来，方便审计。
    #   TRANSFER      —— 两个 Agent 之间的普通转账
    #   ESCROW_LOCK   —— 把钱锁进托管账户
    #   ESCROW_RELEASE—— 托管释放给收款方
    #   ESCROW_REFUND —— 托管退回付款方
    #   LLM_COST      —— 调用模型产生的成本（真金白银流出 Agent 钱包）
    #   FEE           —— 系统手续费（v0.1 不收取，先留位置）
    #
    # 约定：amount 永远是正数，方向由 from/to 决定。
    #       系统账户用 'SYS_TREASURY' 表示。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS ledger_transactions (
        tx_id       TEXT PRIMARY KEY,
        tx_type     TEXT NOT NULL,

        from_agent  TEXT,               -- 付款方；系统铸币时为 NULL
        to_agent    TEXT,               -- 收款方

        amount      REAL NOT NULL CHECK (amount >= 0),
        asset       TEXT NOT NULL DEFAULT 'AI',

        reason      TEXT,               -- 人类可读的原因
        task_id     TEXT,               -- 关联任务（可空）
        contract_id TEXT,               -- 关联合同（可空）
        escrow_id   TEXT,               -- 关联托管（可空）

        created_at  TEXT NOT NULL,
        memo        TEXT                -- 额外说明 / JSON
    )
    """,

    # 常用查询的索引：按钱包查流水、按任务查流水
    "CREATE INDEX IF NOT EXISTS idx_ledger_from ON ledger_transactions (from_agent)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_to   ON ledger_transactions (to_agent)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_task ON ledger_transactions (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_time ON ledger_transactions (created_at)",

    # --------------------------------------------------------
    # llm_calls —— 每次模型调用的账单
    #
    # 【这是本项目区别于普通 Multi-Agent 项目的地方】
    # LLM 不是免费的魔法，它是 Agent 的「生产资料成本」。
    # 每次调用都要记账：用了多少 token、折算多少 AI 币。
    #
    # 注意 ：api_cost_usd（真实美元成本）和 cost_ai（折算成 AI 币）
    #        要分开存。因为两者汇率会变，不能混在一起。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        call_id         TEXT PRIMARY KEY,
        agent_id        TEXT NOT NULL,

        provider        TEXT NOT NULL,      -- mock / deepseek / doubao / grok
        model           TEXT NOT NULL,

        input_tokens    INTEGER NOT NULL DEFAULT 0,
        output_tokens   INTEGER NOT NULL DEFAULT 0,
        total_tokens    INTEGER NOT NULL DEFAULT 0,

        -- 这次调用发生时，系统用的兑换比例快照。
        -- 为什么要存下来？
        --     如果以后你调整了兑换比例，历史记录仍然能按
        --     当时的比例复核。账本必须永远可追溯。
        -- 注意这里【不给 DEFAULT】：经济参数只允许来自
        --     config/config.yaml，不允许在表结构里藏一个数字。
        tokens_per_ai_coin INTEGER NOT NULL,
        cost_ai         REAL NOT NULL DEFAULT 0,   -- 折算成 AI 币的成本
        api_cost_usd    REAL NOT NULL DEFAULT 0,   -- 真实美元成本

        task_id         TEXT,
        success         INTEGER NOT NULL DEFAULT 1,
        error           TEXT,

        created_at      TEXT NOT NULL
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_llm_calls_agent ON llm_calls (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_task  ON llm_calls (task_id)",
)

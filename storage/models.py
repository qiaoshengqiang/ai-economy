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
    communication_messages —— 通信日志：每条消息的当前状态
    communication_events   —— 通信日志：状态事件流（append-only）
    tasks                —— 任务主体（当前状态）
    task_offers          —— 任务的报价历史（append-only）
                      （bids/contracts/escrows/workspace_*
                        留给后续步骤，现在不建，避免过度设计）
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
TABLE_COMM_MESSAGES: Final = "communication_messages"
TABLE_COMM_EVENTS: Final = "communication_events"
#: 每个 Agent 的 Inbox 运行时状态：是否启用 + legacy cutover 边界。
#: 🔒 这是「Runtime 是否消费收件箱」的事实，不是消息本体。
TABLE_COMM_INBOX_STATE: Final = "communication_inbox_state"
TABLE_TASKS: Final = "tasks"
TABLE_TASK_OFFERS: Final = "task_offers"
TABLE_TASK_AGREEMENTS: Final = "task_agreements"
TABLE_TASK_EXECUTIONS: Final = "task_executions"

# ---- Step 4F：Task Shared Context / Alignment ----
TABLE_SHARED_CONTEXTS: Final = "task_shared_contexts"
TABLE_CONTEXT_REVISIONS: Final = "task_context_revisions"
TABLE_CONTEXT_CHANGES: Final = "task_context_changes"

# ---- Workspace Core v1（第一阶段）----
# 【设计原则（任务书 §48）】
#     🔒 **只有 Explicit Lock 是持久化事实。**
#        `Effective Lock` / `Effective Controller` **永远派生** ——
#        没有表、没有列、没有缓存，避免两份会漂移的事实。
TABLE_WS_RESOURCES: Final = "workspace_resources"
TABLE_WS_EXPLICIT_LOCKS: Final = "workspace_explicit_locks"
TABLE_WS_REVISIONS: Final = "workspace_revisions"
TABLE_WS_AUDITS: Final = "workspace_audits"
TABLE_WS_EVENTS: Final = "workspace_events"

# ---- Change Pipeline V1 / Phase 1：Worktree Manager ----
# 【为什么**不能**塞进 workspace_resources】（任务书 §6）
#     Workspace 是**逻辑**协作空间；worktree 是 **Git materialization**。
#     两者的身份、生命周期、权威来源都不同 ——
#     塞进同一张表会让「logical resource」和「物理工作目录」互相污染
#     （这正是任务书 §4 要守的边界）。
TABLE_GIT_WORKTREES: Final = "git_worktrees"
TABLE_WORKTREE_INTENTS: Final = "worktree_intents"
# Worktree 自己的事件流。
# 【为什么是**独立**表，而不是复用 workspace_events】（任务书 §25）
#     任务书明确：「不要把这些硬塞进 Workspace Resource Event」，
#     同时「不要创建第二套完全平行的 audit system」。
#     所以这里的做法是：**复用它的事件表形态**（append-only +
#     operation_id + actor + detail），但在**不同的域**里单独一张表 ——
#     因为订阅方不同（Workspace Event 的订阅者是 Task 参与者；
#     Worktree Event 的订阅者是 Change Pipeline / CI）。
TABLE_WORKTREE_EVENTS: Final = "worktree_events"

# ---- Public Working Memory（Task 范围内的公共工作记忆）----
# 【为什么独立于 Shared Context】
#     Shared Context 是 Proposal → Alignment 后的**共同认知**；
#     Public Working Memory 是可写的**操作状态**（计划/分工/清单/进度）。
#     两者生命周期与写入规则不同，不能混成一张表。
#
# 【为什么存整份快照】
#     工作记忆是**小对象**；快照让 Revision N 的读取是 O(1)，
#     历史 append-only，便于审计。与 Shared Context 同一模式。
TABLE_PUBLIC_WORKING_MEMORY: Final = "task_public_working_memory"
TABLE_PUBLIC_WORKING_MEMORY_REVISIONS: Final = (
    "task_public_working_memory_revisions")

# ---- Private Working Memory（单个 Agent、单个 Task）----
# 【为什么与 Public 分表】
#     Public 是参与者可见的计划/清单；Private 是 Agent 自己的抽象结构。
#     读写权限、生命周期、清理时机都不同，不能混表。
TABLE_PRIVATE_WORKING_MEMORY: Final = "agent_private_working_memory"
TABLE_PRIVATE_WORKING_MEMORY_REVISIONS: Final = (
    "agent_private_working_memory_revisions")

# ---- Private Short / Long-Term Memory（跨 Task）----
# 【短期】固定容量（8），超过一半时压缩，digest 进入长期。
# 【长期】本版只存不读，留给后续长期记忆模块。
TABLE_PRIVATE_SHORT_TERM_MEMORY: Final = "agent_short_term_memory"
TABLE_PRIVATE_SHORT_TERM_ARCHIVE: Final = "agent_short_term_memory_archive"
TABLE_PRIVATE_LONG_TERM_MEMORY: Final = "agent_long_term_memory"

# ---- Agent Wakeup V1：定时唤醒 / 闹钟 ----
# 🔒 Alarm 是 Agent 自己的**调度状态**，不是 Message、Task 或 Heartbeat。
#    Activity Record 是唤醒结果，不是聊天消息。
TABLE_AGENT_ALARMS: Final = "agent_alarms"
TABLE_AGENT_WAKEUP_RUNS: Final = "agent_wakeup_runs"


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
        currency    TEXT NOT NULL DEFAULT 'AIC',
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
        asset       TEXT NOT NULL DEFAULT 'AIC',

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
    # 每次调用都要记账：用了多少 token、折算多少 AIC。
    #
    # 注意 ：api_cost_usd（真实美元成本）和 cost_ai（折算成 AIC）
    #        要分开存。因为两者汇率会变，不能混在一起。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        call_id         TEXT PRIMARY KEY,
        agent_id        TEXT NOT NULL,

        provider        TEXT NOT NULL,      -- mock / deepseek / doubao / grok

        -- 🔴 2026-10-07 审计：`model` 一个字段同时承担了两件事，
        --    于是「请求了 deepseek-chat，响应回显 deepseek-flash」
        --    这种情况无法被记录、也无法被追问。
        --
        --    `model` 保留为**响应模型**（向后兼容的别名）。
        model           TEXT NOT NULL,
        requested_model TEXT,               -- Runtime 实际请求的模型
        response_model  TEXT,               -- API 返回体里的模型（事实）
        requested_cognitive_budget INTEGER, -- Agent 请求的认知预算（max_tokens 上限）
        actual_max_tokens          INTEGER, -- Runtime 裁剪后真正使用的上限
        finish_reason   TEXT,               -- stop / length / ...
        output_status   TEXT,               -- VALID_CONTENT / EMPTY_CONTENT
        requested_credential_ref    TEXT,   -- 逻辑凭据标识
        requested_credential_source TEXT,   -- agent_env / global_env / ...
        requested_credential_env    TEXT,   -- 环境变量**名**（绝不是值）

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
        cost_ai         REAL NOT NULL DEFAULT 0,   -- 折算成 AIC 的成本
        api_cost_usd    REAL NOT NULL DEFAULT 0,   -- 真实美元成本

        -- 这次调用耗时（毫秒）。
        -- 为什么带单位后缀？因为叫 latency 的话，
        --     没人知道是秒还是毫秒，1.5 秒当成 1.5 毫秒就是个静默 bug。
        latency_ms      REAL NOT NULL DEFAULT 0,

        -- 这次调用的 token 数是模拟的还是真实的。
        -- 为什么必须存？
        --     MockProvider 返回的是估算值，真实 Provider 返回的是
        --     API 给的准确值。以后算经济成本时，你必须能一眼分清
        --     哪些记录是模拟数据、哪些是真花了钱的 ——
        --     否则拿模拟数据去对真实账单，账永远对不上。
        simulated       INTEGER NOT NULL DEFAULT 0,

        -- token 数据的来源（Step 2.5 新增）。
        --
        --   api       真实 usage，可以直接拿去算钱 / 对账单
        --   estimated 我们自己估算的，只能做粗略预算
        --   unknown   API 没给，我们也不知道。
        --             这时 token 列存的是 0 —— 但那是【占位的 0】，
        --             不是「真的用了 0 个 token」。两者含义完全不同。
        --
        -- 为什么 token 列不干脆改成可空（NULL）？
        --     因为 SQLite 改不了已有列的 NOT NULL 约束，
        --     已经建好的库没法平滑升级。
        --     所以用这一列承载「这个数字可不可信」的信息。
        --     ★ 判断 token 是否可信，一律看 token_source，不要看数字大小。
        token_source    TEXT NOT NULL DEFAULT 'unknown',

        task_id         TEXT,
        success         INTEGER NOT NULL DEFAULT 1,
        error           TEXT,

        created_at      TEXT NOT NULL
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_llm_calls_agent ON llm_calls (agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_task  ON llm_calls (task_id)",

    # --------------------------------------------------------
    # communication_messages —— 通信日志：消息的【当前状态】
    #
    # 【为什么消息日志要单独建表，而不是塞进 messages 表】
    #     现在还没有 messages 表（消息只活在内存队列里）。
    #     而这张表是【日志】，不是消息本体 ——
    #     消息本体是 communication/message.py 里的 Message 对象。
    #
    #     两者的区别很重要：
    #         Message      通信用的数据（会被序列化、发出去）
    #         这张表        观测用的记录（不参与通信，删了也不影响通信）
    #     所以「Message 不等同于日志」这条要求在物理上是成立的。
    #
    # 【为什么以 message_id 为主键】
    #     一条消息可能被投递多次（网络重发、重试）。
    #     主键 + UPSERT 保证「一个 message_id 只有一行状态」，
    #     多次投递的痕迹记在 communication_events 里。
    #     这样查询「这条消息现在什么状态」永远是确定的。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS communication_messages (
        message_id      TEXT PRIMARY KEY,

        -- 串起一整条业务链路用的两个 ID
        --   trace_id        一次完整任务 / 业务流程（跨多条消息）
        --   conversation_id 一轮对话 / 协商（通常一个来回）
        trace_id        TEXT,
        conversation_id TEXT,

        from_agent      TEXT NOT NULL,
        to_agent        TEXT NOT NULL,
        message_type    TEXT NOT NULL,

        -- 生命周期时间戳。哪一步没发生就是 NULL，
        -- 所以「卡在哪一步」可以直接从这一行看出来。
        created_at      TEXT,
        sent_at         TEXT,
        received_at     TEXT,
        processed_at    TEXT,

        -- 通信方式与地址。
        -- 【为什么 address 要单独存】
        --     将来是 P2P 时，"transport=p2p, address=p2p://12D3KooW..."
        --     才能说明消息往哪儿去了。只存 transport 不够定位问题。
        transport       TEXT,
        address         TEXT,
        is_remote       INTEGER NOT NULL DEFAULT 0,

        -- 当前状态（取最后一个事件的状态）
        --
        -- 【注意 status 是「最新状态」，不是「处理过没有」】
        --     它会被后来的事件覆盖：
        --         处理成功      → status = PROCESSED
        --         又来一条重复  → status = DUPLICATE   ← PROCESSED 被抹掉了
        --     所以 **不能** 拿它当去重依据。
        --     去重依据是下面的 processed_once。
        status          TEXT NOT NULL DEFAULT 'CREATED',
        retry_count     INTEGER NOT NULL DEFAULT 0,

        -- ============================================================
        -- 持久化的处理事实（Step 4D.1）
        -- ============================================================
        --
        -- 【原来错在哪】
        --     「这条消息处理过了吗」只记在进程内存里
        --     （communication/dedup.py 的 SeenMessageTracker）。
        --     进程一重启就忘光了 —— 同一条消息会被再处理一遍。
        --
        --     单机演示时看不出来。一旦有重发 / 重试 / 多进程，
        --     后果是「同一个 SUBMIT 交付两次」这类真实业务损坏。
        --
        -- 【processed_once：只增不减的成功事实】
        --     0 → 1 之后**永不回退**（UPSERT 时用 MAX 语义）。
        --     这才是去重真正的依据：查它，而不是查 status。
        --
        --     为什么单独一个字段而不是复用 processed_at？
        --         processed_at 是时间戳，语义是「什么时候处理完的」；
        --         而且它也会被 COALESCE 逻辑影响。
        --         一个专门的 0/1 标志更难被误用。
        processed_once  INTEGER NOT NULL DEFAULT 0,

        -- 【原子认领】谁正在处理这条消息
        --     两个 worker 同时收到同一条消息时，
        --     用带条件的 UPDATE 抢认领：
        --         UPDATE ... SET claim_id=? WHERE message_id=?
        --           AND processed_once=0 AND claim_id IS NULL
        --     影响行数为 1 的那个才算抢到 —— 这是原子的，
        --     不存在「都发现没人处理 → 都去处理」的 TOCTOU 窗口。
        claim_id        TEXT,
        claimed_at      TEXT,

        -- 【失败事实，但不代表永久作废】
        --     处理失败可能是**临时**的（数据库忙、系统异常），
        --     所以失败了只累加这个计数，
        --     **不**把 processed_once 置 1。
        --     否则重试的机会就被永久抹掉了。
        failure_count   INTEGER NOT NULL DEFAULT 0,

        -- 失败信息。成功时为 NULL。
        error_code      TEXT,
        error_message   TEXT,

        -- payload 的元信息。
        -- 【为什么即使存了完整 payload 也要存这两个】
        --     生产环境可能只留元数据和 hash（避免日志无限膨胀）。
        --     现在就把字段准备好，将来切换时不用改表结构。
        --     payload_size 是【原始】大小；payload_hash 算的是脱敏后的内容。
        payload_size    INTEGER NOT NULL DEFAULT 0,
        payload_hash    TEXT,

        -- 完整 payload（开发环境方便调试；已脱敏；超长会截断）
        payload         TEXT,

        protocol_version TEXT,
        reply_to        TEXT,

        updated_at      TEXT
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_comm_msg_trace  ON communication_messages (trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_comm_msg_conv   ON communication_messages (conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_comm_msg_status ON communication_messages (status)",
    "CREATE INDEX IF NOT EXISTS idx_comm_msg_from   ON communication_messages (from_agent)",
    "CREATE INDEX IF NOT EXISTS idx_comm_msg_to     ON communication_messages (to_agent)",
    "CREATE INDEX IF NOT EXISTS idx_comm_msg_inbox_pending "
    "ON communication_messages (to_agent, status, created_at)",

    # --------------------------------------------------------
    # communication_events —— 通信日志：状态【事件流】
    #
    # append-only。只 INSERT，不 UPDATE、不 DELETE。
    #
    # 【为什么状态要单独记事件，而不是只更新那一行】
    #     只看最终状态的话，「这条消息失败过两次又成功了」
    #     和「一次就成功」看起来一模一样。
    #     而这两件事在排查问题时意义完全不同 ——
    #     前者说明网络在抖，后者说明一切正常。
    #
    #     事件流还能回答：
    #         · 每两步之间隔了多久（延迟在哪一段）
    #         · 有没有重复投递
    #         · 失败的具体原因和顺序
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS communication_events (
        event_id        TEXT PRIMARY KEY,
        message_id      TEXT NOT NULL,

        -- 冗余存 trace/conversation，是为了「只查事件表」也能定位，
        -- 不用每次都 JOIN 回消息表。日志表宁可多一点冗余。
        trace_id        TEXT,
        conversation_id TEXT,

        -- 事件类型：CREATED / SENT / DELIVERED / RECEIVED /
        --          PROCESSING / PROCESSED / DUPLICATE / FAILED /
        --          TIMEOUT / RETRY
        status          TEXT NOT NULL,

        -- 这个事件是谁触发的（发送方还是接收方）
        actor           TEXT,

        transport       TEXT,
        address         TEXT,

        detail          TEXT,

        error_code      TEXT,
        error_message   TEXT,

        payload_size    INTEGER,
        payload_hash    TEXT,

        created_at      TEXT NOT NULL
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_comm_evt_message ON communication_events (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_comm_evt_trace   ON communication_events (trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_comm_evt_conv    ON communication_events (conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_comm_evt_status  ON communication_events (status)",
    "CREATE INDEX IF NOT EXISTS idx_comm_evt_time    ON communication_events (created_at)",

    # --------------------------------------------------------
    # communication_inbox_state —— Agent Inbox 运行时状态
    #
    # 【为什么需要这张表】
    #     消息本身已经在 communication_messages 里持久化；
    #     但 Runtime 还需要知道：
    #         · 这个 Agent 是否启用 inbox 消费
    #         · 哪条时间线之前的历史消息不重放（cutover）
    #
    # 🔒 它不是消息状态，也不是 Task 状态。
    #    它只控制「Runtime 从哪里开始消费」。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS communication_inbox_state (
        agent_id      TEXT PRIMARY KEY,
        enabled       INTEGER NOT NULL DEFAULT 0,
        cutover_at    TEXT NOT NULL,
        last_seen_at  TEXT,
        updated_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comm_inbox_enabled "
    "ON communication_inbox_state (enabled)",

    # --------------------------------------------------------
    # tasks —— 任务主体
    #
    # 【注意这里的字段设计反映了两组「不能合并」的概念】
    #
    #   1) task_id 与 current_trace_id 分开
    #         task_id            任务的稳定身份（跨执行不变）
    #         current_trace_id   「这一次执行」的链路
    #
    #         同一个任务失败重跑时，trace 会换，task_id 不变。
    #         如果合成一个字段，重跑就会变成「另一个任务」，
    #         历史没法归并，也算不出「这个任务一共花了多少」。
    #
    #   2) budget_* 与 agreed_price_* 分开
    #         budget        发起方愿意付的**上限**
    #         agreed_price  谈成的**实际价格**
    #
    #         用同一个字段表示两个概念，会导致账本凭空多计
    #         （预算 100、成交 25，混用就会多记 75）。
    #
    # 【为什么金额拆成两列而不是一列 JSON】
    #     拆开才能用 SQL 做聚合，比如「所有任务的成交总额」。
    #     JSON 列没法直接 SUM。
    #
    # 【为什么没有 workspace_permissions / workspace_members】
    #     因为本步骤不实现 Workspace 权限。
    #     workspace_id 只是一个逻辑引用，不产生任何权限。
    #     建空表会让人误以为系统已经有那个能力。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id             TEXT PRIMARY KEY,

        -- 这一次执行的链路（和 task_id 是两回事，见上）
        current_trace_id    TEXT,

        creator             TEXT NOT NULL,
        contractor          TEXT,

        -- JSON 数组：未来一个任务可以有多个参与 Agent
        participants        TEXT,

        status              TEXT NOT NULL DEFAULT 'CREATED',

        -- SOLO / TEAM。只影响角色规则（Solo 允许承接方自验），
        -- 不定义第二套生命周期。
        mode                TEXT NOT NULL DEFAULT 'TEAM',

        -- 当前 Iteration。COMPLETED 后 Reopen 时 +1；历史在 append-only 表中。
        iteration           INTEGER NOT NULL DEFAULT 1,

        description         TEXT,

        -- 【只是逻辑标识，不是路径、不是权限】
        -- 真正的 Workspace 访问要经过未来的 Workspace Gateway
        workspace_id        TEXT,

        -- 经济约定（不是真实账目）
        budget_amount         REAL,
        budget_currency       TEXT,
        agreed_price_amount   REAL,
        agreed_price_currency TEXT,

        -- JSON：验收条件，如 {"type": "tests"}
        acceptance          TEXT,

        -- JSON 数组：任务**需要**哪些能力（Step 4B）
        --
        -- 【注意这只是声明】
        --     它描述「这个任务需要什么」，
        --     不表示「已经找到了谁」，也不产生任何权限。
        --     候选 Agent 由 AgentDirectory 查找，选择由发起方 Agent 决定。
        required_capabilities TEXT,

        expires_at          TEXT,

        -- 乐观锁版本号（Step 4D.1）
        --
        -- 【为什么需要它】
        --     save() 原本是无条件 UPSERT —— 谁后写谁覆盖，
        --     而且两边都以为自己成功了。这种 bug 不会报错，
        --     只会让数据悄悄变错。
        --
        --     有了它，save() 变成 compare-and-set：
        --         UPDATE tasks SET ... version = version + 1
        --          WHERE task_id = ? AND version = ?
        --     影响行数为 0 就说明「这个 Task 已经被别人改过了」，
        --     此时必须抛异常，绝不静默覆盖。
        --
        -- 【为什么它不属于 Task Domain State】
        --     并发冲突是 persistence / application 层的问题，
        --     不是任务生命周期的一个阶段。
        --     所以不新增 CONFLICT / PROCESSING 之类的状态。
        version             INTEGER NOT NULL DEFAULT 0,

        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        note                TEXT
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks (status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_creator   ON tasks (creator)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_contractor ON tasks (contractor)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks (workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_trace     ON tasks (current_trace_id)",

    # --------------------------------------------------------
    # task_offers —— 报价历史（append-only）
    #
    # 【为什么要单独一张表，而不是把报价塞进 tasks 的 JSON 列】
    #     因为报价是**历史事件**：只追加，不改写。
    #     而且将来要能查询，比如：
    #         「这个任务被还了几次价？」
    #         「平均压价幅度是多少？」
    #     单列一个表才能做这些聚合。
    #
    # 【message_id 可以为空】
    #     因为 Task Core 不依赖通信层。
    #     将来把报价接到通信上之后，这里会填那条消息的 ID，
    #     于是就能从任务一路查到通信日志。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS task_offers (
        offer_id    TEXT PRIMARY KEY,

        task_id     TEXT NOT NULL,

        -- TASK_OFFER / COUNTER_OFFER / ACCEPT
        offer_type  TEXT NOT NULL,

        amount      REAL NOT NULL,
        currency    TEXT NOT NULL,

        -- 关联的通信消息（可空 —— Task Core 不依赖通信层）
        message_id  TEXT,

        -- Step 4C：让报价历史自解释
        --   reply_to        回应的是哪一条消息（ACCEPT 靠它反查被接受的报价）
        --   from_agent      谁提的（角色 / 轮次验证靠它）
        --   trace_id        哪一次执行
        --   conversation_id 哪一轮对话
        reply_to        TEXT,
        from_agent      TEXT,
        trace_id        TEXT,
        conversation_id TEXT,

        note        TEXT,
        created_at  TEXT NOT NULL,

        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_task_offers_task ON task_offers (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_offers_type ON task_offers (offer_type)",

    # --------------------------------------------------------
    # task_agreements —— 成交凭证（append-only）
    #
    # 【这不是账本，也不是支付记录】
    #     ACCEPT 只表示「合同成立」。这里一分钱都没动。
    #     以后 Payment 阶段可以**引用**这条凭证去执行转账。
    #
    # 【为什么要单独一张表】
    #     tasks.agreed_price 会随任务状态变化而被后续操作影响，
    #     而「当时到底谈成了什么」必须有一份**不会再变**的记录。
    #     和账本一样：只 INSERT，不改写。
    #
    # 【accepted_offer_message_id 为什么不是外键】
    #     因为它指向的是**通信消息**，而消息在通信日志里。
    #     Task Core 不依赖 communication，所以这里只存 ID，
    #     需要时自己去 journal 查（§18 要的就是这条可追踪的路径）。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS task_agreements (
        agreement_id    TEXT PRIMARY KEY,
        task_id         TEXT NOT NULL,

        initiator       TEXT NOT NULL,
        contractor      TEXT NOT NULL,

        price_amount    REAL NOT NULL,
        price_currency  TEXT NOT NULL,

        accepted_offer_message_id TEXT,

        trace_id        TEXT,
        conversation_id TEXT,

        created_at      TEXT NOT NULL,

        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    # --------------------------------------------------------
    # task_executions —— 执行阶段的事件记录（Step 4D）
    #
    # 【为什么是 append-only 的事件表，而不是在 tasks 上加几个字段】
    #
    #     1. 返工是正常流程
    #            VERIFY(false) → 返工 → 再 SUBMIT → 再 VERIFY
    #        所以一个任务会**多组** SUBMIT / VERIFY。
    #        如果只在 tasks 上存「最近一次」，上一轮为什么不通过就丢了 ——
    #        而那正是承接方返工时最需要的信息。
    #
    #     2. 不许把 Task 变成大 JSON 袋子
    #        把 submission / verification 塞成 tasks 上的两个 JSON 列，
    #        会让 Task 慢慢变成一个什么都装的容器。
    #        执行事件是**独立的事实**，值得独立一张表。
    #
    #     3. 沿用 task_offers 已经验证过的模式
    #        议价阶段用「事件表 + 四 ID 关联」记录报价历史，效果很好。
    #        执行阶段用同一个模式，架构上是一致的。
    #
    # 【主键为什么是 message_id】
    #     每个事件对应且只对应一条消息，所以 message_id 天然唯一。
    #     把它当主键让**幂等变成结构性的**：
    #     重复投递会撞主键，数据库层面就不可能写入第二条 ——
    #     不依赖应用层的判断是否记得做。
    #
    #     而且这样**不需要发明新 ID**（没有 execution_id / revision_id）。
    #     「第几轮」是推导出来的（数 SUBMIT 有几条），不单独存，
    #     所以不会出现「记录的轮次和实际事件数对不上」的漂移。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS task_executions (
        -- 触发这条记录的消息。同时是幂等键。
        message_id      TEXT PRIMARY KEY,

        task_id         TEXT NOT NULL,

        -- START / SUBMIT / VERIFY / COMPLETE
        event_type      TEXT NOT NULL,

        -- ---- SUBMIT 专有 ----
        result_type     TEXT,
        result_ref      TEXT,
        result_summary  TEXT,
        result_metadata TEXT,

        -- ---- VERIFY 专有 ----
        passed          INTEGER,       -- 0 / 1，NULL 表示不是 VERIFY
        feedback        TEXT,

        reply_to        TEXT,
        from_agent      TEXT,
        trace_id        TEXT,
        conversation_id TEXT,

        -- 事件之后任务处于什么状态（审计时不必回放）
        status_after    TEXT,

        created_at      TEXT NOT NULL,

        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_agreements_task ON task_agreements (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_agreements_time ON task_agreements (created_at)",

    # ========================================================
    # Step 4F：Task Shared Context / Alignment（三张表）
    # ========================================================
    #
    # 【为什么是三张而不是一张】
    #
    #     一张表（tasks 上加一个 context JSON 列）做不到这三件事：
    #
    #         ① 保留修订历史
    #            「Revision 3 取代 2」不等于「2 可以删掉」。
    #            一个 Agent 看到 Revision 7 时必须能追溯它是怎么来的。
    #
    #         ② 记录「提议过但没通过」的东西
    #            被拒绝的 Change 本身就是有价值的历史 ——
    #            「当时讨论过 Redis，但因为 X 否掉了」
    #            能防止三周后有人再提一遍同样的方案。
    #
    #         ③ 让乐观锁有一个明确的落点
    #            CAS 需要一个「当前版本」的单行事实，
    #            而历史需要一个 append-only 的序列。两者形态不同。
    #
    #     所以拆成：
    #         task_shared_contexts   当前版本（**唯一会被 UPDATE 的表**）
    #         task_context_revisions 历史版本（**append-only，永不 UPDATE**）
    #         task_context_changes   提议与裁决（**append-only**）
    #
    # 【为什么 payload 存物化全文而不是 diff】
    #     读「Revision 5 长什么样」应该是 O(1) 的，不该回放 5 条 diff。
    #     代价是存储冗余 —— 但 Shared Context 是**小**对象
    #     （目标/约束/若干决策假设问题），不是大文档。
    #     diff 链断裂会丢历史；物化全文明天也读得出来。
    #
    # 【注意：这里没有 status 列】
    #     Task Core 才是生命周期的唯一权威。
    #     Shared Context 存 status 就会出现「两个都声称正确」的值。
    #     要展示任务状态时，Snapshot 现场读 Task Core（引用，不是副本）。
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS task_shared_contexts (
        -- 一个 Task 一个 Context。task_id 直接当主键。
        task_id         TEXT PRIMARY KEY,

        -- Context 自己的标识（和 task_id 不同：任务身份 vs 上下文身份）
        context_id      TEXT NOT NULL,

        -- 当前版本。**乐观锁的落点**：CAS 就在这一列上。
        current_revision INTEGER NOT NULL DEFAULT 1,

        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,

        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS task_context_revisions (
        task_id         TEXT NOT NULL,
        revision        INTEGER NOT NULL,

        context_id      TEXT NOT NULL,

        -- 这一版的**物化全文**（JSON）
        payload         TEXT NOT NULL,

        created_at      TEXT NOT NULL,
        created_by      TEXT,
        parent_revision INTEGER,

        -- 由哪条 Change 产生（Revision 1 为 NULL：初始版不是变更产生的）
        change_id       TEXT,
        change_reason   TEXT,

        PRIMARY KEY (task_id, revision),

        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS task_context_changes (
        change_id       TEXT PRIMARY KEY,
        task_id         TEXT NOT NULL,

        proposed_by     TEXT NOT NULL,
        change_type     TEXT NOT NULL,

        -- 提议者基于哪一版（乐观锁快照）
        target_revision INTEGER NOT NULL,

        -- 变更载荷（载荷形状随 change_type 变化）
        payload         TEXT NOT NULL,

        -- 针对哪条已有记录（RESOLVE_QUESTION / CONFIRM_ASSUMPTION）
        target          TEXT,
        expected_value  TEXT,

        -- 为什么改 / 依据是什么
        reason          TEXT NOT NULL,
        source          TEXT,

        created_at      TEXT NOT NULL,

        -- PROPOSED / ACCEPTED / REJECTED
        status          TEXT NOT NULL DEFAULT 'PROPOSED',
        decided_at      TEXT,
        decided_by      TEXT,
        decided_reason  TEXT,
        result_revision INTEGER,

        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    # --------------------------------------------------------
    # Workspace Core v1 —— 资源树
    # --------------------------------------------------------
    # 🔒 `resource_id` 是**稳定的身份**；`path` 只是派生的位置缓存。
    #    rename / move 之后 resource_id 不变、path 改变 ——
    #    所以 Revision / Audit / Lock / Handoff 全部引用 resource_id，
    #    **不把 path 当身份**（任务书 §29）。
    #
    # 🔒 `state` 支持 active / deleted ——
    #    删除不让资源在数据库里瞬间消失（任务书 §21）。
    """
    CREATE TABLE IF NOT EXISTS workspace_resources (
        resource_id     TEXT PRIMARY KEY,
        workspace_id    TEXT NOT NULL,
        parent_id       TEXT,
        name            TEXT NOT NULL,
        path            TEXT NOT NULL,
        resource_type   TEXT NOT NULL,
        state           TEXT NOT NULL DEFAULT 'active',
        revision        INTEGER NOT NULL DEFAULT 1,
        content         TEXT,
        created_by      TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,

    # --------------------------------------------------------
    # Workspace Core v1 —— 显式责任锁（**唯一的锁事实**）
    # --------------------------------------------------------
    # 🔒 `resource_id` 做主键 ⇒ 一个资源最多一条显式锁
    #    ⇒ **同一 File 不可能有两个 Explicit Holder** ——
    #      「同一 File 单 Holder」由 schema 保证，不靠应用层自觉。
    #
    # 🔒 创建资源时**不写**这张表（任务书 §11）——
    #    新资源隐式继承父目录责任，重复记录同一责任是冗余。
    #
    # ⚠️ **没有 effective_* 列** —— Effective Lock 永远派生。
    """
    CREATE TABLE IF NOT EXISTS workspace_explicit_locks (
        resource_id   TEXT PRIMARY KEY,
        holder        TEXT NOT NULL,
        kind          TEXT NOT NULL,
        acquired_at   TEXT NOT NULL,
        acquired_by   TEXT NOT NULL
    )
    """,

    # --------------------------------------------------------
    # Workspace Core v1 —— Revision（append-only）
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS workspace_revisions (
        revision_id     TEXT PRIMARY KEY,
        resource_id     TEXT NOT NULL,
        workspace_id    TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        created_by      TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        change_type     TEXT NOT NULL,
        change_ref      TEXT
    )
    """,

    # --------------------------------------------------------
    # Workspace Core v1 —— Audit（含失败尝试，长期保存）
    # --------------------------------------------------------
    # 🔒 **包含失败的尝试**（EDIT_ATTEMPT / result = FAILED）——
    #    与 Event 严格区分（任务书 §30）。
    """
    CREATE TABLE IF NOT EXISTS workspace_audits (
        audit_id      TEXT PRIMARY KEY,
        operation_id  TEXT NOT NULL,
        workspace_id  TEXT NOT NULL,
        action        TEXT NOT NULL,
        actor         TEXT NOT NULL,
        resource_id   TEXT,
        result        TEXT NOT NULL,
        error_code    TEXT,
        detail        TEXT,
        task_id       TEXT,
        created_at    TEXT NOT NULL
    )
    """,

    # --------------------------------------------------------
    # Workspace Core v1 —— Workspace Event（只记录成功变化）
    # --------------------------------------------------------
    # 🔒 **没有 `*_FAILED` 事件** —— 失败属于 Audit。
    # 🔒 **Event ≠ 向所有 Agent 发 Message** ——
    #    它只是让未来的 Runtime / Communication 层**可以订阅**
    #    （任务书 §31 / §46）。
    """
    CREATE TABLE IF NOT EXISTS workspace_events (
        event_id      TEXT PRIMARY KEY,
        workspace_id  TEXT NOT NULL,
        event_type    TEXT NOT NULL,
        resource_id   TEXT,
        revision      INTEGER,
        actor         TEXT NOT NULL,
        detail        TEXT,
        created_at    TEXT NOT NULL
    )
    """,

    # --------------------------------------------------------
    # Change Pipeline V1 / Phase 1 —— Git Worktree
    # --------------------------------------------------------
    # 🔒 `workspace_revision` 与 `base_commit` 是**两个不同的字段**。
    #
    #     workspace_revision = Workspace 的**逻辑**状态版本
    #     base_commit        = Git 的 commit sha
    #
    #     它们几乎总是不一致，而且**必须允许**不一致。
    #     合并成一个字段会让 Patch 阶段的 applicability 无法正确判定
    #     （ADR-0033 §三）。
    #
    # ⚠️ `physical_path` **只允许在本表与内部日志里出现**。
    #    Agent-facing 的返回必须剥掉它（任务书 §5）。
    #
    # 【state 的取值】见 change/worktree.py 的 WorktreeState
    #     unprovisioned / provisioning / ready / active
    #     / frozen / archived
    #     / provision_failed / inconsistent / orphaned
    """
    CREATE TABLE IF NOT EXISTS git_worktrees (
        worktree_id         TEXT PRIMARY KEY,
        workspace_id        TEXT NOT NULL,
        task_id             TEXT,
        repository_ref      TEXT NOT NULL,
        base_commit         TEXT NOT NULL,
        workspace_revision  INTEGER NOT NULL,
        git_ref             TEXT,
        state               TEXT NOT NULL,
        physical_path       TEXT,
        detail              TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        last_verified_at    TEXT
    )
    """,

    # --------------------------------------------------------
    # Change Pipeline V1 / Phase 1 —— Intent Log（任务书 §9）
    # --------------------------------------------------------
    # 🔒 **intent 记录的是「系统准备做什么」**，
    #    写在副作用**之前**。这是 ADR-0036 的 intent log 落地点：
    #
    #        DB: 写 INTENT（pending）
    #        FS: git worktree add
    #        DB: 写 CONFIRM（completed）
    #
    #    崩在中间 → intent 停在 pending → 启动或操作边界时 reconciliation。
    #
    # 🔒 `idempotency_key` 上有**唯一索引** ——
    #    同一个逻辑操作重试时，第二次插入会被数据库拒绝，
    #    而不是再建一个 Git worktree。
    """
    CREATE TABLE IF NOT EXISTS worktree_events (
        event_id      TEXT PRIMARY KEY,
        workspace_id  TEXT NOT NULL,
        event_type    TEXT NOT NULL,
        worktree_id   TEXT,
        actor         TEXT NOT NULL,
        detail        TEXT,
        created_at    TEXT NOT NULL
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS worktree_intents (
        intent_id       TEXT PRIMARY KEY,
        workspace_id    TEXT NOT NULL,
        kind            TEXT NOT NULL,
        state           TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        operation_id    TEXT NOT NULL,
        worktree_id     TEXT,
        payload         TEXT,
        last_error      TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,

    # --------------------------------------------------------
    # Output Commit —— 任务完成后的输出仓库提交请求
    # --------------------------------------------------------
    # 🔒 这不是 Task 生命周期状态；Task 仍然是 COMPLETED。
    #    唯一索引保证同一个 Task / iteration / snapshot / target
    #    不会因为重试变成两个 commit request。
    """
    CREATE TABLE IF NOT EXISTS commit_requests (
        request_id      TEXT PRIMARY KEY,
        task_id         TEXT NOT NULL,
        iteration       INTEGER NOT NULL,
        snapshot_seq    INTEGER NOT NULL,
        snapshot_commit TEXT NOT NULL,
        snapshot_tree   TEXT NOT NULL,
        mode            TEXT NOT NULL,
        repo_mode       TEXT NOT NULL DEFAULT 'output',
        status          TEXT NOT NULL,
        output_repo     TEXT NOT NULL,
        target_ref      TEXT NOT NULL,
        base_commit     TEXT,
        commit_sha      TEXT,
        merge_sha       TEXT,
        authorized_by   TEXT,
        authorized_at   TEXT,
        error           TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,

    # --------------------------------------------------------
    # Public Working Memory
    # --------------------------------------------------------
    # 🔒 当前指针表：**唯一会被 UPDATE** 的表（CAS 在 revision 上）。
    #    payload 是整份工作记忆的 JSON 快照。
    """
    CREATE TABLE IF NOT EXISTS task_public_working_memory (
        task_id         TEXT PRIMARY KEY,
        revision        INTEGER NOT NULL,
        payload         TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        updated_by      TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    # 历史版本：append-only，永不 UPDATE / DELETE。
    """
    CREATE TABLE IF NOT EXISTS task_public_working_memory_revisions (
        task_id         TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        payload         TEXT NOT NULL,
        author          TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (task_id, revision),
        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    # --------------------------------------------------------
    # Private Working Memory（每个 Agent 每个 Task 一份）
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS agent_private_working_memory (
        agent_id        TEXT NOT NULL,
        task_id         TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        payload         TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (agent_id, task_id),
        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS agent_private_working_memory_revisions (
        agent_id        TEXT NOT NULL,
        task_id         TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        payload         TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (agent_id, task_id, revision),
        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    # --------------------------------------------------------
    # Private Short / Long-Term Memory（跨 Task）
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS agent_short_term_memory (
        agent_id        TEXT PRIMARY KEY,
        revision        INTEGER NOT NULL,
        payload         TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS agent_short_term_memory_archive (
        agent_id        TEXT NOT NULL,
        entry_id        TEXT NOT NULL,
        payload         TEXT NOT NULL,
        archived_at     TEXT NOT NULL,
        long_term_entry_id TEXT,
        PRIMARY KEY (agent_id, entry_id)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS agent_long_term_memory (
        agent_id        TEXT NOT NULL,
        entry_id        TEXT NOT NULL,
        payload         TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (agent_id, entry_id)
    )
    """,

    # --------------------------------------------------------
    # Agent Wakeup V1：闹钟定义
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS agent_alarms (
        alarm_id           TEXT PRIMARY KEY,
        agent_id           TEXT NOT NULL,
        status             TEXT NOT NULL,
        reason             TEXT NOT NULL,
        instruction        TEXT NOT NULL,
        task_ids_json      TEXT NOT NULL DEFAULT '[]',
        schedule_kind      TEXT NOT NULL,
        interval_seconds   INTEGER,
        next_fire_at       TEXT NOT NULL,
        last_fired_at      TEXT,
        fire_count         INTEGER NOT NULL DEFAULT 0,
        max_runs           INTEGER,
        expires_at         TEXT,
        dedupe_key         TEXT,
        last_error         TEXT,
        created_by         TEXT NOT NULL,
        source_message_id  TEXT,
        conversation_id    TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_alarms_due "
    "ON agent_alarms (status, next_fire_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_alarms_agent "
    "ON agent_alarms (agent_id, status)",

    # --------------------------------------------------------
    # Agent Wakeup V1：每次唤醒的 Activity Record
    # --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS agent_wakeup_runs (
        run_id          TEXT PRIMARY KEY,
        wakeup_id       TEXT NOT NULL,
        agent_id        TEXT NOT NULL,
        kind            TEXT NOT NULL,
        scheduled_for   TEXT NOT NULL,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        outcome         TEXT NOT NULL,
        summary         TEXT,
        job_id          TEXT,
        detail_json     TEXT NOT NULL DEFAULT '{}',
        UNIQUE (wakeup_id, scheduled_for)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_wakeup_runs_agent "
    "ON agent_wakeup_runs (agent_id, started_at)",
)


# ============================================================
# 必须在【补列之后】才能建的索引
# ============================================================
# 【为什么单独一组】
#     索引建立在列之上。如果某一列是后来 ALTER TABLE 加上去的，
#     那么在老库上「CREATE INDEX ... (那一列)」会直接报
#     "no such column" —— 因为执行 SCHEMA 的时候列还不存在。
#
#     所以这些索引不能放在 SCHEMA 里，必须等 migrate() 补完列再建。
#
#     Step 4C 第一次踩到这个坑：给 task_offers 加了 from_agent 列
#     并顺手在同一批里建索引，结果老库一启动就崩。
POST_MIGRATION_INDEXES: Final[tuple[str, ...]] = (
    # ---- Step 4D：执行事件的两个常用查询方向 ----
    #     按任务查（"这个任务经历了什么"）
    #     按发送方查（"这个 Agent 提交过什么"）
    "CREATE INDEX IF NOT EXISTS idx_task_executions_task "
    "ON task_executions (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_executions_from "
    "ON task_executions (from_agent)",
    "CREATE INDEX IF NOT EXISTS idx_task_offers_msg  ON task_offers (message_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_offers_from ON task_offers (from_agent)",

    # ---- Step 4F：Shared Context 的三个查询方向 ----
    #     按任务列出历史版本（"这个任务经历过哪些版本"）
    "CREATE INDEX IF NOT EXISTS idx_context_revisions_task "
    "ON task_context_revisions (task_id, revision)",
    #     按任务列出提议（含被拒绝的）
    "CREATE INDEX IF NOT EXISTS idx_context_changes_task "
    "ON task_context_changes (task_id, created_at)",
    #     按状态找待裁决的提议
    "CREATE INDEX IF NOT EXISTS idx_context_changes_status "
    "ON task_context_changes (task_id, status)",

    # ---- Workspace Core v1 ----
    #     路径解析 / list()：同一 workspace 内路径唯一
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ws_resources_path "
    "ON workspace_resources (workspace_id, path)",
    #     树遍历：找子资源（descendants / delete / move 用）
    "CREATE INDEX IF NOT EXISTS idx_ws_resources_parent "
    "ON workspace_resources (workspace_id, parent_id)",
    #     按状态过滤（默认只看 active）
    "CREATE INDEX IF NOT EXISTS idx_ws_resources_state "
    "ON workspace_resources (workspace_id, state)",
    #     按 holder 反查「某人负责哪些资源」
    "CREATE INDEX IF NOT EXISTS idx_ws_locks_holder "
    "ON workspace_explicit_locks (holder)",
    #     版本历史
    "CREATE INDEX IF NOT EXISTS idx_ws_revisions_resource "
    "ON workspace_revisions (resource_id, revision_number)",
    #     Audit：按 workspace / 按资源 / 按 operation 串联
    "CREATE INDEX IF NOT EXISTS idx_ws_audits_ws "
    "ON workspace_audits (workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ws_audits_resource "
    "ON workspace_audits (resource_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ws_audits_op "
    "ON workspace_audits (operation_id)",
    #     Event 订阅：按 workspace 拉事件流
    "CREATE INDEX IF NOT EXISTS idx_ws_events_ws "
    "ON workspace_events (workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ws_events_resource "
    "ON workspace_events (resource_id, created_at)",

    # ---- Change Pipeline V1 / Phase 1 ----
    #
    # 🔒🔒 **这条部分唯一索引是「同一 Workspace 最多一个 live worktree」
    #       的**唯一**保证（任务书 §17）。**
    #
    #     任务书明确禁止：
    #         if not exists: create        ← 非原子，两个线程会都通过
    #
    #     这里是**数据库层**的原子保证：
    #        · 两个并发 create → 第二个 INSERT 直接违反唯一约束
    #        · 不需要任何应用层锁、不需要全局锁
    #
    #     🔑 关键是 `WHERE state IN (...)` —— 只约束**活着**的那三个状态，
    #        所以「历史 / 归档 worktree 可以存在」与「只能有一个活的」
    #        同时成立（任务书 §3）。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_one_live_per_workspace "
    "ON git_worktrees (workspace_id) "
    "WHERE state IN ('provisioning', 'ready', 'active')",

    # 按 workspace 查全部 worktree（含历史）
    "CREATE INDEX IF NOT EXISTS idx_worktrees_workspace "
    "ON git_worktrees (workspace_id, created_at)",

    # 按状态找需要 reconciliation 的记录
    "CREATE INDEX IF NOT EXISTS idx_worktrees_state "
    "ON git_worktrees (state)",

    # 🔒 intent 的幂等锚点 —— **唯一**索引
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_worktree_intents_idempotency "
    "ON worktree_intents (idempotency_key)",

    # 找未解决的 intent（startup reconciliation 用）
    "CREATE INDEX IF NOT EXISTS idx_worktree_intents_unresolved "
    "ON worktree_intents (state, created_at)",

    "CREATE INDEX IF NOT EXISTS idx_worktree_intents_workspace "
    "ON worktree_intents (workspace_id, created_at)",

    "CREATE INDEX IF NOT EXISTS idx_worktree_events_ws "
    "ON worktree_events (workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_worktree_events_wt "
    "ON worktree_events (worktree_id, created_at)",

    # ---- Output Commit ----
    # 同一个 Task / iteration / snapshot / target 只允许一条请求。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_commit_requests_once "
    "ON commit_requests "
    "(task_id, iteration, snapshot_tree, output_repo, target_ref)",
    # 人类确认：按 Task 查待确认请求。
    "CREATE INDEX IF NOT EXISTS idx_commit_requests_task "
    "ON commit_requests (task_id, status, created_at)",
    # 全局待确认列表（Front context）。
    "CREATE INDEX IF NOT EXISTS idx_commit_requests_status "
    "ON commit_requests (status, created_at)",
)


# ============================================================
# 增量迁移
# ============================================================
# 问题：CREATE TABLE IF NOT EXISTS 对【已经存在】的表完全不起作用。
#       所以如果项目已经跑过、data/economy.db 已经建好了，
#       光改上面的 DDL 是加不上新列的 —— 你的库会一直缺 latency_ms。
#
# 解决：启动时检查列是否存在，缺了才 ALTER TABLE 补上。
#
# 格式：表名 -> [(列名, 列定义), ...]
COLUMN_MIGRATIONS: Final[dict] = {
    "commit_requests": [
        # Output Commit V1：区分输出仓库（tasks/<id> 子树）
        # 与项目仓库（未来直接修改项目）。
        ("repo_mode", "TEXT NOT NULL DEFAULT 'output'"),
    ],
    "llm_calls": [
        # 🔴 2026-10-08：调用归属与 Turn 关联（Telemetry Repair V2）。
        #
        #    【为什么必须加这三列】
        #    上一轮真实实验出现了 163 条调用 vs 85 条 turn，无法归因 ——
        #    因为记录里**没有任何键**能把一次调用和它的调用方对上。
        #    只靠 token 数 / 时间戳反推是不允许的（§2）。
        #
        #    老数据一律 NULL —— 「当时没记」就是不知道，**不回填猜测**。
        ("call_purpose", "TEXT"),
        ("cognition_turn_id", "TEXT"),
        ("experiment_run_id", "TEXT"),
        ("latency_ms", "REAL NOT NULL DEFAULT 0"),
        ("simulated", "INTEGER NOT NULL DEFAULT 0"),
        ("token_source", "TEXT NOT NULL DEFAULT 'unknown'"),
        # 🔴 2026-10-07 可观测性补全。
        #    老数据一律 NULL —— 「当时没记」就是不知道，**不回填猜测**。
        ("requested_model", "TEXT"),
        ("response_model", "TEXT"),
        ("requested_cognitive_budget", "INTEGER"),
        ("actual_max_tokens", "INTEGER"),
        ("finish_reason", "TEXT"),
        ("output_status", "TEXT"),
        ("requested_credential_ref", "TEXT"),
        ("requested_credential_source", "TEXT"),
        ("requested_credential_env", "TEXT"),
    ],
    # Step 4B 给 tasks 加了「需要什么能力」。
    # 老库里的任务是 NULL（表示「没有声明能力要求」），这是合理的默认值，
    # 所以只需要加列，不需要回填数据。
    "tasks": [
        ("required_capabilities", "TEXT"),
        # Step 4D.1：乐观锁版本号。
        #
        # 【为什么需要它】
        #     save() 原本是无条件 UPSERT —— 谁后写谁覆盖，
        #     而且两边都以为自己成功了。
        #     有了 version，save() 就变成 compare-and-set：
        #         UPDATE ... WHERE task_id=? AND version=?
        #     影响行数为 0 就说明「这个 Task 已经被别人改过了」。
        #
        # 【为什么默认 0 是安全的】
        #     老库里的任务从没被版本化过，
        #     给它们 version=0 表示「还没人改过」。
        #     第一次 save() 会把它推到 1 —— 行为正确。
        ("version", "INTEGER NOT NULL DEFAULT 0"),
        # ADR-0056：Solo 自完成 + Reopen / Iteration。
        #
        # 老库里的任务默认 TEAM / iteration=1 ——
        # 它们没有 Solo 语义，也不能凭空多出历史轮次。
        ("mode", "TEXT NOT NULL DEFAULT 'TEAM'"),
        ("iteration", "INTEGER NOT NULL DEFAULT 1"),
    ],
    # Step 4C 给报价历史加了四个关联字段。
    # 老数据是 NULL（表示「不知道关联哪条消息」），这是合理的默认值。
    "task_offers": [
        ("reply_to", "TEXT"),
        ("from_agent", "TEXT"),
        ("trace_id", "TEXT"),
        ("conversation_id", "TEXT"),
    ],
    # --------------------------------------------------------
    # Step 4D.1：把「这条消息处理过了吗」变成持久化事实。
    #
    # 【原来的问题】
    #     SeenMessageTracker 是进程内的 OrderedDict。
    #     进程重启 → 忘光了 → 同一条消息会被再处理一次。
    #
    # 【为什么不能直接用已有的 status 列】
    #     status 是「最新状态」，会被后来的事件覆盖：
    #         处理成功  → status = PROCESSED   ✅
    #         又来一条重复 → status = DUPLICATE   ← PROCESSED 被抹掉了
    #     用它做去重依据，重复投递反而会把「处理过」这个事实擦掉。
    #
    #     所以需要一个**只增不减**的字段。
    #
    # 【这四个字段各自的作用】
    #     processed_once  成功的持久化事实。0 → 1 之后**永不回退**。
    #                     这是去重真正的依据。
    #     claim_id        谁认领了这条消息（NULL = 没人）。
    #                     用条件 UPDATE 原子认领，避免两个 worker 同时处理。
    #     claimed_at      认领时间（排查用）。
    #     failure_count   处理失败过几次。**不表示消息永久作废** ——
    #                     失败可能是临时的，不能把失败误标成完成。
    # --------------------------------------------------------
    "communication_messages": [
        ("processed_once", "INTEGER NOT NULL DEFAULT 0"),
        ("claim_id", "TEXT"),
        ("claimed_at", "TEXT"),
        ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


# ============================================================
# 数据回填
# ============================================================
# 加列的时候，老数据会拿到 DEFAULT 值 —— 但那不一定是对的。
#
# 具体到这里：
#     Step 2 时期的记录全是 MockProvider 产生的，simulated=1，
#     它们的 token_source 应该是 'estimated'。
#     但 ADD COLUMN 只能填一个统一的默认值 'unknown'，
#     于是老记录被错误地标成了「来源不明」。
#
# 所以加完列要补一次数据。格式：(说明, SQL)
#
# 为什么可以每次启动都跑？
#     因为这些语句都是幂等的 —— 第一次跑完就没有行再匹配了。
#     真正的迁移框架（记录版本号、只跑一次）等库变复杂了再说，
#     现在多跑几次 UPDATE 的成本可以忽略。
DATA_MIGRATIONS: Final[tuple] = (
    (
        "把 Step 2 时期 simulated=1 的老记录标回 estimated",
        f"""
        UPDATE llm_calls
           SET token_source = 'estimated'
         WHERE simulated = 1
           AND token_source = 'unknown'
        """,
    ),
    # --------------------------------------------------------
    # Step 3.7：货币代码 AI → AIC
    #
    # 【为什么这条迁移是必须的，不是可选的】
    #
    #     账本余额是【按 asset 过滤后求和】算出来的：
    #         SELECT SUM(...) FROM ledger_transactions WHERE asset = ?
    #     钱包也一样 —— 它拿自己那一行的 currency 去查账本。
    #
    #     所以只要代码默认值改成 'AIC' 而老数据还是 'AI'，
    #     就会出现「新 Agent 用 AIC、老 Agent 用 AI」的混用：
    #
    #         ❌ 老 Agent 转钱给新 Agent 直接失败
    #            （实测：WalletError: 币种不一致）
    #         ❌ 账本里同一个系统出现两种资产标签
    #
    #     这个失败是实测出来的，不是推测。所以必须把老数据一起改名。
    #
    # 【为什么可以 UPDATE 账本 —— 这不违反 append-only】
    #
    #     账本不允许 UPDATE 的是【金额】—— 那才是钱本身。
    #     这里改的是一个【货币符号的写法】：
    #         'AI'  →  'AIC'
    #     金额、方向、时间、原因一个字段都没动。
    #
    #     这更接近「给同一个单位换个名字」，不是改账。
    #     真正的改账（金额错了）必须走冲正分录（红字），
    #     绝不能 UPDATE —— 这条线不能松。
    #
    # 【幂等性】
    #     WHERE 条件是「等于旧值 'AI'」，改完就没有行再匹配，
    #     所以每次启动跑一遍是安全的。
    # --------------------------------------------------------
    (
        "把钱包里的货币代码从 AI 改成 AIC",
        """
        UPDATE wallets
           SET currency = 'AIC'
         WHERE currency = 'AI'
        """,
    ),
    (
        "把账本里的资产代码从 AI 改成 AIC",
        """
        UPDATE ledger_transactions
           SET asset = 'AIC'
         WHERE asset = 'AI'
        """,
    ),
)

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
TABLE_TASKS: Final = "tasks"
TABLE_TASK_OFFERS: Final = "task_offers"


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
        status          TEXT NOT NULL DEFAULT 'CREATED',
        retry_count     INTEGER NOT NULL DEFAULT 0,

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

        expires_at          TEXT,
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

        note        TEXT,
        created_at  TEXT NOT NULL,

        FOREIGN KEY (task_id) REFERENCES tasks (task_id)
    )
    """,

    "CREATE INDEX IF NOT EXISTS idx_task_offers_task ON task_offers (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_offers_type ON task_offers (offer_type)",
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
    "llm_calls": [
        ("latency_ms", "REAL NOT NULL DEFAULT 0"),
        ("simulated", "INTEGER NOT NULL DEFAULT 0"),
        ("token_source", "TEXT NOT NULL DEFAULT 'unknown'"),
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

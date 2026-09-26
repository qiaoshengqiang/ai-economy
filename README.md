# AI Economy OS

> 一个「AI Agent 经济系统」的原型。
>
> 每个 Agent 都是一个独立的经济主体：有自己的身份、工作空间、记忆、钱包和经济状态。
> Agent 调用不同公司的大模型 API 来获得智力能力，但 **Agent 不等于 LLM**。
> Agent 可以自己决定做任务、拒绝任务、外包任务、向其他 Agent 询价、报价、支付、收款。
> Agent 的最终能力不仅由模型能力决定，也由它能否在经济系统中长期生存决定。

**当前版本：v0.1（开发中）** —— 目标：跑通一次真实的 Agent 经济交易。

---

## 目录

- [核心设计原则](#核心设计原则)
- [当前进度](#当前进度)
- [快速开始](#快速开始)
- [运行测试](#运行测试)
- [项目结构](#项目结构)
- [架构](#架构)
- [经济模型](#经济模型)
- [未来扩展方向](#未来扩展方向)

---

## 核心设计原则

### 1. Agent 不等于 LLM

Agent 是**经济主体**，LLM 只是它可以调用的**智力资源**。

```
Agent
 ├── Identity        我是谁
 ├── Memory          我记得什么
 ├── Wallet          我有多少钱
 ├── Economy         我所在的经济系统
 ├── Communication   我怎么和别人说话
 ├── Task Manager    我在做什么
 └── Brain           我的智力
       └── LLM Router
             ├── DeepSeek
             ├── Doubao
             ├── Grok
             └── future providers
```

**代码里的体现**：`agents/` 目录下的代码中不会出现任何厂商名字。
Agent 只会说 `brain.generate(...)`，至于背后是哪个公司的模型，Agent 不关心。

换来的是：明天 DeepSeek 涨价、或者某个厂商挂了，只改配置，Agent 代码一行不动。

**这条原则由测试强制保证**（`tests/test_llm_provider.py::TestArchitectureGuards`）：

| 守卫 | 检查什么 |
|---|---|
| `test_agent_layer_has_no_vendor_names_in_code` | 用 AST 解析 `agents/`，代码里（不含注释）不得出现厂商名 |
| `test_provider_layer_imports_no_storage` | Provider 不许碰数据库 |
| `test_router_has_no_if_elif_provider_chain` | Router 里不许有 `if provider == "xxx"` 链 |
| `test_llm_layer_does_not_import_agents_or_economy` | 依赖方向单向：agents → llm，不能反过来 |

**三层职责的边界**（Step 2 建立）：

```
Agent    经济主体。会破产、会赚钱、有声誉。
   ↓
Brain    Agent 的器官。补默认参数 + 记账。可以碰数据库。
   ↓
Router   派发员。谁调的都无所谓，所以它能通用。不知道 agent_id。
   ↓
Provider 外部资源。只做一件事：messages → LLMResponse。绝不碰数据库。
```

为什么记账放在 Brain 而不是 Router 或 Provider？
因为记账需要 `agent_id` + 调用结果两个信息，**只有 Brain 同时知道**。
这样切分之后，Router 和 Provider 都能被单独测试，不用 mock 数据库。

### 2. 余额是算出来的，不是存下来的

这是整个系统最重要的一个技术决定。

```python
# ❌ 错误做法：谁都能改，说不清钱从哪来
wallet.balance = wallet.balance - 20

# ✅ 本系统做法：往账本追加一笔，余额永远是求和的结果
ledger.record(TX_TRANSFER, amount=20, from_agent="agent_a", to_agent="agent_b")
balance = ledger.balance_of("agent_a")   # 实时算出来
```

`wallets` 表里**没有 balance 字段**。这带来：

| 性质 | 意义 |
|---|---|
| 所有经济变化都有据可查 | 可审计 |
| 余额不可能凭空出现 | 只有 `GENESIS_MINT` 能创造货币 |
| 余额可随时重新计算验证 | 可对账 |
| 以后可换成区块链后端 | Agent 代码不用改 |

### 3. 账本是 append-only 的

只允许追加交易，不允许修改或删除历史记录。
发现记错了怎么办？**再记一笔反向交易（冲正）**，而不是抹掉历史。

### 4. 工作空间隔离是代码强制的，不是口头约定

Agent 永远拿不到真实文件系统路径，只能通过 `Workspace` 对象访问文件：

```
Agent  →  Workspace（边界检查）  →  agents/<自己的 id>/
```

任何试图跑到自己目录外的路径（`../../agent_b/memory` 或绝对路径）都会抛
`WorkspaceViolationError`。

为什么单机阶段就要做？因为以后 Agent B 会搬到另一台机器。
现在建立边界，以后只需替换 `Workspace` 的实现，Agent 代码不动。

### 5. 经济逻辑绝不交给 LLM

LLM 可以分析、估算、决策、谈判、写代码。
但 LLM **不能**直接修改余额、修改账本、支付、改别人的钱包。

```
LLM 说："我决定支付 Agent B 20 AI"
    ↓
Economy 检查：余额够吗？有合同吗？托管里有吗？
    ↓
检查通过才真正写 Ledger
    ↓
不通过就拒绝
```

LLM 只能**提出意图**，永远不能**直接改账**。

---

## 当前进度

### ✅ Step 1：最小运行骨架（已完成）

- [x] 项目结构、配置系统、虚拟环境
- [x] `AgentIdentity` —— 独立身份
- [x] `Workspace` —— 独立工作空间 + 隔离边界
- [x] `Wallet` —— 钱包（余额从账本推导）
- [x] `Ledger` —— 交易账本（唯一真相来源）
- [x] `Economy` —— 经济系统核心 + 创世铸币 + 对账审计
- [x] `BaseAgent` —— 经济主体骨架
- [x] SQLite 数据库（4 张表）
- [x] 启动横幅
- [x] **命令行经济操作**（转账 / 余额 / 流水 / 对账）
- [x] 239 个 pytest 测试全绿

**验收标准**（已达成）：

```
$ python3 main.py

================================
        AI Economy OS v0.1
================================

Agent A
Wallet: 100 AI

Agent B
Wallet: 100 AI

System status: READY
```

### ✅ Step 1.5：经济操作入口（已完成）

你需要的「控制一个 Agent 向另一个 Agent 转钱」的入口。

**关键点：它走的是 `Economy.transfer()`，而不是直接调钱包。**
这是 OS 级的执行入口，未来 LLM 提出支付意图时也会落到这个方法上。

参见下面的 [命令行经济操作](#命令行经济操作)。

### ✅ Step 1.6：配置解耦与金额显示修复（已完成）

在做命令速查的过程中发现并修掉了三个问题。

**1. 核心经济参数被硬编码在代码里**

`core/config.py` 和 `core/economy.py` 里原本都有
`config.get("economy.tokens_per_ai_coin", 10000)` 这样的兜底默认值。
后果是：配置里写 5000、代码里写 10000 时，说不清系统实际用的是哪个数。

现在改成**不给默认值，缺了就明确报错**：

```
!! 启动失败 !!
错误类型: ConfigError
错误信息: 配置缺少必需参数：economy.tokens_per_ai_coin
```

**2. 测试硬编码了配置值**

测试里原本写死了 `balance == 100`、`tokens_per_ai_coin == 10000`。
后果是：你只要把 `initial_balance` 改成 250 做个实验，
17 个测试立刻变红 —— 但代码其实完全没坏。

现在测试通过显式参数和配置派生来定期望值，
以下配置组合全部 `120 passed`：

| 余额 | 兑换比例 | 结果 |
|---|---|---|
| 100 | 10000 | ✅ |
| 250 | 500 | ✅ |
| 0.5 | 1000000 | ✅ |
| 1000000 | 12345 | ✅ |
| 3 | 1 | ✅ |

**3. 金额显示会变成科学计数法**

`f"{balance:.4g}"` 在大额时输出 `1e+05 AI`。
对「钱」来说这是不可接受的显示方式。

现在统一用 `format_amount()`：

```
修复前:  Agent A     1e+05 AI  ACTIVE
修复后:  Agent A    100000 AI  ACTIVE
```

**另外新增了两条架构保护测试：**

| 测试 | 保护什么 |
|---|---|
| `test_exchange_rate_is_not_hardcoded_in_source` | 用 AST 解析源码，禁止把兑换比例写成代码里的字面量或默认值 |
| `TestRequiredConfig` 系列 | 配置缺失 / 为 0 / 负数 / 非数字时必须明确报错，不许悄悄兜底 |

> 这一轮很能说明测试的价值：这三个问题里有两次是**我自己**在
> 做配置实验时踩出来的，而每次都是测试第一时间发现并报出来的。

### ✅ Step 2：LLM Provider 抽象层（已完成）

**唯一目标：Agent 与具体 LLM 厂商彻底解耦。**

跑通了这条链路：

```
Agent → Brain → Router → MockProvider → LLMResponse → Agent
```

新增的四个文件都在 `llm/` 下，加上 `agents/brain.py`。

**验收要点：**

| 要求 | 状态 |
|---|---|
| `LLMProvider` 统一接口 | ✅ 抽象基类，签名强制一致 |
| `LLMResponse` 统一结构 | ✅ content / provider / model / tokens / latency / raw_response |
| `MockProvider` 不需要 API Key | ✅ 无网络无 Key 也能跑通全链路 |
| Mock 返回 token 用量 | ✅ 且标记 `simulated=True` |
| Router + 注册表（非 if/elif） | ✅ 加厂商只改一行注册 |
| Brain 接入 Agent | ✅ `agent.generate()` → Brain → Router |
| 调用记录进 SQLite | ✅ 新增 `latency_ms` / `simulated` 列 |
| **不加一行 Agent 代码就能换厂商** | ✅ 见下面的验证 |

**解耦验证（实测）：**

写一个假想的 DeepSeekProvider，注册后让 agent_b 使用，
结果 Agent 层代码零改动：

```
已实现的 provider : ['deepseek', 'mock']
agent_b 的 provider: deepseek
解析出的 model     : deepseek-chat
返回内容           : [假装这是 DeepSeek 的回答]
llm_calls 记录     : provider=deepseek model=deepseek-chat simulated=0

agents/base_agent.py: ✅ 代码里没有 deepseek
agents/brain.py     : ✅ 代码里没有 deepseek
```

**本阶段明确不做的事**（都留给后面）：
- ❌ 不接真实 DeepSeek / Doubao / Grok API（只登记为「未实现」）
- ❌ 不做自动模型选择、成本优化路由、自动 fallback
- ❌ 不算 AI 币成本（只记 token，`cost_ai` 故意留 0 表示「还没算」）

### ⏳ 后续阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Step 3 | Agent-to-Agent 通信（Message + Transport） | 待开始 |
| Step 4 | Task / Bid / Contract / Escrow | 待开始 |
| Step 5 | 完整经济交易 Demo（A 外包给 B，结算） | 待开始 |
| Step 6 | Cost Calculator（把 token 用量接到经济系统） | 待开始 |

---

## 快速开始

### 环境要求

- macOS（Apple Silicon 或 Intel）
- Python 3.11+（推荐 3.13）
- 不需要 Docker、不需要数据库服务器

### 1. 创建虚拟环境

```bash
cd ai-economy

# 如果你用 Homebrew 装的 Python（推荐）
/opt/homebrew/bin/python3.13 -m venv .venv

# 或者用系统默认的
python3 -m venv .venv
```

> **注意**：macOS 自带的 `/usr/bin/python3` 是 3.9，版本偏旧。
> 建议用 Homebrew 装的 3.13。用 `python3 --version` 确认一下。

### 2. 激活并安装依赖

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. 配置 API Key（可选）

```bash
cp .env.example .env
```

**v0.1 阶段可以完全跳过这一步。**
系统默认使用 `MockProvider`，没有任何 API Key 也能跑通整个经济系统。

### 4. 启动

```bash
python main.py
```

---

## 命令行经济操作

这是你实际操作这个经济系统的入口。

> **推荐用 `./dev.sh`** —— 它自动使用 `.venv` 里的 Python，
> 不会再出现「忘记激活虚拟环境」的报错。

### 转钱（核心命令）

让一个 Agent 向另一个 Agent 支付：

```bash
./dev.sh transfer agent_a agent_b 20
```

```
转账成功
----------------------------------------------
  付款方:  Agent A (agent_a)
  收款方:  Agent B (agent_b)
  金额:    20 AI
  原因:    manual_transfer
  交易号:  tx_044d342e1c98

余额变化
----------------------------------------------
  Agent A:  100 -> 80 AI
  Agent B:  100 -> 120 AI

账本审计: OK   (货币总量 200 AI, 共 3 笔交易)
```

带原因和任务号：

```bash
./dev.sh transfer agent_a agent_b 20 --reason task_payment --task-id task_001
```

**Agent 可以用三种写法指定**（都不区分大小写）：

| 写法 | 例子 |
|---|---|
| agent_id | `agent_a` |
| 显示名 | `"Agent A"` |
| 简写 | `a` |

### 查看余额

```bash
./dev.sh balances
```

```
Agent 余额
--------------------------------------------------------
  Agent          ID                     余额  状态
--------------------------------------------------------
  Agent A        agent_a             100 AI  ACTIVE
  Agent B        agent_b             100 AI  ACTIVE
--------------------------------------------------------
  合计                                  200 AI
```

### 查看交易流水

```bash
./dev.sh ledger                # 全部
./dev.sh ledger --agent a      # 只看 A 的
```

```
交易流水 —— 全部
------------------------------------------------------------------------------
  时间                   类型             从 → 到                              金额
------------------------------------------------------------------------------
  2026-09-26 05:36:42  GENESIS_MINT   ∅铸币 → agent_a                   100 AI
  2026-09-26 05:36:42  GENESIS_MINT   ∅铸币 → agent_b                   100 AI
  2026-09-26 05:36:42  TRANSFER       agent_a → agent_b                20 AI
------------------------------------------------------------------------------
  共 3 笔
```

### 对账

```bash
./dev.sh audit
```

检查「账本里的货币总量」和「所有钱包余额之和」是否一致。
**任何绕过账本直接改余额的代码，都会让这里 FAILED。**

### 被拒绝的操作

经济规则会拒绝非法操作，并给出清晰提示（不是 Python 堆栈）：

| 情况 | 提示 |
|---|---|
| 余额不足 | `agent_a 余额不足：需要 9999.0 AI，可用 74.5 AI` |
| 自己转自己 | `不能给自己转账：Agent A 的付款方和收款方是同一个 Agent` |
| 负数金额 | `转账金额必须大于 0，收到 -50.0` |
| Agent 不存在 | `找不到 Agent：'agent_z'` + 列出可用的 |
| 付款方已破产 | `Agent A 当前无法承担经济责任，转账被拒绝` |

**退出码约定**：

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 程序错误（启动失败、代码 bug） |
| `2` | 被经济规则拒绝（预期内的失败） |

### 重置

想重新看一次「首次启动」（重新发钱）：

```bash
./dev.sh clean
```

---

## 运行测试

```bash
python -m pytest
```

更详细的输出：

```bash
python -m pytest -v
```

只跑某一部分：

```bash
python -m pytest tests/test_ledger.py -v
```

---

## 项目结构

```
ai-economy/
│
├── README.md                  ← 你在这里
├── requirements.txt
├── .env.example               ← 复制成 .env 后填 API Key
├── .gitignore
│
├── main.py                    ← 启动入口
│
├── config/
│   └── config.yaml            ← 全局配置（经济参数、LLM 注册表）
│
├── core/                      ← 经济系统内核
│   ├── paths.py               ← 所有路径计算集中在这里
│   ├── config.py              ← 配置加载（全局 ⊕ Agent 私有）
│   ├── identity.py            ← AgentIdentity
│   ├── workspace.py           ← 工作空间 + 隔离边界
│   ├── ledger.py              ← 交易账本（唯一真相来源）
│   ├── wallet.py              ← 钱包 + 钱包仓库
│   ├── economy.py             ← 经济系统核心 + transfer() 执行入口
│   └── logging_setup.py       ← 日志
│
├── agents/
│   ├── base_agent.py          ← 经济主体骨架
│   ├── brain.py               ← Agent 的智力门面（调 Router）
│   ├── agent_a/
│   │   ├── config.yaml        ← A 的私有配置
│   │   ├── workspace/         ← A 的工作产物
│   │   └── memory/            ← A 的私有记忆
│   └── agent_b/               ← 同理
│
├── llm/                       ← LLM Provider 系统（Step 2 已完成）
│   ├── base.py                ←   LLMProvider 契约 + LLMResponse
│   ├── mock.py                ←   MockProvider（不需要 Key）
│   ├── router.py              ←   ProviderRegistry + LLMRouter
│   └── recorder.py            ←   调用记录（token 用量）
│
├── communication/             ← (Step 3) Agent-to-Agent 通信
├── storage/
│   ├── database.py            ← SQLite 连接、事务、迁移
│   └── models.py              ← 表结构定义
│
├── tests/                     ← 239 个测试
└── data/
    └── economy.db             ← SQLite 数据库（运行产物，不进 git）
```

---

## 架构

```
                     AI Economy OS
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
     Identity           Economy          Communication
        │                  │                  │
      Agent            Wallet/Ledger        Message
        │                  │                  │
      Memory            Contract           Transport
        │                  │                  │
      Brain             Escrow              Local
        │                  │                  │
    LLM Router         Settlement          Network
        │
 ┌──────┼────────┐
 │      │        │
DeepSeek Doubao Grok
```

| 概念 | 在本系统中的角色 |
|---|---|
| **LLM** | 生产力 |
| **Agent** | 经济主体 |
| **Wallet** | 经济能力 |
| **Communication** | 交易基础 |
| **Ledger** | 经济记忆 |
| **Task/Contract** | 经济关系 |
| **Settlement** | 价值转移 |

---

## 经济模型

### 货币

系统内部经济单位叫 **AI 币**（符号 `AI`）。v0.1 用本地 Ledger 模拟，**不接区块链**。

### Token ↔ AI 币 换算

```yaml
# config/config.yaml
economy:
  tokens_per_ai_coin: 10000     # 1 AI币 ≈ 10,000 tokens
```

**这个数字是经济模型参数，不是常量。** 它只出现在配置文件里，代码中任何地方都不硬编码。

### 交易类型

| 类型 | 含义 |
|---|---|
| `GENESIS_MINT` | 创世铸币。**全系统唯一能凭空创造货币的入口** |
| `TRANSFER` | Agent 之间的普通转账 |
| `ESCROW_LOCK` | 把钱锁进托管账户 |
| `ESCROW_RELEASE` | 托管释放给收款方 |
| `ESCROW_REFUND` | 托管退回付款方 |
| `LLM_COST` | 调用模型的成本（真实经营成本） |
| `FEE` | 系统手续费（v0.1 不收，留位置） |

### LLM 成本进经济系统

这是本项目与其他 Multi-Agent 项目最大的区别之一。

在普通 Agent 项目里，调用 LLM 是免费的；在本系统里，它是**真实的经营成本**：

```
Agent B 收入          20.0 AI
- LLM 成本 (28k tok)  -2.8 AI
- 其他成本             0.0 AI
─────────────────────────────
= 真实利润            17.2 AI
```

**一个 Agent 如果一直亏本调用模型，最终会破产。**
这才是「Agent 靠经济系统生存」的真正含义。

### 破产

```
balance <= 0  →  INSOLVENT
```

破产不等于程序崩溃。Agent 依然存在、依然有记忆，
只是不能再承担它付不起的经济责任了。

v0.1 不做贷款、抵押、信用、保险、破产重组 —— 但概念保留。

---

## 未来扩展方向

> 以下均为**设计方向记录**，v0.1 **不实现**。
> 现在记录是为了确保当前架构留好了接口。

### Agent 自我生存

```
收入 - LLM成本 - 工具成本 - 外包成本 - 存储成本 - 通信成本 = 净利润
```

### Agent 破产演化

持续亏损 → 余额下降 → 无法支付成本 → 无法接任务 → `insolvent`

### Agent 竞争

比较**质量、成功率、成本、时间、信誉**，而不是简单「最低价获胜」。

### Agent 专业化

```
Agent A  软件开发
Agent B  测试
Agent C  研究
Agent D  数据处理
```

### Agent 市场

```
Task Market       任务市场
Service Market    服务市场
Agent Market      Agent 本身可以买卖
Capital Market    资本市场
```

### 分布式演进路径

```
单机双 Agent（现在）
    ↓
多 Agent（本地）
    ↓
多机器（LocalTransport → NetworkTransport）
    ↓
跨网络
    ↓
开放式分布式网络
```

**当前为此预留的接口：**

| 现在 | 未来替换成 | 谁不用改 |
|---|---|---|
| `LocalTransport` | `NetworkTransport` / `P2PTransport` | Agent 代码 |
| `LocalLedger` | `BlockchainLedger` | Agent / Wallet 代码 |
| `Workspace` | `RemoteWorkspace` | Agent 代码 |

---

## v0.1 明确不做的事

为了聚焦，以下全部推迟：

- ❌ 真正的区块链 / NFT / 加密货币
- ❌ P2P 网络 / 自动发现 Agent
- ❌ Docker / Kubernetes / 多机器部署
- ❌ 复杂 Agent framework
- ❌ 向量数据库 / 大型 Memory 系统
- ❌ 多 Agent 动态生成 / 自我复制
- ❌ 自动赚钱机器人 / 复杂金融市场

**现在唯一的目标：完成一次真实的 Agent 经济交易。**

---

## 开发约定

1. **能用标准库就不引第三方库。** 运行 AI Economy OS 本身零第三方依赖。
2. **所有会变的东西都放配置。** 看见硬编码的数字就问一句「这是不是该进 yaml」。
3. **每加一个功能，先加测试。** 经济系统的 bug 很隐蔽，光看余额查不出来。
4. **不要跳过失败的测试。** 先分析原因，改代码，再往下走。

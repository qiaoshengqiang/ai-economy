# AI Economy OS

> 一个可以从单机多 Agent 逐步演化为多机器、跨网络的 **Agent Operating Environment**。
>
> **Agent 是独立的经济主体，不是被调用的函数。**

**当前版本：`v0.2.0`（开发中）**
**当前最佳工作方式：Human → Front Agent → Pi Agent → 自主工作**
**测试基线：`5702 passed, 2 skipped, 30 deselected`**

> - 当前真实能力、推荐模式与已知限制：[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)
> - 架构长期 Source of Truth：[`ARCHITECTURE_CONSTITUTION.md`](docs/architecture/ARCHITECTURE_CONSTITUTION.md)
> - Step 1 → Workspace Phase 0 历史记录：[`docs/HISTORY.md`](docs/HISTORY.md)

---

## 最高原则

> **项目的核心不是「功能越来越多」，而是「边界越来越清楚」。**
> **优先保持边界清晰，再增加功能。**

| # | 原则 | 含义 |
|---|---|---|
| 1 | **边界先于功能** | 新能力先定义「谁拥有什么、什么不能越过」，再写代码。边界不清的功能不加。 |
| 2 | **Agent 是独立经济主体** | 有自己的身份、钱包、记忆、判断和生存压力，不是被系统调用的函数。 |
| 3 | **系统提供机制，不替 Agent 决策** | 系统负责协议、状态、硬约束、持久化、消息路由；定价、谈判、选择由 Agent 自己负责。 |
| 4 | **领域状态是唯一权威** | Domain State > Shared Context > Communication History > Private Memory > Inference；不造第二套可修改状态。 |
| 5 | **经济逻辑不交给 LLM** | LLM 只能提出意图，不能直接改余额或账本；所有经济变化可审计、可重算。 |
| 6 | **文档不把未来写成现在** | 每条能力必须标 `CURRENT` / `DESIGNED` / `FUTURE`；`DESIGNED` 不得写成已有。 |

**不可破坏的边界**（完整清单见 [Constitution §3](docs/architecture/ARCHITECTURE_CONSTITUTION.md) 与 [AGENTS §6](AGENTS.md)）：

| 领域 | 不等号 |
|---|---|
| 身份 / 通信 | `Identity ≠ Address` · `Message ≠ Transport` · `Communication ≠ Business Logic` |
| 能力 / 权限 / 经济 | `Capability ≠ Permission` · `Capability ≠ Economy` · `Wallet Ownership ≠ Machine Permission` · `Workspace ≠ Permission` |
| Task / 共享知识 | `Task Core ≠ Task Manager` · `Shared Context ≠ Message History` · `Memory ≠ Alignment` · `Agreement ≠ Payment` · `Budget ≠ Price` |
| Agent / 策略 | `DecisionPolicy ≠ Decision Execution` · `Agent Strategy ≠ Negotiation Mechanism` · `Private Memory ≠ Private Workspace` |
| 空间 / 来源 | `Logical Workspace ≠ Physical Workspace` · `Human Demand ≠ Task` · `Human ≠ Task Creator` |

---

## 它是什么

**AI Economy OS** 是一个 Agent Operating Environment：

- Agent 有独立身份、能力、记忆和钱包；
- Agent 可以自己接任务、拒绝任务、外包、询价、报价、支付、收款；
- Agent 调用外部 LLM 获得智力，但 **Agent ≠ LLM**；
- 系统只提供协议、状态、约束和基础设施，不替 Agent 做策略。

**它不是**：Chatbot · Multi-Agent Demo · LLM 工作流 · 并排跑 Agent 的脚本。

---

## 当前能力（v0.2.0）

| 领域 | 当前能力 | 状态 |
|---|---|---|
| Agent Core | Actions · Cognition · Experience · Reflection · Self Model · Stage · Budget · Delegation | `CURRENT` |
| Task Core | 状态机 · 议价 · `TaskAgreement` · 执行 / 提交 / 验收 / 完成 · 幂等 · 乐观锁 | `CURRENT` |
| Shared Context | 持久化 · 修订历史 · Alignment · 乐观锁 | `CURRENT` |
| Workspace Core v1 | 资源树 · Controller · Explicit/Effective Lock · Revision · Audit · Event · Gateway | `CURRENT`（已冻结） |
| Tool Runtime | python / pytest / node / playwright · 公共只读工具库 · 独立 run 目录 · 超时与输出上限 | `CURRENT` |
| Vision | 原生读图 · 截图回喂 · Playwright smoke gate | `CURRENT` |
| Work | Front `autonomous` / `relay` · Pi `workflow` / `autonomous` · inbox/outbox · 恢复 / Reopen · 批量 handoff | `CURRENT` |
| 交付 | Output Commit（manual / limited / auto）· Project Workdir（直接改已有项目） | `CURRENT` |
| UI | AIEOS 浏览器界面 · Agent 状态 · Task 卡片（状态色 / 筛选 / 归档 / 删除）· 可复用确认弹窗 | `CURRENT` |
| 启动器 | `./aieos`：启动 / 停止 / 重启 / 状态 / 参数 / 日志 / doctor | `CURRENT`（Phase 1） |
| Agent 管理 | 中文名（canonical `agents.name`）· 新增 Agent（写配置 + 重启）· 归档 / 恢复 · 启停与引擎 | `CURRENT` |
| 经济 | Wallet · Ledger · 余额推导 · LLM 成本入账 · 对账 | `CURRENT` |
| 平台 | macOS `CURRENT` · Linux `UNVERIFIED` · Windows 适配层骨架 `DESIGNED` | — |

> 这不是完整清单。完整状态表、推荐配置和已知限制见
> [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)。

---

## 快速开始

### 环境要求

- macOS（Apple Silicon / Intel）—— **当前主要验证平台**
- Linux —— 预期可用，尚未建立完整 CI
- Windows 原生 / WSL —— **未支持**；适配步骤见
  [`PLATFORM_COMPATIBILITY.md`](docs/architecture/PLATFORM_COMPATIBILITY.md) §0
- Python 3.11+（推荐 3.13）
- 不需要 Docker、不需要数据库服务器

### 1. 创建虚拟环境

```bash
cd ai-economy

# 如果你用 Homebrew 装的 Python（推荐）
python3.13 -m venv .venv

# 或者用系统默认的
python3 -m venv .venv
```

> macOS 自带的 `/usr/bin/python3` 版本偏旧。用 `python3 --version` 确认。

### 2. 安装依赖

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. 配置 API Key

```bash
cp .env.example .env
```

- **只跑经济系统 / 测试**：可以显式使用 `mock` provider，不需要 Key。
- **跑 UI + Pi 主链路**：需要配置真实 provider 的 Key。

配置权威在 `config/config.yaml` + `config/llm.yaml`；
secret 只从 `.env` / 环境变量读取。

### 4. 启动 AIEOS（推荐）

```bash
./aieos start
```

打开：

```text
http://127.0.0.1:8000/
```

常用管理命令：

```bash
./aieos status
./aieos restart
./aieos stop
./aieos logs -f
```

默认 Front 是 `小艾`（agent_a），Pi Agent 是 `小博`…`小伊`（agent_b…agent_e）。
`aieos` 会检查 `.venv`、Pi CLI 和端口；失败时打印最近日志。
详见 [`START_AIEOS.md`](START_AIEOS.md) 和
[`docs/architecture/AIEOS_CLI.md`](docs/architecture/AIEOS_CLI.md)。

### 5. 命令行入口

```bash
./dev.sh run          # 启动 CLI（不启动 UI）
./dev.sh doctor       # 检查环境
./dev.sh ui           # 前台启动 UI
```

> ❌ 不要直接敲 `python main.py` 或 `pytest`。
> ✅ 统一用 `./dev.sh`，它会自动使用 `.venv` 里的 Python。

---

## 常用命令

| 目的 | 命令 |
|---|---|
| 启动 / 停止 UI | `./aieos start` / `stop` / `restart` / `status` |
| 修改启动参数 | `./aieos config show` · `./aieos config set port 8001` |
| 查看日志 | `./aieos logs -f` |
| 前台启动 UI | `./dev.sh ui` |
| 启动 CLI | `./dev.sh run` |
| 跑全量测试 | `./dev.sh test` |
| 环境检查 | `./dev.sh doctor` |
| Human → Front → Work | `./dev.sh work --demand "做一个 hello.txt"` |
| Human Demand → Agent 自建 Task | `./dev.sh demand --file prompt.txt --minutes 20 --budget 20` |
| 经济操作 | `./dev.sh transfer` · `balances` · `ledger` · `audit` |
| 重置数据库 | `./dev.sh clean` |

全部 25 个命令见 [`COMMANDS.md`](COMMANDS.md)。

---

## 架构一屏

```text
Human
  │
  ▼
AIEOS UI（ui/）
  │
  ▼
Front Agent（work/）
  │  handoff / task_text
  ▼
Pi Agent（work/pi_runtime.py）
  │  read / write / edit / bash / grep / find / ls
  ▼
Tool Runtime（tool_runtime/）── 受控执行 ──▶ 公共只读工具库
  │
  ▼
Task Core + Shared Context（core/ + services/）
  │
  ▼
Workspace Core · Output Commit · Project Workdir
  │
  ▼
SQLite（storage/）· Ledger（core/economy.py）· LLM Providers（llm/）
```

`main.py` 是**唯一组合根**：把 Economy、Communication、Task、Workspace、
Tool Runtime、UI 接起来。其他层互相不知道对方是怎么创建的。

依赖方向（有守卫）：

```text
services/       → core/ + communication/      （不直接依赖 storage/）
core/           → storage/ + agents/
communication/  → core/ + storage/
llm/            → core/ + storage/
tool_runtime/   → core/ + 标准库
```

> 完整依赖图、已知例外和四条守卫见 [`AGENTS.md`](AGENTS.md) §3。

---

## 项目结构

```text
ai-economy/
├── main.py                 ← 组合根 / CLI 入口
├── start_aieos.sh          ← UI 启停脚本
├── dev.sh                  ← 开发命令入口
│
├── ui/                     ← AIEOS 浏览器界面（server / static / task 页）
├── work/                   ← Front / Pi runtime · handoff · 生命周期 · 快照
├── front/                  ← Front Agent
├── demand/                 ← Human Demand 入口
│
├── agents/                 ← Agent 定义与 Brain
├── actions/                ← Agent 动作执行器
├── cognition/              ← 认知
├── experience/             ← 经验
├── reflection/             ← 反思
├── selfmodel/              ← 自我模型
├── stage/                  ← 阶段
├── budget/                 ← 认知预算
├── delegation/             ← 委派
│
├── services/               ← Task 应用层（门面 / Handler / 议价 / 执行 / 对齐）
├── core/                   ← Task Core · Economy · Workspace · Shared Context · Platform
├── communication/          ← Message · Transport · Journal
├── llm/                    ← Provider · Router · Recorder · Cost
├── tool_runtime/           ← 受控代码执行 / 测试
├── change/                 ← Git Worktree
├── commit/                 ← Output Commit
├── storage/                ← SQLite（29 张表）
│
├── resources/              ← 公共只读工具库
├── config/                 ← config.yaml + llm.yaml
├── docs/                   ← 架构文档 / ADR / 历史
├── tests/                  ← 5702 个测试 + 30 个真实 API 测试
│
├── tasks/                  ← Task 运行目录（不进 git）
├── runs/                   ← UI / 日志 / 输出（不进 git）
└── data/                   ← 本地数据库（不进 git）
```

---

## 经济模型

### 货币与账本

- 内部货币：**AI 币（AIC，AI Coin）**。
- `wallets` 表**没有 balance 字段**；余额永远由 Ledger 求和得出。
- Ledger 是 **append-only**：不改历史，记错了就追加一笔反向交易。
- `GENESIS_MINT` 是全系统唯一能凭空创造货币的入口。

### 成本与破产

```yaml
# config/config.yaml
economy:
  tokens_per_ai_coin: 10000     # 1 AIC ≈ 10,000 tokens
```

LLM 调用是真实经营成本，会记 `LLM_COST` 并进入 Ledger。
持续亏损的 Agent 会进入 `INSOLVENT`，但不会导致程序崩溃。

### 当前边界

- **`COMPLETE` 不移动钱**：任务完成只结束生命周期，不做结算。
- **`Agreement ≠ Payment`**：成交价是协议事实，不等于付款事实。
- `ESCROW_LOCK / RELEASE / REFUND` 目前是**预留常量**，
  没有托管 / 结算流程；`FEE` 同样是预留。

---

## 平台支持

| 平台 | 状态 | 说明 |
|---|---|---|
| macOS | `CURRENT` | 当前主要开发 / 验证环境 |
| Linux | `UNVERIFIED` | 预期可用，尚无完整 CI |
| Windows 原生 | `DESIGNED` | 适配层骨架已就位，按文档实现 `WindowsAdapter` |
| WSL | `EXPERIMENTAL` | 路径语义需单独处理 |

平台差异已收敛到 `core/platform_support/`：
进程组、文件锁、符号链接、权限位都通过 `PlatformAdapter`，
业务代码不判断操作系统。

- 适配指南：[`PLATFORM_COMPATIBILITY.md`](docs/architecture/PLATFORM_COMPATIBILITY.md) §0
- 边界守卫：`tests/test_platform_guards.py`
- 契约测试：`tests/test_platform_support.py`

---

## 测试

```bash
./dev.sh test                              # 全量（不含真实 API）
./dev.sh test -v                           # 详细输出
./dev.sh test tests/test_ledger.py -v      # 单个文件
./dev.sh test tests/test_ledger.py -k x    # 单个测试
```

当前基线：**5702 passed, 2 skipped, 30 deselected**。

另有 30 个真实 API 测试，需要 Key 且会产生费用，单独运行：

```bash
./dev.sh test-real deepseek
./dev.sh test-real doubao
```

项目对关键边界有守卫测试（`tests/*guards*.py`），新增守卫必须配反向测试。

---

## 未来发展方向

> 以下均为**设计方向记录**，不是当前能力。
> 记录它们，是为了确保当前架构留好了接口。

### Agent 自我生存

```text
收入 - LLM成本 - 工具成本 - 外包成本 - 存储成本 - 通信成本 = 净利润
```

### Agent 破产演化

持续亏损 → 余额下降 → 无法支付成本 → 无法接任务 → `INSOLVENT`。

### Agent 竞争

比较**质量、成功率、成本、时间、信誉**，而不是简单「最低价获胜」。

### Agent 专业化

```text
Agent A  软件开发
Agent B  测试
Agent C  研究
Agent D  数据处理
```

### Agent 市场

```text
Task Market       任务市场
Service Market    服务市场
Agent Market      Agent 本身可以买卖
Capital Market    资本市场
```

### 分布式演进

```text
多 Agent 本地（现在）
    ↓
多机器（LocalTransport → NetworkTransport）
    ↓
跨网络
    ↓
开放式分布式网络
```

**为演进预留的接口：**

| 现在 | 未来替换成 | 谁不用改 |
|---|---|---|
| `LocalTransport` | `NetworkTransport` / `P2PTransport` | Agent 代码 |
| 本地 Ledger | 其他 Ledger 后端 | Agent / Wallet 代码 |
| 本地 Workspace | `RemoteWorkspace` | Agent 代码 |

> 长期观察信号：收入、成本、成功率、返工率、谈判表现、生存能力。
> 这些是观察 Agent 实际行为的外部信号；V1 不实现 Reputation / Ranking /
> 自动最优选择 / 策略学习。

---

## 当前版本明确不做

为避免把临时部署当成永久架构，以下能力需要明确任务书才解锁：

- ❌ 区块链 / NFT / 加密货币
- ❌ P2P 网络 / 自动发现 Agent
- ❌ Docker / Kubernetes / 多机器部署
- ❌ 向量数据库 / 语义检索（当前是结构化记忆，不是向量库）
- ❌ 多 Agent 动态生成 / 自我复制
- ❌ 自动赚钱机器人 / 复杂金融市场
- ❌ Payment / Escrow / Settlement
- ❌ OS 级执行沙箱 / IAM / ACL
- ❌ Human Task Manager / Coordinator

> 完整范围与解锁条件见 [`AGENTS.md`](AGENTS.md) §7。

---

## 文档导航

| 文档 | 作用 | 什么时候读 |
|---|---|---|
| [`docs/INSTALL.md`](docs/INSTALL.md) | **源码安装 / 朋友安装 / AI 协助配置** | 第一次安装或换机器时 |
| [`docs/PROVIDERS.md`](docs/PROVIDERS.md) | **服务商接入（OpenCode / DeepSeek / 豆包 / 百炼）** | 新增或排查模型服务商时 |
| [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) | **当前版本真实状态、推荐模式、已知限制** | 想知道「现在到底能做什么」 |
| [`AGENTS.md`](AGENTS.md) | **Developer Context / 入口** | 开始工作前 |
| [`ARCHITECTURE_CONSTITUTION.md`](docs/architecture/ARCHITECTURE_CONSTITUTION.md) | **长期架构 Source of Truth** | 改架构之前 |
| [`PLATFORM_COMPATIBILITY.md`](docs/architecture/PLATFORM_COMPATIBILITY.md) | **平台适配指南 / Windows 待办** | 做跨平台适配时 |
| [`TOOL_RUNTIME_V1_DESIGN.md`](docs/architecture/TOOL_RUNTIME_V1_DESIGN.md) | **受控代码执行 / 测试** | 做代码执行时 |
| [`PROTOCOL.md`](PROTOCOL.md) | 消息协议 | 改协议时 |
| [`docs/architecture/AIEOS_CLI.md`](docs/architecture/AIEOS_CLI.md) | **AIEOS Launcher CLI · 启动 / 参数 / 日志 / doctor** | 启动、重启、改启动参数时 |
| [`COMMANDS.md`](COMMANDS.md) | 全部命令 | 想知道能跑什么 |
| [`docs/HISTORY.md`](docs/HISTORY.md) | **Step 1 → Workspace Phase 0 历史** | 想了解演进过程 |
| [`docs/architecture/adr/`](docs/architecture/adr/README.md) | 架构决策记录 | 想知道「为什么这样设计」 |

---

## License

AI Economy OS is licensed under the **Apache License 2.0**.

- [`LICENSE`](LICENSE)
- [`NOTICE`](NOTICE)

Copyright 2026 AI Economy OS contributors.

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

**代码里的体现**：`agents/base_agent.py` 中不会出现任何厂商名字。
Agent 只会说 `brain.generate(...)`，至于背后是哪个公司的模型，Agent 不关心。

换来的是：明天 DeepSeek 涨价、或者某个厂商挂了，只改配置，Agent 代码一行不动。

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
- [x] 60 个 pytest 测试全绿

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

### ⏳ 后续阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Step 2 | LLM Provider 系统 + Router + MockProvider | 待开始 |
| Step 3 | Agent-to-Agent 通信（Message + Transport） | 待开始 |
| Step 4 | Task / Bid / Contract / Escrow | 待开始 |
| Step 5 | 完整经济交易 Demo（A 外包给 B，结算） | 待开始 |
| Step 6 | Cost Calculator、真实模型接入 | 待开始 |

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
│   ├── economy.py             ← 经济系统核心
│   └── logging_setup.py       ← 日志
│
├── agents/
│   ├── base_agent.py          ← 经济主体骨架
│   ├── agent_a/
│   │   ├── config.yaml        ← A 的私有配置
│   │   ├── workspace/         ← A 的工作产物
│   │   └── memory/            ← A 的私有记忆
│   └── agent_b/               ← 同理
│
├── llm/                       ← (Step 2) LLM Provider 系统
├── communication/             ← (Step 3) Agent-to-Agent 通信
├── storage/
│   ├── database.py            ← SQLite 连接、事务
│   └── models.py              ← 表结构定义
│
├── tests/                     ← 60 个测试
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

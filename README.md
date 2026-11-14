# AI Economy OS

> 一个「AI Agent 经济系统」的原型。
>
> 每个 Agent 都是一个独立的经济主体：有自己的身份、工作空间、记忆、钱包和经济状态。
> Agent 调用不同公司的大模型 API 来获得智力能力，但 **Agent 不等于 LLM**。
> Agent 可以自己决定做任务、拒绝任务、外包任务、向其他 Agent 询价、报价、支付、收款。
> Agent 的最终能力不仅由模型能力决定，也由它能否在经济系统中长期生存决定。

**当前版本：v0.2.0（开发中）** —— 当前最佳工作方式：Front → Pi Agent → 自主工作。

> 当前版本的真实能力、推荐模式、已知限制与版本变化，见
> [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)。

---


---

## 📚 文档导航

| 文档 | 作用 | 什么时候读 |
|---|---|---|
| [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) | **当前版本现状** —— 推荐模式 / 已知限制 / 版本变化 | 想知道「现在到底能做什么」时 |
| [`AGENTS.md`](AGENTS.md) | **Developer Context** —— 入口与导航 | **开始工作前** |
| [`docs/architecture/ARCHITECTURE_CONSTITUTION.md`](docs/architecture/ARCHITECTURE_CONSTITUTION.md) | **长期架构 Source of Truth** | 改架构之前 |
| [`docs/architecture/TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md) | Task Shared Context / Alignment 规范 | 做 Shared Context 时 |
| [`docs/architecture/adr/`](docs/architecture/adr/README.md) | **架构决策记录**（为什么这样设计） | 想知道历史理由时 |
| [`PROTOCOL.md`](PROTOCOL.md) | 消息协议细节 | 改协议时 |
| [`COMMANDS.md`](COMMANDS.md) | 全部命令 | 想知道能跑什么时 |
| 本文件 | 怎么把项目跑起来 | 第一次接触时 |

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

> ⚠️ **本节是从 Step 1 开始的详细历史记录，最新只到「Workspace Phase 0」。**
> **Step 4F 之后的内容不在这里** —— 当前真实状态请看
> [`AGENTS.md`](AGENTS.md) §2（它是每次开工前的入口）。

### 🚧 最新进度（Step 4F 之后）

| 阶段 | 内容 | 状态 |
|---|---|---|
| Step 4F | Task Shared Context / Alignment | ✅ 已实现 |
| Workspace Core v1 | 资源树 · Controller · Lock · Revision · Audit · Event · Gateway | ✅ 已冻结（ADR-0029/0030/0031） |
| Change Pipeline Phase 1 | Git Worktree Manager · `hello-demo` | ✅ 已实现（ADR-0032…0037） |
| Agent Core v1 | Actions · Cognition · Experience · Reflection · Self Model · Stage · Budget · Delegation | ✅ 已实现 |
| Tool Runtime V1 | 公共只读工具库（python / pytest / node / playwright）· 独立 run 目录 · 受控执行 | ✅ 已实现（ADR-0040） |
| Vision Perception V1 | 原生读图（deepseek-flash）· run_tool 截图回喂 · Playwright 配置发现 + smoke gate | ✅ 已实现（ADR-0041） |
| Demand Entry V1 | `./dev.sh demand` · Agent 自主 `task_create` · 串行 RunScheduler · CLI | ✅ 已实现（ADR-0042） |
| Workspace Read/Edit V1 | 范围读取（offset/limit）· `workspace_edit` old/new 局部替换 | ✅ 已实现（ADR-0043） |
| Workspace Search V1 | `workspace_search` 只读字面搜索 · Demand CLI 接入工作记忆/短期/长期/私有空间 | ✅ 已实现（ADR-0044） |
| Pi Work | Front → Pi Agent → workflow / autonomous · 快照 · 恢复 / Reopen | ✅ 已实现 |
| Output Commit | 独立输出仓库 · manual / limited / auto · Front `commit_confirm` | ✅ 已实现（ADR-0058） |
| Project Workdir | 直接修改已有项目目录 · shadow snapshot · 项目锁 | ✅ Phase 1（ADR-0060） |
| 实验线 | Human Demand → 多 Agent 协作生产 / 自主分工观察（`experiments/`） | 🚧 进行中 |

**测试基线**：5642 passed / 2 skipped / 30 deselected

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
- [x] 5642 个 pytest 测试全绿（另有 30 个真实 API 测试单独跑）

**验收标准**（已达成）：

```
$ python3 main.py

================================
        AI Economy OS v0.2.0
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

### ✅ Step 2.5：真实 DeepSeek Provider（已完成）

**唯一目标：验证 Step 2 那套抽象在真实 HTTP API 下是否成立。**

结论：**成立。** 接入真实厂商只动了两处：

| 改动 | 位置 |
|---|---|
| 新增 `llm/deepseek.py` | 全部 DeepSeek 细节关在这一个文件里 |
| 注册表加一行 `registry.register("deepseek", DeepSeekProvider)` | `llm/router.py` |

**`core/` 一行没动；`agents/` 一行没动。**

调用链：

```
Agent → Brain → Router → DeepSeekProvider → 真实 HTTPS → LLMResponse → Agent
```

**验收结果：**

| 要求 | 状态 |
|---|---|
| MockProvider 继续正常 | ✅ |
| DeepSeekProvider 能真实调用 | ✅ 实测连通 api.deepseek.com |
| Agent 无需任何 DeepSeek 专属代码 | ✅ 由 AST 守卫测试强制 |
| Router 能按名字找到它 | ✅ |
| 真实 usage 进入 LLMResponse | ✅ `token_source="api"` |
| `simulated=false` 表示真实数据 | ✅ |
| API 错误进入统一异常体系 | ✅ 401→AuthenticationError，429→RateLimitError 等 |
| API Key 不泄露 | ✅ 只从环境变量读，异常信息自动抹除 |
| 原有测试全部通过 | ✅ 314 passed |
| 架构守卫全部通过 | ✅ 5 条守卫 + 反向测试验证过非空转 |

**新增的能力：`token_source` 三态**

这是本步骤最重要的设计修正。原本 `simulated: bool` 只能表达「模拟/真实」两态，
但真实场景有三种：

| token_source | 含义 | 能不能拿去算钱 |
|---|---|---|
| `api` | API 返回的真实 usage | ✅ 可以，能对账单 |
| `estimated` | 我们自己估算的 | ⚠️ 只能做粗略预算 |
| `unknown` | API 没返回，我们也不知道 | ❌ 不能，也**不许猜** |

关键规则：**API 没给 usage 时，token 字段留 `None`（未知），绝不用估算值顶替。**
因为 token 在这个项目里要换钱，把猜测当事实会让账永远对不上。

**真实调用验证**（用无效 Key 实测真实 HTTPS 往返）：

```
$ ./dev.sh ask "你好" --provider deepseek

  模型调用失败
  DeepSeek 鉴权失败（HTTP 401）。
  通常是这几种原因：
    · DEEPSEEK_API_KEY 的值不对或已失效
    · Key 前后有空格或换行
    · Key 被撤销了
  重试没有意义，请检查环境变量。
```

**本阶段明确不做**：不接 Doubao / Grok、不做自动选择、不做 fallback、
不把 token 折算成 AI 币（`cost_ai` 仍是 0 = 还没算）。

### ✅ Step 2.6：豆包 Provider + 共用基类提取（已完成）

**加豆包时发现：DeepSeek / 豆包 / Grok 三家的接口形状完全一样**
（都是 OpenAI 兼容格式）。所以先把「形状」抽成共用基类，
再加豆包 —— 而不是把那 647 行复制一份。

| 文件 | 行数 | 职责 |
|---|---|---|
| `llm/openai_compatible.py` | 704 | 共用：HTTP / 请求构造 / 响应解析 / 错误翻译 |
| `llm/deepseek.py` | **84** | 只有 DeepSeek 的身份（从 647 行瘦身） |
| `llm/doubao.py` | 153 | 只有豆包的身份 |

**加第 N 家的成本 = 一个 100~150 行的文件 + 注册表一行。**

#### 真实调用实测结果（两个 provider 都验证过）

```
$ ./dev.sh test-real deepseek      14 passed
$ ./dev.sh test-real doubao        15 passed
```

**发现 1：豆包的模型名有两种写法，必须显式配置**

豆包没有「固定模型名」，必须二选一：

| 写法 | 形态 | 说明 |
|---|---|---|
| 模型名称（推荐） | `doubao-seed-1-6-250615` | 直接用，无需创建接入点 |
| 推理接入点 ID | `ep-2024xxxxxxxxx-xxxxx` | 需要先在控制台创建并部署 |

所以 `DoubaoProvider` **故意不给默认模型** —— 猜一个只会让人困惑。

**发现 2：某些豆包模型会「静默忽略」max_tokens**

```
传 max_tokens=16  →  实际输出 2586 tokens（finish_reason=length）
```

两个可配置的解决方案（都不用改代码）：

| 方案 | 配置 |
|---|---|
| 换字段名 | `max_tokens_field: max_completion_tokens` |
| 关掉思考 | `extra_body: {thinking: {type: disabled}}` |

> 注意 `max_tokens` 和 `max_completion_tokens` 不能同时传，否则 400。

**发现 3（本步骤最重要的经济数据）：思考的成本**

同一句问题，同一个模型，可见答案几乎一样：

| | 输出 tokens | 可见字数 | 耗时 |
|---|---|---|---|
| 带思考（默认） | **1913** | 90 字 | **41.1 秒** |
| 关掉思考 | **64** | 96 字 | 1.8 秒 |

**30 倍 token、22 倍延迟，换来几乎一样的答案。**

这就是这个项目要研究的核心命题落地版：
**Agent 的智力成本，和它产出的可见结果完全不成比例。**
普通 Agent 项目看不到这件事（调用是免费的）。

**发现 4：我们的 token 估算在不同厂商之间不可靠**

| Provider | 我们估算 | 真实输入 | 偏差 |
|---|---|---|---|
| DeepSeek | 14 | 9 | 我们估高了 1.6 倍（安全方向） |
| **豆包** | 14 | **51** | **我们估低了 3.7 倍（危险方向）** |

这条数据直接证明了 `token_source` 那个设计的必要性：
**只有 `api` 来源的 token 才能拿去算钱，`estimated` 只能做粗略预算。**

#### 顺手修掉的一个测试缺陷

加豆包时发现：测试套件会读取用户本机的 `.env`。
你 `.env` 里写了 `DEEPSEEK_MODEL=...`，环境变量优先级高于配置文件，
于是「配置生效」类的断言会莫名失败 —— 删掉 `.env` 又通过。

这种「依赖用户本机状态」的测试是假信号。现在 `conftest.py` 加了
自动夹具，让单元测试与 `.env` 隔离（`real_api` 测试除外，它们就是要用真实 Key）。

### ✅ Step 3：Agent-to-Agent 通信（已完成）

**唯一目标：让两个独立 Agent 通过统一抽象互相收发消息。**

```
Agent A → CommunicationManager → Message → Transport → LocalTransport → Agent B
                                                                          ↓
Agent A ← CommunicationManager ← Message ← Transport ←────────────── 回复
```

**六条关键设计**

| # | 设计 | 为什么 |
|---|---|---|
| 1 | `Agent 身份 ≠ 通信地址` | 同一个 Agent 可以在本地也可以用 p2p 地址；搬机器时账本/合同/声誉不受影响 |
| 2 | `Transport` 是抽象接口 | 换 P2P 时 Agent / Manager 一行都不用改 |
| 3 | 注册表**只存数据**，不存活对象 | 远端 Agent 才能作为普通记录存在（你没有对方的 Python 对象） |
| 4 | 本地绑定表在 Manager，不在 Registry | 「谁是本地的」是通信信息，不是身份信息 |
| 5 | 通信层不依赖 LLM / Economy | 回一个 PONG 不需要加载模型；TASK_OFFER 里的钱归 Economy 管 |
| 6 | 默认**不自动回复** | 两边都自动回复会形成无限往返，每轮都可能花钱 |

**为未来留下的接口（本步骤都不实现）**

| 接口 | 留给谁 |
|---|---|
| `Transport.discover()` | P2P 节点发现 —— 注册表查不到时去问传输层 |
| `AgentRegistry.register_remote()` | 远端 Agent 目录 |
| `MessageType.TASK_OFFER` 等 8 个类型 | Step 4/5 的经济消息（现在只有名字） |
| `Message.payload` 的 `task_id` / `workspace_id` | Task Workspace 接口 |
| `Message.extensions` | 签名 / nonce / TTL 等安全字段（前向兼容，转发不丢） |

**验收结果**

| 要求 | 状态 |
|---|---|
| Agent A ↔ Agent B 双向通信 | ✅ |
| Message 可 JSON 序列化/反序列化（无损） | ✅ |
| Agent ID 与 Address 分离 | ✅ |
| Transport 是抽象接口 | ✅ 不能直接实例化 |
| LocalTransport 是当前唯一实现 | ✅ |
| Agent 不依赖 LocalTransport | ✅ 连 `communication` 都不 import |
| Communication 不依赖 LLM | ✅ 由 AST 守卫强制 |
| Communication 不依赖 Economy | ✅ 由 AST 守卫强制 |
| 支持 message_id + 重复检测 | ✅ 重复消息只处理一次 |
| Runtime 组装通信依赖 | ✅ |
| 未来可加 P2PTransport 而不改 Agent | ✅ 有测试现场定义一个 P2PTransport 验证 |
| 不实现 P2P / 支付 / Task Market / Wallet | ✅ |

**测试：422 → 633（新增 211）**

| 新增文件 | 数量 | 测什么 |
|---|---|---|
| `test_message.py` | 51 | 创建/校验/序列化/前向兼容/回复 |
| `test_address.py` | 28 | 身份与地址分离、解析、不可变性 |
| `test_local_transport.py` | 33 | 抽象契约、收发、失败路径、信箱隔离 |
| `test_registry.py` | 34 | 纯数据、本地/远端、通信状态≠经济状态 |
| `test_communication.py` | 44 | 完整链路、去重、可替换传输、真 Agent 集成 |
| `test_communication_guards.py` | 21 | 7 条架构守卫（已用反向测试验证非空转） |

**顺手发现并修掉的一个真 bug**

`Address.parse("ws://h:8080/agent_b")` 原本只取 `netloc`，
把路径丢掉了 —— 于是同一台机器上的不同 Agent 会解析成同一个地址。
现在 netloc 和 path 一起保留。

### ✅ Step 3.5：通信日志（Communication Journal）（已完成）

**目标：出问题时能靠日志定位，而不是靠猜。**

```
Message → Transport → CommunicationRecorder → SQLite
```

#> 完整的协议规范（消息格式、状态机、Transport 契约、版本演进规则）
> 见 **[`PROTOCOL.md`](PROTOCOL.md)**。

### 最重要的一条：Message ≠ 日志

| | Message | 日志 |
|---|---|---|
| 是什么 | 通信用的数据 | 观测用的记录 |
| 会发给对方吗 | 会 | 不会 |
| 删掉会怎样 | 通信没了 | **通信照常工作** |
| 存在哪 | 内存 / 网络 | SQLite 两张表 |

`communication/message.py` 里**没有一行代码提到日志** —— 由架构守卫强制保证。

#### 两张表

| 表 | 作用 | 写入方式 |
|---|---|---|
| `communication_messages` | 每条消息的**当前状态**（一行一条，message_id 主键） | UPSERT |
| `communication_events` | 状态**事件流** | append-only |

**为什么状态要拆成「一行 + 事件流」：**

- 只看最终状态的话，「失败过两次又成功了」和「一次就成功」看起来一样
- 一行保证「一个 message_id 只有一条状态」，反复投递不会产生无法区分的重复记录
- 事件流保留完整历史：每步间隔、重试次数、失败原因、重复投递次数

#### 记录的状态

```
CREATED → SENT → DELIVERED → RECEIVED → PROCESSING → PROCESSED
                              ↓            ↓            ↓
                            DUPLICATE    FAILED       FAILED
```
（`QUEUED` / `TIMEOUT` / `RETRY` 常量已留，等有 ACK 机制和重试逻辑时启用）

#### 一条消息的日志长这样

```
$ ./dev.sh communication-log --message msg_4ad6a166

消息 msg_4ad6a166
------------------------------------------------------------------------
  trace_id        : trc_0b327e83
  conversation_id : conv_64be7b6c
  from → to       : agent_a → agent_b
  message_type    : TEXT
  status          : PROCESSED
  transport       : local
  address         : local://agent_b
  is_remote       : False
  retry_count     : 0
  payload_size    : 25
  payload_hash    : e7d0955f2780b8b9d60591c…

  时间线
    created_at    : 2026-09-26T07:51:02.478368
    sent_at       : 2026-09-26T07:51:02.479265
    received_at   : 2026-09-26T07:51:02.479804
    processed_at  : 2026-09-26T07:51:02.480245

  事件流
    [07:51:02] CREATED     agent_a    消息已构造，等待投递
    [07:51:02] SENT        agent_a    传输层已接受（local）
    [07:51:02] DELIVERED   agent_b    已进入 agent_b 的收件箱
    [07:51:02] RECEIVED    agent_b    已从 agent_b 的收件箱取出
    [07:51:02] PROCESSING  agent_b    agent_b 开始处理
    [07:51:02] PROCESSED   agent_b    agent_b 处理完成
```

**四个时间戳分开存的价值**：哪一步没发生，看哪一列是 `NULL` 就知道。
只发了没收 → `status=DELIVERED`、`received_at=NULL`。

#### 两种链路 ID

| ID | 管什么 | 谁继承 |
|---|---|---|
| `trace_id` | 一次完整任务 / 业务流程 | **回复自动继承** —— 整条任务的往返消息都能串起来 |
| `conversation_id` | 一轮对话 / 协商 | 同上 |

回复不继承 `trace_id` 的话，一次任务的来回消息在日志里就是互不相干的孤儿。

#### 敏感信息自动脱敏

日志会被查询、打印、贴到聊天里求助 —— **一旦 Key 进去就等于泄露，而且永久留在数据库里**。

所以在**入口处自动过滤**（不靠人自觉）：

| 手段 | 例子 |
|---|---|
| 键名匹配 | `api_key` / `secret` / `private_key` / `mnemonic` / `access_token` / `authorization` |
| 值特征匹配 | `sk-xxxxx` / `ark-xxxxx` / `Bearer xxx` / `-----BEGIN PRIVATE KEY-----` |
| 错误信息 | 异常里拼了请求头也会被抹掉 |

**注意 `token` 本身没有列为敏感** —— 因为 `input_tokens` / `total_tokens` / `tokens_per_ai_coin` 都是正当字段。一律脱敏会让日志没法看。

#### 日志失败不影响通信

日志是观测手段，不该成为通信的单点故障。
`CommunicationRecorder` 默认 `strict=False`：写失败只记 warning，消息照常发送。
测试里用 `strict=True` 让失败暴露出来。

#### 为 P2P 预留

日志里存 `{transport, address, is_remote}`，而不是「本地」这种假设：

| 传输 | transport | address | is_remote |
|---|---|---|---|
| 本地 | `local` | `local://agent_b` | 0 |
| 未来 P2P | `p2p` | `p2p://12D3KooW...` | 1 |

表结构不用改。

#### 验收结果

| 需求验收点 | 状态 |
|---|---|
| 1. 发送消息产生日志 | ✅ CREATED/SENT/DELIVERED |
| 2. 接收消息产生日志 | ✅ RECEIVED/PROCESSING/PROCESSED |
| 3. 成功流程状态完整 | ✅ 6 个事件、4 个时间戳齐全 |
| 4. 失败流程有错误记录 | ✅ FAILED + error_code + error_message |
| 5. 相同 message_id 不产生无法区分的重复记录 | ✅ UPSERT 一行 + 事件流留痕 |
| 6. 日志不含钱包私钥 / API Key | ✅ 入口自动脱敏，含错误信息 |
| 7. 不影响现有通信功能 | ✅ 日志失败通信照常 |
| 8. 原有测试继续通过 | ✅ 633 → 748 全绿 |

**测试：633 → 748（新增 115）**

| 新增/修改 | 数量 |
|---|---|
| `test_communication_recorder.py` | 86 |
| `test_communication_guards.py` | +6（日志边界守卫） |
| `test_cli.py` | +23（日志查询命令、位置参数、参数报错） |

### ✅ Step 3.6：协议语义修正（已完成）

**目标：让协议现在稳定、简单，并为 Step 4+ 留出正确扩展空间。**

**不写新功能，只修协议定义。** 核心结论：

> 检查后确认 `Message` 数据结构**已经能表达**修正后的协议所需的一切，
> 因此**没有改任何核心通信代码** —— 只改协议文档 + 补测试。

#### 五个概念必须分开

| 概念 | 回答 | 协议载体 |
|---|---|---|
| Agent Identity | 我是**谁** | `agent_id` |
| Transport Address | **怎么**找到我 | `Address(scheme, endpoint)` |
| Task | 我在做**哪件事** | `payload.task_id` |
| Workspace | 这件事的**工作空间** | `payload.workspace_id` |
| Workspace Permission | 我**被允许**做什么 | **协议中不存在** |

**最关键的一条**：

```
workspace_id  ≠  权限
```

知道一个 `workspace_id` **不会**带来任何访问能力。
协议里**不存在**任何「凭 ID 换权限」的机制。

#### 结构化经济 payload

```json
// TASK_OFFER —— budget 是"预算上限"，是金额对象
"budget": { "amount": 100, "currency": "AIC" }

// COUNTER_OFFER —— price 是"要价"
"price":  { "amount":  25, "currency": "AIC" }

// ACCEPT —— 同时带 price 和 reply_to，自成可审计记录
"price":  { "amount":  25, "currency": "AIC" },
"reply_to": "msg_counter_002"
```

**为什么不能是裸数字**：`100` 没有货币语义，机器读不出「100 什么」。

**`budget` ≠ `price`**：

| | 字段 | 谁提 | 含义 |
|---|---|---|---|
| 预算 | `budget` | 需求方 | 我**愿意付的上限** |
| 价格 | `price` | 双方 | **实际成交的数** |

混用一个字段会导致账本凭空多计 75 AIC。**成交价的权威来源是 `ACCEPT.price`。**

#### 三态标注（诚实性）

协议里每一项能力都标为：

| 标记 | 含义 |
|---|---|
| ✅ 已实现 | 代码真会产生/处理，有测试 |
| ⚠️ 已定义未实现 | 协议定义了语义，代码无逻辑 |
| 🔒 预留 | 只是字段位，语义未定死 |

**沿用这个标注后，协议不再把「注册了」说成「实现了」：**

| 项 | 数量 | 状态 |
|---|---|---|
| 消息类型 | 12 个 | 4 个在用，8 个**已定义未实现** |
| 生命周期状态 | 11 个 | **6 个常规发出**，`FAILED`/`DUPLICATE` 按需，`QUEUED`/`TIMEOUT`/`RETRY` **永不发出** |
| 结构化 payload | 6 种 | **已定义，代码不校验** |
| 签名 / 重放防护 / TTL | 3 个字段 | **🔒 预留，未实现** |

#### 一条刻意的边界

> **通信层不校验 payload 的业务形状。**

`{"message_type": "TASK_OFFER", "payload": {"budget": 100}}`
（裸数字，违反协议）**当前会被正常投递** —— 因为校验它等于让通信层解释业务语义。

校验属于 Step 4 的 Task 层，落点是 `register_type_handler`。

#### 预留字段

| 字段 | 含义 | 状态 |
|---|---|---|
| `base_commit` | 基于哪个代码版本**开始**（不是 current / final） | 🔒 未来用于 Git Worktree / 冲突检测 |
| `acceptance` | **什么条件算完成**（tests / manual / artifact / custom） | 🔒 支付链 `SUBMIT→VERIFY→PASS→SETTLE` 的依据 |
| `expires_at` | 有效截止时间 | 🔒 报价不能永久有效；**当前不校验** |

#### 货币代码（已在 Step 3.7 统一）

本步骤发现协议示例用 `AIC` 而 `config.yaml` 用 `AI`，只做了标记。
**Step 3.7 已统一为 `AIC`**（含数据迁移），见下一节。

**测试：748 → 832（新增 84）**

| 新增 | 数量 |
|---|---|
| `test_protocol_payloads.py` | 84 |

### ✅ Step 3.7：协议收尾（已完成）—— **Step 3 正式冻结**

**三件小事，之后不再动协议。**

#### 1. 货币代码统一为 `AIC`

```text
AIC = AI Coin
```

| 位置 | 现在 |
|---|---|
| `config.yaml` `economy.currency` | `"AIC"` |
| 钱包 `wallets.currency` | `"AIC"` |
| 账本 `ledger_transactions.asset` | `"AIC"` |
| 协议示例 | `"AIC"` |

> **名字 vs 代码**：`AI Coin` 是**名字**（人读），`AIC` 是**代码**（机器读）。
> 「AI Economy」是项目名，不是货币代码。

**⚠️ 这一步必须带数据迁移** —— 不是可选的：

余额是「按 `asset` 过滤后求和」算出来的。如果代码用 `AIC` 而老数据是 `AI`：
- 旧 Agent 用 `AI`、新 Agent 用 `AIC` → **币种不一致**
- 实测后果：**转账直接失败**（`WalletError: 币种不一致`）

所以加了 2 条幂等的 `DATA_MIGRATIONS`，把 `wallets.currency` 和
`ledger_transactions.asset` 里的 `'AI'` 改成 `'AIC'`。

> **这不违反账本 append-only**：账本不允许 UPDATE 的是**金额**。
> 这里改的只是货币符号的写法，**金额一个字节都没动**（已验证：77.0 → 77.0）。

#### 2. `ACCEPT` 的最终成交价语义（两种情况）

```text
ACCEPT.price = 被 ACCEPT 的那条 Offer 所要求的价格

如果 reply_to 指向 TASK_OFFER     →  ACCEPT.price = TASK_OFFER.budget
如果 reply_to 指向 COUNTER_OFFER  →  ACCEPT.price = COUNTER_OFFER.price
```

| 情况 | 流程 | 成交价 |
|---|---|---|
| A 直接接受 | `TASK_OFFER(100)` → `ACCEPT` | **100**（= budget） |
| B 还价后接受 | `TASK_OFFER(100)` → `COUNTER_OFFER(25)` → `ACCEPT` | **25**（= counter price） |

#### 3. `task_id` ≠ `trace_id`

| | `task_id` | `trace_id` |
|---|---|---|
| 代表 | 业务任务的**稳定身份** | 一次具体的**执行链路** |
| 生命周期 | 整个任务不变 | 一次执行结束后固定 |
| 位置 | `payload`（业务数据） | 信封（通信元数据） |
| 用途 | 归属、结算 | 日志追踪、故障排查 |

```
TASK-001                 ← task_id 始终是它
 ├── trace-A             ← 第一次执行，失败了
 └── trace-B             ← 重试，成功了
```

如果 Task 就等于 `trace_id`，重试时 `task_id` 会变 ——
同一个业务任务在账本里变成两个，历史无法归并。

#### 4. `message_id` 暂时承担 Offer 标识

**当前不引入 `offer_id`。** 一条报价的 `message_id` 就是它的标识，
`reply_to` 指出它回应的是哪一条。

```
offer-001  TASK_OFFER      budget = 100
offer-002  COUNTER_OFFER   price  =  60   reply_to = offer-001
offer-003  COUNTER_OFFER   price  =  30   reply_to = offer-002
ACCEPT                     price  =  30   reply_to = offer-003
```

多轮还价也能完整重建。将来确需 `offer_id` 时，走**协议版本升级**。

**测试：832 → 857（新增 25）**

| 变化 | 数量 |
|---|---|
| `test_protocol_payloads.py` | +23（ACCEPT 两情况、task_id≠trace_id、报价链） |
| 货币统一守卫 | +2（原「已知不一致」提醒改为强制统一） |
| 货币断言更新 | `test_cli` / `test_startup` / `test_economy` / `test_ledger` / `test_wallet` / `conftest` |

**代码改动说明**：`core/` 只有**默认值字符串和注释**，`communication/` 只有**一行注释**，
**零逻辑改动**（`git diff` 可验证）。

### ✅ Step 4A：Task Core + Task Workspace 架构铺垫（已完成）

**目标不是「任务市场」，而是建立一个稳定、独立、可持久化、可测试的 Task Core，
同时正式把 Task Workspace 纳入架构模型。**

#### 两种 Workspace（从此必须分清）

| | Agent Private Workspace | Task Workspace |
|---|---|---|
| 文件 | [`core/workspace.py`](core/workspace.py) | [`core/task_workspace.py`](core/task_workspace.py) |
| 归属 | 每个 **Agent** 一个 | 每个 **Task** 一个 |
| 可见性 | **默认不对别人开放** | 任务级共享，可多 Agent 参与 |
| 文件 I/O | ✅ 有（带路径边界检查） | ❌ **完全没有** |
| 例子 | `agents/agent_a/workspace/` | `WS-001`（只是一个标识） |

> `private` 是「我的东西」，`task` 是「这件事的东西」。

#### Task 数据结构

```python
Task(
    task_id           = "task_a1b2c3d4"     # 任务身份（稳定）
    current_trace_id  = "trc_e5f6a7b8"      # 这一次执行（会变）
    creator           = "agent_a"
    contractor        = "agent_b"
    participants      = ["agent_a", "agent_b"]
    status            = "ACCEPTED"
    description       = "..."
    workspace_id      = "WS-001"            # 只是逻辑引用
    budget            = Money(100)          # 预算【上限】
    agreed_price      = Money(25)           # 实际【成交】价
    acceptance        = {"type": "tests"}
    expires_at        = None
    offers            = [Offer, Offer, ...] # 完整报价链
    created_at / updated_at
)
```

**两组「绝不能合并」的概念**：

| 字段 | 含义 | 为什么不能合并 |
|---|---|---|
| `task_id` | 任务身份 | 重跑时它不变；合并了就变成「另一个任务」，历史无法归并 |
| `current_trace_id` | 这一次执行 | 失败重跑会换新 trace |
| `budget` | 我愿意付的**上限** | 合并了账本会凭空多计（预算 100、成交 25 → 多记 75） |
| `agreed_price` | **实际谈成**的数 | |

#### Task 状态机（独立于通信状态机）

```
CREATED → OFFERED → NEGOTIATING → ACCEPTED
       → IN_PROGRESS → SUBMITTED → VERIFIED → COMPLETED

异常终态：REJECTED · CANCELLED · EXPIRED · FAILED
```

两条值得注意的边：

- `NEGOTIATING → NEGOTIATING`（**唯一允许的自转换**）—— 一轮谈判能还价多次
- `SUBMITTED → IN_PROGRESS`（**返工**）—— 验收不通过就打回重做，比直接判死更符合实际

**状态不能被直接赋值**：

```python
task.status = "COMPLETED"     # ❌ AttributeError
task.complete()               # ✅ 走状态机，会校验
```

#### 三条业务规则已变成代码保证

| 规则 | 实现 |
|---|---|
| 成交价不能超过预算 | `task.accept(Money(150))` 在预算 100 时抛 `BudgetExceededError` |
| 状态只能按状态机走 | `status` 是只读 property，没有 setter |
| 报价历史必须保留 | `task.offers` + `task_offers` 表，不是只存 `agreed_price` |

#### 四条边界（都有架构守卫 + 反向测试）

| Task Core 不依赖 | 守卫方式 |
|---|---|
| `llm/*` | AST 检查 import + 模型厂商名 |
| `communication/*` | AST 检查 import + `Message`/`MessageStatus` 引用 |
| `wallet` / `ledger` | AST 检查 import + 方法名分词匹配 |
| 文件系统 | AST 检查 import + `open()` 调用 + `mkdir` |

**持久化单独一个文件**（[`core/task_store.py`](core/task_store.py)）就是为了让
「Task Core 是纯的」这件事**可验证** —— 否则它只是一句口号。

#### 数据库

```sql
tasks         -- 任务当前状态（budget_* 与 agreed_price_* 各两列）
task_offers   -- 报价历史（append-only）
```

**没有** `workspace_members` / `workspace_permissions` / `workspace_files` ——
建空表会让人以为系统已经有那个能力。

#### 验收结果（对照 Step 4A 的 21 条）

| # | 要求 | 状态 |
|---|---|---|
| 1–5 | 清晰的 Task Core / 稳定 task_id / task_id 与 trace_id 分离 / budget 与 agreed_price 分离 / 货币用 AIC | ✅ |
| 6–7 | 可引用 workspace_id / 不产生权限 | ✅ 有守卫 |
| 8–11 | 独立状态机 / 与 Message 状态分离 / 不依赖 LLM / 不操作 Wallet-Ledger | ✅ 有守卫 |
| 12–16 | 不操作文件系统 / 两种 Workspace 分离 / Task-specific / 未来支持多 Agent / 不实现权限 | ✅ |
| 17–19 | 不实现 Git / Payment / P2P | ✅ 有守卫 |
| 20–21 | 旧测试继续通过 / 新增 Task 测试通过 | ✅ 857 → 1168 |

**测试：857 → 1168（新增 311）**

| 新增文件 | 数量 | 测什么 |
|---|---|---|
| `test_money.py` | 40 | 金额值类型、跨货币不可比、与配置一致 |
| `test_task_workspace.py` | 55 | 无 I/O、无权限、两种 Workspace 分离 |
| `test_task.py` | 114 | 身份、状态机、budget≠price、报价链、边界 |
| `test_task_store.py` | 47 | 持久化往返、两个概念分开存、报价历史 |
| `test_task_guards.py` | 41 | 7 类架构守卫（已用 9 项反向测试验证） |
| `test_cli.py` | +14 | `task-demo` / `task-log` |

### ✅ Step 4B：Agent Capability 与自主协作（已完成）

**一句话目标**：让一个 Task Initiator Agent 能根据任务所需能力**发现**潜在合作 Agent，
并**自行决定**是否发起邀约；系统提供发现基础设施，但**不建立中央 Task Manager**，
也**不替 Agent 做经济决策**。

#### 完整链路

```
Agent A（Task Initiator）
  │  创建 Task + 声明需要什么能力
  ↓
required_capabilities
  │
  ↓
AgentDirectory.find_capable()      ← 只回答「谁声明了这些能力」
  │
  ↓
候选列表 [B, C, D]                  ← 全部返回，不排序、不推荐
  │
  ↓
**Agent A 自己决定用谁**             ← 这一步属于 Agent，不属于系统
  │
  ↓
TASK_OFFER  →  Communication  →  Agent B
                                    │
                                    ↓
                              TaskService（适配层）
                                    │  校验 + 关联
                                    ↓
                              Task Core（状态 = OFFERED）
                                    │
                                    ↓
                              B 自己判断：ACCEPT / COUNTER_OFFER / REJECT
```

#### 三条刻意守住的边界

| 边界 | 含义 |
|---|---|
| **Capability ≠ Permission** | B 声明 `python_coding` **不代表**它能访问 `WS-001` |
| **Capability ≠ Economy** | 能力匹配**不产生任何账目** |
| **发现 ≠ 决策** | Directory 给候选，**选谁是 Agent 的决定** |

#### Capability：只有名字和集合运算

```python
profile = CapabilityProfile.of(["python_coding", "data_analysis"])
profile.supports("python_coding")        # True
profile.covers(["python_coding"])        # True（全部满足才算）
profile.missing(["python", "rust"])      # {"rust"} —— 说清差什么
```

**刻意没有的东西**（有守卫 + 反向测试）：分数、等级、排名、信誉、推荐、`best_agent()`。

> **声明 ≠ 认证**：Agent 说自己会 `postgresql`，只表示它**声称**会。
> 系统没有（也不该在 4B）证明这件事。

#### Agent Directory：可替换的发现接口

```python
directory.register_agent("agent_b", ["python_coding", "data_analysis"])
directory.find_capable(["python_coding"])   # → [entry, ...] 全部候选
```

**为一个未来实现留的接口**：`AgentDirectory` 是抽象类，
`LocalAgentDirectory` 只是当前实现。将来换 `NetworkAgentDirectory` / P2P 发现时，
**调用方代码一行都不用改**（有测试用一个「假想的远端目录」验证了这一点）。

**地址是字符串，不是 `communication.Address`** —— 这样 `core/` 不必依赖 `communication/`，
避免循环依赖。

#### TaskService：消息 → 领域 的适配层

**为什么放在新的 `services/` 包，而不是 `communication/handlers/`：**

```
services/  →  core/ + communication/ + storage/
core/      →  不依赖任何上层
communication/  →  不依赖 services/
```

`communication/` 必须能**在完全不知道 Task 存在的情况下**工作 ——
否则它就不能被单独使用、单独测试、单独替换。有守卫盯着这条线。

| 方法 | 做什么 |
|---|---|
| `candidates_for(task)` | 找候选（**不选人**） |
| `handle_offer(message)` | 校验 + 建立/关联本地任务 + 记 `message_id` + 对齐 `trace_id` |
| `handle_counter_offer(message)` | 同上 + **预算校验** |

**业务校验放在 Task 层**（`budget.currency == AIC`、`price <= budget`）——
通信层继续只搬运消息（有守卫禁止 `communication/` 出现 `budget` 这个词）。

#### 四个 ID 缝成一条链

```
Task (task_xxx)
  └── offers[].message_id ──→ Communication Journal
                                 └── trace_id / conversation_id
```

收到消息时，`TaskService` 把任务的 `current_trace_id` **对齐**到消息的 `trace_id`。
于是从任务能查到通信日志，反之亦然（有测试验证这条链是通的）。

> **重跑场景**：新执行带新 `trace_id`，但 `task_id` **不变** ——
> 这正是 Step 4A 把两个字段分开的用途。

#### 一个必须说清楚的实现局限

`capability-demo` 里 A 和 B 跑在**同一个进程、共用同一个数据库**，
所以 Handler 走的是「**关联**到已有任务」而不是「**新建**本地任务」。

真实部署中每个 Agent 有**自己的**存储，那时 B 会新建一条记录，但 `task_id` 相同 ——
因为它就是同一个业务任务。**Handler 两条路径都实现并测试了**（`result.action` 是 `created` 还是 `linked`）。

#### 验收结果

| 要求 | 状态 |
|---|---|
| Capability 声明与匹配（无评分/无排名/无信誉） | ✅ |
| AgentDirectory 接口 + 本地实现 | ✅ |
| Task 声明 `required_capabilities`，**自己不查找 Agent** | ✅ 有守卫 |
| TASK_OFFER / COUNTER_OFFER 经 Handler 进入 Task 层 | ✅ |
| `budget.currency == AIC` / `price <= budget` 校验 | ✅ Task 层 |
| B **不自动接单**（处理完状态是 `OFFERED`） | ✅ 有测试 |
| 四个 ID 可互相关联 | ✅ 有测试 |
| 不写死 `agent_b` | ✅ 有守卫（注入 `agent_b` 会被抓到） |
| 不实现 Task Manager / Coordinator / 自动分配 / 选最佳 / Reputation / Ranking / 价格优化 / Workspace Permission / Gateway / Git / Payment / Escrow / P2P | ✅ 有守卫 |
| 旧测试继续通过 | ✅ |

**测试：1168 → 1475（新增 307）**

| 新增 / 修改 | 数量 |
|---|---|
| `test_capability.py` | 77 |
| `test_agent_directory.py` | 84 |
| `test_task_service.py` | 57 |
| `test_capability_guards.py` | 56 |
| `test_task.py`（required_capabilities） | +18 |
| `test_cli.py`（capability-demo） | +12 |
| `test_task_guards.py`（**随 4B 演进**） | +3 |

> **关于 `test_task_guards.py` 的演进**：Step 4A 的守卫写着「一个都不许接入通信层」，
> 因为那时还没到接线的时候。Step 4B 的任务恰恰是接线，所以守卫**有控制地放宽**了：
> 只许 `TASK_OFFER` / `COUNTER_OFFER`，且只能从组合根接；
> `ACCEPT` / `REJECT` / `SUBMIT` / `VERIFY` / `PAYMENT_*` 仍然禁止。
> **保护力没有降低，反而更精确** —— 多接一个类型就会红。

### ✅ Step 4C：低成本自主任务协商与成交（已完成）

**一句话目标**：让 Agent 能以**极低计算成本**完成结构化任务议价，
并把最终决定可靠地落到 Task 状态和成交凭证上；
LLM 是未来可选的「思考器」，而不是每一轮谈判的必经之路。

#### 完整链路

```
Agent A
  ↓
DecisionPolicy（本地规则，零 LLM 调用）
  ↓
Decision（ACCEPT / COUNTER_OFFER / REJECT）
  ↓
TaskService（校验 + 执行）
  ↓
Task Core（改状态）
```

#### DecisionPolicy：三层分离

| 层 | 回答什么问题 | 能不能改状态 |
|---|---|---|
| **DecisionPolicy** | 「我要接受、还价，还是拒绝？」 | ❌ 不能（纯函数） |
| **TaskService** | 「这个决定符合协议规则吗？」 | ⚠️ 只通过 Task Core |
| **Task Core** | 「这个合法决定如何改变任务状态？」 | ✅ 只有它能改 |

**为什么必须这样分**：一次任务可能来回十几轮报价。
如果每轮都调大模型，**谈判成本可能高于任务本身的价值**。
所以第一版用确定性的 `RuleBasedDecisionPolicy`：

```
参数（来自 Agent **自己的** config.yaml）：
    accept_price      接受线（卖家：≥ 就接；买家：≤ 就接）
    opening_ask       第一次还价报多少
    walk_away_price   绝对底线，比它更差直接拒绝
    max_rounds        最多还几次价（防止无限拉扯）

四条规则（按顺序，确定性）：
    1. 已经还过价 且 报价达到接受线        → ACCEPT
    2. 报价比底线更差                      → REJECT
    3. 还价次数用完                        → REJECT
    4. 否则                                → COUNTER_OFFER（按轮次让步）
```

**方向由任务关系决定，不是由 agent_id**：

```python
is_initiator = (task.creator == me)   # 我是买方
# 卖方：价越高越好   买方：价越低越好
```

这样换成任何两个 Agent 都成立 —— 代码里**没有** `if agent_id == "agent_b"`（有守卫）。

#### 为什么默认不接受第一个报价

因为 `TASK_OFFER.budget` 是**上限**，不是报价：

```
买方说「我最多出 100」  ≠  「我出 100」
```

卖方收到 100 的预算，正确做法是**报出自己的价格**（60），而不是按 100 成交。
想「对方给够就立刻拿下」的 Agent 把 `accept_first_offer` 设成 True 即可 —— 那是它的选择。

#### 议价示例（negotiation-demo 场景 1）

```
A → TASK_OFFER      budget = 100 AIC
B → COUNTER_OFFER   60 AIC
A → COUNTER_OFFER   40 AIC
B → COUNTER_OFFER   25 AIC
A → ACCEPT          25 AIC   ✅ 成交

budget = 100 AIC        ← 预算（上限，没变）
agreed_price = 25 AIC   ← 成交价
```

#### 成交证据链

```
Task（ACCEPTED）
  └── 报价历史（每条都带 message_id / reply_to / from_agent / trace / conversation）
  └── TaskAgreementRecord（不可变，append-only）
        accepted_offer_message_id  ← 能反查到「接受的是哪一条报价」
```

**`TaskAgreementRecord` 不是账本，也不是支付记录** ——
它只说「双方在这个时间、通过这条报价，达成了这个价格的协议」。

#### 三条边界（守卫 + 反向测试）

| 边界 | 说明 |
|---|---|
| **ACCEPT ≠ 付款** | 成交后钱包、账本**一个字节都不动** |
| **ACCEPT ≠ Workspace 权限** | 成交不创建 Workspace、不授予任何访问权 |
| **谈判不调 LLM** | 决策层不 import `llm/*`，全程零模型调用 |

#### 角色 / 轮次验证

| 规则 | 目的 |
|---|---|
| 只有参与方能响应 | 防止无关 Agent 替别人的任务做决定 |
| 不能回自己刚发的报价 | 防止一个人代表双方签合同 |
| 终态任务不能再响应 | 防止已拒绝的任务被重新报价 |

判断依据**全部来自任务字段**（`creator` / `contractor` / `participants` /
`offer.from_agent`），没有任何具体 Agent 的名字。

#### 业务拒绝 vs 协议错误

| 情况 | 处理 | 任务状态 |
|---|---|---|
| 价格不合适 → `REJECT` | **业务行为** | → `REJECTED` ✅ |
| 还价超过预算 | **协议违规** | **不变**（回 `ERROR`） |

#### 验收结果（对照 30 条）

| 组 | 状态 |
|---|---|
| 1–5 ACCEPT（直接接受 / 接受还价 / 价格匹配 / agreed_price / 状态） | ✅ |
| 6–7 budget 与 agreed_price 不混淆 | ✅ |
| 8–9 price ≤ budget、AIC 校验 | ✅ |
| 10–15 多轮 NEGOTIATING / 报价历史 / reply_to / message_id / trace / conversation | ✅ |
| 16 角色 / 轮次验证 | ✅ |
| 17–19 ACCEPT 不转账、不建 Workspace、不调 LLM | ✅ 有回归测试 |
| 20–22 DecisionPolicy 独立、不调 LLM、不硬编码 B | ✅ 有守卫 |
| 23–27 无 Task Manager、Task 不依赖通信、通信不依赖 Task、无 Workspace 权限、无 P2P | ✅ 有守卫 |
| 28–30 旧测试通过、新测试通过、negotiation-demo 完成 | ✅ |

**测试：1475 → 1635（新增 160）**

| 新增 | 数量 | 测什么 |
|---|---|---|
| `test_decision_policy.py` | 41 | 四条规则 / 确定性 / 无副作用 / 不调 LLM |
| `test_negotiation.py` | 48 | ACCEPT / COUNTER / REJECT / 角色轮次 / 凭证 / 隔离 |
| `test_negotiation_guards.py` | 58 | 7 类架构守卫 |
| `test_cli.py` | +13 | `negotiation-demo` |

> **守卫又一次演进了**：Step 4B 的守卫只允许接入 `TASK_OFFER` / `COUNTER_OFFER`，
> Step 4C 增加了 `ACCEPT` / `REJECT`。每多接一个类型都必须**显式改守卫**，
> 这正是它的价值。`SUBMIT` / `VERIFY` / `PAYMENT_*` 仍然禁止。

### ✅ 架构重构：Task Application Layer Refactor（已完成）

**不是新功能，是重构。** 目标：让 Step 4C 的代码在进入 4D、Workspace、Git、
Settlement、Agent Life Loop 之后**仍然能继续演化**，而不是每走一步就推翻 TaskService。

#### 问题

Step 4C 结束时 `services/task_service.py` 是 **1453 行**，一个类承担了 **19 项职责**：

```
Message 接收 · 类型判断 · Task 查找/创建/链接 · TASK_OFFER 处理
COUNTER_OFFER 处理 · ACCEPT 处理 · REJECT 处理 · Negotiation 逻辑
DecisionPolicy 调用 · Offer History 写入 · Task 状态变化 · TaskAgreement 创建
角色处理 · 同进程 Task Link · 本地 Task 创建 · 消息业务校验
重复消息防护 · 与 Communication 连接 · payload ↔ Domain 转换
```

继续往里加 `START` / `SUBMIT` / `VERIFY` / `COMPLETE`，它就会变成 **Task God Object**。

#### 重构后

```
                    Communication
                          │
                          ▼
                    TaskService          398 行（代码 158）← 薄门面
                   /     |      \
                  ▼      ▼       ▼
              Handlers  Negotiation  Mapper
              (按类型)   (规则+落地)  (协议↔数据)
                          │
                          ▼
                    DecisionPolicy       ← 只做决定
                          │
                          ▼
                       Decision
                          │
                          ▼
                      Task Core          ← 只有它改状态
                          │
                          ▼
                      TaskStore          ← 只有它碰数据库
```

| 模块 | 总行 | 纯代码 | 唯一职责 | 变了才改它 |
|---|---|---|---|---|
| `task_service.py` | 398 | **158** | 门面 / 路由 / 公开 API | 对外 API 变了 |
| `task_handlers.py` | 508 | 259 | 一种消息一个 Handler | 加消息类型 |
| `task_negotiation_service.py` | 915 | 507 | 议价规则 + 状态落地 | 议价业务变了 |
| `task_message_mapper.py` | 321 | 159 | payload ↔ 应用数据 | 协议变了 |
| `task_result.py` | 142 | 48 | 处理器返回类型 | 返回结构变了 |

**门面从 1453 行 → 398 行（纯代码 1174 → 158，降到 13%）。**

#### 最重要的成果：4D 扩展点

```python
# Step 4D 要加一个消息类型时，只需要：
registry.register(SubmitHandler(negotiation))

# TaskService 一行都不用改。
```

有测试**现场验证**这一点：定义一个假 Handler、注册、`handle_message` 正确路由 ——
门面完全没动。这是本次重构最核心的验收点。

#### 三条边界（守卫 + 8 项反向测试）

| 边界 | 守卫 |
|---|---|
| 门面保持薄 | 纯代码行数 ≤ 250（现在 158）；不许出现 `_accept_locally` 之类实现细节 |
| 无 God Module | 单模块纯代码 ≤ 700；最大模块 ≤ 全包 55% |
| 依赖单向 | `core` 不许 import `services`；`handlers → negotiation` 不能反；`services` 内部无环 |

**守卫覆盖整个 `services/` 包**（原来只看 `task_service.py` —— 那样把逻辑挪到别的文件就能绕过）。

#### 行为 100% 不变

**Refactor, not redesign.** 重构前后 `negotiation-demo` 的输出**逐字节一致**：

```
A → TASK_OFFER      budget = 100 AIC
B → COUNTER_OFFER   60 AIC
A → COUNTER_OFFER   40 AIC
B → COUNTER_OFFER   25 AIC
A → ACCEPT          25 AIC   ✅ 成交
```

**测试：1635 → 1709（新增 74）** —— 原有 1635 项**一项没改**，全部继续通过。

### ✅ Step 4D：任务执行 / 提交 / 验收 / 完结（已完成）

**一句话目标**：让「任务已经谈成」第一次真正进入
「另一个 Agent 正在执行、提交、验收、完成」的状态 ——
而**不是**让系统开始跑代码。

#### 完整生命周期

```
ACCEPTED ──START──→ IN_PROGRESS ──SUBMIT──→ SUBMITTED
                        ↑                       │
                        │                  VERIFY│
                        └──── passed=false ─────┤
                                                │passed=true
                                                ↓
                                            VERIFIED ──COMPLETE──→ COMPLETED
```

**返工是正常流程，不是异常。** 交付不合格通常可以修，
所以 `VERIFY(false)` 让任务回到 `IN_PROGRESS`，而不是判死。

#### 四个新消息

| 消息 | 谁发 | 状态变化 | 必需字段 |
|---|---|---|---|
| `START` | 承接方 | `ACCEPTED → IN_PROGRESS` | `task_id` |
| `SUBMIT` | 承接方 | `IN_PROGRESS → SUBMITTED` | `task_id` + **`result_ref`** |
| `VERIFY` | 发起方 | `SUBMITTED → VERIFIED` 或 `→ IN_PROGRESS` | `task_id` + **`passed`** |
| `COMPLETE` | 发起方 | `VERIFIED → COMPLETED` | `task_id` |

**角色边界**：`START`/`SUBMIT` 只能承接方发，`VERIFY`/`COMPLETE` 只能发起方发。
承接方**不能自己宣布「我验收自己通过」** —— 干活的不能既当运动员又当裁判。

#### 议价与执行彻底分开

```
Message
   ↓
Handler（按类型）
   ├──→ TaskNegotiationService   OFFER / COUNTER_OFFER / ACCEPT / REJECT
   └──→ TaskExecutionService     START / SUBMIT / VERIFY / COMPLETE
             ↓
          Task Core（复用 4A 已有的状态机，不建第二套）
             ↓
          TaskStore
```

两者都继承 `TaskApplicationService`（共享幂等 / trace 缝合 / 协议违规回复），
但**互相不知道**。因为：

- **议价怎么谈** 会随 Agent 的智能程度不断演化（Agent 的私有策略）
- **执行怎么走** 由协议和状态机固定（系统硬约束）

混在一起就会出现「想改议价策略时得动执行代码」。

#### SUBMIT 的成果引用

```json
{
  "task_id": "TASK-4D-1",
  "result": {
    "result_type": "code",
    "result_ref": "csv-analysis-v2",
    "summary": "已修复空输入",
    "metadata": {"files": 3}
  }
}
```

> 🔒 **`result_ref` 是逻辑名字，不是权限。**
> 它不是文件路径、不是 URL、不是访问令牌、不是 Git 引用。
> `TaskResultRef` 会**拒绝**长得像路径或 URL 的值 ——
> 否则承接方就能通过提交一个构造过的名字让发起方去读任意文件。

#### VERIFY 是第一版人工验收

`passed` 由发起方**显式**给出。**不会**收到 SUBMIT 就自动跑测试或问模型。

这不是偷懒，是刻意的边界：「什么算通过」是业务判断，
不该由系统替 Agent 决定。未来的自动验收会是**另一个可选组件**，
而不是把这个语义改掉。

**`passed=false` 时必须给出 `feedback`** —— 否则承接方只能盲改。
feedback 落进 `task_executions` 表，所以「上一轮为什么不通过」是**可查询**的。

#### 执行历史（append-only）

新增 `task_executions` 表，主键就是 `message_id`：

- 幂等是**结构性**的 —— 重复投递撞主键，数据库层面就不可能写第二条
- **不发明新 ID** —— 没有 `execution_id` / `revision_id`
- **「第几轮」是算出来的**（数 SUBMIT 有几条），不是存出来的字段，所以不会漂移

#### COMPLETE 不做结算

```
「活干完了」和「钱给了」是两件事。
```

`COMPLETE` 只结束生命周期。没有 Payment / Escrow / Wallet / Ledger 变化 ——
有测试**直接跑完整个流程**然后断言钱包、账本、审计全部不变。

#### 协议版本仍是 1

4D 是 **additive** 变更：只增加新的消息类型，
没有改任何已有类型的语义、字段或形状。有守卫断言这一点。

#### 验收结果（对照 §二十八）

| 组 | 状态 |
|---|---|
| 功能：完整走通 ACCEPTED → … → COMPLETED | ✅ |
| 功能：VERIFY(false) → 返工 → VERIFY(true) | ✅ |
| Message：四种新消息通过统一 Registry 路由 | ✅ 8 种 |
| Domain：复用 4A 状态机，无第二套生命周期 | ✅ 有测试钉住两者不漂移 |
| Negotiation：仍然只负责议价 | ✅ 有守卫 |
| Strategy：DecisionPolicy 保持独立、无副作用 | ✅ |
| Verification：人工验收，不自动执行 | ✅ 有守卫 |
| Result：SUBMIT 有结构化成果引用 | ✅ |
| Economy：Wallet / Ledger 不变 | ✅ 有测试直接验证 |
| Workspace：不创建、不授权、不读写 | ✅ 有守卫 |
| Git：无 | ✅ |
| LLM：不是 4D 必需依赖 | ✅ 有守卫 |
| Reliability：无 retry / queue | ✅ |
| Architecture：门面不变胖、无循环依赖、无 God Module | ✅ 有守卫 |
| 测试：原有 1709 + 新增全部通过 | ✅ |

**测试：1709 → 1915（新增 206）**

| 新增 | 数量 | 测什么 |
|---|---|---|
| `test_task_execution.py` | 52 | 业务规则 / 角色 / 状态 / 返工 / 幂等 |
| `test_execution_handlers.py` | 36 | 消息路由 / 完整生命周期 / 边界 |
| `test_execution_guards.py` | 99 | 10 项架构守卫 |
| `test_cli.py` | +19 | `task-4d-demo` |

**10 项新守卫全部做了反向测试**，全部抓到。

### ✅ Step 4D.1：可靠性 / 并发加固（已完成）

**不是新功能。** 只修两个「不报错但会悄悄变错」的问题，
为进入 Workspace 之前把地基补上。

#### 问题 A：消息去重是进程内的

```
进程运行 → 收到 X → 记住 X → 处理 X
进程重启 → Tracker 丢失
再次收到 X → **系统不知道处理过** → 再处理一遍
```

3.x 时重复处理一条报价只是「多记一条」。**4D 之后重复处理一条
`SUBMIT` 就是多交付一次成果** —— 而 Workspace 阶段之后还会变成
「多写一个文件」。所以必须修。

**修法**：复用已有的 `communication_messages` 表，新增四个字段：

| 字段 | 含义 |
|---|---|
| `processed_once` | **成功的持久化事实**。0 → 1 之后永不回退 |
| `claim_id` / `claimed_at` | 原子认领（谁在处理） |
| `failure_count` | 失败过几次（**不**表示永久作废） |

> 🔒 **为什么不直接复用 `status` 列？**
> `status` 是「最新状态」，会被后来的事件覆盖：
>
> ```
> 处理成功      → status = PROCESSED
> 又来一条重复  → status = DUPLICATE   ← PROCESSED 被抹掉了
> ```
>
> 用它做去重依据，重复投递反而会把「处理过」这个事实擦掉。
> 所以需要一个**只增不减**的字段。

**原子认领**（避免 TOCTOU race）：

```sql
UPDATE communication_messages
   SET claim_id = ?, claimed_at = ?
 WHERE message_id = ?
   AND processed_once = 0
   AND claim_id IS NULL
-- 影响行数 1 → 我抢到了；0 → 别人在处理，或已经处理过
```

**失败 ≠ 完成**：`mark_failed` 只累加 `failure_count`、记错误、
释放认领 —— `processed_once` **保持 0**。否则一次临时失败
就会让这条消息永远失去被正确处理的机会，而且没人发现。

**内存 tracker 的定位变了**：从「正确性来源」降级为「快路径缓存」。
清空它、换新实例、进程重启 —— 正确性都不受影响（有测试证明）。

#### 问题 B：Task 保存是静默覆盖

```
A 读 task(v=5) → 改成 SUBMITTED
B 读 task(v=5) → 改成 VERIFIED
B 先写成功；A 后写 → 把 VERIFIED 覆盖回 SUBMITTED
两边都以为自己成功了。**不报错**。
```

**修法**：`tasks` 加 `version`，`save()` 变成 compare-and-set：

```sql
UPDATE tasks SET ..., version = version + 1
 WHERE task_id = ? AND version = ?
```

影响行数为 0 → 抛 `ConcurrentTaskModification`。

**不自动重试、不自动重放。** 状态变化有语义，自动重放可能产生
重复状态变化、重复消息，或者替 Agent 做了决定。
本阶段的目标是**正确发现冲突**，不是自动解决所有冲突。

**并发冲突不是 Task 状态** —— 不新增 `CONFLICT` / `PROCESSING` /
`RETRY`。它是 persistence/application 层的问题。

#### 问题 C：状态变化和事件记录不在一个事务里

```python
self.store.record_execution(record)   # 事务 1
self.store.save(task)                 # 事务 2
```

两个事务之间崩溃 → 半成功（事件有、状态没有，或反过来）。

**为什么 4D 没做**：做不到。`Database.transaction()` 不可重入：

```
with db.transaction():
    with db.transaction():   # ❌ cannot start a transaction within a transaction
```

而 `TaskStore` 每个方法都自己开事务。

**修法**：让 `transaction()` **可重入**（按深度判断 BEGIN/COMMIT），
然后把「状态 + 事件」包进一个事务。

| 结果 | 保证 |
|---|---|
| 成功 | ✅ 状态改了 **且** 事件写了 |
| 失败 | ❌ 状态没改 **且** 事件没写 |

#### 验收结果

| 项 | 状态 |
|---|---|
| 处理事实跨进程重启保留 | ✅ 有测试（新 runtime / 新 tracker / 同一个库） |
| 重复消息业务副作用只有一次 | ✅ X/X/X 三次投递只有一次副作用 |
| 并发认领只有一个成功 | ✅ 原子条件 UPDATE |
| 失败不被标成完成 | ✅ `processed_once` 保持 0 |
| stale write 被拒绝、不静默覆盖 | ✅ 抛 `ConcurrentTaskModification` |
| 不自动重试 / 不自动重放 | ✅ 有守卫 + 有测试 |
| 状态 + 事件事务一致 | ✅ 双向失败路径都测了回滚 |
| 协议仍是 v1 | ✅ 全部在存储/应用层解决 |
| Wallet / Ledger 不变 | ✅ |
| 迁移顺序（先列后索引） | ✅ 有专门的测试 |

**测试：1915 → 2140（新增 225）**

| 新增 | 数量 | 测什么 |
|---|---|---|
| `test_message_persistence.py` | 38 | 持久化去重 / 重复 / 重启 / 失败语义 / 降级模式 |
| `test_task_concurrency.py` | 27 | 版本号 / compare-and-set / 冲突 / 不自动重试 |
| `test_transaction_atomicity.py` | 19 | 可重入事务 / 成功路径 / 双向失败回滚 |
| `test_reliability_guards.py` | 131 | 10 组守卫 + 迁移顺序 |
| `test_cli.py` | +10 | `reliability-demo` |

**10 组守卫全部做了反向测试。** 其中两条一开始太弱
（匹配到了自己的方法名 / 文档里的 SQL 示例），加强后才真正抓到。

### ✅ Workspace Phase 0：多进程验证 + 认领租约（已完成）

**不是 Workspace 实现。** 是进 Workspace 之前必须做的两个前置验证
—— 因为 Workspace 会引入**更长的执行任务**和**真实的文件副作用**，
这两件事在单进程假设下都会变成灾难。

#### 任务 A：真实多进程验证 —— 测出了两个真实缺陷

4D.1 的并发设计**只在单进程单连接下验证过**。用真正的
`multiprocessing.Process`（不是线程）跑起来之后，两个缺陷立刻暴露：

**① 写事务用的是 `BEGIN`（延迟事务）→ 多进程下并发保护失效**

```
Process A: BEGIN → SELECT（拿 SHARED）→ UPDATE（想升级成写锁）
Process B: BEGIN → SELECT（拿 SHARED）→ UPDATE（想升级成写锁）
                ↑ 两边都持读锁，都在等对方 → 死锁
```

SQLite 检测到死锁会**立刻**返回 `SQLITE_BUSY`，**不理会 `busy_timeout`**。
于是调用方拿到的是：

```
OperationalError: database is locked      ← 而不是 ConcurrentTaskModification
```

**并发保护在真实多进程下被静默绕过了。** 修法：写事务改用
`BEGIN IMMEDIATE`（事务开始就拿写锁，没有升级步骤）。
只读事务仍然用 `BEGIN`，可以并发读。

**② `PRAGMA journal_mode = WAL` 是排他操作，却在每个连接上无条件执行**

多进程同时连接时互相撞成 `database is locked` —— 甚至在
`connect()` 阶段就炸。修法：先读当前模式，只有不是 WAL 时才切换。

**③ 连接缓存不是 PID 感知的**

`fork` 之后子进程会继承父进程的 `Database._conn`。
SQLite 明确说明跨 fork 复用连接不安全。这在 macOS 上看不出来
（默认 `spawn`，不可能继承），但 Linux 默认 `fork`。

修法：缓存里记 `owner_pid`，PID 不匹配就丢弃重建。

#### 任务 B：认领租约

4D.1 给了 `claim_id` / `claimed_at` —— 但那是**永久**认领。
worker 被 `kill -9` 之后，这个 claim 永远卡在那里：

```
UNCLAIMED ──claim──→ CLAIMED ──success──→ PROCESSED
                        │
                        ├── failure ──→ UNCLAIMED
                        └── lease 过期 → UNCLAIMED     ← Phase 0 新增
```

**回收仍然是一条原子条件 UPDATE**（消除 TOCTOU）：

```sql
UPDATE communication_messages
   SET claim_id = ?, claimed_at = ?
 WHERE message_id = ?
   AND processed_once = 0
   AND (claim_id IS NULL OR claimed_at IS NULL OR claimed_at < ?)
```

> 🔒 **租约只管「谁现在有权处理」，不管「处理成功没有」**
>
> | 租约过期 | 结果 |
> |---|---|
> | 别人可以接手 | ✅ |
> | `processed_once` 不变 | ✅ 成功事实与租约无关 |
> | `failure_count` 不变 | ✅ worker 崩溃 ≠ 业务失败 |
> | 消息不删除 | ✅ |
>
> 有守卫逐条钉住这四条。

**租约可配置**：`config.yaml` 的 `system.reliability.message_claim_lease_seconds`
（默认 300 秒），由组合根注入 `CommunicationRecorder`。
有测试证明配置**真的被读取**（改成 42 → recorder 就是 42）。

#### 验收结果

| 项 | 状态 |
|---|---|
| 两进程打开/读写同一库 | ✅ 真进程、独立连接、都成功 |
| WAL 在子进程生效 | ✅ `journal_mode=wal` |
| 同一消息只有一个认领成功 | ✅ barrier 对齐起跑，2/4 进程都测了 |
| 同一 Task CAS 只有一个成功 | ✅ 另一个抛 `ConcurrentTaskModification` |
| 失败者不覆盖成功者 | ✅ 最终 version 只 +1 |
| 不同 Task 可并行 | ✅ 不会被过度锁死 |
| 压力测试无损坏 | ✅ `PRAGMA integrity_check = ok` |
| 租约未过期不可抢 | ✅ |
| 租约过期可接手 | ✅ `reclaimed=True` + `previous_owner` |
| `processed_once=1` 永久不可抢 | ✅ 即使租约早已过期 |
| 崩溃后租约到期恢复 | ✅ 真进程直接退出，不依赖任何清理 |
| fork 后连接独立 | ✅ 显式测了 `get_context("fork")` |
| 协议仍是 v1 / Wallet 零变化 | ✅ |

**测试：2140 → 2333（新增 193）**

| 新增 | 数量 | 测什么 |
|---|---|---|
| `test_multiprocess_database.py` | 14 | 真进程：打开 / 认领 / CAS / 并行 / 压力 / 回滚 / fork |
| `test_message_claim_lease.py` | 33 | 租约语义 / 边界 / 原子回收 / 崩溃恢复 / 配置 |
| `test_multiprocess_guards.py` | 137 | 11 组守卫 + 迁移 |
| `test_cli.py` | +19 | `reliability-multiprocess-demo` |
| 既有守卫更新 | +9 | 认领条件改成带租约的版本 |

**11 组守卫全部做了反向测试。** 其中一条一开始太弱
（查原文，而文档里为了解释这个修复大量提到了 `BEGIN IMMEDIATE`），
改成查纯代码之后才真正抓到。

### ⏳ 后续阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Task Workspace | 创建 / 授权 / 隔离 / 共享 | ✅ Workspace Core v1 已实现（Permission ACL 仍 `FUTURE`） |
| 结算 | Payment / Escrow / Settlement（可引用 `TaskAgreementRecord`） | 待开始（`FORBIDDEN NOW`） |
| Step 5 | 完整经济交易 Demo（A 外包给 B，结算） | 待开始 |
| Step 6 | Cost Calculator（把 token 用量接到经济系统） | ✅ 已实现（`llm_calls.cost_ai` + `charge_llm_cost`） |

---

## 快速开始

### 环境要求

- macOS（Apple Silicon 或 Intel）—— **当前主要验证平台**
- Linux —— 预期可用，尚未建立完整 CI
- Windows 原生 / WSL —— **未支持**；已知差异与待改清单见
  [`docs/architecture/PLATFORM_COMPATIBILITY.md`](docs/architecture/PLATFORM_COMPATIBILITY.md)
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

**当前阶段可以完全跳过这一步。**
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

**用项目自带的 `./dev.sh test`**（它会自动用 `.venv`，并排除需要真实 API Key 的测试）：

```bash
./dev.sh test                    # 全量（不含真实 API 测试）
./dev.sh test -v                 # 详细输出
./dev.sh test tests/test_ledger.py -v   # 只跑某一部分
```

> ❌ **不要直接敲 `pytest`** —— 那可能用到系统 Python，报
> 「No module named pytest」。原因见 [`COMMANDS.md`](COMMANDS.md) §0。

---

## 项目结构

```
ai-economy/
│
├── README.md                  ← 你在这里
├── COMMANDS.md                ← 命令速查
├── PROTOCOL.md                ← 通信协议规范（Step 3 / 3.5）
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
│   ├── money.py               ←   金额值类型（Step 4A）
│   ├── capability.py          ←   能力声明与匹配（Step 4B）
│   ├── decision_policy.py     ←   议价决策策略（Step 4C）
│   ├── task_agreement.py      ←   成交凭证（Step 4C）
│   ├── task_execution.py      ←   成果引用 / 验收结论 / 执行记录（Step 4D）
│   ├── agent_directory.py     ←   能力发现目录（Step 4B）
│   ├── task.py                ←   Task 领域对象 + 状态机（Step 4A）
│   ├── task_store.py          ←   Task 持久化（Step 4A）
│   ├── task_workspace.py      ←   Task Workspace 逻辑引用（Step 4A）
│   ├── workspace.py           ←   Agent 私有工作空间（有真实文件 I/O）
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
├── llm/                       ← LLM Provider 系统
│   ├── base.py                ←   LLMProvider 契约 + LLMResponse + 异常体系
│   ├── openai_compatible.py   ←   OpenAI 兼容格式的共用基类
│   ├── mock.py                ←   MockProvider（不需要 Key）
│   ├── deepseek.py            ←   DeepSeekProvider（84 行）
│   ├── doubao.py              ←   DoubaoProvider（153 行）
│   ├── router.py              ←   ProviderRegistry + LLMRouter
│   └── recorder.py            ←   调用记录（token 用量）
│
├── services/                  ← Task 应用层（消息 → 领域）
│   ├── task_service.py        ←   门面 / 编排（对外唯一入口）
│   ├── task_handlers.py       ←   8 个处理器 + 注册表（议价 4 + 执行 4）
│   ├── task_application_service.py ← 应用服务基类（幂等/trace/协议违规）
│   ├── task_negotiation_service.py ← 议价规则 + 状态落地（Step 4C）
│   ├── task_execution_service.py   ← 执行/提交/验收/完成（Step 4D）
│   ├── task_message_mapper.py ←   协议 payload ↔ 应用数据
│   └── task_result.py         ←   所有处理器的返回类型
│
├── communication/             ← Agent-to-Agent 通信（Step 3 已完成）
│   ├── message.py             ←   Message（可序列化、可扩展）
│   ├── address.py             ←   Address（与身份分离）
│   ├── transport.py           ←   Transport 抽象接口
│   ├── local_transport.py     ←   LocalTransport（同进程内存）
│   ├── registry.py            ←   AgentRegistry（只存数据）
│   ├── manager.py             ←   CommunicationManager
│   ├── dedup.py               ←   重复消息检测
│   ├── recorder.py            ←   通信日志（Step 3.5）
│   └── errors.py              ←   通信异常体系
├── storage/
│   ├── database.py            ← SQLite 连接、事务、迁移
│   └── models.py              ← 表结构定义
│
├── tests/                     ← 5642 个测试 + 30 个真实 API 测试
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

系统内部经济单位叫 **AI 币**（符号 `AI`）。当前版本用本地 Ledger 模拟，**不接区块链**。

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
| `FEE` | 系统手续费（当前版本不收，留位置） |

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

当前版本不做贷款、抵押、信用、保险、破产重组 —— 但概念保留。

---

## 未来扩展方向

> 以下均为**设计方向记录**，当前版本**不实现**。
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

## 当前版本明确不做的事

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

---

## License

AI Economy OS is licensed under the **Apache License 2.0**.

- [`LICENSE`](LICENSE)
- [`NOTICE`](NOTICE)

Copyright 2026 AI Economy OS contributors.

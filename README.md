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
- [x] 832 个 pytest 测试全绿（另有 30 个真实 API 测试单独跑）

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

#### ⚠️ 一处待你决定的命名冲突

协议示例用 **`AIC`**，但 [`config/config.yaml`](config/config.yaml) 里经济系统配的是 **`AI`**：

```yaml
economy:
  currency: "AI"
```

**本步骤没有改任何一侧**，只在文档里标记。需要在 Step 6（支付）之前统一。

**测试：748 → 832（新增 84）**

| 新增 | 数量 |
|---|---|
| `test_protocol_payloads.py` | 84 |

### ⏳ 后续阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Step 4 | Task / Bid / Contract / Task Workspace | 待开始 |
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
├── tests/                     ← 832 个测试 + 30 个真实 API 测试
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

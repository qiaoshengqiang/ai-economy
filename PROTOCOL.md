# AI Economy OS —— 通信协议规范

> **协议版本**：`1`（`PROTOCOL_VERSION = "1"`）
> **状态**：🟢 **可冻结** —— Step 3.7 收尾后协议不再变动
> **实现程度**：本地双 Agent 可用；跨网络所需的部分**尚未实现**（见 [第 2 节](#2-实现状态诚实版)）
> **最后同步**：Step 3.7（货币代码统一为 `AIC`、ACCEPT 成交价语义、`task_id` ≠ `trace_id`）

---

## 0. 这份文档的定位

### 它是规范，不是教程

本文描述**协议长什么样**（格式、规则、状态机、语义边界），不解释**为什么这样设计**。
设计理由写在各个源文件的文件头注释里。

### 权威来源

**代码是唯一权威。** 本文档如果和代码冲突，以代码为准。

| 内容 | 权威文件 |
|---|---|
| 消息格式、类型、校验、序列化 | [`communication/message.py`](communication/message.py) |
| 地址格式 | [`communication/address.py`](communication/address.py) |
| 传输契约 | [`communication/transport.py`](communication/transport.py) |
| 生命周期状态 | [`communication/recorder.py`](communication/recorder.py) |
| 收发编排、查找顺序 | [`communication/manager.py`](communication/manager.py) |
| 异常体系 | [`communication/errors.py`](communication/errors.py) |
| 日志表结构 | [`storage/models.py`](storage/models.py) |

### 它有守卫，但守卫看的是代码不是这份文档

`tests/test_communication_guards.py` 会强制协议的关键**约定**（类型常量存在、
`Message` 不依赖日志、`Transport` 是抽象类、传输层不自己写日志……）。
那些测试红了，说明协议被破坏了。

**但它们不会检查这份文档的内容。** 也就是说：如果有人改了
`MessageType` 却忘了改本文档，测试**不会**报错 —— 本文档会悄悄过期。

所以改协议时请顺手同步这里。**代码永远是权威。**

---

## 1. 概念分离（本协议的地基）

AI Economy OS 有一条长期原则：

> **Agent 可以拥有资产，但不能因为拥有资产就自动拥有机器权限；
> Agent 可以获得任务，但任务权限必须由 Task Workspace 明确授予；
> Agent 可以修改代码，但修改必须经过受控 Workspace、版本控制和验收流程。**

这条原则落到协议上，就是**五个概念必须分开、任何一个都不能替代另一个**：

| 概念 | 回答什么问题 | 协议里的载体 | 谁能改它 |
|---|---|---|---|
| **Agent Identity** | 我是**谁** | `agent_id`（如 `agent_b`） | 身份系统 |
| **Transport Address** | 通过什么**位置/机制**找到我 | `Address(scheme, endpoint)` | 网络/传输层 |
| **Task** | 我在做**哪件事** | `payload.task_id` | Task 系统（Step 4） |
| **Workspace** | 这件事的**工作空间** | `payload.workspace_id` | Workspace 系统（Step 4+） |
| **Workspace Permission** | 我**被允许**做什么 | **协议中不存在** | Workspace Gateway（未来） |

### 最容易搞混的一条

```
workspace_id  ≠  权限
```

`workspace_id` **只是 Task Workspace 的引用 / 标识**。

Agent 知道一个 `workspace_id`，**不会因此获得任何访问能力**。
它甚至不代表这个 Workspace 存在、不属于别人、或当前有效。

> **本协议中不存在任何「凭 workspace_id 换取访问权」的机制。**
> 未来真正访问 Workspace 必须经过 Workspace Manager / Gateway
> 并配合 Capability / Permission 检查。
> 详见 [第 11 节](#11-task-workspace-的协议边界)。

### 身份 ≠ 地址

```
agent_id   "agent_b"           ← 身份：这个 Agent 是谁（稳定不变）
address    "local://agent_b"   ← 位置：现在怎么找到它（可以变）
           "p2p://12D3KooW..." ← 换机器了，身份不变
```

**任何实现都不得把两者合并。** 合了之后，Agent 一换机器，
账本记录、合同、声誉就全对不上了。

---

## 2. 实现状态（诚实版）

本协议用**三种状态**描述每一项能力：

| 状态 | 含义 |
|---|---|
| ✅ **已实现** | 代码真的会产生 / 处理它，有测试覆盖 |
| ⚠️ **已定义未实现** | 协议定义了语义，但代码里没有任何逻辑 |
| 🔒 **预留** | 只是字段位 / 常量名，连语义都还没定死 |

### 2.1 总览

| 能力 | 状态 | 说明 |
|---|---|---|
| 消息结构 | ✅ 已实现 | 无损序列化往返 |
| 地址格式 | ✅ 已实现 | 4 种 scheme；未知 scheme 不拒绝 |
| 链路 ID（trace / conversation） | ✅ 已实现 | 回复自动继承 |
| 生命周期状态 | ⚠️ **部分** | 11 个已定义，**6 个常规发出**（见 [8.2](#82-定义-vs-实际发出)） |
| 消息类型 | ⚠️ **部分** | 12 个已注册，**4 个在用**（见 [6](#6-消息类型登记表)） |
| 结构化 payload | ⚠️ **已定义未实现** | 本文定义形状，代码**不校验**（见 [7](#7-结构化-payload-规范)） |
| 线上传输 | ⚠️ **仅内存** | 只有 `LocalTransport`，消息不过网络 |
| 投递保证 | ❌ 未实现 | **至多一次**，无 ACK、无重试、无持久化 |
| 消息签名 | 🔒 预留 | 字段位已留（`extensions.signature`） |
| 加密 | 🔒 预留 | —— |
| 重放防护 | 🔒 预留 | 字段位已留（`extensions.nonce`） |
| 超时机制 | 🔒 预留 | 字段位已留（`payload.expires_at`），**不校验** |
| 版本协商 | 🔒 预留 | 有 `protocol_version` 字段，但**无协商逻辑** |
| 节点发现 | 🔒 预留 | 扩展点已留（`Transport.discover()`） |

### 2.2 当前**不能**做什么

以下能力当前**完全不存在**。列出它们是为了防止读者误以为协议已经覆盖：

```
❌ P2P 网络                 ❌ 持久化投递（durable delivery）
❌ ACK 机制                 ❌ 自动重试
❌ 消息加密                 ❌ 消息签名 / 身份验证
❌ 重放攻击防护             ❌ TTL / expires_at 校验
❌ 任务执行授权             ❌ Workspace 权限
❌ 支付 / Escrow            ❌ 自动验收
❌ 多货币                   ❌ 跨节点日志聚合
```

其中若干项**在字段层面已预留**（见上表 🔒 行）——
但「有字段」不等于「有功能」。

---

## 3. 地址格式

### 3.1 语法

```
<scheme>://<endpoint>
```

| Scheme | endpoint 形态 | 状态 |
|---|---|---|
| `local` | agent_id，如 `agent_b` | ✅ 已实现 |
| `p2p` | peer id，如 `12D3KooW...` | 🔒 预留 |
| `ws` | `host:port/path` | 🔒 预留 |
| `https` | `host/path` | 🔒 预留 |
| *其它* | 任意 | 允许（**不是白名单**） |

### 3.2 解析规则

- **必须有 `://`**，否则 `InvalidAddressError`
- **`netloc` 和 `path` 一起保留**：`ws://h:8080/agent_b` → endpoint = `h:8080/agent_b`
  （只取 netloc 会丢路径，导致同一主机的不同 Agent 解析成同一地址）
- **解析出的 `agent_id` 是 endpoint 的占位值** —— 因为地址字符串里不含逻辑身份

### 3.3 双字段语义

```python
Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")
str(addr)       # "p2p://12D3KooW"    ← 只显示位置
addr.to_dict()  # {"agent_id": "agent_b", "scheme": "p2p", "endpoint": "12D3KooW"}
                #  ↑ 两个都留：一个给记账，一个给投递
```

### 3.4 约束

- 地址**不可变**（`frozen=True`）—— 防止注册表里的地址被悄悄改掉
- 可哈希，可作字典键
- `agent_id` 和 `scheme` 都不能为空

### 3.5 地址**不是**什么

| 地址不是 | 说明 |
|---|---|
| 身份 | 身份在 `agent_id` 字段，地址会变 |
| 权限 | 知道地址只意味着「能发消息」，不意味着「能做任何事」 |
| `workspace_id` | 两者完全无关（见 [11](#11-task-workspace-的协议边界)） |

---

## 4. 消息格式

### 4.1 线上格式

```json
{
  "message_id": "msg_e28c2ffb",
  "from_agent": "agent_a",
  "to_agent": "agent_b",
  "message_type": "TEXT",
  "timestamp": "2026-09-26T08:07:50.302347+00:00",
  "payload": {"text": "Hello Agent B"},
  "trace_id": "trc_1b4dd76b",
  "conversation_id": "conv_47b1a2ab",
  "protocol_version": "1"
}
```

带回复时多一个字段：

```json
"reply_to": "msg_3bed087f"
```

### 4.2 字段定义

| 字段 | 类型 | 必需 | 默认 | 说明 |
|---|---|---|---|---|
| `message_id` | string | ✅ | `msg_` + 8 hex | 唯一标识，幂等基础 |
| `from_agent` | string | ✅ | — | 发送方 agent_id |
| `to_agent` | string | ✅ | — | 接收方 agent_id |
| `message_type` | string | ✅ | `TEXT` | 自动转**大写** |
| `timestamp` | string | ✅ | 当前 UTC | ISO 8601，**带时区** |
| `payload` | object | ✅ | `{}` | 业务内容（形状见 [第 7 节](#7-结构化-payload-规范)） |
| `trace_id` | string | ✅ | `trc_` + 8 hex | 一次任务（见 [10](#10-链路与追踪)） |
| `conversation_id` | string | ✅ | `conv_` + 8 hex | 一轮对话 |
| `reply_to` | string | ❌ | 无 | 直接回复哪条消息；**为空时不出现** |
| `protocol_version` | string | ✅ | `"1"` | 版本 |
| `extensions` | object | ❌ | `{}` | 未知字段容器，**不直接出现在线上** |

### 4.3 ID 格式

所有 ID 都是 `<前缀>_<8位十六进制>`：

| 前缀 | 用于 | 示例 |
|---|---|---|
| `msg_` | 消息 | `msg_e28c2ffb` |
| `trc_` | 任务链路 | `trc_1b4dd76b` |
| `conv_` | 对话轮次 | `conv_47b1a2ab` |
| `call_` | LLM 调用（另一层） | `call_3f9a2b1c` |
| `evt_` | 日志事件 | `evt_9c2f...` |

前缀是**可依赖的约定**：`communication-log` 命令就是靠它判断该查什么的。

### 4.4 `payload` 的保留键

以下键在协议层面有**固定含义**，业务方不得另作他用：

| 键 | 访问器 | 含义 | 状态 |
|---|---|---|---|
| `text` | `msg.text` | 文本内容（取不到返回 `""`） | ✅ 已实现 |
| `task_id` | `msg.task_id` | **Task 引用**（逻辑标识） | ✅ 已实现（仅透传） |
| `workspace_id` | `msg.workspace_id` | **Task Workspace 引用**（不是权限！） | ✅ 已实现（仅透传） |
| `budget` | — | 预算上限（金额对象，见 7.1） | ⚠️ 已定义未实现 |
| `price` | — | 协商价格（金额对象，见 7.1） | ⚠️ 已定义未实现 |
| `base_commit` | — | 工作基线版本（见 7.6） | 🔒 预留 |
| `acceptance` | — | 验收条件（见 7.6） | 🔒 预留 |
| `expires_at` | — | 有效期截止（见 7.6） | 🔒 预留 |

> **注意**：`msg.task_id` / `msg.workspace_id` 只是**读取 payload 里对应的键**。
> 通信层**不解释**它们的含义，也不因为它们改变任何行为。
> 它们存在的意义是：未来 Task 层不必发明第二套字段名。

**其余键由消息类型自行约定**，通信层不干预。

---

## 5. 校验规则

### 5.1 信封校验（已实现）

消息在**构造时**校验。不满足即抛 `MessageValidationError`。

| 规则 | 说明 |
|---|---|
| `from_agent` 非空 | |
| `to_agent` 非空 | |
| `message_type` 非空 | |
| `message_id` 非空 | |
| `trace_id` 非空 | 缺了链路就串不起来 |
| `conversation_id` 非空 | 同上 |
| **`from_agent ≠ to_agent`** | 禁止自环 |
| `payload` 是 object | 非字典拒绝 |
| `payload` 为 `None` 时归一成 `{}` | |
| `extensions` 是 object | 非字典拒绝 |

**归一化**（不报错，自动修正）：
- 标识类字段会 `strip()`
- `message_type` 转大写
- `payload` / `extensions` **深拷贝** —— 外部改原字典不影响已构造的消息

**为什么禁止自环**：Agent 之间一旦形成自动回复自环，会无限往返，而每轮都可能花钱调模型。

### 5.2 payload 校验（**未实现，且刻意如此**）

> **通信层不校验 `payload` 的业务形状。**

也就是说，下面这条**当前会被正常接受并投递**：

```json
{"message_type": "TASK_OFFER", "payload": {"budget": 100}}
```

（`budget` 应该是金额对象而不是裸数字，见 [7.1](#71-金额对象)。）

**为什么不在这里拦？**

因为那等于让通信层**解释消息的业务语义**，而这是 Step 3 明令禁止的边界：

```
Communication  只负责「把消息送到」
Task / Economy 负责「解释消息里的含义」
```

如果通信层开始校验 `budget` 的形状，那么：
- 每加一种消息类型都要改通信层
- 通信层会被迫依赖 Task / Economy 的规则
- 两个层次糊在一起，无法独立演进

**那么谁来拦？** Step 4 的 Task 层。
它的类型处理器（`register_type_handler`）是天然的落点：
收到 `TASK_OFFER` 时先校验 payload，不合法就回 `REJECT` 或 `ERROR`。

本文 [第 7 节](#7-结构化-payload-规范) 定义的形状就是给那个校验器用的**规范**。

---

## 6. 消息类型登记表

三种状态严格区分（详见 [第 2 节](#2-实现状态诚实版)）：

### 6.1 ✅ 已实现并使用（4 个）

| 类型 | 用途 | 接收方行为 |
|---|---|---|
| `TEXT` | 普通文本 | 交给处理函数 |
| `PING` | 探活 | 交给处理函数 |
| `PONG` | 探活回复 | 交给处理函数 |
| `ERROR` | 出错 | 交给处理函数 |

> **重要**：这 4 个**都没有特殊处理逻辑** —— 它们和任何未知类型一样，
> 被投递给处理函数就完了。**协议层不解释 `message_type` 的含义。**
>
> 所谓「已实现」只是说：演示和测试里真的在用它们。
> 连内置的「收到 PING 自动回 PONG」都没有 —— 那要自己注册类型处理器（见 6.4）。

### 6.2 ⚠️ 已定义语义、**未实现**（经济消息，8 个）

这些类型的**payload 形状已在 [第 7 节](#7-结构化-payload-规范) 定义**，
但代码里**没有任何逻辑**产生或处理它们。

| 类型 | 方向 | 语义 | payload 形状 |
|---|---|---|---|
| `TASK_OFFER` | 需求方 → 承接方 | 任务邀约（含预算上限） | [7.2](#72-task_offer) |
| `COUNTER_OFFER` | 承接方 → 需求方 | 还价 / 报价 | [7.3](#73-counter_offer) |
| `ACCEPT` | 需求方 → 承接方 | 接受报价（形成成交价） | [7.4](#74-accept--reject) |
| `REJECT` | 双向 | 拒绝 | [7.4](#74-accept--reject) |
| `SUBMIT` | 承接方 → 需求方 | 提交成果 | [7.5](#75-执行阶段start--submit--verify--complete) |
| `VERIFY` | 需求方 → 承接方 | 验收结论 | [7.5](#75-执行阶段start--submit--verify--complete) |
| `PAYMENT_REQUEST` | 收款方 → 付款方 | 请求付款 | 🔒 未定义形状 |
| `PAYMENT_RESULT` | 付款方 → 收款方 | 付款结果 | 🔒 未定义形状 |

> **`PAYMENT_*` 的形状刻意未定义。** 因为支付的前提是验收，
> 验收的前提是 Workspace 权限，现在定义形状只会拍脑袋。等 Step 6。

### 6.3 🔒 预留但**连语义都没有**（0 个）

当前没有这一类。所有已注册类型要么在用，要么已有明确定义的 payload 形状。

### 6.4 类型处理器（扩展点）

```python
manager.register_type_handler("TASK_OFFER", task_manager.on_offer)
```

- **优先级高于** Agent 的通用处理器
- 这是 Step 4 接入任务系统的**唯一入口** —— 不需要改 Agent，也不需要改通信层
- **payload 校验应该发生在这个处理器里**（不合法就回 `REJECT` / `ERROR`）

### 6.5 未知类型

**允许通过。** `MessageType` 不是白名单 —— 否则每加一种类型都要改协议文件。

```python
Message(from_agent="a", to_agent="b", message_type="完全瞎写的类型")   # 照常接受
```

---

## 7. 结构化 payload 规范

> **状态：⚠️ 已定义，未实现。**
> 本文定义形状，**代码不校验**（见 [5.2](#52-payload-校验未实现且刻意如此)）。
> 这份规范是给 Step 4 的校验器用的。

### 7.1 金额对象

**所有涉及钱的字段都用同一个结构**，绝不使用裸数字：

```json
{ "amount": 100, "currency": "AIC" }
```

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `amount` | number | ✅ | 数值。非负数 |
| `currency` | string | ✅ | 货币代码 |

**为什么不能用裸数字：**

```json
"budget": 100          ❌ 100 什么？人民币？AI 币？100 个什么？
"budget": {"amount": 100, "currency": "AIC"}   ✅ 语义完整
```

协议不实现多货币系统，但**结构上必须容得下**。
未来加货币不需要改协议。

#### 本项目的唯一货币代码：`AIC`

```text
AIC = AI Coin
```

**整个项目统一使用 `AIC`**，包括：

| 位置 | 值 |
|---|---|
| [`config/config.yaml`](config/config.yaml) `economy.currency` | `"AIC"` |
| `economy.currency_name` | `"AI Coin"`（名字，不是代码） |
| 协议示例 | `"AIC"` |
| 账本 `ledger_transactions.asset` | `"AIC"` |
| 钱包 `wallets.currency` | `"AIC"` |

> **`currency` 的值必须等于 `economy.currency` 的配置值。**
> Step 3.7 已把两边统一为 `AIC`。

**注意区分「代码」和「名字」：**

- **代码** `AIC` —— 出现在数据里，机器读的
- **名字** `AI Coin` —— 出现在界面和文档里，人读的

「AI Economy」是**项目名**，不是货币代码，不受此约定影响。

**已有的旧数据已迁移**：`wallets.currency` 和 `ledger_transactions.asset`
里残留的 `'AI'` 会被 [`storage/models.py`](storage/models.py) 的
`DATA_MIGRATIONS` 自动改成 `'AIC'`（幂等，每次启动跑一遍是安全的）。

> **为什么这个迁移是必须的**：余额是「按 `asset` 过滤后求和」算出来的。
> 如果代码用 `AIC` 而老数据是 `AI`，就会出现新老 Agent 币种不一致 ——
> **实测后果是转账直接失败**（`WalletError: 币种不一致`）。
> 迁移只改货币符号的写法，**金额一个字节都没动**。

**金额不设精度上限**（协议层不规定小数位）。
精度属于经济系统的事（`economy.amount_precision`）。

### 7.2 `TASK_OFFER`

**方向**：需求方 → 承接方
**含义**：邀请对方承接一个任务，并给出**预算上限**。

```json
{
  "message_id": "msg_offer_001",
  "from_agent": "agent_a",
  "to_agent": "agent_b",
  "message_type": "TASK_OFFER",
  "timestamp": "2026-09-26T09:00:00+00:00",
  "trace_id": "trc_task001",
  "conversation_id": "conv_round1",
  "protocol_version": "1",
  "payload": {
    "task_id": "TASK-001",
    "workspace_id": "WS-001",
    "description": "写一个 CSV 分析程序",
    "budget": {
      "amount": 100,
      "currency": "AIC"
    },
    "required_capabilities": ["python_coding", "data_analysis"],
    "base_commit": "abc123",
    "acceptance": {
      "type": "tests"
    },
    "expires_at": "2026-09-26T10:00:00+00:00"
  }
}
```

| 字段 | 必需 | 说明 |
|---|---|---|
| `task_id` | ✅ | Task 逻辑标识 |
| `workspace_id` | ❌ | Task Workspace 引用（**不是权限**，见 [11](#11-task-workspace-的协议边界)） |
| `description` | ✅ | 人类可读的任务描述 |
| `budget` | ✅ | **金额对象**：预算**上限**（不是成交价，见 7.7） |
| `required_capabilities` | ❌ | 承接方需要具备的能力（Step 4B 新增，见下） |
| `base_commit` | ❌ | 🔒 工作基线版本（见 7.6） |
| `acceptance` | ❌ | 🔒 验收条件（见 7.6） |
| `expires_at` | ❌ | 🔒 报价有效期（见 7.6） |

**第一条消息不需要 `reply_to`**（它不在回复任何东西）。

#### `required_capabilities`（Step 4B 新增）

```json
"required_capabilities": ["python_coding", "data_analysis"]
```

| 性质 | 说明 |
|---|---|
| 可选 | 不传表示「没有特定能力要求」 |
| 语义 | 发起方认为这个任务**需要**哪些能力 |
| **不是** | 权限、认证、或「已找到承接方」的证明 |

**三个必须分清的点：**

1. **它是声明，不是认证**
   发起方说「需要 python_coding」，不代表谁会做；
   承接方声明「我会 python_coding」，也只是**声称**。

2. **它不产生任何权限**
   ```
   Agent 声明 python_coding  ≠  它能访问 WS-001
   ```
   能力（能不能做）和 Workspace 权限（被不被允许做）是两个维度。

3. **匹配是集合运算，不是评分**
   判断标准只有一个：`required ⊆ declared`。
   没有分数、没有排名、没有「最佳人选」。

**谁来做匹配？** 不是通信层，也不是 Task：

```
Task（只声明需要什么）
  ↓
AgentDirectory（谁声明了这些能力？）
  ↓
候选列表（可能是 B、C、D）
  ↓
发起方 Agent **自己决定**用谁
  ↓
TASK_OFFER
  ↓
Communication
```

> 通信层**不解释** `required_capabilities` —— 它只搬运。
> 有测试守着这条线（`communication/` 里不许出现这个字段名）。

### 7.3 `COUNTER_OFFER`

**方向**：承接方 → 需求方（也可以反向用于压价）
**含义**：我还价 / 我报价。

```json
{
  "message_id": "msg_counter_002",
  "from_agent": "agent_b",
  "to_agent": "agent_a",
  "message_type": "COUNTER_OFFER",
  "timestamp": "2026-09-26T09:01:00+00:00",
  "trace_id": "trc_task001",
  "conversation_id": "conv_round1",
  "reply_to": "msg_offer_001",
  "protocol_version": "1",
  "payload": {
    "task_id": "TASK-001",
    "price": {
      "amount": 25,
      "currency": "AIC"
    }
  }
}
```

| 字段 | 必需 | 说明 |
|---|---|---|
| `task_id` | ✅ | 同一个 Task |
| `price` | ✅ | **金额对象**：我要求的**实际价格**（不是预算） |
| `expires_at` | ❌ | 🔒 这个报价的有效期 |
| `reason` | ❌ | 可选的人类可读说明 |

**这条消息的 `message_id` 就是这次报价的标识**（见 [10.4](#104-当前版本用-message_id-标识一次报价)）——
当前版本不引入独立的 `offer_id`。

**业务校验（Step 4B 起在 Task 层执行）：**

| 规则 | 在哪一层 |
|---|---|
| `price.currency == AIC` | Task 层 |
| `price <= TASK_OFFER.budget` | **Task 层**（预算就是上限） |

> 注意第二条**不是**通信规则。通信层继续只负责把消息送到，
> 它不知道「预算」是什么（有守卫测试禁止 `communication/` 出现 `budget` 这个词）。

> **绝不要用 `TEXT` 表达议价。**
>
> ```json
> {"message_type": "TEXT", "payload": {"text": "我接，报价 25"}}   ❌
> ```
>
> 这在 Demo 里没问题，但在真实经济系统里不可接受：
> 「25」是字符串的一部分，机器读不出来，无法自动对账、无法审计、
> 无法作为合同依据。**经济行为必须是机器可读的结构化数据。**

### 7.4 `ACCEPT` / `REJECT`

**`ACCEPT`** —— 接受某个报价，**形成成交价**。

#### 最终成交价的协议规则

```text
ACCEPT.price = 被 ACCEPT 的那条 Offer 所要求的价格
```

展开成两种情况：

```text
如果 reply_to 指向 TASK_OFFER     →  ACCEPT.price = TASK_OFFER.budget
如果 reply_to 指向 COUNTER_OFFER  →  ACCEPT.price = COUNTER_OFFER.price
```

**情况 A：直接接受 `TASK_OFFER`**（不还价，成交价 = 预算）

```
A ──TASK_OFFER (budget = 100 AIC)──→ B
A ←────────ACCEPT (price = 100 AIC)── B
```

```json
{
  "message_type": "ACCEPT",
  "from_agent": "agent_b",
  "to_agent": "agent_a",
  "trace_id": "trc_task001",
  "conversation_id": "conv_round1",
  "reply_to": "msg_task_offer",
  "protocol_version": "1",
  "payload": {
    "task_id": "TASK-001",
    "price": {
      "amount": 100,
      "currency": "AIC"
    }
  }
}
```

> 这里 `price` **等于** `budget`。预算被全额接受，没有任何折让。

**情况 B：接受 `COUNTER_OFFER`**（还价后成交）

```
A ──TASK_OFFER (budget = 100 AIC)──→ B
A ←──COUNTER_OFFER (price = 25 AIC)── B
A ──ACCEPT (price = 25 AIC)────────→ B
```

```json
{
  "message_type": "ACCEPT",
  "from_agent": "agent_a",
  "to_agent": "agent_b",
  "trace_id": "trc_task001",
  "conversation_id": "conv_round1",
  "reply_to": "msg_counter",
  "protocol_version": "1",
  "payload": {
    "task_id": "TASK-001",
    "price": {
      "amount": 25,
      "currency": "AIC"
    }
  }
}
```

> 这里 `price`（25）**不等于** `budget`（100）。还价生效了，
> 预算 100 只是**上限**，实际成交 25。

#### 字段表

| 字段 | 必需 | 说明 |
|---|---|---|
| `task_id` | ✅ | 同一个 Task |
| `price` | ✅ | **成交价**，取值见上面的两种规则 |
| `reply_to` | ✅ | **必须指向被接受的那条报价** |

#### 为什么同时带 `price` 和 `reply_to`

- 只带 `reply_to` → 读日志时要回溯才能知道成交价是多少
- 只带 `price` → 不知道接受的是哪条报价（对方中途改过价就说不清）
- **两个都带** → 自成一条可审计记录

> **协议规则**：`ACCEPT.price` 必须等于 `reply_to` 所指消息的价格
> （`TASK_OFFER` 取 `budget`，`COUNTER_OFFER` 取 `price`）。
> 不一致即为协议违规。
>
> ✅ **Step 4C 起这条规则已在代码里强制**：
> [`services/task_service.py`](services/task_service.py) 的 `handle_accept()`
> 会去报价历史里查 `reply_to` 对应的那条报价，价格不一致就回 `ERROR`
> 并且**不改任务状态**。
>
> 同时 `reply_to` 是**必需的** —— 一条不指向任何已知报价的 ACCEPT 会被拒绝，
> 因为那样就无法回答「双方最终接受的是哪一条报价」。

#### 成交证据链（Step 4C）

一次成交会同时留下三样东西，可以用 `reply_to` 串起来：

```
Task（状态 ACCEPTED，agreed_price = 25 AIC）
  └── 报价历史
        TASK_OFFER      100 AIC   message_id = msg_1
        COUNTER_OFFER    60 AIC   message_id = msg_2   reply_to = msg_1
        COUNTER_OFFER    40 AIC   message_id = msg_3   reply_to = msg_2
        COUNTER_OFFER    25 AIC   message_id = msg_4   reply_to = msg_3
        ACCEPT           25 AIC   message_id = msg_5   reply_to = msg_4  ← 被接受的是这条
  └── 成交凭证 TaskAgreementRecord
        accepted_offer_message_id = msg_4
        trace_id / conversation_id
```

> **成交凭证不是账本，也不是支付记录。**
> 它只说明「双方在这个时间、通过这条报价，达成了这个价格的协议」。
> 付款属于后续步骤，可以引用这条凭证。
> 有架构守卫禁止这张表出现任何支付字段。

**`REJECT`** —— 拒绝：

```json
{
  "message_type": "REJECT",
  "reply_to": "msg_counter_002",
  "payload": {
    "task_id": "TASK-001",
    "reason": "超出预算"
  }
}
```

`reason` 可选、人类可读。**拒绝不需要金额字段。**

> **拒绝是业务行为，不是协议错误。**
> 价格不合适 → `REJECT`，任务进入 `REJECTED`（正常）。
> 还价超过预算 → 那条消息**无效**，回 `ERROR`，任务状态**不变**。
> 两者在 Task 层严格区分（有测试）。

### 7.5 执行阶段：`START` / `SUBMIT` / `VERIFY` / `COMPLETE`

> ✅ **Step 4D 起这一节已在代码里实现**，并且新增了 `START` / `COMPLETE`。
> 这是 **additive** 变更：只增加新类型，没有改任何已有类型的语义或形状，
> 所以 `protocol_version` **仍然是 1**。

**`START`** —— 承接方开工：

```json
{
  "message_type": "START",
  "from_agent": "agent_b",
  "to_agent": "agent_a",
  "reply_to": "msg_accept_003",
  "payload": { "task_id": "TASK-001" }
}
```

`ACCEPTED → IN_PROGRESS`。**只有承接方能发** ——
发起方不能替承接方宣布开工。

**`SUBMIT`** —— 承接方提交成果：

```json
{
  "message_type": "SUBMIT",
  "from_agent": "agent_b",
  "to_agent": "agent_a",
  "reply_to": "msg_accept_003",
  "payload": {
    "task_id": "TASK-001",
    "result": {
      "result_type": "code",
      "result_ref": "csv-analysis-v2",
      "summary": "已修复空输入",
      "metadata": { "files": 3 }
    }
  }
}
```

`IN_PROGRESS → SUBMITTED`。**`result` 是必填的** ——
不能只发「我做好了」，验收需要一个**对象**。

> 🔒 **`result_ref` 是逻辑名字，不是权限**
>
> 它**不是**文件路径、URL、访问令牌、Workspace token、Git 引用。
> `core/task_execution.py` 里的 `TaskResultRef` 会**拒绝**
> 以 `/`、`~`、`./`、`../` 开头或含 `://` 的值。
>
> 为什么要把这条做成硬约束：
> 如果 `result_ref` 长得像路径，下游就会倾向于把它当路径用。
> 一旦真的 `open(result_ref)`，承接方就能通过提交一个精心构造的名字
> **让发起方去读任意文件** —— 一次典型的路径注入。
>
> 早期草案里的 `artifacts` / `result_commit` 字段已由
> `result` 取代（后者更结构化，且不暗示文件系统或 Git 语义）。

**`VERIFY`** —— 需求方给出验收结论：

```json
{
  "message_type": "VERIFY",
  "from_agent": "agent_a",
  "to_agent": "agent_b",
  "reply_to": "msg_submit_004",
  "payload": {
    "task_id": "TASK-001",
    "passed": false,
    "feedback": "接口返回值错误：第 3 个函数在空输入时抛异常"
  }
}
```

- `passed = true`  → `SUBMITTED → VERIFIED`
- `passed = false` → `SUBMITTED → IN_PROGRESS`（**返工**，不是判死）

**只有发起方能发** —— 承接方不能自己宣布「我验收自己通过」。
**`passed=false` 时必须给 `feedback`**，否则承接方只能盲改。

**`COMPLETE`** —— 发起方结项：

```json
{
  "message_type": "COMPLETE",
  "from_agent": "agent_a",
  "to_agent": "agent_b",
  "reply_to": "msg_verify_005",
  "payload": { "task_id": "TASK-001", "summary": "验收通过，正式结束" }
}
```

`VERIFIED → COMPLETED`。**只结束生命周期，不做结算** ——
没有 Payment / Escrow / Wallet / Ledger 变化。

#### 执行链的 reply_to

```
ACCEPT   msg_3
  ↓ START.reply_to    = msg_3
  ↓ SUBMIT.reply_to   = msg_4
  ↓ VERIFY.reply_to   = msg_5
  ↓ COMPLETE.reply_to = msg_6
```

和议价链一样，执行链也可以一路回溯到成交。

> 🔒 **这个形状仍然可能调整。**
> `VERIFY.passed` 目前只是布尔值 —— 真实的验收结论将来可能需要
> 更细的结构（哪条测试失败、证据在哪）。
> 但**第一版刻意保持简单**：验收由发起方显式给出，
> 系统不自动执行测试、不调用模型判断。

### 7.6 预留字段：`base_commit` / `acceptance` / `expires_at`

这三个字段**当前不实现任何功能**，只是把语义定下来，
免得未来又要改协议。

#### `base_commit`

> **这个任务 / 工作结果，是基于哪个代码版本开始的。**

```json
"base_commit": "abc123"
```

**它不是** `current_commit`（当前最新版本），
**也不是** `final_commit`（最终产出）。

**它解决什么问题**：

```
TASK-001
  base_commit = ABC123
       │
       │  B 在干活（可能干很久）
       │
       ▼
  主项目推进到了 DEF456
       │
       ▼
  系统因此知道：
      「B 的工作不是基于最新版本」
```

这是未来多 Agent **并行修改同一项目**、做
`Git Worktree` / `Patch` / `Rebase` / `Merge` / **冲突检测**的基础。

**Git Worktree** 指：为每个 Task 开一个独立的工作副本，
基于 `base_commit` 检出，B 在里面改，产出 patch。
这样多个 Agent 同时改同一个项目不会互相踩。

🔒 **本步骤不实现任何 Git 功能。**

#### `acceptance`

> **什么条件下算这个任务完成了。**

```json
"acceptance": { "type": "tests" }
```

| `type` | 含义 | 状态 |
|---|---|---|
| `tests` | 跑测试通过即算完成 | 🔒 预留 |
| `manual` | 需求方人工判断 | 🔒 预留 |
| `artifact` | 检查产物存在 / 格式 | 🔒 预留 |
| `custom` | 自定义规则 | 🔒 预留 |

**为什么现在就要定义：** 因为未来的支付链是

```
SUBMIT → VERIFY → PASS → SETTLE
```

**没有 `acceptance`，「PASS」就没有依据** —— 而没有依据的支付等于任意转账。

🔒 **本步骤不实现自动验收系统。**

#### `expires_at`

> **这条消息 / 这个业务对象的有效截止时间。**

```json
"expires_at": "2026-09-26T10:00:00+00:00"
```

**为什么要它**：报价不能永久有效。

```
A：100 AIC，30 分钟内有效
   ↓
   （B 一天后才看到）
   ↓
B 接受 —— 但这时 A 的预算可能早就用掉了
```

没有 `expires_at`，「接受一个过期报价」在协议层面就无法被判定为无效。

**语义范围**：它能出现在 `TASK_OFFER` / `COUNTER_OFFER`（报价有效期），
未来也可能出现在其它业务消息上。

🔒 **本步骤不实现任何超时校验。** 当前字段被原样投递，不会被检查。

### 7.7 `budget` ≠ `price`（**必须分清**）

这是本次协议修正里最容易搞错的一条：

| 概念 | 字段 | 谁提 | 含义 |
|---|---|---|---|
| **预算** | `budget` | **需求方**（在 `TASK_OFFER` 里） | **我愿意付的上限** |
| **价格** | `price` | 双方（在 `COUNTER_OFFER` / `ACCEPT` 里） | **实际成交的数** |

> **`budget` 绝不表示成交金额。**

一次完整议价：

```
A ──TASK_OFFER────→ B     budget = 100 AIC        「我最多付 100」
A ←─COUNTER_OFFER── B     price  =  25 AIC        「我要 25」
A ──ACCEPT────────→ B     price  =  25 AIC        「成交，25」

最终成交价 = 25 AIC   （来自 ACCEPT.price，不是 budget）
```

**为什么必须分开**：

1. `budget` 是**约束**（谈判上限），`price` 是**结果**（成交额）
2. 如果混用一个字段，就无法回答「这笔交易是压价成交还是按预算成交的」
3. 未来的经济分析（Agent 的议价能力、利润率）依赖这两个数**分别存在**
4. 账本只能记 `price`；记 `budget` 会凭空多计 75 AIC

**最终成交价的权威来源是 `ACCEPT.price`**（见 7.4）。
`TASK_OFFER` / `COUNTER_OFFER` / `ACCEPT` 三者共同**形成**价格，
但只有 `ACCEPT` 记录**定案**的那一个。

---

## 8. 生命周期状态机

### 8.1 状态图

```
                CREATED
                   │
                 SENT ──────────┐
                   │            │
              DELIVERED ────────┤
                   │            │
              RECEIVED ─────────┤
                   │            │
              PROCESSING ───────┤
                   │            │
              PROCESSED         │
                                ▼
            （任意阶段）      FAILED
                   │
              DUPLICATE
```

### 8.2 定义 vs 实际发出

> **「协议定义了 11 个状态」不等于「Runtime 会发出 11 个状态」。**

**已定义（`MessageStatus` 里的全部 11 个）：**

```
CREATED  QUEUED  SENT  DELIVERED  RECEIVED  PROCESSING  PROCESSED
FAILED   DUPLICATE  TIMEOUT  RETRY
```

**实际会发出的（实测）：**

| 状态 | 何时发出 | 实测 |
|---|---|---|
| `CREATED` | 消息构造完成，交给传输层**之前** | ✅ 成功流程 |
| `SENT` | 传输层接受 | ✅ 成功流程 |
| `DELIVERED` | 进入接收方信箱 | ✅ 成功流程 |
| `RECEIVED` | 从信箱取出 | ✅ 成功流程 |
| `PROCESSING` | 开始调用处理函数 | ✅ 成功流程 |
| `PROCESSED` | 处理函数正常返回 | ✅ 成功流程 |
| `FAILED` | 投递失败 **或** 处理函数抛异常 | ✅ 失败流程 |
| `DUPLICATE` | 同一 `message_id` 再次到达且已处理过 | ✅ 重复流程 |
| `QUEUED` | —— | ❌ **永不发出**（本地传输无独立队列阶段） |
| `TIMEOUT` | —— | ❌ **永不发出**（无 ACK 机制） |
| `RETRY` | —— | ❌ **永不发出**（无重试逻辑） |

> **后三个是「已定义未实现」。** 不要以为日志里会出现它们。
> 未来实现 ACK / 重试后，它们才会开始出现。

**终态**：`PROCESSED` / `FAILED` / `DUPLICATE`

### 8.3 四个易混状态的精确语义

| 状态 | 精确含义 | 谁触发 | 当前实现能力 |
|---|---|---|---|
| `SENT` | **Transport 接受了这条消息，进入发送流程** | 发送方的 Transport | ✅ 严格区分 |
| `DELIVERED` | **目标 Transport / 接收端实际收到了这条消息** | 接收方的 Transport | ⚠️ 与 `SENT` **同一瞬间**（见下） |
| `RECEIVED` | **接收方 Communication Layer 从队列里取到了它** | 接收方 Manager | ✅ 严格区分 |
| `PROCESSED` | **消息处理逻辑执行完毕** | 接收方 Manager | ✅ 严格区分 |

**关于 `DELIVERED` 的诚实说明：**

`LocalTransport` 里，「交给传输层」（`SENT`）和「进入对方信箱」（`DELIVERED`）
是**同一个操作**，两者时间戳几乎相同。

我们仍然记两条，因为：

> **状态机的形状必须和传输方式无关。**

否则「查询已送达消息数」这类语句在本地和网络上的行为就不一样了。

**但必须如实说明**：本地场景下 `DELIVERED` **不增加任何信息量** ——
它只是为了让本地和未来的网络传输共用同一套状态机。
将来 `P2PTransport` 里，`SENT` = 写进 socket、`DELIVERED` = 收到对方 ACK，
那时两者才真正不同。

### 8.4 时间戳映射

| 状态 | 写入消息表的哪一列 |
|---|---|
| `CREATED` | `created_at` |
| `SENT` | `sent_at` |
| `RECEIVED` | `received_at` |
| `PROCESSED` / `FAILED` | `processed_at` |
| `DELIVERED` / 其它 | 无对应列（时间只在事件流里） |

**这个设计的用途**：看哪一列是 `NULL` 就知道卡在哪一步。

---

## 9. 交付语义（Transport 契约）

### 9.1 抽象接口

实现一个新的 Transport 需要覆盖：

| 方法 | 必需 | 签名 |
|---|---|---|
| `start()` | 可选 | `async () -> None` |
| `close()` | **抽象** | `async () -> None` |
| `open_mailbox(agent_id)` | **抽象** | `(str) -> None` |
| `close_mailbox(agent_id)` | **抽象** | `(str) -> None` |
| `has_mailbox(agent_id)` | **抽象** | `(str) -> bool` |
| `send(message, to)` | **抽象** | `async (Message, Address) -> DeliveryReceipt` |
| `receive(agent_id, timeout=None)` | **抽象** | `async (str, float?) -> Message?` |
| `address_for(agent_id)` | **抽象** | `(str) -> Address` |
| `discover(agent_id)` | 可选 | `async (str) -> Address?` |
| `describe()` | 可选 | `() -> dict` |

类属性 `scheme` 必须设置（如 `"local"` / `"p2p"`）。

### 9.2 发送结果 `DeliveryReceipt`

```python
{
  "message_id":   "msg_x",
  "to":           "local://agent_b",     # str(Address)
  "to_agent":     "agent_b",
  "scheme":       "local",
  "accepted":     True,
  "detail":       "已放入信箱",
  "delivered_at": "2026-09-26T08:07:59+00:00"
}
```

### 9.3 接收结果 `ReceiveStatus`

| 值 | 含义 | 是否算错误 |
|---|---|---|
| `delivered` | 成功交给处理函数 | — |
| `duplicate` | 重复消息，已跳过 | **不是错误** |
| `timeout` | 等待超时 | **不是错误**（正常的「暂时没有」） |
| `no_handler` | 收到但无处理函数 | **不是错误** |

**关键约定**：`receive()` 超时返回 `None`，**不抛异常**。

### 9.4 收件人查找顺序

```
1. 注册表里有？              → 用它的 Address
2. 问 Transport.discover()   → 有就登记成远端条目
3. 都没有                    → AgentNotFoundError
```

**第 2 步是 P2P 的唯一接入点。** 有了它，接网络时 Manager 一行都不用改。

### 9.5 投递保证：**at-most-once**（至多一次）

> **当前协议不保证 exactly-once，也不保证 durable delivery。**

明确定义如下：

| 保证 | 提供？ | 说明 |
|---|---|---|
| **at-most-once**（至多一次） | ✅ | 一条消息最多被投递一次；可能**丢失** |
| at-least-once（至少一次） | ❌ | 无 ACK、无重试 |
| exactly-once（恰好一次） | ❌ | 分布式环境下极难，本项目未考虑 |
| durable delivery（持久投递） | ❌ | 队列**仅在内存**；进程退出后未取的消息丢失 |

**具体限制：**

```
❌ 进程退出 → 信箱里没被取走的消息全部丢失
❌ 网络中断 → 无重连、无重发
❌ 接收方崩溃 → 消息丢失，发送方不知道
❌ 无持久化 → 重启后无法补投
```

**这些限制是刻意的** —— Step 3 是本地双 Agent 原型，
先把**协议形状**和**状态机**定对，可靠性机制留给后续步骤。

**但有一条已经为未来准备好了**：见 [9.6](#96-message_id-的幂等语义)。

### 9.6 `message_id` 的幂等语义

> **`message_id` 是单条 Message 的唯一标识。
> 收到相同 `message_id` 时，系统必须把它识别为「同一条消息」，
> 而不是当作一条新的业务消息。**

当前实现：

| 层面 | 行为 |
|---|---|
| 内存去重 | 每个 Agent 一份 `SeenMessageTracker`（有界，默认记住最近 1024 条） |
| 重复到达 | 标记为 `DUPLICATE`，**不交给处理函数**，返回 `ReceiveStatus.duplicate` |
| 日志 | 消息表该 `message_id` **只有一行**（UPSERT）；重复次数体现在事件流里 |

**为什么这是可靠性机制的基础**：

一旦未来有了重试（at-least-once），同一消息必然会被投递多次。
到那时，**幂等去重是唯一能防止「TASK_OFFER 被处理两次」的东西**。
没有它，Agent B 可能会接两次同一个任务、交付两份成果、收两次钱。

当前去重是**进程内、非持久化**的 —— 重启后记忆丢失。
分布式的持久化幂等属于后续步骤。

---

## 10. 链路与追踪

### 10.1 四个标识的职责（**不可互相替代**）

| 字段 | 回答什么问题 | 作用域 | 谁继承 |
|---|---|---|---|
| `task_id` | 这是**哪个业务任务**？ | **跨执行、跨重试** | 由业务层携带（在 `payload` 里） |
| `trace_id` | 这是**哪一次执行 / 协作链路**？ | 一次执行内的多条消息 | 该次执行内所有消息共享 |
| `conversation_id` | 这是**哪一轮对话 / 协商**？ | 一轮往返（几条消息） | 该轮内所有消息共享 |
| `reply_to` | 这条消息**直接回复**的是哪**一条**消息？ | **单条消息** | 不继承 —— 每条各自指向 |

**它们不是彼此的替代品：**

- 只有 `task_id` → 知道属于哪个任务，但不知道是哪次执行、哪条回复哪条
- 只有 `trace_id` → 知道哪次执行，但不知道是哪个业务任务
- 只有 `conversation_id` → 知道哪一轮，但跨轮就看不出关系
- 只有 `reply_to` → 能串成链，但要回溯整条链才知道属于哪次执行

### 10.2 `task_id` ≠ `trace_id`（**必须分清**）

> **不要把 `Task` 和 `trace_id` 写成完全等价的关系。**

| | `task_id` | `trace_id` |
|---|---|---|
| 代表 | 一个业务任务的**稳定身份** | 一次具体的**执行 / 协作链路** |
| 例子 | `TASK-001` | `trc_a1b2c3d4` |
| 生命周期 | **整个任务**从头到尾不变 | **一次执行**结束后就固定了 |
| 存在位置 | `payload`（业务数据） | 信封（通信元数据） |
| 用途 | 业务归属、结算依据 | **日志追踪、链路追踪、故障排查** |

**为什么不能等价 —— 同一个任务可能执行多次：**

```
TASK-001                        ← task_id 始终是它
 ├── trace-A                    ← 第一次执行：跑到一半失败了
 └── trace-B                    ← 第二次执行：重试，成功了
```

如果 `Task` 就等于 `trace_id`，那么重试时 `task_id` 就会变 —— 
**同一个业务任务在账本和日志里变成两个不同的任务**，历史无法归并，
「这个任务到底花了多少钱」也算不出来。

**正确的表述**：

```text
一个 Task 的稳定身份        = task_id
Task 当前正在哪条链路上执行  = trace_id（可以随重试而变）
Task 可以关联当前 trace_id
```

🔒 **当前不实现重试。** 本步骤只把语义定清楚，
免得未来做重试时才发现 `task_id` 被当成了 `trace_id`。

### 10.3 完整示例

```
task_id  = TASK-001                 ← 业务任务（一直在 payload 里）
trace_id = trc_exec_1               ← 这一次执行
│
├─ conversation_id = conv_1         ← 第一轮：议价
│    msg_1  TASK_OFFER      A→B     （无 reply_to —— 它是起点）
│    msg_2  COUNTER_OFFER   B→A     reply_to = msg_1
│    msg_3  ACCEPT          A→B     reply_to = msg_2
│
└─ conversation_id = conv_2         ← 第二轮：交付
     msg_4  SUBMIT          B→A     reply_to = msg_3
     msg_5  VERIFY          A→B     reply_to = msg_4
```

**注意 `msg_4.reply_to = msg_3`** —— 即使换了 `conversation_id`，
`reply_to` 仍然指向链上的上一条。这让整条任务可以被完整重建。

### 10.4 当前版本：用 `message_id` 标识一次报价

> **当前不引入 `offer_id`。**

一条 `TASK_OFFER` 或 `COUNTER_OFFER` 的 **`message_id` 即可唯一标识该次报价消息**。
`reply_to` 指出当前消息**回应 / 接受的是哪一条具体消息**。

于是报价链可以完整重建：

```
TASK_OFFER      message_id = offer-001
COUNTER_OFFER   message_id = offer-002    reply_to = offer-001
ACCEPT                                    reply_to = offer-002
```

多轮还价同样够用：

```
offer-001  TASK_OFFER      budget = 100
offer-002  COUNTER_OFFER   price  =  60    reply_to = offer-001
offer-003  COUNTER_OFFER   price  =  40    reply_to = offer-002
offer-004  COUNTER_OFFER   price  =  30    reply_to = offer-003
ACCEPT                     price  =  30    reply_to = offer-004
```

**为什么现在不需要 `offer_id`：**

- `message_id` 已经是**唯一且不可变**的
- 每条报价消息本来就只表达一次出价 —— 两者是**一一对应**的
- 多一个 ID 就多一个可能不一致的来源
- 每一轮的报价历史都还在（消息都在日志里），可审计

🔒 **未来如果确实需要**（例如一条消息里携带多轮报价、或报价需要脱离消息存在），
再通过**协议版本升级**引入独立的 `offer_id`。
Step 4 不应擅自添加。

### 10.5 继承规则（强制）

`Message.reply()` **必须**继承原消息的 `trace_id` 和 `conversation_id`：

```python
original.trace_id        == reply.trace_id          # 必须相同
original.conversation_id == reply.conversation_id   # 必须相同
original.message_id      == reply.reply_to          # 必须指向原消息
reply.message_id         != original.message_id     # 必须不同
```

同时 `from_agent` / `to_agent` **自动对调**。

**第一条消息不需要 `reply_to`** —— 它不在回复任何东西。

**为什么必须自动继承**：手工写回复极易把 from/to 写反，而在本地测试时不明显；
到了网络上就是消息石沉大海。链路 ID 不继承的话，
一次任务的往返消息在日志里就是互不相干的孤儿。

### 10.6 通过 `manager.send()` 指定

```python
await manager.send("agent_a", "agent_b",
                   text="...", trace_id="trc_task001",
                   conversation_id="conv_round1")
```

不指定则自动生成。

**Step 4 的 Task 应该：**

- 在 `payload.task_id` 里带上**稳定的业务任务身份**
- 给**同一次执行**的所有消息盖上**同一个 `trace_id`**
- 重试时生成**新的 `trace_id`**，但 `task_id` 保持不变（见 10.2）

---

## 11. Task Workspace 的协议边界

> **本步骤不实现 Workspace。这一节只定义边界，防止未来走歪。**

### 11.1 `workspace_id` 是什么

**只是 Task Workspace 的引用 / 标识。**

```json
"workspace_id": "WS-001"
```

它回答「这个任务关联哪个工作空间」，**仅此而已**。

### 11.2 `workspace_id` **不是**什么

| ❌ 它不是 | 为什么 |
|---|---|
| **权限** | 知道 ID 不等于被授权。持有 `WS-001` 不代表你能读它 |
| **文件路径** | 不是 `./workspaces/WS-001`，也不是任何文件系统位置 |
| **网络地址** | 不是 URL，不能当 `Address` 用 |
| **访问令牌** | 不能凭它换取任何访问能力 |
| **存在性保证** | 它可能不存在、可能已被删除、可能不属于这个 Task |

### 11.3 当前的强制措施

| 措施 | 说明 |
|---|---|
| 类型是 `str` | `Message.workspace_id` 只返回字符串，不做任何解析 |
| 无相关 API | `CommunicationManager` / `Transport` / `Message` 上**不存在**任何 `grant` / `permission` / `open_workspace` / `access` 类方法 |
| 通信层不使用它 | 全代码库中 `workspace_id` 只出现在「读取 payload」一处，没有任何行为依赖它 |
| 地址与之无关 | `Address` 只有 `agent_id` / `scheme` / `endpoint` 三个字段 |

### 11.4 未来正确的访问路径

```
Remote Agent
     ↓
Communication        （收到带 workspace_id 的消息 —— 仅此而已）
     ↓
Task                 （这个 workspace_id 属于哪个 Task？当前有效吗？）
     ↓
Workspace Manager    （创建 / 分配 workspace）
     ↓
Workspace Gateway    （鉴权：这个 Agent 对这次访问有 Capability 吗？）
     ↓
受限路径
     ↓
Git Worktree         （基于 base_commit 检出的独立副本）
     ↓
Patch / Commit
     ↓
Tests                （按 acceptance 验收）
     ↓
Merge
```

**绝不应该是：**

```
Remote Agent → 直接访问本地文件系统        ❌
Remote Agent → 凭 workspace_id 换到路径    ❌
```

> **核心原则**：Agent 不能因为「建立了通信连接」或「拥有资产」
> 就自动获得本机文件系统权限。权限必须由 Task 显式授予，
> 并且经过 Gateway 检查。

🔒 **以上全部未实现。** 协议只保证 `workspace_id` 不被误当成权限。

> **Step 4A 的进展（协议未变）**
>
> Step 4A 实现了上面这条边界的**引用侧**：
> [`core/task_workspace.py`](core/task_workspace.py) 的 `TaskWorkspaceRef`
> 只是一个逻辑标识 —— 没有路径、没有 I/O、没有任何权限方法，
> 并且有架构守卫锁住这一点。
>
> Workspace Manager / Gateway / Capability **仍然全部未实现**。
> 协议本身在 Step 4A 里**没有任何改动**。

---

## 12. 安全

### 12.1 当前状态

| 机制 | 状态 |
|---|---|
| 传输加密 | ❌ **未实现** |
| 消息签名 | ❌ **未实现** |
| 身份验证 | ❌ **未实现** |
| 重放防护 | ❌ **未实现** |
| TTL / `expires_at` 校验 | ❌ **未实现**（字段只是被原样投递） |
| 发送方身份校验 | ⚠️ **仅限本地**：发送方必须已在注册表，不能伪造 `from_agent` |

> **⚠️ 请勿误解**：当前**没有任何**消息经过签名或身份验证。
> 本机进程内的任何代码都可以构造一条 `from_agent` 为任意**已注册** Agent 的消息。
> 这是本地原型的可接受状态，但**绝不能**延续到跨网络阶段。

**实测确认**：带假签名的消息会被正常接受，`signature` 原样进入 `extensions`，
**不会被校验**。

### 12.2 预留字段位

以下字段名已确定，**协议版本 1 不实现，但数据结构容得下**：

| 字段 | 未来用途 |
|---|---|
| `signature` | 消息认证 |
| `nonce` | 重放攻击防护 |
| `ttl` | 存活时间 / 过期 |

它们通过 `extensions` 承载（展开到顶层），已测试「转发不丢」。

### 12.3 未来签名应该签什么（**设计原则，现在不实现**）

> **签名应针对「规范化后的完整消息信封（canonical message envelope）」，
> 而不是只签 `payload`。**

未来签名**必须**覆盖：

```
message_id       from_agent       to_agent       message_type
timestamp        payload          trace_id       conversation_id
reply_to         protocol_version
```

**为什么不能只签 payload**：

如果只签 payload，攻击者可以：
- 改 `from_agent` → **冒充他人身份**
- 改 `to_agent` → **改变收件人**
- 改 `timestamp` → **重放 / 篡改时序**
- 改 `trace_id` → **把消息嫁接到别的任务上**

这些字段全都在信封里，不在 payload 里。**只签 payload 等于没签。**

**规范化（canonicalization）的难点**：

JSON 的键顺序、数字格式（`100` vs `100.0`）、Unicode 转义都会影响字节。
签名前必须有一个确定的、双方一致的序列化方式。

🔒 **本步骤不定义 canonicalization，也不实现任何密码学。**
上面这段只是**标记设计方向**，避免未来有人做出「只签 payload」这种错误实现。

### 12.4 日志脱敏（强制，已实现）

**通信日志入口处自动脱敏。** 因为日志会被查询、打印、贴到聊天里求助 ——
一旦 Key 进去就等于泄露，且永久留在数据库里。

**键名命中即脱敏**（转义为 `***REDACTED***`）：

```
api_key  apikey  api-key  secret  password  passwd  pwd
private_key  privatekey  privkey  mnemonic  seed_phrase  seedphrase
authorization  credential  access_token  refresh_token  auth_token
bearer  wallet_key  signing_key
```

**值特征命中即脱敏**：

```
sk-[A-Za-z0-9_\-]{12,}
ark-[A-Za-z0-9_\-]{12,}
(?i)bearer\s+[A-Za-z0-9_\-\.]{12,}
-----BEGIN [A-Z ]*PRIVATE KEY-----
```

**错误信息也脱敏**（异常里可能拼了请求头）。

**明确不脱敏**：`input_tokens` / `output_tokens` / `total_tokens` / `tokens_per_ai_coin`
—— 这些是正当业务字段，一律脱敏会让日志没法看。

**脱敏只发生在日志里**，消息本体不受影响 —— 对方收到的仍是原文。

---

## 13. 通信日志协议

### 13.1 核心约束

> **Message ≠ 日志。** 删掉整张日志表，通信照常工作。

由架构守卫强制：
- `communication/message.py` 不得 import recorder / storage
- `transport.py` / `local_transport.py` 不得自己写日志
- 整个 `communication/` 里**只有 `recorder.py` 能碰数据库**
- `recorder` 是 `CommunicationManager` 的**可选**参数

### 13.2 两张表

**`communication_messages`** —— 每条消息的当前状态（`message_id` 主键，UPSERT）

```
message_id  trace_id  conversation_id
from_agent  to_agent  message_type
created_at  sent_at   received_at   processed_at
transport   address   is_remote
status      retry_count
error_code  error_message
payload_size  payload_hash  payload
protocol_version  reply_to  updated_at
```

**`communication_events`** —— 状态事件流（**append-only**）

```
event_id  message_id  trace_id  conversation_id
status    actor
transport address   detail
error_code  error_message
payload_size  payload_hash
created_at
```

### 13.3 幂等规则

> **相同 `message_id` 只有一行。**

反复投递不会产生无法区分的重复记录。投递次数体现在事件流的 `SENT` 事件条数里。

UPSERT 时用 `COALESCE` 保护前面的时间戳不被后续事件冲成 `NULL`。

### 13.4 payload 处理

| 字段 | 含义 |
|---|---|
| `payload_size` | **原始**字符数（脱敏后） |
| `payload_hash` | 脱敏后内容的 **sha256**（64 位十六进制） |
| `payload` | 完整正文（开发环境）；生产可关（`store_payload=False`） |

**hash 算的是脱敏后的内容**：生产环境可能只留 hash 不留正文；
如果 hash 算原文，它会成为「能否猜出密钥」的旁路。

**hash 与键顺序无关**（序列化时 `sort_keys=True`），
所以可以用来判断「两条消息的 payload 是否相同」。

### 13.5 写入失败策略

默认 `strict=False`：

> **日志写失败只记 warning，不影响通信。**

理由：日志是观测手段，不该成为通信的单点故障。
测试用 `strict=True` 让失败暴露出来。

---

## 14. 版本演进规则

### 14.1 什么时候递增 `protocol_version`

| 变更 | 是否递增 | 做法 |
|---|---|---|
| 加新消息类型 | ❌ 不用 | `MessageType` 不是白名单 |
| 加新 `payload` 字段 | ❌ 不用 | payload 开放 |
| 加新顶层字段 | ❌ 不用 | 收进 `extensions`，前向兼容 |
| **让已定义的 payload 字段变必需** | ⚠️ **要** | 旧消息会被判为非法 |
| **改 payload 字段的类型**（如 `budget: 100` → `budget: {}`） | ✅ **必须** | 破坏性 |
| **删除**已有信封字段 | ✅ 必须 | 破坏性 |
| **改变**已有信封字段的含义 | ✅ 必须 | 破坏性 |
| 改变信封校验规则（更严格） | ✅ 必须 | 旧消息会无法解析 |

### 14.2 payload schema 的演进

[第 7 节](#7-结构化-payload-规范) 里的形状**当前没有被代码校验**，
所以改它们**不会**破坏现有测试。但这不代表可以随便改：

- **加可选字段** → 安全，不用递增
- **把可选字段变成必需** → 要递增（旧发送方会违反新规范）
- **改字段名** → 要递增
- **改金额对象的结构** → 要递增

**建议**：即使不递增 `protocol_version`，
也应在 [7](#7-结构化-payload-规范) 里标注变更日期，
因为经济系统的历史记录需要能追溯「当时用的是什么格式」。

### 14.3 兼容性承诺

- **协议版本 1 的实现，必须能解析并转发带未知字段的消息**
- 未知字段**不得丢弃**（否则未来加签名时，旧节点转发会破坏链路）
- 收到更高 `protocol_version` 时，**当前行为是照常解析**（无协商逻辑，字段仅记录）

---

## 15. 实现一个新的 Transport

### 15.1 清单

```
[ ] 继承 communication.transport.Transport
[ ] 设置类属性 scheme
[ ] 实现 7 个抽象方法：
      close / open_mailbox / close_mailbox / has_mailbox
      send / receive / address_for
[ ] （可选）覆盖 start() —— 绑定端口、建立连接池
[ ] （可选）覆盖 discover() —— 节点发现
[ ] （可选）覆盖 describe() —— 自报状态
[ ] 在组合根（main.py 的 Runtime）里替换 LocalTransport
[ ] 补测试：至少覆盖收发、找不到收件人、超时、关闭后发送
```

### 15.2 `send()` 的约定

**必须区分两种情况**：

| 情况 | 行为 |
|---|---|
| 拿到了响应（含 4xx/5xx） | 返回 `DeliveryReceipt` / 抛 `DeliveryError` |
| 根本没拿到响应（超时、断网） | 抛 `TimeoutError` / `DeliveryError` |

**绝不能**让 `KeyError` / `AttributeError` 这类异常冒到上层 ——
「对方不在」是分布式环境的常态，不是代码 bug。

### 15.3 不需要改的地方

接入新 Transport **不应该**需要改：

- `communication/message.py`
- `communication/manager.py`（查找顺序已支持 `discover()`）
- `communication/registry.py`
- `agents/*`（连 `communication` 包都不 import）
- `core/economy.py` / `core/wallet.py` / `core/ledger.py`

如果发现必须改其中一个，说明协议设计有问题 —— 应该改协议，而不是绕过它。

---

## 16. 异常体系

```
CommunicationError                    ← 基类
├── AgentNotFoundError                  收件人/发送方不存在（常态）
├── InvalidAddressError                 地址格式不对
├── DuplicateAgentError                 同一 agent_id 重复注册
├── MessageValidationError              消息字段非法
├── DuplicateMessageError               重复消息（仅 strict_dedup 模式）
└── TransportError                      ← 传输层错误基类
    ├── DeliveryError                   知道往哪送，但送失败了
    └── TransportClosedError            传输层已关闭
```

**关键约定**：

| 异常 | 是错误吗 | 上层该怎么办 |
|---|---|---|
| `AgentNotFoundError` | 否（常态） | 优雅处理，不要崩溃 |
| `DuplicateMessageError` | 否 | 仅在严格模式下抛出 |
| `DeliveryError` | 是 | 可以重试（未来） |
| `MessageValidationError` | 是 | 发送前就该拦住 |

> 「通信失败 ≠ Agent 崩溃」—— 这是本协议的错误处理总原则。

**注意**：`MessageValidationError` 只覆盖**信封**校验。
payload 的业务校验属于 Task 层，未来应当有独立的异常类型
（大概率是 `TaskValidationError` 之类，**不属于通信层**）。

---

## 17. 附录：一条消息的完整旅程

```
发送方
  │
  ├─ 构造 Message（信封校验 + 生成 message_id / trace_id / conversation_id）
  │      ↓ 日志 CREATED
  │
  ├─ Manager 解析收件人地址（注册表 → discover → 报错）
  │      ↓
  ├─ Transport.send(message, address)
  │      ↓ 日志 SENT
  ├─ 进入接收方信箱
  │      ↓ 日志 DELIVERED
  ┄┄┄┄（本地：以上同瞬间）┄┄┄┄
接收方
  │
  ├─ Transport.receive(agent_id)
  │      ↓ 日志 RECEIVED
  │
  ├─ 去重检查（message_id）
  │      ├─ 已见过 → 日志 DUPLICATE → 结束
  │      └─ 新消息 → 继续
  │
  ├─ 找处理函数（类型处理器 → Agent 处理器 → no_handler）
  │      ↓ 日志 PROCESSING
  │
  ├─ 调用处理函数
  │      │   （Step 4 会在这里校验 payload 形状）
  │      ├─ 抛异常 → 日志 FAILED（含 error_code / error_message）→ 异常上抛
  │      └─ 返回   → 日志 PROCESSED
  │
  └─ 若返回了回复（str / dict / Message）
         ↓ reply() 自动对调收发双方 + 继承链路 ID + 记 reply_to
         回到「发送方」的开头，形成闭环
```

---

## 18. 相关文档

| 文档 | 内容 |
|---|---|
| [`README.md`](README.md) | 项目总览、各步骤设计与验收 |
| [`COMMANDS.md`](COMMANDS.md) | 命令速查（含 `communication-log` 查询用法） |
| 各 `.py` 文件头 | 设计理由（**为什么**这样设计） |

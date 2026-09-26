# AI Economy OS —— 通信协议规范

> **版本**：协议版本 `1`（`PROTOCOL_VERSION = "1"`）
> **状态**：本地双 Agent 可用；跨网络所需的部分尚未实现（见 [第 2 节](#2-实现状态诚实版)）
> **最后同步**：Step 3.5

---

## 0. 这份文档的定位

### 它是规范，不是教程

本文描述**协议长什么样**（格式、规则、状态机），不解释**为什么这样设计**。
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

## 1. 术语

| 术语 | 含义 |
|---|---|
| **Agent** | 经济主体。有身份、钱包、声誉。 |
| **Agent ID** | Agent 的**逻辑身份**。稳定不变，跨机器有效。 |
| **Address** | Agent 的**通信位置**。可以变（换机器、换网络入口）。 |
| **Message** | 一次通信的数据单元。可序列化成 JSON。 |
| **Transport** | 把 Message 送到 Address 的机制。 |
| **Envelope** | *（本协议暂未使用）* 传输层元数据外层。 |
| **trace_id** | 串起**一次完整任务**的 ID。 |
| **conversation_id** | 串起**一轮对话**的 ID。 |
| **Journal** | 通信日志。观测用，不参与通信。 |

### 身份 ≠ 地址

这是本协议最重要的一条约束：

```
agent_id   "agent_b"           ← 身份：这个 Agent 是谁
address    "local://agent_b"   ← 位置：现在怎么找到它
           "p2p://12D3KooW..." ← 位置变了，身份不变
```

**任何实现都不得把两者合并。** 合了之后，Agent 一换机器，
账本记录、合同、声誉就全对不上了。

---

## 2. 实现状态（诚实版）

| 能力 | 状态 | 说明 |
|---|---|---|
| 消息结构 | ✅ 已定 | 有序列化往返测试（无损） |
| 地址格式 | ✅ 已定 | 支持多 scheme，未知 scheme 不拒绝 |
| 链路 ID（trace/conversation） | ✅ 已定 | 回复自动继承，有测试 |
| 生命周期状态 | ⚠️ **部分** | 11 个状态，**8 个真在用**，3 个是空位 |
| 消息类型 | ⚠️ **部分** | 12 个类型，**4 个有语义**，8 个只是名字 |
| 线上传输 | ⚠️ **仅内存** | 只有 `LocalTransport`，消息不过网络 |
| 投递保证 | ❌ 未实现 | 无 ACK、无重试、无持久化队列 |
| 消息签名 | ❌ 未实现 | 字段位已留（`extensions`） |
| 加密 | ❌ 未实现 | —— |
| 重放防护 | ❌ 未实现 | 字段位已留（`nonce` / `ttl`） |
| 版本协商 | ❌ 未实现 | 有 `protocol_version` 字段，但无协商逻辑 |
| 节点发现 | ❌ 未实现 | 扩展点已留（`Transport.discover()`） |

**一句话**：形状定了，跨网络的细节没填。

---

## 3. 地址格式

### 3.1 语法

```
<scheme>://<endpoint>
```

| Scheme | endpoint 形态 | 状态 |
|---|---|---|
| `local` | agent_id，如 `agent_b` | ✅ 已实现 |
| `p2p` | peer id，如 `12D3KooW...` | 预留 |
| `ws` | `host:port/path` | 预留 |
| `https` | `host/path` | 预留 |
| *其它* | 任意 | 允许（**不是白名单**） |

### 3.2 解析规则

- **必须有 `://`**，否则 `InvalidAddressError`
- **`netloc` 和 `path` 一起保留**：`ws://h:8080/agent_b` → endpoint = `h:8080/agent_b`
  （只取 netloc 会丢路径，导致同一主机的不同 Agent 解析成同一地址）
- **解析出的 `agent_id` 填成 endpoint 作为占位** —— 因为地址字符串里不含逻辑身份

### 3.3 双字段语义

```python
Address(agent_id="agent_b", scheme="p2p", endpoint="12D3KooW")
str(addr)     # "p2p://12D3KooW"   ← 只显示位置
addr.to_dict()  # {"agent_id": "agent_b", "scheme": "p2p", "endpoint": "12D3KooW"}
                #  ↑ 序列化时两个都留：一个给记账，一个给投递
```

### 3.4 约束

- 地址**不可变**（`frozen=True`）——防止注册表里的地址被悄悄改掉
- 可哈希，可作字典键
- `agent_id` 和 `scheme` 都不能为空

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
| `message_id` | string | ✅ | `msg_` + 8 hex | 唯一标识，用于去重 |
| `from_agent` | string | ✅ | — | 发送方 agent_id |
| `to_agent` | string | ✅ | — | 接收方 agent_id |
| `message_type` | string | ✅ | `TEXT` | 自动转**大写** |
| `timestamp` | string | ✅ | 当前 UTC 时间 | ISO 8601，**带时区** |
| `payload` | object | ✅ | `{}` | 业务内容 |
| `trace_id` | string | ✅ | `trc_` + 8 hex | 一次任务 |
| `conversation_id` | string | ✅ | `conv_` + 8 hex | 一轮对话 |
| `reply_to` | string | ❌ | 无 | 在回哪条消息；**为空时不出现** |
| `protocol_version` | string | ✅ | `"1"` | 版本 |
| `extensions` | object | ❌ | 随消息构造 | 未知字段容器，**不直接出现在线上** |

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

### 4.4 `payload` 的约定字段

| 键 | 访问器 | 用途 |
|---|---|---|
| `text` | `msg.text` | 文本内容（取不到返回 `""`） |
| `task_id` | `msg.task_id` | 关联 Task（Step 4） |
| `workspace_id` | `msg.workspace_id` | 关联 Task Workspace（Step 4） |

其它键由消息类型自行约定。

---

## 5. 校验规则

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
- `from_agent` / `to_agent` / `message_type` / `message_id` 会 `strip()`
- `message_type` 转大写
- `payload` / `extensions` **深拷贝** —— 外部改原字典不影响已构造的消息

**为什么禁止自环**：Agent 之间一旦形成自动回复自环，会无限往返，而每轮都可能花钱调模型。

---

## 6. 消息类型注册表

### 6.1 已用起来的（4 个）

| 类型 | 用途 | 接收方行为 |
|---|---|---|
| `TEXT` | 普通文本 | 交给处理函数 |
| `PING` | 探活 | 交给处理函数 |
| `PONG` | 探活回复 | 交给处理函数 |
| `ERROR` | 出错 | 交给处理函数 |

> **重要**：这 4 个**都没有特殊处理逻辑** —— 它们和任何未知类型一样，
> 被投递给处理函数就完了。协议层不解释 `message_type` 的含义。
>
> 所谓「已用起来」只是说：演示和测试里真的在用它们。
> 连内置的「收到 PING 自动回 PONG」都没有 —— 那要自己注册类型处理器（见 6.3）。

### 6.2 预留类型（8 个，**从未被使用过**）

```
TASK_OFFER       COUNTER_OFFER    ACCEPT          REJECT
SUBMIT           VERIFY           PAYMENT_REQUEST PAYMENT_RESULT
```

这些常量存在，但发送它们**不会触发任何经济行为**。
经济语义属于 Economy 层，不属于通信层。

### 6.3 类型处理器（扩展点）

```python
manager.register_type_handler("PING", lambda msg: "PONG")
```

- **优先级高于** Agent 的通用处理器
- 这样加新消息类型**不需要改 Agent，也不需要改通信层**

### 6.4 未知类型

**允许通过。** `MessageType` 不是白名单 —— 否则每加一种类型都要改协议文件。

---

## 7. 生命周期状态机

### 7.1 状态图

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

### 7.2 状态定义

| 状态 | 写入时机 | 当前是否会触发 |
|---|---|---|
| `CREATED` | 消息构造完成，交给传输层**之前** | ✅ |
| `QUEUED` | 进入待发队列 | ❌ 空位（本地传输无独立队列阶段） |
| `SENT` | 传输层接受 | ✅ |
| `DELIVERED` | 进入接收方信箱 | ✅ 本地与 `SENT` 同一瞬间 |
| `RECEIVED` | 从信箱取出 | ✅ |
| `PROCESSING` | 开始调用处理函数 | ✅ |
| `PROCESSED` | 处理函数正常返回 | ✅ |
| `FAILED` | 投递失败 **或** 处理函数抛异常 | ✅ |
| `DUPLICATE` | 同一 `message_id` 再次到达且已处理过 | ✅ |
| `TIMEOUT` | 发出但未在期限内确认 | ❌ 空位（需 ACK 机制） |
| `RETRY` | 正在重试 | ❌ 空位（无重试逻辑） |

**终态**：`PROCESSED` / `FAILED` / `DUPLICATE`

### 7.3 时间戳映射

| 状态 | 写入消息表的哪一列 |
|---|---|
| `CREATED` | `created_at` |
| `SENT` | `sent_at` |
| `RECEIVED` | `received_at` |
| `PROCESSED` / `FAILED` | `processed_at` |
| `DELIVERED` / 其它 | 无对应列（时间只在事件流里） |

**这个设计的用途**：看哪一列是 `NULL` 就知道卡在哪一步。

### 7.4 为什么本地也记 `SENT` 和 `DELIVERED` 两条

本地传输里两者是同一瞬间。但仍然记两条，因为：

> **状态机的形状必须和传输方式无关。**

否则「查询已送达消息数」在本地和网络上行为不一致，日志就没法通用。

---

## 8. 交付语义（Transport 契约）

### 8.1 抽象接口

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

### 8.2 发送结果 `DeliveryReceipt`

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

### 8.3 接收结果 `ReceiveStatus`

| 值 | 含义 | 是否算错误 |
|---|---|---|
| `delivered` | 成功交给处理函数 | — |
| `duplicate` | 重复消息，已跳过 | **不是错误** |
| `timeout` | 等待超时 | **不是错误**（正常的「暂时没有」） |
| `no_handler` | 收到但无处理函数 | **不是错误** |

**关键约定**：`receive()` 超时返回 `None`，**不抛异常**。

### 8.4 收件人查找顺序

```
1. 注册表里有？         → 用它的 Address
2. 问 Transport.discover()  → 有就登记成远端条目
3. 都没有               → AgentNotFoundError
```

**第 2 步是 P2P 的唯一接入点。** 有了它，接网络时 Manager 一行都不用改。

### 8.5 投递保证等级

**当前等级：至多一次（at-most-once），且仅在进程内。**

- 无 ACK
- 无重试
- 无持久化（进程退出后，队列里未取的消息丢失）
- 队列可选有上限（满了抛 `DeliveryError`，不静默丢弃）

---

## 9. 链路与追踪

### 9.1 两个维度

| ID | 粒度 | 一次任务里通常有几个 |
|---|---|---|
| `trace_id` | 一次完整任务 / 业务流程 | **1 个**（所有消息共用） |
| `conversation_id` | 一轮对话 / 协商 | 多个 |

### 9.2 示例

```
任务 TASK-001（trace_id = trc_task001）
  │
  ├─ 议价轮次（conversation_id = conv_1）
  │    TASK_OFFER     A → B
  │    COUNTER_OFFER  B → A
  │    ACCEPT         A → B
  │
  └─ 交付轮次（conversation_id = conv_2）
       SUBMIT         B → A
       VERIFY         A → B
```

### 9.3 继承规则（强制）

`Message.reply()` **必须**继承原消息的 `trace_id` 和 `conversation_id`：

```python
original.trace_id        == reply.trace_id          # 必须相同
original.conversation_id == reply.conversation_id   # 必须相同
original.message_id      == reply.reply_to          # 必须指向原消息
reply.message_id         != original.message_id     # 必须不同
```

同时 `from_agent` / `to_agent` **自动对调**。

**为什么必须自动继承**：手工写回复极易把 from/to 写反，而在本地测试时不明显；到了网络上就是消息石沉大海。链路 ID 不继承的话，一次任务的往返消息在日志里就是互不相干的孤儿。

### 9.4 通过 `manager.send()` 指定

```python
await manager.send("agent_a", "agent_b",
                   text="...", trace_id="trc_task001",
                   conversation_id="conv_round1")
```

不指定则自动生成。

---

## 10. 前向兼容

### 10.1 规则

`Message.from_dict()` **不丢弃未知字段**，一律收进 `extensions`：

```python
raw = {..., "signature": "future-sig", "nonce": "abc", "ttl": 30}
msg = Message.from_dict(raw)
msg.extensions   # {"signature": "future-sig", "nonce": "abc", "ttl": 30}
msg.to_dict()    # 转发时原样带出去
```

### 10.2 为什么

旧版本节点收到新版本消息时：

| 做法 | 后果 |
|---|---|
| 丢弃未知字段 | 转发给第三方时签名就没了，链路断裂 |
| **存进 `extensions`** | 原样保留，转发不丢 |

### 10.3 已知字段清单

```
message_id  from_agent  to_agent  message_type  timestamp
payload     trace_id    conversation_id
reply_to    protocol_version
```

其余一律进 `extensions`。

### 10.4 `extensions` 的线上形态

`extensions` **不作为一个嵌套键出现**，而是**展开到顶层**：

```python
msg.extensions = {"signature": "xxx"}
msg.to_dict()   # {"...", "signature": "xxx"}   ← 顶层
```

这样新字段在旧节点眼里就是「多出来的顶层键」，符合直觉。

---

## 11. 安全

### 11.1 当前状态

| 机制 | 状态 |
|---|---|
| 传输加密 | ❌ 无（本地内存传输） |
| 消息签名 | ❌ 无 |
| 身份验证 | ❌ 无 |
| 重放防护 | ❌ 无 |
| 发送方身份校验 | ⚠️ **仅限本地**：发送方必须已在注册表，不能伪造 `from_agent` |

### 11.2 预留字段位

以下字段名已确定，**协议版本 1 不实现，但数据结构容得下**：

| 字段 | 用途 |
|---|---|
| `signature` | 消息签名 |
| `nonce` | 重放防护 |
| `ttl` | 存活时间 |
| `protocol_version` | 版本协商（字段已在，无协商逻辑） |

它们通过 `extensions` 承载，已测试「转发不丢」。

### 11.3 日志脱敏（强制）

**通信日志入口处自动脱敏。** 因为日志会被查询、打印、贴到聊天里求助 —— 一旦 Key 进去就等于泄露，且永久留在数据库里。

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

## 12. 通信日志协议

### 12.1 核心约束

> **Message ≠ 日志。** 删掉整张日志表，通信照常工作。

由架构守卫强制：
- `communication/message.py` 不得 import recorder / storage
- `transport.py` / `local_transport.py` 不得自己写日志
- 整个 `communication/` 里**只有 `recorder.py` 能碰数据库**
- `recorder` 是 `CommunicationManager` 的**可选**参数

### 12.2 两张表

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

### 12.3 幂等规则

> **相同 `message_id` 只有一行。**

反复投递不会产生无法区分的重复记录。投递次数体现在事件流的 `SENT` 事件条数里。

UPSERT 时用 `COALESCE` 保护前面的时间戳不被后续事件冲成 `NULL`。

### 12.4 payload 处理

| 字段 | 含义 |
|---|---|
| `payload_size` | **原始**字符数（脱敏后） |
| `payload_hash` | 脱敏后内容的 **sha256**（十进制 64 位十六进制） |
| `payload` | 完整正文（开发环境）；生产可关（`store_payload=False`） |

**hash 算的是脱敏后的内容**：生产环境可能只留 hash 不留正文，如果 hash 算原文，它会成为「能否猜出密钥」的旁路。

**hash 与键顺序无关**（序列化时 `sort_keys=True`），所以可以用来判断「两条消息的 payload 是否相同」。

### 12.5 写入失败策略

默认 `strict=False`：

> **日志写失败只记 warning，不影响通信。**

理由：日志是观测手段，不该成为通信的单点故障。
测试用 `strict=True` 让失败暴露出来（否则日志悄悄没写进去，测试还全绿）。

---

## 13. 版本演进规则

### 13.1 什么时候递增 `protocol_version`

| 变更 | 是否递增 | 做法 |
|---|---|---|
| 加新消息类型 | ❌ 不用 | `MessageType` 不是白名单 |
| 加新 `payload` 字段 | ❌ 不用 | payload 开放 |
| 加新顶层字段 | ❌ 不用 | 收进 `extensions`，前向兼容 |
| **删除**已有字段 | ✅ 必须 | 破坏性 |
| **改变**已有字段的含义/类型 | ✅ 必须 | 破坏性 |
| 改变校验规则（更严格） | ✅ 必须 | 会让旧消息无法解析 |

### 13.2 兼容性承诺

- **协议版本 1 的实现，必须能解析并转发带未知字段的消息**
- 未知字段**不得丢弃**（否则未来加签名时，旧节点转发会破坏链路）
- 收到更高 `protocol_version` 时，**当前行为是照常解析**（无协商逻辑，字段仅记录）

### 13.3 未来版本的实现须知

如果要加签名/加密，正确的路径是：

1. 新字段通过 `extensions` 传递
2. 实现方在**自己的层**校验签名（不要塞进 `Message` 构造）
3. 递增 `protocol_version`
4. 补一条守卫测试，确认旧节点转发不丢签名

---

## 14. 实现一个新的 Transport

### 14.1 清单

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

### 14.2 `send()` 的约定

**必须区分两种情况**：

| 情况 | 行为 |
|---|---|
| 拿到了 HTTP/网络响应（含 4xx/5xx） | 返回 `HTTPResult` / 抛 `DeliveryError` |
| 根本没拿到响应（超时、断网） | 抛 `LLMTimeoutError` / `DeliveryError` |

**绝不能**让 `KeyError` / `AttributeError` 这类异常冒到上层 ——
「对方不在」是分布式环境的常态，不是代码 bug。

### 14.3 不需要改的地方

接入新 Transport **不应该**需要改：

- `communication/message.py`
- `communication/manager.py`（查找顺序已支持 `discover()`）
- `communication/registry.py`
- `agents/*`（连 `communication` 包都不 import）
- `core/economy.py` / `core/wallet.py` / `core/ledger.py`

如果发现必须改其中一个，说明协议设计有问题 —— 应该改协议，而不是绕过它。

---

## 15. 异常体系

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

> 「通信失败 ≠ Agent 崩溃」——这是本协议的错误处理总原则。

---

## 16. 附录：一条消息的完整旅程

```
发送方
  │
  ├─ 构造 Message（校验 + 生成 message_id / trace_id / conversation_id）
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
  ├─ 去重检查
  │      ├─ 已见过 → 日志 DUPLICATE → 结束
  │      └─ 新消息 → 继续
  │
  ├─ 找处理函数（类型处理器 → Agent 处理器 → no_handler）
  │      ↓ 日志 PROCESSING
  │
  ├─ 调用处理函数
  │      ├─ 抛异常 → 日志 FAILED（含 error_code / error_message）→ 异常上抛
  │      └─ 返回   → 日志 PROCESSED
  │
  └─ 若返回了回复（str / dict / Message）
         ↓ reply() 自动对调收发双方 + 继承链路 ID + 记 reply_to
         回到「发送方」的开头，形成闭环
```

---

## 17. 相关文档

| 文档 | 内容 |
|---|---|
| [`README.md`](README.md) | 项目总览、各步骤设计与验收 |
| [`COMMANDS.md`](COMMANDS.md) | 命令速查（含 `communication-log` 查询用法） |
| 各 `.py` 文件头 | 设计理由（**为什么**这样设计） |

# AI Economy OS —— 命令速查

> 有需要时直接翻这个文件。所有命令都在本项目目录下执行。
>
> 先进入项目目录（只需要做一次）：
>
> ```bash
> cd /Users/qiaoshengqiang/Documents/deepseek-harness/default-workspace/ai-economy
> ```

---

## 目录

- [0. 一句话规则](#0-一句话规则)
- [1. 最常用](#1-最常用)
- [2. 经济操作](#2-经济操作)
- [3. 查看状态](#3-查看状态)
- [4. 跑测试](#4-跑测试)
- [5. 环境与故障排查](#5-环境与故障排查)
- [6. 常用场景（照着做）](#6-常用场景照着做)
- [7. 完整参数参考](#7-完整参数参考)
- [8. 报错对照表](#8-报错对照表)
- [9. 等价写法对照](#9-等价写法对照)

---

## 0. 一句话规则

**所有命令都用 `./dev.sh` 开头，不要直接敲 `python`。**

```bash
./dev.sh xxx        ✅ 正确 —— 自动使用项目专属的 .venv
python xxx          ❌ 错误 —— 会用到系统 Python，报 "No module named pytest"
```

为什么？项目跑在一个隔离的虚拟环境 `.venv` 里。`./dev.sh` 会自动找到它，
你就不需要记「先 source 激活」这件事了。

> `./` 是必须的。它表示「当前目录下的 dev.sh」。
> 只敲 `dev.sh` 会报 `command not found`。

---

## 1. 最常用

这四条覆盖 90% 的日常。

```bash
# 启动系统，看启动横幅
./dev.sh run

# 跑全部测试
./dev.sh test

# 看两个 Agent 现在各有多少钱
./dev.sh balances

# 跑一遍 Step 2 的完整 LLM 链路演示
./dev.sh demo

# 看系统环境是否正常（出问题时先跑这个）
./dev.sh doctor
```

---

## 2. 经济操作

### 转钱（核心命令）

让一个 Agent 向另一个 Agent 付钱。

```bash
./dev.sh transfer <付款方> <收款方> <金额>
```

**例子：**

```bash
# Agent A 付 20 AI 给 Agent B
./dev.sh transfer agent_a agent_b 20

# 简写：a / b
./dev.sh transfer a b 20

# 用显示名（有空格要加引号）
./dev.sh transfer "Agent A" "Agent B" 20

# 小数金额
./dev.sh transfer a b 2.8

# 反向转（B 付给 A）
./dev.sh transfer b a 10.5
```

**带原因和任务号：**

```bash
./dev.sh transfer a b 20 --reason task_payment
./dev.sh transfer a b 20 --reason task_payment --task-id task_001
./dev.sh transfer a b 20 --reason "任务定金"
```

**三种指定 Agent 的写法都行（不区分大小写）：**

| 写法 | 例子 | 说明 |
|---|---|---|
| agent_id | `agent_a` | 精确 |
| 显示名 | `"Agent A"` | 有空格要加引号 |
| 简写 | `a` | 最省事 |

**成功时的输出：**

```
转账成功
----------------------------------------------
  付款方:  Agent A (agent_a)
  收款方:  Agent B (agent_b)
  金额:    20 AI
  原因:    manual_transfer
  交易号:  tx_044d342e1c98
  时间:    2026-09-26T05:36:42.359484+00:00

余额变化
----------------------------------------------
  Agent A:  100 -> 80 AI
  Agent B:  100 -> 120 AI

账本审计: OK   (货币总量 200 AI, 共 3 笔交易)
```

---

## 3. 查看状态

```bash
# 所有 Agent 的余额
./dev.sh balances

# 全部交易流水（默认最多 50 笔）
./dev.sh ledger

# 只看 Agent A 的流水
./dev.sh ledger --agent a
./dev.sh ledger --agent agent_a
./dev.sh ledger --agent "Agent A"

# 只看最近 5 笔
./dev.sh ledger --limit 5

# 对账：检查账本有没有被写坏
./dev.sh audit

# 启动横幅（含汇率、数据库路径等）
./dev.sh run

# 只输出两行余额（脚本友好）
./dev.sh run --quiet
```

**`balances` 的输出：**

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

**`audit` 的输出：**

```
账本对账
----------------------------------------------
  交易笔数:      4
  货币总量:      200 AI
  Agent 钱包合计: 200 AI
  系统金库:      0 AI
  差额:          0 AI
----------------------------------------------
  结果: OK —— 账本一致，没有凭空多出或消失的货币
```

---

## 3.5 LLM 命令（Step 2）

### 跑一遍完整链路演示

```bash
./dev.sh demo
```

它会告诉你「Agent 到底怎么用到模型的」：

```
================================
        AI Economy OS v0.1
          Step 2 Demo
================================

调用链
--------------------------------
  Agent A
    ↓  agent.generate(messages=...)
  Brain
    ↓  provider = 'mock'（来自配置）
  Router
    ↓  按名字查注册表
  Provider
    ↓  MockProvider
  LLMResponse

对话
--------------------------------
User:
  Hello, who are you?

Agent A:
  Mock response generated for Agent A.

Token usage
--------------------------------
  Provider:  mock
  Model:     mock-model-v1
  Input:     87
  Output:    9
  Total:     96
  Latency:   0.06 ms
  Simulated: 是（估算值，不是真实计费数据）

调用记录
--------------------------------
  llm_calls 新增: 1 条
  agent_a 累计: 1 次
  累计 tokens: 96 (输入 87 / 输出 9)

系统状态
--------------------------------
  Agent A 余额: 100 AI（未扣费）
  账本审计: OK  (货币总量 200 AI)
  System status: READY
```

自定义提问、换 Agent：

```bash
./dev.sh demo --prompt "帮我分析这个任务"
./dev.sh demo --agent b
```

### 问一个 Agent 一句话

```bash
./dev.sh ask "你好"
./dev.sh ask --agent b "你能写 Python 吗"
./dev.sh ask --agent a "帮我分析一下这个任务"
```

输出：

```
Agent A  [mock/mock-model-v1]
----------------------------------------------------------
  你: 你好

  Agent A: Mock response generated for Agent A.

Token usage
--------------------------------
  Provider:  mock
  Model:     mock-model-v1
  Input:     92
  Output:    9
  Total:     101

  累计: 2 次调用, 197 tokens
```

### 看有哪些模型提供商

```bash
./dev.sh providers
```

```
LLM Providers
----------------------------------------------------------
  默认 provider: mock

  可直接使用
    deepseek   启用   model=deepseek-chat
    doubao     启用   model=doubao-seed-evolving-latest-version
    mock       启用   model=mock-model-v1  ← 当前默认

  已登记但尚未实现
    grok       需要新建 llm/grok.py

----------------------------------------------------------
  每个 Agent 当前用的大脑：
    agent_a    provider=mock       model=mock-model-v1
    agent_b    provider=mock       model=mock-model-v1
```

注意 `可直接使用` 只表示「代码已实现、配置没关掉」。
能不能真的调通，还取决于你有没有配对应的 API Key 和模型名。

### 查看模型的调用记录

调用记录存在 SQLite 的 `llm_calls` 表里。想直接看：

```bash
./dev.sh shell
```

```python
import sqlite3
conn = sqlite3.connect("data/economy.db")
conn.row_factory = sqlite3.Row
for row in conn.execute(
    "SELECT agent_id, provider, model, total_tokens, latency_ms, "
    "simulated, success, created_at FROM llm_calls ORDER BY rowid DESC LIMIT 10"
):
    print(dict(row))
```

或者用现成的 `Recorder`：

```python
from core.config import load_global_config
from storage.database import create_database
from llm import LLMCallRecorder

db = create_database()
recorder = LLMCallRecorder(db)

print(recorder.summary("agent_a"))   # 汇总 token
print(recorder.recent("agent_a"))    # 最近几条
```

### 用真实的 DeepSeek

**第一步：配 Key**

```bash
cp .env.example .env
open -e .env        # 填入 DEEPSEEK_API_KEY
```

Key 只放在 `.env` 里。`.env` 已被 `.gitignore` 排除，永远不会进 git。

**第二步：临时用一次（不改任何配置）**

```bash
./dev.sh ask "你好，请用一句话介绍自己" --provider deepseek
./dev.sh demo --provider deepseek --prompt "说：好"
```

**第三步：或者把某个 Agent 固定改成用 DeepSeek**

改 `agents/agent_a/config.yaml`：

```yaml
brain:
  provider: "deepseek"      # 从 mock 改成 deepseek
  model: ""                 # 留空就用 llm.providers.deepseek.model
```

然后这个 Agent 以后每次思考都走真实 API —— **Agent 的代码一行都不用改。**

**成功时的输出：**

```
Agent A  [deepseek/deepseek-chat]
----------------------------------------------------------
  你: 你好

  Agent A: 你好！我是 DeepSeek...

Token usage
--------------------------------
  Provider:      deepseek
  Model:         deepseek-chat
  Input tokens:  26
  Output tokens: 9
  Total tokens:  35
  Latency:       812.34 ms
  Simulated:     False
  Token source:  api  (来自 API 的真实用量，可以拿去对账单)
```

**注意 `Token source`：**

| 值 | 含义 |
|---|---|
| `api` | API 返回的真实用量 —— 可以拿去算钱、对账单 |
| `estimated` | 我们估算的（MockProvider）—— 只能做粗略预算 |
| `unknown` | API 没返回，我们也不知道 —— **没有猜，就是不知道** |

**没配 Key 时的提示：**

```
模型调用失败
  LLM provider 'deepseek' 当前不可用：环境变量 DEEPSEEK_API_KEY 未设置或为空。
请复制 .env.example 为 .env，然后填入你的 Key：
    cp .env.example .env
    # 编辑 .env，填入 DEEPSEEK_API_KEY=<你的Key>
```

**Key 错误时：**

```
模型调用失败
  DeepSeek 鉴权失败（HTTP 401）。
通常是这几种原因：
  · DEEPSEEK_API_KEY 的值不对或已失效
  · Key 前后有空格或换行
  · Key 被撤销了
重试没有意义，请检查环境变量。
```

### 用真实的豆包

**第一步：配 Key 和模型名**

```bash
cp .env.example .env
open -e .env
```

豆包要填**两项**（`DOUBAO_API_KEY` 和 `DOUBAO_MODEL`），因为豆包没有固定模型名。

**`DOUBAO_MODEL` 有两种写法，必须二选一：**

| 写法 | 形态 | 说明 |
|---|---|---|
| 模型名称（推荐） | `doubao-seed-1-6-250615` | 直接用，无需创建接入点 |
| 推理接入点 ID | `ep-2024xxxxxxxxx-xxxxx` | 需要先在火山方舟控制台创建并部署 |

在火山方舟控制台的「模型广场」能看到当前可用的模型名。

**第二步：调用**

```bash
./dev.sh ask "你好" --provider doubao
./dev.sh demo --provider doubao
./dev.sh test-real doubao          # 跑完整的真实集成测试
```

**没填模型名时的提示：**

```
模型调用失败
  豆包（火山方舟） 没有配置模型。

豆包需要你指定以下两种之一：

  ① 模型名称（推荐，直接用）
     形如 doubao-seed-1-6-250615、doubao-1-5-pro-32k-250115
     在火山方舟控制台的「模型广场」可以看到当前可用的模型名。

  ② 推理接入点 ID（较老的方式）
     形如 ep-2024xxxxxxxxx-xxxxx
     需要先在控制台「在线推理」里创建接入点，然后复制它的 ID。

填在哪里（二选一即可）：
  · 环境变量 DOUBAO_MODEL
  · 或 config/config.yaml 的 llm.providers.doubao.model
```

#### ⚠️ 豆包的两个坑（都是实测踩到的）

**坑 1：某些模型会「静默忽略」`max_tokens`**

```
传 max_tokens=16  →  实际输出 2586 tokens（finish_reason=length）
```

两个解法，都是改配置、不用改代码：

```yaml
# config/config.yaml → llm.providers.doubao
# 解法 A：换成新字段名
max_tokens_field: "max_completion_tokens"

# 解法 B：关掉思考
extra_body:
  thinking:
    type: disabled
```

> `max_tokens` 和 `max_completion_tokens` 不能同时传，否则 400。

**坑 2：思考的代价极大**

同一句问题（「请用大约三百字介绍一下人工智能的发展历史」）：

| | 输出 tokens | 可见字数 | 耗时 |
|---|---|---|---|
| 带思考（默认） | **1913** | 90 字 | **41.1 秒** |
| 关掉思考 | **64** | 96 字 | 1.8 秒 |

**30 倍 token、22 倍延迟，答案几乎一样。** 想省钱就关掉思考。

### 换成 Grok？

**还没实现。** 改了会得到明确的「怎么做」提示：

```
模型调用被拒绝
----------------------------------------------
  provider 'grok' 还没有实现。
怎么做：
  1. 新建 llm/grok.py
  2. 写一个继承 OpenAICompatibleProvider 的 GrokProvider
  3. 在 llm/router.py 的 build_default_registry() 里注册它
  提示：照抄 llm/doubao.py 就行 —— 它只有 153 行，
        因为 HTTP / 解析 / 错误翻译都在共用基类里。
```

**加一家新厂商只需要三步**，Agent / Brain / Economy 全都不用动。

参考实现的行数对比：

| 文件 | 行数 | 内容 |
|---|---|---|
| `llm/openai_compatible.py` | 704 | 共用：HTTP / 请求构造 / 响应解析 / 错误翻译 |
| `llm/deepseek.py` | 84 | 只有 DeepSeek 的身份 |
| `llm/doubao.py` | 153 | 只有豆包的身份 |

---

## 3.8 通信命令（Step 3）

### 跑一遍 Agent 间通信演示

```bash
./dev.sh communication-demo
```

它会完整显示消息从 A 到 B 再回到 A 的全过程：

```
================================
        AI Economy OS v0.1
   Step 3 Communication Demo
================================

通信链路
----------------------------------------------
  Agent A
    ↓  send TEXT
  CommunicationManager
    ↓  造 Message / 查地址 / 去重
  Transport（LocalTransport, scheme=local）
    ↓  投递到信箱
  Agent B

① 发送
----------------------------------------------
  message_id  : msg_02246ca1
  from        : agent_a
  to          : agent_b
  type        : TEXT
  timestamp   : 2026-09-26T07:44:20.365360+00:00
  reply_to    : None
  scheme      : local

② 接收
----------------------------------------------
  状态        : delivered
  Agent B 收到: Hello Agent B
  自动回复    : msg_e334900c → agent_a

③ 回复到达发送方
----------------------------------------------
  Agent A 收到: Agent B 收到了：「Hello Agent B」
  这是对哪条消息的回复 : msg_02246ca1
  与原始消息 ID 一致   : True

往返消息
----------------------------------------------
  方向                       类型       消息 ID
  agent_a → agent_b        TEXT     msg_02246ca1
  agent_b → agent_a        TEXT     msg_e334900c

统计
----------------------------------------------
  发出      : 2
  已处理    : 2
  重复跳过  : 0
  超时      : 0
  注册表    : ['agent_a', 'agent_b']
  传输层    : LocalTransport （信箱 ['agent_a', 'agent_b']）
```

**注意输出里的最后三行**：全程没有调用任何模型，
也没有涉及任何 AI 币 —— 这正是通信层干净的证据。

可以换发送方、接收方和内容：

```bash
./dev.sh communication-demo --text "任务来了"
./dev.sh communication-demo --sender b --receiver a
./dev.sh communication-demo --timeout 5
```

### 查通信日志（Step 3.5）

出问题时，这是你的第一站。

**看最近的消息：**

```bash
./dev.sh communication-log
```

```
通信日志 —— 最近的消息
------------------------------------------------------------------------------------------------
  时间                   状态          方向                     类型         消息 ID
------------------------------------------------------------------------------------------------
  2026-09-26 07:51:02  PROCESSED   agent_b → agent_a      TEXT       msg_0860eb25
  2026-09-26 07:51:02  PROCESSED   agent_a → agent_b      TEXT       msg_4ad6a166
------------------------------------------------------------------------------------------------
  共 4 条
```

**列出最近的链路：**

```bash
./dev.sh communication-log --traces
```

```
最近的通信链路（trace）
----------------------------------------------------------------------------------------
  trace_id                消息     已处理    失败  最后活动
----------------------------------------------------------------------------------------
  trc_0b327e83             2       2     0  2026-09-26 07:51:02
  trc_9befdd0a             2       2     0  2026-09-26 07:51:02
----------------------------------------------------------------------------------------
```

**看某条链路（一次任务）的全部消息：**

```bash
./dev.sh communication-log trc_0b327e83          # 直接给 ID
./dev.sh communication-log --trace trc_0b327e83  # 显式写法，等价
```

**直接给 ID 时会按前缀自动判断查什么：**

| 前缀 | 查什么 |
|---|---|
| `trc_` | 一条链路（一次任务）的所有消息 |
| `conv_` | 一轮对话 |
| `msg_` | 一条消息的完整事件流 |

所以这三种都能直接用：

```bash
./dev.sh communication-log trc_0b327e83
./dev.sh communication-log conv_64be7b6c
./dev.sh communication-log msg_4ad6a166
```

**看某条消息的完整事件流：**

```bash
./dev.sh communication-log msg_4ad6a166
```

```
消息 msg_4ad6a166
------------------------------------------------------------------------
  trace_id        : trc_0b327e83
  conversation_id : conv_64be7b6c
  from → to       : agent_a → agent_b
  status          : PROCESSED
  transport       : local
  address         : local://agent_b
  is_remote       : False
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

**排错时的用法：**

```bash
./dev.sh communication-log --status FAILED    # 只看失败的
./dev.sh communication-log --agent b          # 只看和 B 相关的
```

**不确定有哪些 ID？** 默认列表显示的是**消息**（`msg_`），
要看**链路**用 `--traces`：

```bash
./dev.sh communication-log --traces           # 列出所有链路
./dev.sh communication-log                    # 列出最近的消息
```

**怎么知道卡在哪一步？** 看哪一列时间戳是空的：

| 现象 | 含义 |
|---|---|
| `status=DELIVERED` 且 `received_at=NULL` | 消息进了对方的信箱，但对方一直没取 |
| `status=FAILED` 且有 `error_message` | 对方取了，但处理时出错 |
| 事件流里有 `DUPLICATE` | 同一条消息被投递了多次 |
| `retry_count > 0` | 重试过（目前还没有重试逻辑，字段先留好） |

**协议细节**（消息字段、状态机、前向兼容规则）见
[`PROTOCOL.md`](PROTOCOL.md)。

### 通信层能做什么 / 不能做什么

| 能做 | 不能做 |
|---|---|
| ✅ 两个 Agent 互相发消息 | ❌ 跨进程 / 跨机器（要等 `P2PTransport`） |
| ✅ 自动回复（返回 str/dict/Message） | ❌ 支付、转账 |
| ✅ 重复消息只处理一次 | ❌ 任务、议价、合同 |
| ✅ 按消息类型注册专门处理器 | ❌ 消息签名 / 加密 |
| ✅ 远端 Agent 登记（只有地址） | ❌ 自动发现节点 |
| ✅ 通信日志（按 message/trace/conversation 查） | ❌ Web 面板 / Prometheus / Grafana |
| ✅ 敏感信息自动脱敏 | ❌ 日志轮转 / 归档 |

---

## 4. 跑测试

```bash
# 全部测试（2140 个，不含真实 API 测试）
./dev.sh test

# 详细输出，显示每个测试的名字
./dev.sh test -v

# 只跑一个文件
./dev.sh test tests/test_ledger.py
./dev.sh test tests/test_wallet.py
./dev.sh test tests/test_transfer.py
./dev.sh test tests/test_economy.py
./dev.sh test tests/test_cli.py
./dev.sh test tests/test_startup.py

# 只跑名字里含某关键词的测试
./dev.sh test -k transfer
./dev.sh test -k insolvent
./dev.sh test -k ledger

# 跑单个测试函数
./dev.sh test tests/test_wallet.py::test_transfer_between_wallets -v

# 测试失败时停下来，并显示完整的错误
./dev.sh test -x --tb=long

# 真实调用 DeepSeek API（需要 Key，会真的花钱）
./dev.sh test-real deepseek
```

**默认测试永不联网。** `./dev.sh test` 会排除所有打了 `real_api` 标记的测试 ——
所以不管你有没有 API Key，它都应该稳定通过。

### 真实 API 测试

```bash
./dev.sh test-real deepseek     # 只跑 DeepSeek
./dev.sh test-real doubao       # 只跑豆包
./dev.sh test-real all          # 两家都跑
```

- **有 Key** → 真的调用对应 API，验证真实 usage、真实延迟、整条 Agent 链路
- **没 Key** → 该 provider 的用例被 skip，**不会失败**（另一个 provider 照跑）

它验证的是假 HTTP 层验证不了的东西：

| 验证点 | 为什么必须真调 |
|---|---|
| 真实返回里 `choices[0].message.content` 真的存在吗 | 字段名假设错了只有真调才知道 |
| `usage` 真的叫 `prompt_tokens` / `completion_tokens` / `total_tokens` 吗 | 同上 |
| 我们估算的 token 和真实值差几倍 | 只有对比才能知道估算靠不靠谱 |
| Agent → Brain → Router → DeepSeek 整条链路通不通 | 端到端只有真跑才算数 |

**可用的测试文件：**

| 文件 | 测什么 | 数量 |
|---|---|---|
| `tests/test_address.py` | 身份与地址分离 / 解析 / 不可变性 | 28 |
| `tests/test_agent_directory.py` | 目录 register / find_capable / 不决策 / 可替换实现 | 84 |
| `tests/test_brain.py` | Brain、Agent 接入、调用记录 | 34 |
| `tests/test_capability.py` | 能力声明 / 归一化 / 集合匹配 / 无评分 | 77 |
| `tests/test_capability_guards.py` | Capability 与 Directory 架构守卫 | 56 |
| `tests/test_cli.py` | 命令行命令（通信 / task / capability / 4D / 可靠性） | 122 |
| `tests/test_communication.py` | 完整链路 / 去重 / 可替换传输 / 真 Agent | 44 |
| `tests/test_communication_guards.py` | 通信层架构守卫（含日志边界） | 27 |
| `tests/test_communication_recorder.py` | 通信日志 / 脱敏 / 去重 / 查询 | 86 |
| `tests/test_decision_policy.py` | 决策四条规则 / 确定性 / 无副作用 / 不调 LLM | 41 |
| `tests/test_deepseek_provider.py` | DeepSeek 请求序列化 / 响应解析 / 错误翻译 / Key 安全 | 72 |
| `tests/test_doubao_provider.py` | 豆包特有：模型名两种写法 / 404 / 401 / 429 的含义 | 28 |
| `tests/test_economy.py` | 经济系统、隔离、审计、配置 | 32 |
| `tests/test_execution_guards.py` | 4D 架构守卫（10 项） | 99 |
| `tests/test_execution_handlers.py` | 4D 消息路由 / 完整生命周期 / 边界证明 | 36 |
| `tests/test_ledger.py` | 交易账本、金额校验、金额显示格式 | 21 |
| `tests/test_llm_provider.py` | LLM 契约 / MockProvider / 注册表 / Router / 架构守卫 | 72 |
| `tests/test_local_transport.py` | Transport 抽象 / 收发 / 失败路径 | 33 |
| `tests/test_message.py` | Message 创建 / 校验 / 序列化 / 前向兼容 | 51 |
| `tests/test_message_persistence.py` | 4D.1 消息持久化去重 / 重复 / 重启 / 失败语义 | 38 |
| `tests/test_money.py` | 金额值类型 / 跨货币安全 / 与配置一致 | 40 |
| `tests/test_negotiation.py` | ACCEPT / COUNTER / REJECT / 角色轮次 / 成交凭证 / 隔离 | 48 |
| `tests/test_negotiation_guards.py` | 议价层架构守卫（覆盖整个 services 包） | 59 |
| `tests/test_protocol_payloads.py` | 结构化 payload / ACCEPT 成交价 / task_id≠trace_id / 报价链 | 109 |
| `tests/test_provider_contract.py` | 两家共用的契约（同一批断言参数化跑两遍） | 80 |
| `tests/test_registry.py` | 注册表纯数据 / 本地远端 / 状态分离 | 34 |
| `tests/test_reliability_guards.py` | 4D.1 架构守卫（10 项）+ 迁移顺序 | 131 |
| `tests/test_service_layer_guards.py` | 应用层重构守卫：薄门面 / 无 God Module / 扩展点 / API 兼容 | 77 |
| `tests/test_startup.py` | 启动流程、重启不重复发钱 | 6 |
| `tests/test_task.py` | Task 身份 / 状态机 / budget≠price / 报价链 / 能力要求 / 边界 | 132 |
| `tests/test_task_concurrency.py` | 4D.1 乐观锁 / compare-and-set / 冲突拒绝 / 不自动重试 | 27 |
| `tests/test_task_execution.py` | 4D 业务规则：角色 / 状态 / 返工 / 幂等 / 结果引用 | 52 |
| `tests/test_task_guards.py` | Task Core 架构守卫（八种消息已接入） | 44 |
| `tests/test_task_service.py` | TASK_OFFER handler / 校验 / 链路关联 / 隔离性 | 57 |
| `tests/test_task_store.py` | Task 持久化 / 往返无损 / 报价历史 / 成交凭证 / 执行事件 | 47 |
| `tests/test_task_workspace.py` | Task Workspace：无 I/O、无权限、与私有空间分离 | 55 |
| `tests/test_transaction_atomicity.py` | 4D.1 可重入事务 / 状态+事件原子性 / 双向回滚 | 19 |
| `tests/test_transfer.py` | 转账入口与各种拒绝情况 | 27 |
| `tests/test_wallet.py` | 钱包、余额、扣款 | 15 |
| | **合计** | **2140** |

另有 `tests/test_real_api.py`（30 项）需要真实 API Key，
默认不跑，见下面的「真实 API 测试」。

> 想看每个测试到底测什么，直接打开 `tests/` 目录里的文件 —— 每个测试都有中文注释。

---

## 5. 环境与故障排查

```bash
# 检查环境（Python 版本、依赖、目录、配置文件、数据库）
./dev.sh doctor

# 进入交互式 Python（可以手动试 API）
./dev.sh shell

# 清除数据库和日志，重新开始（会重新发钱）
./dev.sh clean

# 查看帮助
./dev.sh                 # dev.sh 自己的命令列表
./dev.sh --help          # main.py 的命令列表
```

**`./dev.sh doctor` 应该看到：**

```
▶ 环境检查

Python 版本：
Python 3.13.15

解释器路径（必须是 .venv 里的）：
    .../ai-economy/.venv/bin/python        ← 关键是这行

关键依赖：
   ✅ pytest
   ✅ yaml
   ✅ dotenv

项目目录：
   ✅ core/
   ✅ agents/
   ✅ storage/
   ✅ config/
   ✅ tests/

配置文件：
   ✅ config/config.yaml
   ✅ agents/agent_a/config.yaml
   ✅ agents/agent_b/config.yaml
```

**手动重建虚拟环境**（`.venv` 被误删时用）：

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./dev.sh doctor
```

---

## 6. 常用场景（照着做）

### 场景 A：我想看看这个系统长什么样

```bash
./dev.sh clean          # 从零开始
./dev.sh run            # 看启动横幅
```

应该看到 `Agent A / Wallet: 100 AI`、`Agent B / Wallet: 100 AI`、`System status: READY`。

---

### 场景 B：我想做一次转账，然后确认账没错

```bash
./dev.sh balances             # 转账前：A 100, B 100
./dev.sh transfer a b 20      # A 付 20 给 B
./dev.sh balances             # 转账后：A 80,  B 120
./dev.sh audit                # 应该还是 OK，总量 200
```

**重点**：转账**不会改变货币总量**。不管转多少次，`audit` 里「货币总量」永远应该是 200。

---

### 场景 C：我想看看非法操作会不会被挡住

```bash
# 余额不足
./dev.sh transfer a b 99999

# 自己转自己
./dev.sh transfer a a 10

# 负数金额
./dev.sh transfer a b -50

# 不存在的 Agent
./dev.sh transfer agent_z b 10
```

四条都应该输出「操作被经济系统拒绝」+ 一句中文说明，**不能出现 Python 堆栈**。

---

### 场景 D：我想重新开始

```bash
./dev.sh clean
./dev.sh run
```

`clean` 会删掉 `data/economy.db`。下次运行会重新创建两个 Agent 并重新发 100 AI。

---

### 场景 E：我想看 Agent 到底怎么用上模型的

```bash
./dev.sh demo
```

它会一步步显示整条链路：

```
Agent A
  ↓  agent.generate(messages=...)
Brain
  ↓  provider = 'mock'（来自配置）
Router
  ↓  按名字查注册表
Provider
  ↓  MockProvider
LLMResponse
```

然后打印对话内容、token 用量，并确认这次调用**已经记进了数据库**。

想换内容或换 Agent：

```bash
./dev.sh demo --prompt "帮我分析这个任务"
./dev.sh demo --agent b
```

只想随便问一句：

```bash
./dev.sh ask "你好"
./dev.sh ask --agent b "你能写 Python 吗"
```

想看有哪些模型可用：

```bash
./dev.sh providers
```

**注意**：现在只有 `mock` 能用。`deepseek` / `doubao` / `grok`
都显示「需要新建 llm/xxx.py」—— 这是故意的，它们还没实现。

---

### 场景 F：我想改兑换比例

编辑 `config/config.yaml` 第 33 行：

```yaml
economy:
  tokens_per_ai_coin: 10000     # 改成你想要的数字
```

```bash
open -e config/config.yaml     # 用文本编辑器打开
./dev.sh run | grep Exchange   # 确认生效
```

> 注意：改比例**不会**改变已有余额，只影响未来计算模型成本。

**改错了会怎样？** 系统会拒绝启动，并告诉你哪里不对：

```
!! 启动失败 !!
错误类型: ConfigError
错误信息: 配置缺少必需参数：economy.tokens_per_ai_coin
请在 config/config.yaml 里补上它。
```

| 你填的 | 结果 |
|---|---|
| 删掉了这一行 | `配置缺少必需参数：economy.tokens_per_ai_coin` |
| `0` | `economy.tokens_per_ai_coin 必须大于 0，收到：0` |
| `-5` | `economy.tokens_per_ai_coin 必须大于 0，收到：-5` |
| `一万` | `economy.tokens_per_ai_coin 必须是整数，收到：'一万'` |

**这是故意设计的**：经济参数配错了就该立刻停下来，
而不是悄悄用一个代码里写死的数字继续跑 —— 那样你永远说不清账是怎么算的。

---

### 场景 G：我想改初始余额

编辑 `config/config.yaml` 第 37 行：

```yaml
economy:
  initial_balance: 100     # 每个新 Agent 出生时拿多少钱
```

**注意**：每个 Agent 自己的 `config.yaml` 里也有一个 `initial_balance`，
它会**覆盖**全局的值：

```yaml
# agents/agent_a/config.yaml:19
economy:
  initial_balance: 100     # A 自己的出生资金
```

想让 A 和 B 拿不同的启动资金，就分别改这两个文件。
（这正是「Agent 是独立经济主体」的体现。）

改完要重建数据库才生效：

```bash
./dev.sh clean      # 删掉旧账本
./dev.sh run        # 重新创建 Agent 并重新发钱
```

---

### 场景 H：改了配置以后，测试会不会报错？

**正常情况下不会。** 测试和你的配置是解耦的 ——
不管你改 `initial_balance` 还是 `tokens_per_ai_coin`，
`./dev.sh test` 都应该是 `120 passed`。

这一点专门验证过，以下配置组合全部通过：

| 配置 | 结果 |
|---|---|
| 余额 100，比例 10000（默认） | ✅ 120 passed |
| 余额 250，比例 5000 | ✅ 120 passed |
| 余额 250，比例 500 | ✅ 120 passed |
| 余额 7（很小） | ✅ 120 passed |
| 余额 0.5，比例 1000000 | ✅ 120 passed |
| 余额 100000（很大） | ✅ 120 passed |
| 余额 1000000，比例 12345 | ✅ 120 passed |
| 余额 3，比例 1 | ✅ 120 passed |
| 余额 2，比例 10 | ✅ 120 passed |

**如果测试真的因为改配置而失败了，那是 bug，请告诉我。**
测试应该只检验「代码逻辑对不对」，不该检验「你把参数调成了多少」。

> 唯一的例外：如果你删掉了 `tokens_per_ai_coin`，或者把它填成
> `0` / 负数 / 非数字，那属于配置本身不合法，系统会在启动时
> 明确报错（见场景 F）—— 这不算测试失败。

---

### 场景 I：我把代码改坏了，想确认哪里出问题

```bash
./dev.sh test -x --tb=long     # 第一个失败的测试就停下，显示完整错误
./dev.sh doctor                # 检查环境
```

把输出完整复制给我。

---

## 7. 完整参数参考

### `./dev.sh` 自己的命令

| 命令 | 作用 |
|---|---|
| `./dev.sh` | 显示帮助 |
| `./dev.sh test [参数]` | 跑测试（参数透传给 pytest） |
| `./dev.sh run [参数]` | 启动系统（参数透传给 main.py） |
| `./dev.sh clean` | 删除数据库和日志 |
| `./dev.sh shell` | 进入交互式 Python |
| `./dev.sh doctor` | 检查环境 |
| `./dev.sh <其它>` | 自动透传给 `main.py` |

### `main.py` 的命令

| 命令 | 作用 |
|---|---|
| （不加命令） | 启动横幅 |
| `start` | 同上，显式写法 |
| `demo` | **Step 2** 完整 LLM 链路演示 |
| `ask <内容>` | **Step 2** 问某个 Agent 一句话 |
| `providers` | **Step 2** 列出所有 LLM provider |
| `communication-demo` | **Step 3** Agent 间通信链路演示 |
| `task-demo` | **Step 4A** Task Core 演示 |
| `task-log` | **Step 4A** 查看任务 |
| `capability-demo` | **Step 4B** 能力发现与自主委派演示 |
| `negotiation-demo` | **Step 4C** 自主议价演示 |
| `task-4d-demo` | **Step 4D** 执行 / 提交 / 验收演示 |
| `reliability-demo` | **Step 4D.1** 可靠性 / 并发加固演示 |
| `communication-log` | **Step 3.5** 查询通信日志 |
| `transfer <付款方> <收款方> <金额>` | 转钱 |
| `balances` | 看余额 |
| `ledger` | 看流水 |
| `audit` | 对账 |

### 全局参数

| 参数 | 作用 |
|---|---|
| `--db <路径>` | 指定数据库文件（默认 `data/economy.db`） |
| `--quiet` | 只输出最简信息 |
| `-h` / `--help` | 帮助 |

**`transfer` 的专属参数：**

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--reason <文字>` | `manual_transfer` | 转账原因，会写进账本 |
| `--task-id <ID>` | 无 | 关联任务 ID |

**`ledger` 的专属参数：**

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--agent <Agent>` | 全部 | 只看某个 Agent |
| `--limit <数字>` | 50 | 最多显示几笔 |

**`ask` / `demo` 的专属参数：**

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--agent <Agent>` | `agent_a` | 用哪个 Agent |
| `--provider <名字>` | 用 Agent 配置 | 临时指定 LLM provider |
| `--prompt <文字>` | `Hello, who are you?` | 只属于 `demo`，要发的内容 |

**`communication-demo` 的专属参数：**

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--sender <Agent>` | `agent_a` | 发送方 |
| `--receiver <Agent>` | `agent_b` | 接收方 |
| `--text <文字>` | `Hello Agent B` | 要发的内容 |
| `--timeout <秒>` | `2.0` | 等待接收的超时 |

**`communication-log` 的专属参数：**

| 参数 | 作用 |
|---|---|
| `--message <msg_id>` | 看一条消息的完整事件流和时间线 |
| `--trace <trc_id>` | 看一条链路（一次任务）的所有消息 |
| `--conversation <conv_id>` | 看一轮对话 |
| `--traces` | 列出最近的链路 |
| `--agent <Agent>` | 只看与某个 Agent 相关的消息 |
| `--status <状态>` | 只看某个状态（如 `FAILED`） |
| `--limit <数字>` | 最多显示几条（默认 20） |

### 退出码（写脚本时用）

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 程序错误（bug、启动失败） |
| `2` | 被经济规则拒绝（预期内的失败，比如余额不足） |

---

## 8. 报错对照表

| 你看到的报错 | 原因 | 怎么修 |
|---|---|---|
| `No module named pytest` | 没激活虚拟环境，用了系统 Python | 改用 `./dev.sh test` |
| `command not found: dev.sh` | 漏了 `./` | 敲 `./dev.sh` |
| `No such file or directory: dev.sh` | 不在项目目录里 | 先 `cd` 到项目目录 |
| `zsh: permission denied: ./dev.sh` | 脚本没有执行权限 | `chmod +x dev.sh` |
| `找不到虚拟环境：.venv/bin/python` | `.venv` 不存在或被删了 | 见 [第 5 节](#5-环境与故障排查) 重建 |
| `找不到 Agent：'agent_z'` | Agent 名字写错 | 用 `./dev.sh balances` 看可用名字 |
| `操作被经济系统拒绝` | **正常**，经济规则挡住了非法操作 | 看下面的中文说明 |
| `ModuleNotFoundError: No module named 'yaml'` | 依赖没装 | `./.venv/bin/python -m pip install -r requirements.txt` |
| `!! 启动失败 !!` | 启动过程有问题 | 把输出完整发我 |

**注意区分两种「失败」：**

```
操作被经济系统拒绝        ← 正常。规则在起作用，不是 bug。
!! 启动失败 !!            ← 有问题。把输出发我。
```

---

## 9. 等价写法对照

`./dev.sh xxx` 只是让命令变短。它做的事你也能手动做：

| `./dev.sh` 写法 | 等价的完整写法 |
|---|---|
| `./dev.sh test` | `./.venv/bin/python -m pytest` |
| `./dev.sh run` | `./.venv/bin/python main.py` |
| `./dev.sh transfer a b 20` | `./.venv/bin/python main.py transfer a b 20` |
| `./dev.sh balances` | `./.venv/bin/python main.py balances` |
| `./dev.sh doctor` | （脚本里的检查逻辑，没有等价单条命令） |

或者先激活环境再直接用 `python`：

```bash
source .venv/bin/activate    # 激活后终端提示符会变成 (.venv)
python -m pytest
python main.py balances
deactivate                   # 退出环境
```

**推荐始终用 `./dev.sh`** —— 它不会搞错 Python，也不会依赖当前终端的状态。

---

## 附：配置文件在哪

| 文件 | 作用 |
|---|---|
| `config/config.yaml` | **全局配置**：兑换比例、初始余额、LLM 提供商注册表、日志等级 |
| `agents/agent_a/config.yaml` | Agent A 的私有配置：用哪个模型、技能、行为 |
| `agents/agent_b/config.yaml` | Agent B 的私有配置 |
| `.env` | API Key（现在不需要；从 `.env.example` 复制） |
| `data/economy.db` | SQLite 数据库（运行产物，可被 `clean` 删除） |

**最常改的两处：**

```yaml
# config/config.yaml:33
tokens_per_ai_coin: 10000        # 1 AI币 = 多少 token

# config/config.yaml:37
initial_balance: 100             # 新 Agent 出生拿多少钱
```

---

## 附：项目文档在哪

| 文件 | 内容 |
|---|---|
| `README.md` | 项目介绍、架构、设计原则、经济模型、未来方向 |
| `PROTOCOL.md` | 通信协议规范：消息格式、状态机、结构化 payload、Workspace 边界 |
| `COMMANDS.md` | 本文件，命令速查 |
| `communication/*.py` | Step 3 通信层，每个文件顶部都有设计说明 |
| `core/*.py` | 每个文件顶部都有大段中文注释解释「为什么这样设计」 |
| `tests/*.py` | 每个测试都有中文说明，是了解系统行为最快的地方 |

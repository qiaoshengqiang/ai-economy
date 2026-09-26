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

## 4. 跑测试

```bash
# 全部测试（120 个）
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
```

**可用的测试文件：**

| 文件 | 测什么 | 数量 |
|---|---|---|
| `tests/test_wallet.py` | 钱包、余额、扣款 | 15 |
| `tests/test_ledger.py` | 交易账本、金额校验、金额显示格式 | 21 |
| `tests/test_economy.py` | 经济系统、隔离、审计、配置 | 32 |
| `tests/test_transfer.py` | 转账入口与各种拒绝情况 | 27 |
| `tests/test_startup.py` | 启动流程、重启不重复发钱 | 6 |
| `tests/test_cli.py` | 命令行命令 | 19 |
| | **合计** | **120** |

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

### 场景 E：我想改兑换比例

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

### 场景 F：我想改初始余额

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

### 场景 G：改了配置以后，测试会不会报错？

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
> 明确报错（见场景 E）—— 这不算测试失败。

---

### 场景 H：我把代码改坏了，想确认哪里出问题

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
| `COMMANDS.md` | 本文件，命令速查 |
| `core/*.py` | 每个文件顶部都有大段中文注释解释「为什么这样设计」 |
| `tests/*.py` | 每个测试都有中文说明，是了解系统行为最快的地方 |

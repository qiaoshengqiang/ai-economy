# 安装与使用（当前状态）

> **状态：源码安装版，不是打包好的 macOS App。**
>
> 目标读者：**不懂技术的使用者 + 帮他配置的 AI**。
> 当前没有 `.app` / `.dmg` / 安装器；需要在一台有终端权限的 macOS 上
> 按本文安装。装好后日常用 `./aieos` 启动即可。

---

## 1. 先看结论

| 问题 | 当前答案 |
|---|---|
| Intel Mac 能运行吗？ | 能，但需要安装 Python / Node / Pi CLI |
| 能下载后双击安装吗？ | 不能，当前没有打包产物 |
| 需要打包吗？ | 给不懂技术的人用，**建议后续打包**；当前先由 AI 协助源码安装 |
| 支持 Windows 吗？ | 不支持，当前主要验证 macOS |
| 需要 Docker 吗？ | 不需要 |
| 需要数据库服务器吗？ | 不需要，使用本地 SQLite |

装好后的日常入口：

```bash
./aieos start
./aieos status
./aieos stop
./aieos logs -f
```

---

## 2. 需要什么

### 2.1 机器

```text
macOS（Intel 或 Apple Silicon）
终端 / 管理员权限
```

Apple Silicon 和 Intel 需要各自架构的 Python / Node；Pi 本身是 JS，
但 Node 运行时要匹配机器架构。

### 2.2 软件

| 软件 | 要求 | 用途 |
|---|---|---|
| Python | 3.11+，推荐 3.13 | 运行 AIEOS |
| Node.js | **>= 22.19.0** | 运行 Pi CLI |
| Git | 任意较新版本 | 克隆仓库 / Worktree |
| Google Chrome | 可选 | Playwright 浏览器验证 |

> macOS 自带的 `/usr/bin/python3` 往往是 3.9，不能用。
> 用 `python3 --version` 确认。

安装命令（Homebrew 示例）：

```bash
brew install python@3.13 node@22 git
```

没有 Homebrew 时，AI 应从 python.org 安装 Python，从 nodejs.org 安装
Node；不要让用户自己猜。

### 2.3 外部组件（不在 git 仓库里）

| 组件 | 是否必须 | 说明 |
|---|---|---|
| Pi CLI | 使用 Pi Work 时**必须** | `@earendil-works/pi-coding-agent`，约 173MB |
| Playwright node_modules | 使用浏览器验证时**必须** | `resources/public/tools/playwright` 下 `npm install` |
| API Key | 真实模型调用时必须 | 写入 `.env`，不要提交 |

---

## 3. 安装步骤

### 3.1 获取代码

```bash
git clone <repo-url> ai-economy
cd ai-economy
```

如果拿到的是 zip，解压后进入目录即可。

> 不要从另一台机器复制 `.venv/`、`runs/`、`tasks/`、`.env`。
> 这些是机器本地运行产物。

### 3.2 Python 虚拟环境

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` 包含：

```text
python-dotenv
PyYAML
pytest
```

### 3.3 配置 API Key

```bash
cp .env.example .env
```

然后编辑 `.env`，填入自己的 Key。默认字段：

```text
DEEPSEEK_KEY_AGENT_A=
DEEPSEEK_KEY_AGENT_B=
DEEPSEEK_KEY_AGENT_C=
DEEPSEEK_KEY_AGENT_D=
DEEPSEEK_KEY_AGENT_E=
BAILIAN_KEY_AGENT_D=
BAILIAN_KEY_AGENT_E=
DOUBAO_API_KEY=
```

最少需要给 Front 使用的 Key。Pi Agent 用到的 Key 由 Agent 配置决定；
如果只知道一个 DeepSeek Key，可以把它同时填到 A–E，或后续在设置页调整。

**不要把 `.env` 发给任何人，也不要提交到 git。**

### 3.4 安装 Pi CLI

Pi 不随 git 仓库分发。使用 Pi Work 时必须安装。

方式一：项目内安装（推荐，路径固定在项目里）

```bash
mkdir -p experiments/pi_compare/pi_node
cd experiments/pi_compare/pi_node
npm init -y
npm install @earendil-works/pi-coding-agent@^1.5.2
cd -
```

然后配置路径：

```bash
./aieos config set pi-cli experiments/pi_compare/pi_node/node_modules/.bin/pi
```

方式二：全局安装

```bash
npm install -g @earendil-works/pi-coding-agent@^1.5.2
./aieos config set pi-cli "$(command -v pi)"
```

要求 Node >= 22.19.0。

### 3.5 可选：Playwright

浏览器验证需要：

```bash
cd resources/public/tools/playwright
npm install
cd -
```

并确保本机安装了 Google Chrome。当前 Playwright 配置使用
`channel: chrome`，不额外下载 Chromium。

如果不做浏览器验证，可以先跳过；相关工具会报告不可用。

### 3.6 检查环境

```bash
./aieos doctor
```

预期全部为 ✅。如果有 ❌，按提示修复后再继续。

### 3.7 启动

```bash
./aieos start
./aieos status
```

打开：

```text
http://127.0.0.1:8000/
```

首次打开设置页，检查：

- 服务商 / 模型是否存在；
- API 凭据是否显示“已配置”；
- Agent 列表是否正常；
- 提交模式 / 输出仓库是否需要调整。

需要新增模型服务商（例如 OpenCode）时见
[`docs/PROVIDERS.md`](PROVIDERS.md)。

### 3.8 验证一次最小任务

在 UI 输入：

```text
在当前 Task Workspace 创建一个 hello.txt，内容为 hello。
```

确认 Front 创建任务、Worker 执行、Task 卡片变成 COMPLETED。

---

## 4. 给朋友的 AI 的配置指令

可以把下面这段直接交给帮他安装的 AI：

```text
你在帮助一个不懂技术的用户安装 AIEOS。
请先完整阅读 docs/INSTALL.md，然后按步骤操作。

硬性要求：
1. 不要复制任何 .venv / runs / tasks / .env 目录或文件。
2. 不要泄露或提交用户的 API Key。
3. 先检查：
   uname -m
   python3 --version
   node --version
   git --version
4. Python 必须 >= 3.11；Node 必须 >= 22.19.0。
5. 所有命令在项目根目录执行。
6. 安装完成后必须运行：
   ./aieos doctor
   ./aieos start
   ./aieos status --json
7. 如果失败，保留完整错误输出，不要猜；先看：
   runs/ui/server.log
8. 不要修改 config/config.yaml 中与安装无关的 Agent / 模型配置。
9. 如果 config/config.yaml 里有不属于默认 5 个 Agent 的个人 Agent，
   先问用户是否保留。
10. 安装完成后，让用户在浏览器打开 http://127.0.0.1:8000/，
    并在设置页确认凭据显示“已配置”。
```

---

## 5. 验收标准

```text
[ ] python3 --version        >= 3.11
[ ] node --version           >= 22.19.0
[ ] .venv/bin/python 存在
[ ] python -c "import yaml" 成功
[ ] ./aieos doctor          全部 ✅
[ ] ./aieos start           成功
[ ] ./aieos status --json   running=true, health=true
[ ] 浏览器能打开 http://127.0.0.1:8000/
[ ] 设置页凭据显示“已配置”
[ ] 能完成一个最小任务
```

---

## 6. 常见问题

### `python3.13: command not found`

安装 Python 3.13，或用机器上已有的 3.11+：

```bash
python3 --version
python3 -m venv .venv
```

### `ModuleNotFoundError: No module named 'yaml'`

没有使用项目虚拟环境：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### `FileNotFoundError: .../pi`

Pi CLI 路径不对。配置为实际路径：

```bash
./aieos config set pi-cli /absolute/path/to/pi
./aieos restart
./aieos doctor
```

### `Pi CLI` 找不到 / Node 版本过低

```bash
node --version   # 必须 >= 22.19.0
```

安装 Node 22+ 后重新安装 Pi。

### 端口被占用

```bash
./aieos config set port 8001
./aieos restart
```

### 设置页显示凭据“缺失”

`.env` 里的环境变量名和 `config/llm.yaml` 的 `credentials.<profile>.env`
不一致，或没有填值。

```bash
grep '^DEEPSEEK' .env
```

### 启动失败

```bash
./aieos logs -n 100
```

重点看最后 30 行。

### Intel / Apple Silicon 混淆

- Intel Mac 使用 Intel 版 Python / Node；
- Apple Silicon 使用 arm64 版 Python / Node；
- Pi 是 JS，但 Node 必须是本机架构；
- 不要把 Apple Silicon 的 `.venv` 或 `node_modules` 复制到 Intel Mac。

---

## 7. 分享给朋友时：什么能带走，什么不能

### 7.1 可以带走

Agent 的**定义**可以随配置过去：

```text
config/config.yaml  agents.<id>
  ├── name（例如 小肥鱼）
  ├── brain.model / brain.credential（引用名）
  ├── capabilities / economy.initial_balance
  └── system_prompt / cognition
```

这代表“朋友那边也有一名叫小肥鱼的 Agent”，
但**不是原 Agent 本身**。

### 7.2 绝对不能带走

```text
.env                                ← API Key
runs/                               ← UI 数据库 / 日志 / launcher 配置
tasks/                              ← Task 历史 / Workspace / 快照
data/*.db                           ← CLI 数据库
logs/
agents/*/memory/                    ← 私有记忆
agents/*/workspace/                 ← 私有文件 / 产物
```

这些才是 Agent 的**历史、记忆、资产**：

| 内容 | 存放位置 |
|---|---|
| 身份 / 钱包 / 余额 / 账本 | `runs/ui/aieos.db` / `data/economy.db` 的 `agents` / `wallets` / `ledger_transactions` |
| Task 历史 / 验收 / 消息 | 数据库 + `tasks/<task_id>/` |
| 私有记忆 | `agents/<id>/memory/` |
| 私有文件 / 产物 | `agents/<id>/workspace/` |
| 运行日志 / 快照 | `runs/`、`tasks/<task_id>/.aieos/` |

### 7.3 新机器上会发生什么

朋友拿到 `config/config.yaml` 里的 `agents.<id>` 后，第一次启动会：

```text
读取 Agent 定义（名字 / 模型 / 凭据引用 / initial_balance）
        ↓
创建新的 agents 身份行 + 新钱包
        ↓
按 economy.initial_balance 重新铸币
        ↓
创建空的 memory / workspace
```

所以：

- 名字和配置过去了；
- 历史、记忆、原余额、原账本没有过去；
- 新 Agent 的初始余额来自配置，不是原 Agent 的资产。

> ⚠️ 如果被带走的 Agent 引用了某个 credential profile（例如
> `bailian_key_d`），而朋友的 `.env` 没有对应变量，设置页会显示“缺失”。
> 要么在 `.env` 补 Key，要么删除这个 Agent 定义。

### 7.4 分享用的排除清单

如果打包成 zip，排除：

```text
.venv/
.env
runs/
tasks/
logs/
data/*.db
agents/*/memory/*
agents/*/workspace/*
experiments/*/results/
resources/public/tools/playwright/node_modules/
```

只保留：

```text
源码
config/config.yaml      ← 可以包含 Agent 定义
config/llm.yaml         ← 只含 provider/model/credential 引用
config/worker_roles.yaml
ui/static/
docs/
tests/
```

`config/llm.yaml` 不含明文 Key；Key 永远只在 `.env`。

### 7.5 检查项

分享前确认：

- `.env` 没有被放进压缩包；
- `runs/`、`tasks/`、`data/*.db`、`logs/` 没有被放进压缩包；
- `agents/*/memory/`、`agents/*/workspace/` 没有被放进压缩包；
- 如果不想带走某个本机实验 Agent，删除 `config/config.yaml` 里对应的
  `agents.<id>` 块即可；名字和配置本身可以保留。

---

## 8. 现在还差什么

当前是**源码安装版**。如果要做成“朋友下载后双击就能用”，还差：

| 缺项 | 当前 | 需要做什么 |
|---|---|---|
| macOS `.app` / `.dmg` | 无 | PyInstaller / py2app + 图标 + 启动器 |
| 内置 Python | 无 | 打包 Python 运行时和依赖 |
| 内置 Node + Pi | 无 | 打包 Node / Pi，或首次启动自动安装 |
| 首次启动向导 | 无 | 填 API Key、选择模型、检查环境 |
| 可写数据目录 | 当前在项目根 | 改到 `~/Library/Application Support/AIEOS` |
| 代码签名 / 公证 | 无 | Apple Developer ID，避免 Gatekeeper 拦截 |
| Intel 构建 | 未验证 | 单独构建 x86_64 版本 |
| Windows | 未支持 | 平台适配 + 安装器 |
| 自动更新 | 无 | appcast / Sparkle 或自建更新 |

在这些完成之前，**给朋友的正确方式是：下载源码 + 让 AI 按本文安装**。

---

## 9. 相关文档

- [`START_AIEOS.md`](../START_AIEOS.md) —— 启动与端口
- [`docs/PROVIDERS.md`](PROVIDERS.md) —— 服务商 / 凭据 / 模型接入（含 OpenCode）
- [`docs/architecture/AIEOS_CLI.md`](architecture/AIEOS_CLI.md) —— `./aieos` 命令与配置
- [`docs/architecture/PLATFORM_COMPATIBILITY.md`](architecture/PLATFORM_COMPATIBILITY.md) —— Windows / 平台差异
- [`COMMANDS.md`](../COMMANDS.md) —— 全部命令

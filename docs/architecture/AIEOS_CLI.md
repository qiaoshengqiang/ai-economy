# AIEOS Launcher CLI

> **状态：Phase 1 `CURRENT`**
>
> 入口：`./aieos`
>
> 这是 AIEOS 的进程启动器，不是 Agent 设置页。它负责启动 / 停止 / 重启、
> 进程级启动参数、日志和启动前检查。

---

## 1. 为什么需要它

原来只有：

```bash
./start_aieos.sh start|stop|restart|status
```

参数只能靠环境变量：

```bash
AIEOS_PORT=8001 AIEOS_PI_CLI=/opt/pi ./start_aieos.sh start
```

问题：

- 参数不能持久化；
- 不能查看当前生效参数；
- 没有 `logs` / `doctor` / JSON 状态；
- 容易和 `runs/ui/ui_settings.json` 的运行设置混淆。

`aieos` 把**进程级参数**收进一个本地配置文件，并复用
`start_aieos.sh` 的 PID / 端口 / 健康检查逻辑。

---

## 2. 边界：Launcher 参数 ≠ UI 运行设置

这是本 CLI 最重要的边界。

| 类型 | 权威 | 内容 | 生效 |
|---|---|---|---|
| **Launcher 参数** | `runs/ui/launcher.json` | host / port / db / log / pid / pi-cli / task-root / export-dir | 重启 |
| **UI 运行设置** | `runs/ui/ui_settings.json` | front agent / pi agents / 并行 / work / commit / prompt log | 立即 / 新任务 / 重启 |

`aieos config` **只管理 Launcher 参数**。

以下字段仍由设置页管理，不在 `launcher.json` 里重复保存：

```text
front_agent
pi_work_agents
max_parallel_teams
max_solo_turns
work_mode / front_mode
commit_mode / commit_output_repo / commit_base_ref / commit_auto_merge
pi_timeout
prompt_log
```

如果 UI 设置文件已存在，它会覆盖启动参数中的同名初始值。
`aieos config show` 会把两类参数分开显示。

---

## 3. 命令

### 3.1 生命周期

```bash
./aieos start
./aieos stop
./aieos restart
./aieos status
./aieos status --json
```

- `start` 已运行时不重复启动；
- `stop` 先 SIGTERM，10 秒未退出再 SIGKILL；
- `restart` 先停止再启动；
- `status` 退出码 0 = 正在运行，1 = 未运行。

### 3.2 一次性覆盖启动参数

```bash
./aieos start --port 8001
./aieos restart --port 8001
./aieos restart --pi-cli /opt/pi
./aieos restart --host 0.0.0.0
./aieos restart --db runs/other/aieos.db
```

一次性参数只对本次启动生效，不写文件。

加 `--save` 才持久化：

```bash
./aieos restart --port 8001 --save
```

### 3.3 持久化参数

```bash
./aieos config show
./aieos config get port
./aieos config set port 8001
./aieos config set pi-cli /opt/pi
./aieos config unset port
./aieos config reset
```

`config set/unset/reset` 只改 `runs/ui/launcher.json`。
运行中的实例不会自动重启；需要执行：

```bash
./aieos restart
```

### 3.4 日志 / 界面 / 检查

```bash
./aieos logs
./aieos logs -f
./aieos logs -n 200
./aieos open
./aieos doctor
```

### 3.5 交互菜单

不带参数：

```text
$ ./aieos

AIEOS Launcher
  1) 启动
  2) 停止
  3) 重启
  4) 状态
  5) 修改启动参数
  6) 查看日志
  7) 打开界面
  d) doctor
  q) 退出
```

---

## 4. 配置文件

### 4.1 `runs/ui/launcher.json`

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "db": "",
  "log": "runs/ui/server.log",
  "pid_file": "runs/ui/aieos.pid",
  "pi_cli": "experiments/pi_compare/pi_node/node_modules/.bin/pi",
  "pi_provider": "",
  "pi_model": "",
  "pi_agent_dir": "",
  "task_root": "",
  "export_dir": ""
}
```

- 文件是机器本地的运行时配置，不进 git；
- 空字符串表示使用 `ui.server` 的默认值；
- 相对路径按项目根目录解析；`aieos` 会先把路径参数转成绝对路径再传给
  `start_aieos.sh`，避免 Pi 在任务工作目录中解析相对路径失败；
- 写入使用临时文件 + 原子替换；
- 不包含任何 API Key。

### 4.2 `runs/ui/launcher-state.json`

记录**实际运行实例**，用于 `status`：

```json
{
  "pid": 48198,
  "host": "127.0.0.1",
  "port": 8000,
  "url": "http://127.0.0.1:8000",
  "log": ".../runs/ui/server.log",
  "pid_file": ".../runs/ui/aieos.pid",
  "started_at": "..."
}
```

- 启动成功后写入；
- 停止后删除；
- `status` 发现 PID 已失效时会清理。

---

## 5. 优先级

```text
命令行参数
  > 环境变量
  > runs/ui/launcher.json
  > 默认值
```

运行设置：

```text
runs/ui/ui_settings.json
  > UI 设置页 / API
  > 启动参数的初始值（仅首次）
```

因此：

```bash
AIEOS_PORT=9000 ./aieos start
# 环境变量覆盖 launcher.json

./aieos start --port 9001
# 命令行覆盖环境变量
```

### 环境变量映射

| 参数 | 环境变量 |
|---|---|
| host | `AIEOS_HOST` |
| port | `AIEOS_PORT` |
| db | `AIEOS_DB` |
| log | `AIEOS_LOG` |
| pid_file | `AIEOS_PID_FILE` |
| pi_cli | `AIEOS_PI_CLI` |
| pi_provider | `AIEOS_PI_PROVIDER` |
| pi_model | `AIEOS_PI_MODEL` |
| pi_agent_dir | `AIEOS_PI_AGENT_DIR` |
| task_root | `AIEOS_TASK_ROOT` |
| export_dir | `AIEOS_EXPORT_DIR` |

`start_aieos.sh` 原有的 `AIEOS_PI_AGENTS`、`AIEOS_COMMIT_MODE` 等仍然可用。

---

## 6. 启动流程

`aieos start`：

1. 校验 launcher 参数；
2. 检查是否已运行；
3. 生成环境变量并调用 `start_aieos.sh start`；
4. 脚本检查 `.venv` / `.env` / Pi CLI / 端口；
5. 脚本轮询 `/api/health`；
6. 成功后 `aieos` 写 `launcher-state.json`。

`aieos stop`：

1. 读取 state；
2. 调用 `start_aieos.sh stop`；
3. 清理 state。

`aieos doctor` 检查：

- `.venv/bin/python`
- `.env`
- Pi CLI
- launcher JSON
- UI settings JSON
- log / pid 目录可写
- 端口 / 当前实例健康

阻塞项返回非 0。

---

## 7. 实现文件

```text
aieos                     ← 可执行入口
aieos_cli.py              ← CLI 实现（标准库）
start_aieos.sh            ← 底层进程管理
tests/test_aieos_cli.py   ← Phase 1 单元测试
```

`start_aieos.sh` 新增支持：

```text
AIEOS_HOST
AIEOS_DB
AIEOS_TASK_ROOT
AIEOS_EXPORT_DIR
AIEOS_PI_PROVIDER
AIEOS_PI_MODEL
AIEOS_PI_AGENT_DIR
AIEOS_COMMIT_AUTO_MERGE
```

---

## 8. 已知限制

- `start_aieos.sh` 与 `aieos` 当前面向 macOS / Linux；Windows 适配属于平台层 Phase 2。
- 运行中的实例不会自动应用 `launcher.json` 修改；必须 `restart`。
- `aieos config` 不管理 UI 运行设置；这些请用设置页。
- 通过 `start_aieos.sh` 直接启动的旧实例可能没有 `launcher-state.json`；
  此时 `status` 会退回 PID 文件 + launcher 配置。
- 一次性 `--port` 与旧实例并存时，建议使用 `restart`，不要 `start`。

---

## 9. 后续阶段

### Phase 2

- CLI 管理 UI 运行设置（front agent / pi agents / commit）
  - 运行中：调用 `/api/settings`
  - 停止时：原子写 `ui_settings.json`
- `--profile dev/prod` 多套启动参数
- `aieos settings` 打开设置页

### Phase 3

- Windows 进程管理
- launchd / systemd 服务化
- 多实例（不同端口 + 不同数据库）

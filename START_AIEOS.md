# AIEOS 启动说明

> 推荐使用新的启动器：`./aieos`。
> 它支持启动 / 停止 / 重启 / 状态 / 日志 / 参数持久化 / doctor。
> 完整说明见 [`docs/architecture/AIEOS_CLI.md`](docs/architecture/AIEOS_CLI.md)。
>
> 第一次安装 / 换机器 / 给朋友安装：
> 见 [`docs/INSTALL.md`](docs/INSTALL.md)。
>
> 底层 `start_aieos.sh` 仍然可用。

## 快速启动

在项目根目录执行：

```bash
./aieos start
```

打开：

```text
http://127.0.0.1:8000/
```

停止：

```bash
./aieos stop
```

重启：

```bash
./aieos restart
```

查看状态：

```bash
./aieos status
```

查看日志：

```bash
./aieos logs -f
```

## 默认参数

```text
端口：      8000
Front：     agent_a
Pi Agent：  agent_b,agent_c,agent_d,agent_e
Pi CLI：    experiments/pi_compare/pi_node/node_modules/.bin/pi
日志：      runs/ui/server.log
PID 文件：  runs/ui/aieos.pid
数据库：    runs/ui/aieos.db
```

## 修改启动参数

推荐持久化到 `runs/ui/launcher.json`：

```bash
./aieos config set port 8001
./aieos config set pi-cli /opt/pi
./aieos restart
```

查看当前参数：

```bash
./aieos config show
```

也可以临时覆盖，不写文件：

```bash
./aieos restart --port 8001
```

底层脚本仍支持环境变量：

```bash
# 换端口
AIEOS_PORT=8001 ./start_aieos.sh start

# 换 Pi Agent
AIEOS_PI_AGENTS=agent_b ./start_aieos.sh start

# 换 Pi CLI 路径
AIEOS_PI_CLI=/path/to/pi ./start_aieos.sh start

# 启用独立输出仓库（manual / limited / auto）
AIEOS_OUTPUT_REPO=/path/to/aieos-output \
AIEOS_COMMIT_MODE=manual \
./start_aieos.sh start
```

新增的底层环境变量：

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

也可以组合：

```bash
AIEOS_PORT=8001 \
AIEOS_PI_AGENTS=agent_b \
AIEOS_PI_CLI=/path/to/pi \
./start_aieos.sh restart
```

提交设置也可以在 UI「设置 → 运行」修改；一旦保存到
`runs/ui/ui_settings.json`，它以文件为准，启动参数只作为初始值。

## 启动前检查

脚本会自动检查：

- `.venv/bin/python` 是否存在；
- `.env` 是否存在（没有会提示，但不会阻止启动）；
- Pi CLI 是否存在且可执行；
- 端口是否已被其他进程占用；
- 启动后 `/api/health` 是否返回正常。

如果启动失败，脚本会直接打印最近 30 行 `runs/ui/server.log`。

## 后台运行与前台运行

`start_aieos.sh` 使用 `nohup` 在后台运行，适合日常使用：

```bash
./start_aieos.sh start
```

如果需要在终端前台运行、直接看日志：

```bash
./dev.sh ui --port 8000 --pi-agents agent_b,agent_c,agent_d,agent_e \
  --pi-cli experiments/pi_compare/pi_node/node_modules/.bin/pi
```

## 日志与排查

查看日志：

```bash
tail -f runs/ui/server.log
```

查看状态：

```bash
./start_aieos.sh status
```

常见问题：

### 端口被占用

```text
端口 8000 已被其他进程占用
```

换一个端口：

```bash
AIEOS_PORT=8001 ./start_aieos.sh start
```

或先确认占用进程：

```bash
lsof -iTCP:8000 -sTCP:LISTEN
```

### 找不到 Pi CLI

确认路径存在：

```bash
ls -l experiments/pi_compare/pi_node/node_modules/.bin/pi
```

如果 Pi 安装在其他位置：

```bash
AIEOS_PI_CLI=/path/to/pi ./start_aieos.sh start
```

### 启动后健康检查失败

```bash
tail -n 50 runs/ui/server.log
```

重点检查：

- 端口是否被占用；
- `.env` 中的模型 Key 是否配置；
- Pi CLI 路径是否正确；
- 数据库文件是否有权限。

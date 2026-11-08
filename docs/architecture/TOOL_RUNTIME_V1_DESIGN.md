# Tool Runtime V1 —— 受控代码执行与测试

> **状态：`CURRENT`（V1）**
>
> 本文档说明：Agent 如何在不获得任意 shell 的前提下，真实运行代码与测试。
> 核心边界是 **Task Execution ≠ Code Execution**，以及
> **公共工具库只读 + 每次执行独立 run 目录**。

---

## 1. 为什么需要它

在 Tool Runtime 之前，Agent 能写代码，但不能验证代码：

- `workspace_write` 只把文本写进 Task Workspace；
- `private_write` 只写私有宿主机目录；
- Task Execution 只推进生命周期，不执行代码；
- 所有验证都靠人外部完成。

Tool Runtime 补上一条**受控的**执行通道，让 Agent 可以：

- 运行 Python / pytest；
- 运行 Node.js；
- 运行 Playwright 浏览器测试；
- 读取 stdout / stderr / exit code，并据此修复。

它**不**让 Agent 获得任意 shell、任意下载或宿主机管理权限。

---

## 2. 分层

```text
Agent
  ↓ action: run_tool
actions/run_tool.py             ← 叶子层；只拿注入的 run_fn
  ↓
experiments/.../production.py   ← 组合根：绑定 workspace/task/agent
  ↓
tool_runtime/
  ├── library.py                ← 只读 manifest 加载
  ├── workspace.py              ← Task / Private → run 目录（只读复制）
  └── runner.py                 ← subprocess（shell=False）+ 超时 + 输出上限
  ↓
resources/public/tools/         ← 公共只读工具库
  ├── manifests/*.json
  └── playwright/               ← @playwright/test（node_modules 不进 git）
```

**依赖方向**：`actions/` 不 import `tool_runtime/`；执行器由组合根注入。
`tool_runtime/` 不 import `services/`，不碰 Task 状态机、钱包、协议。

---

## 3. 公共工具库

工具由**项目/人类**预装，Agent 只能使用：

| 工具 | 命令 | 用途 |
|---|---|---|
| `python` | `.venv/bin/python` | 运行 Python 脚本 |
| `pytest` | `.venv/bin/python -m pytest` | Python 测试 |
| `node` | `node` | Node.js 脚本/测试 |
| `playwright` | `@playwright/test` CLI | 浏览器测试（本机 Chrome） |

manifest 在 `resources/public/tools/manifests/`。Agent 能追加 `args`，
不能替换可执行文件，也不能安装新工具。

### Playwright

- `@playwright/test` 安装在 `resources/public/tools/playwright/`（只读使用）；
- 使用本机 **Google Chrome**（`channel: "chrome"`），不依赖下载浏览器缓存；
- `ToolRunner` 会在 run 目录写入默认 `playwright.config.js`（若 Agent 没写），
  这样测试文件只需 `page.goto("file://" + process.cwd() + "/site/index.html")`。

---

## 4. run_tool 接口

Agent 的动作：

```json
{
  "decision": "act",
  "reasoning": "运行测试",
  "actions": [{
    "type": "run_tool",
    "parameters": {
      "tool": "pytest",
      "args": ["-q"],
      "source": "task",
      "path": "",
      "timeout_seconds": 120
    }
  }]
}
```

- `source`：`task`（当前 Task Workspace）或 `private`（Agent 私有空间）
- `path`：可选子路径；空字符串表示整个空间
- `tool`：必须是公共工具库里的名字
- `args`：字符串列表；工具命令固定，Agent 只能追加参数
- `timeout_seconds`：1..300，默认 60

返回（Action output）：

```json
{
  "ran": true,
  "ok": false,
  "tool": "pytest",
  "run_id": "run_...",
  "exit_code": 1,
  "timed_out": false,
  "stdout": "...",
  "stderr": "...",
  "duration_ms": 123.4,
  "code": "tool_exit_nonzero",
  "materialized_files": 4,
  "output_truncated": false,
  "config_used": "qa/playwright.config.js",
  "smoke": {"enabled": true, "ok": true, "entry": "site/index.html"},
  "images": [
    {"path": "test-results/.../screenshot.png", "mime": "image/png", "bytes": 12345}
  ]
}
```

`success=True` 表示**工具真的运行了**；`ok=false` 表示测试/命令失败。
这样失败输出也会回喂给 Agent，而不是被当成“动作没执行”。

### 4.1 Playwright：项目配置发现

Tool Runtime 会在 run 根目录写一个默认 `playwright.config.js`
（`testDir: '.'`、`channel: chrome`），Playwright 默认只读它。
Agent 写在 `qa/playwright.config.js` 的 `baseURL` / `webServer`
因此会被静默忽略。

现在 `_execute()` 会：

- 扫描 run 目录下的子目录 config（排除根 bootstrap）；
- 优先 `qa/playwright.config.js`；
- 只有一个候选时自动追加 `--config=<rel>`；
- 有多个候选时不替 Agent 猜，Agent 仍可显式传 `--config`；
- 把实际使用的配置写进 `config_used`。

### 4.2 Playwright：冒烟门禁（smoke gate）

在跑全量套件前，先跑一个独立的 smoke spec（通常 5–10 秒；带 CDN 模块的页面最多约 20 秒）：

- 启动一个临时静态服务器（随机端口）指向 run work；
- 自动识别入口：`site/index.html` / `index.html` / ...；
- 检查 HTTP 状态、body 非空、`#app` / `#ui-root` 有可见子节点、
  没有 `pageerror`；
- 失败时返回 `code="smoke_failed"`，**不执行全量套件**；
- 可用 `--no-smoke` 或 `PLAYWRIGHT_SMOKE=0` 关闭；
- 入口可通过 `PLAYWRIGHT_SMOKE_ENTRY` 覆盖。

实测：一个 154 字节的坏 `index.html` 从「5 个用例各等 40 秒」
变成 **1.8 秒失败**。
CDN 页面（Three.js importmap）的 `DOMContentLoaded` 可能到 8 秒，
所以门禁的 `page.goto` 超时留到 20 秒；太短会把「慢但正常」误判成失败。

---

## 5. 执行隔离与硬约束

| 约束 | V1 做法 |
|---|---|
| 可执行文件 | 只来自 manifest；Agent 不能指定路径 |
| shell | `shell=False`，命令是列表 |
| cwd | 每次 run 的 `work/` |
| HOME / TMPDIR | 每次 run 独立 |
| 环境变量 | 最小集；不继承 `.env` / API Key |
| 输入文件 | Task / Private → run 目录，只读复制 |
| 输出 | 最多 12,000 字符（头 8k + 尾 4k） |
| 超时 | 默认 60s，最大 300s；超时杀进程组 |
| 工具库写权限 | 无 Agent 写入口 |
| 下载 / 安装 | 无 Agent 动作；由项目/人类预装 |
| 运行目录 | `data/tool_runs/<run_id>/`，不进仓库 |

### 必须说清楚的限制

**V1 不是 OS 级安全沙箱。**

- 没有 macOS sandbox / container / VM 隔离；
- 没有网络出口策略（代码可以访问网络）；
- 没有 CPU/内存 cgroup 级限制；
- 信任模型是“执行项目内的可信测试代码”，而不是“执行不受信任的恶意代码”。

这些属于后续 Tool Runtime V2 的范围，不能因为 V1 有超时和 run 目录就宣称
“已经安全隔离”。

---

## 6. 并发模型

- 工具二进制/依赖：共享只读，可并发；
- 每次 `run_tool`：独立 run 目录、独立 HOME/TMPDIR；
- pytest cache / Playwright `test-results` / 截图 / trace：留在各自 run 目录；
- 安装/更新：不是 Agent 能力；由人类维护，避免共享写竞争；
- 需要全局独占的操作（例如未来工具安装）必须走显式 lease，而不是塞进私有目录。

Playwright 使用 Node runtime + 本机 Chrome；多个 run 各自有独立 Chrome 进程与
`test-results`，不会共享浏览器 profile。

---

## 7. 与既有边界的关系

- **Task Execution ≠ Code Execution**：`TaskExecutionService` 仍然只推进生命周期；
  Tool Runtime 是独立层。
- **Workspace ≠ Permission**：任务/私有文件复制到 run 目录，不代表宿主机权限。
- **Public Tool Library ≠ Agent Private Workspace**：工具只读，Agent 不能安装/改写。
- **Agreement ≠ Payment**：执行测试不产生结算。
- **Observation ≠ Conclusion**：测试输出是观察材料；是否通过由 Agent/验收人判断。

---

## 8. 测试与实验

单元/集成测试：

```bash
./dev.sh test tests/test_tool_runtime.py
./dev.sh test tests/test_llm_multimodal.py
```

真实任务实验：

```bash
# 后端：Python + pytest
./.venv/bin/python experiments/tool_collab/run_tool_task.py \
  --task-type backend \
  --command "实现一个 Python 模块，提供 add(a,b) 和 subtract(a,b)，写 pytest 测试并运行到通过。"

# 网页：单文件 HTML + Playwright
./.venv/bin/python experiments/tool_collab/run_tool_task.py \
  --task-type web \
  --command "完成一个网页待办小游戏（单文件），写 Playwright 测试并运行到通过。"
```

---

## 9. 后续（不在 V1）

- OS 级沙箱 / 网络出口策略；
- 资源配额（CPU / 内存 / 磁盘）；
- 工具版本锁定、checksum、签名、供应链校验；
- 每次运行的审计表（当前依赖 Experience + run 目录）；
- 受控的工具安装/更新 lease；
- Agent 私有工具库（当前不做：私有目录不是权限边界）。

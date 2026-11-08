# Public Tool Library

公共工具库由**项目/人类**预装，Agent 只能通过 `run_tool` 使用，不能下载、安装或改写。

## 工具清单

| 工具 | 来源 | 说明 |
|---|---|---|
| `python` | `.venv/bin/python` | 项目 Python 运行时 |
| `pytest` | `.venv/bin/python -m pytest` | Python 测试运行器 |
| `node` | `node` | Node.js 运行时 |
| `playwright` | `resources/public/tools/playwright/node_modules/@playwright/test` | 浏览器测试运行器，使用本机 Google Chrome |

每个工具的声明在 `manifests/*.json`。`command` 是固定可执行文件，Agent 只能追加 `args`。

## 安装（只做一次，不是 Agent 动作）

```bash
# Python / pytest 由项目 .venv 提供
./.venv/bin/python -m pytest --version

# Playwright 测试运行器（公共只读）
npm install --prefix resources/public/tools/playwright @playwright/test@1.65.0

# 浏览器：本项目使用本机 Google Chrome，不下载 Playwright 浏览器缓存
# （Playwright 默认配置见 manifests/playwright.json 的 bootstrap_files）
```

> Chrome 需要安装在 `/Applications/Google Chrome.app`。
> 如果换机器没有 Chrome，需要先安装浏览器，或调整 manifest 的 Playwright config。

## 并发模型

- 工具二进制/依赖：**共享只读**，可并发使用。
- 每次 `run_tool`：独立 `data/tool_runs/<run_id>/`，独立 `HOME` / `TMPDIR` / `cwd`。
- 运行产物（pytest cache、Playwright `test-results`、截图、trace）：留在 run 目录，不共享。
- 安装/更新：不属于 Agent 能力；由人类/项目维护者执行。

## 边界

- `run_tool` 只执行 manifest 中的命令，`shell=False`。
- 没有任意 URL 下载、没有 `pip/npm install`、没有宿主机 shell。
- 每次执行有超时与输出上限；环境变量不继承 `.env` / API Key。
- **这不是 OS 级安全沙箱**：信任模型与限制见
  [`docs/architecture/TOOL_RUNTIME_V1_DESIGN.md`](../../../docs/architecture/TOOL_RUNTIME_V1_DESIGN.md)。

# `playwright` — 浏览器测试运行器

## 它是什么

项目预装的 Playwright Test（Node 版），使用本机 Google Chrome：

```text
node resources/public/tools/playwright/node_modules/@playwright/test/cli.js test <args>
```

它只运行 `*.spec.js` 测试文件，**不是**浏览器自动化 REPL，也**不是**
Python 的 `playwright` 包。

## 调用形状

```json
{"type":"run_tool",
 "parameters":{"tool":"playwright","args":["tests/foo.spec.js"]}}
```

常用参数：

| 参数 | 含义 |
|---|---|
| `args` | 传给 `playwright test` 的参数（spec 路径、`--project` 等） |
| `source` | `task`（默认）或 `private` |
| `path` | 只复制该路径到 run 目录；省略 = 全部 |
| `timeout_seconds` | 1..300 |

`args` 里写不写开头的 `test` 都可以；runtime 会兼容去掉它。

## 正确流程（必须按顺序）

### 1. 先写 spec 到 Task Workspace

例如 `tests/foo.spec.js`：

```javascript
const { test, expect } = require('@playwright/test');

test('page boots', async ({ page }) => {
  await page.goto('http://127.0.0.1:8000/index.html');
  await expect(page.locator('body')).toContainText('...');
});
```

用 `workspace_write` 写入，不要只在 `run_tool(python/node)` 的副本里写。

### 2. 再运行 spec

```json
{"type":"run_tool",
 "parameters":{"tool":"playwright","args":["tests/foo.spec.js"]}}
```

### 3. 读结果

`exit_code=0` 表示通过。截图、trace、`test-results/` 留在 run 副本里；
截图会以 `images` 回喂给同一轮认知的后续消息。

## ⚠️ 三个真实踩过的坑

### 坑 1：把 HTML 当成测试过滤器

```text
run_tool(playwright, args=["index.html"])
→ Error: No tests found.
```

`index.html` 不是 spec。必须先写 `tests/*.spec.js`。

### 坑 2：用 Python 导入 Playwright

```python
from playwright.sync_api import sync_playwright   # ModuleNotFoundError
```

项目 `.venv` 里没有 Python Playwright。浏览器测试只走 `tool="playwright"`。

### 坑 3：用 Node 直接 require

```javascript
require('@playwright/test')   // Cannot find module
```

依赖路径由 runtime 注入，只有 `tool="playwright"` 的进程能找到它。

## Smoke gate（自动）

在跑全量 spec 之前，runtime 会先做一次 ~5 秒的冒烟检查：

- 找 `site/index.html` / `index.html` / `public/index.html` 等入口；
- 起一个临时静态服务器；
- 检查 HTTP 状态 < 400、body 非空、`#app` / `#ui-root` 有可见子节点、
  没有 `pageerror`。

**smoke 失败时不会执行全量套件**，结果里 `code="smoke_failed"`。
这是为了先排除「页面根本打不开」再花时间跑测试。

`--no-smoke` 只用于诊断，正常验收不要关掉它。

## 项目配置自动发现

如果 Workspace 里有 `qa/playwright.config.js` / `playwright.config.ts`
等，且只有一个候选，runtime 会自动加 `--config <该文件>`。
多个候选时**不替 Agent 猜**，需要自己显式传 `--config`。

## 🔒 写不回来

`run_tool` 在一次性副本里执行。Playwright 生成的截图、报告、
`test-results/`、`playwright-report/` **不会回写** Task Workspace。
要交付测试报告，必须用 `workspace_write` 另写一份文本/JSON。

## 输出

`ok` / `exit_code` / `stdout` / `stderr` / `timed_out` / `duration_ms` /
`smoke` / `config_used` / `images`。

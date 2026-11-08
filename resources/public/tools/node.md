# `node` — Node.js 运行时

## 它是什么

用本机 Node.js 执行 JavaScript：

```text
node <args>
```

它**不是** npm：不能 `npm install`、不能下载依赖、不能执行 shell。

## 调用形状

```json
{"type":"run_tool",
 "parameters":{"tool":"node","args":["scripts/check.js"]}}
```

常用参数：

| 参数 | 含义 |
|---|---|
| `args` | 传给 `node` 的字符串列表 |
| `source` | `task`（默认）或 `private` |
| `path` | 只复制该路径到 run 目录；省略 = 全部 |
| `timeout_seconds` | 1..300 |

## 🔒 写不回来

和 `python` / `pytest` 一样：所有写操作都发生在**一次性 run 副本**里。
`fs.writeFileSync("out.json", ...)` 不会让 Workspace 出现 `out.json`。
**副本里的写不会回写** Task Workspace。
要交付，必须用 `workspace_write` / `workspace_edit`。

## 常见用法

```javascript
// 读取并检查 HTML
const fs = require('fs');
const html = fs.readFileSync('index.html', 'utf8');
console.log('chars', html.length);
```

```javascript
// 做纯计算 / 生成一段文本，再把结果写进 stdout
console.log(JSON.stringify({ok: true}));
```

## ⚠️ 不要用 node 导入 Playwright

Playwright 测试运行器是单独的 `playwright` 工具，依赖路径由 runtime
注入。直接 `require('playwright')` / `require('@playwright/test')`
通常会得到 `Cannot find module`。

浏览器测试请走 `tools/playwright.md` 里的流程。

## 常见失败

| 现象 | 原因 |
|---|---|
| `Cannot find module` | 项目没有该依赖；Agent 不能安装 |
| `SyntaxError` | Node 版本或语法不匹配 |
| 输出被截断 | 单次最多约 12,000 字符 |

## 输出

`ok` / `exit_code` / `stdout` / `stderr` / `timed_out` / `duration_ms`。

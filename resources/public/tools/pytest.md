# `pytest` — Python 测试运行器

## 它是什么

用项目虚拟环境里的 pytest：

```text
.venv/bin/python -m pytest <args>
```

它只运行**已经存在**的测试文件；不会替 Agent 生成测试。

## 调用形状

```json
{"type":"run_tool",
 "parameters":{"tool":"pytest","args":["tests/test_app.py","-q"]}}
```

常用参数：

| 参数 | 含义 |
|---|---|
| `args` | 传给 pytest 的参数（文件、`-q`、`-k` 等） |
| `source` | `task`（默认）或 `private` |
| `path` | 只复制该路径到 run 目录；省略 = 全部 |
| `timeout_seconds` | 1..300 |

## 🔒 写不回来

pytest 会在 run 副本里生成 `.pytest_cache/`、`__pycache__/`、覆盖率文件等。
这些**不会回写** Task Workspace。

如果测试需要产出报告，测试代码应把报告写到 stdout，或由 Agent 另用
`workspace_write` 写入 Workspace。

## 正确用法

1. 先用 `workspace_write` 在 Workspace 写测试文件，例如
   `tests/test_app.py`。
2. 再运行：

```json
{"type":"run_tool",
 "parameters":{"tool":"pytest","args":["tests/test_app.py","-q"]}}
```

3. 看 `exit_code`：0 = 通过，非 0 = 失败。失败时读 `stdout` / `stderr`
   里的断言信息。

## 常见失败

| 现象 | 原因 |
|---|---|
| `No tests ran` | 路径写错 / 测试文件名不符合 `test_*.py` |
| `ModuleNotFoundError` | 项目 `.venv` 缺依赖；Agent 不能安装 |
| 超时 | 用 `timeout_seconds` 调整，或拆小测试范围 |

## 输出

`ok` / `exit_code` / `stdout` / `stderr` / `timed_out` / `duration_ms`。
pytest 的输出可能很长，runtime 只回传前 8k + 后 4k 字符。

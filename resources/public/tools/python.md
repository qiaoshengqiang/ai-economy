# `python` — 项目 Python 运行时

## 它是什么

用项目自己的虚拟环境执行 Python：

```text
.venv/bin/python <args>
```

它**不是**宿主机 shell，**不能**用来 `pip install`、`apt install` 或执行任意
shell 命令。

## 调用形状

```json
{"type":"run_tool",
 "parameters":{"tool":"python","args":["script.py"]}}
```

常用参数：

| 参数 | 含义 |
|---|---|
| `args` | 传给 `python` 的字符串列表 |
| `source` | `task`（默认）或 `private` — 从哪个空间复制文件 |
| `path` | 只复制该路径（目录或文件）到 run 目录；省略 = 全部 |
| `timeout_seconds` | 1..300；省略用 runtime 默认值 |

例：只复制 `src/`，执行其中的脚本：

```json
{"type":"run_tool",
 "parameters":{"tool":"python","args":["src/main.py"],
               "source":"task","path":"src"}}
```

## 🔒 最重要的边界：写不回来

`run_tool` 会把 Workspace 文件**只读复制**到一个独立的 run 目录，
然后在那个副本里执行。

```text
run_tool(python) 里 open("out.txt","w").write(...)
        ↓
只会写进一次性副本
        ↓
Task Workspace 里**不会**出现 out.txt
```

要交付文件，必须另外用 `workspace_write` / `workspace_edit` 写入 Workspace。
下载依赖、生成数据、写临时脚本都一样 —— 副本里的写入**不会自动回写**。

## 常见用法

```python
# 读取 workspace 文件并做静态检查
import pathlib
print(pathlib.Path("index.html").read_text(encoding="utf-8")[:500])
```

```python
# 解析 JSON / 统计行数
import json
data = json.load(open("data.json", encoding="utf-8"))
print(len(data))
```

## 常见失败

| 现象 | 原因 |
|---|---|
| `FileNotFoundError` | 文件不在 Workspace，或没被 `path` 复制进 run 目录 |
| `ModuleNotFoundError` | 项目 `.venv` 没有这个包；Agent 不能安装依赖 |
| 输出被截断 | 单次最多回给 Agent 约 12,000 字符（前 8k + 后 4k） |

## 输出

结果里有 `ok` / `exit_code` / `stdout` / `stderr` / `timed_out` /
`materialized_files` / `duration_ms`。退出码非 0 不代表工具坏了，
只代表脚本返回了非 0。

# Workspace 搜索（V1）

> 状态：**`CURRENT`（V1 已实现）**
> 关联：ADR-0044 · `actions/workspace_search.py` ·
> `services/artifacts.py::search_artifacts`

---

## 1. 为什么需要它

范围读取 + 局部编辑解决了“改得省 token”，但没有解决“**在哪里改**”：

- 工作记忆能记住 Agent 已经记录的“位置/结构/重大事件”，
  但它是 **Agent 自己写的缓存**，不是当前代码的索引；
- 第一次遇到代码库、找所有调用点、代码变化后，都需要重新看 Workspace。

`workspace_search` 是一个**只读的字面搜索**，
不建索引、不写回、不自动写工作记忆。

---

## 2. 动作

```json
{
  "type": "workspace_search",
  "parameters": {
    "query": "refresh_token",
    "path": "src",
    "glob": "*.py",
    "max_results": 20,
    "case_sensitive": true,
    "context_lines": 1
  }
}
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `query` | 必填 | 字面文本（V1 不做正则） |
| `path` | 空 = 整个 Workspace | 文件或子目录 |
| `glob` | 空 = 不过滤 | `*.py` / `src/*.py` |
| `max_results` | 20 | 上限 20 |
| `case_sensitive` | true | 默认大小写敏感 |
| `context_lines` | 0 | 0–3 行上下文 |

返回：

```json
{
  "ok": true,
  "query": "refresh_token",
  "path": "src",
  "glob": "*.py",
  "matches": [
    {"path": "src/auth.py", "line": 42,
     "snippet": "TOKEN = 'refresh_token'",
     "context": [{"line": 41, "text": "..."}]}
  ],
  "total_matches": 3,
  "files_scanned": 12,
  "truncated": false
}
```

Agent 看到的是：

```text
query='refresh_token'  total=3  files=12  truncated=False
src/auth.py:42: TOKEN = 'refresh_token'
...
```

---

## 3. 边界

- **只读**：只经过 `WorkspaceGateway.list/read`；
  参与者检查、路径校验、读取边界照旧。
- **不建索引 / 不缓存**：每次搜索读当前 Workspace。
- **不写回 / 不自动写记忆**：是否把结果记进工作记忆，是 Agent 的决定。
- **硬上限**：
  - 最多扫 200 个文件；
  - 单文件最多 200k 字符；
  - 最多返回 20 条命中；
  - `context_lines` 最多 3。
- **字面匹配**：不做正则、模糊、大小写归一化（可选 `case_sensitive`）。
- **不是 Change Pipeline 的 Patch/Apply**：它是读操作。

---

## 4. 与工作记忆的分工

```text
工作记忆 = Agent 自己记录的“已知结构 / 重大事件 / 位置线索”
Workspace = 当前代码事实
Search    = 当前代码的“在哪里”查询
```

- 如果工作记忆里已有位置 → `workspace_read(offset/limit)` 验证，再 `workspace_edit`；
- 如果没有 → `workspace_search` 找位置；
- 搜到后是否写进工作记忆，由 Agent 自己决定；
- 系统不自动把 search 结果写进工作记忆，也不自动总结。

---

## 5. 测试

```bash
./dev.sh test tests/test_workspace_search.py
```

覆盖：

- 跨文件字面搜索；
- glob / 子目录 / 单文件；
- 大小写敏感；
- max_results / truncated；
- context_lines；
- 空 query；
- executor 的 agent_id 不可伪造；
- search 结果回喂格式。

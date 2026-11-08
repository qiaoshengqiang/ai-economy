# Workspace 范围读取 + 局部编辑（V1）

> 状态：**`CURRENT`（V1 已实现）**
> 关联：ADR-0043 · `actions/workspace_read.py` · `actions/workspace_edit.py` ·
> `services/artifacts.py`

---

## 1. 为什么需要它

在 V1 之前，Agent 修改代码只有**整文件写入**：

```text
workspace_read(path)                  → 读全文
workspace_write(path, replace, 全文)  → 重新输出全文
```

后果：

- 1,000 行文件改一行，模型要输出整个文件；
- 大文件读不全（follow-up 8k/12k 截断）；
- 没有行号/范围，无法定位；
- 不能像 Claude Code / Aider 那样 `old_string → new_string`。

V1 补两个动作：

```text
workspace_read(path, offset, limit)   → 范围读取
workspace_edit(path, old_string, new_string) → 局部替换
```

---

## 2. 范围读取

```json
{
  "type": "workspace_read",
  "parameters": {
    "path": "src/app.py",
    "offset": 120,
    "limit": 80
  }
}
```

语义：

- `offset` 是 **1-based** 起始行；省略 = 第 1 行；
- `limit` 是最多行数；省略 = 读到结尾（旧行为）；
- 显式范围最多 **400 行**；
- 返回：
  ```json
  {
    "content": "...",
    "revision": 3,
    "chars": 1234,
    "total_chars": 45678,
    "total_lines": 1200,
    "offset": 120,
    "limit": 80,
    "start_line": 120,
    "end_line": 199,
    "truncated": true
  }
  ```
- Agent 看到的内容带一行 `[lines 120-199 of 1200]` 头（只对范围读取）；
- 读仍然只检查 Task Participation，不受 Write Lock 影响。

---

## 3. 局部编辑

```json
{
  "type": "workspace_edit",
  "parameters": {
    "path": "src/app.py",
    "old_string": "def add(a, b):\n    return a - b",
    "new_string": "def add(a, b):\n    return a + b",
    "replace_all": false,
    "expected_revision": 3
  }
}
```

语义（严格，不做模糊匹配）：

- `old_string` 必须**唯一命中**；
- 0 个命中 → `no_match`；
- 多个命中且 `replace_all=false` → `multiple_matches`（带命中次数）；
- `replace_all=true` 才允许替换全部；
- `old_string` 必须非空；`new_string` 可以为空（删除文本）；
- `old_string == new_string` → `no_change`；
- `expected_revision` 可选；给了就必须等于当前 revision，
  否则 `stale_revision`；
- 修改仍走 `WorkspaceGateway.edit`：锁 / revision / audit 完全不变；
- 成功后返回新 revision、previous_chars、chars、matched、replaced。

示例错误：

```text
no_match
multiple_matches
stale_revision
no_change
locked_by_other
not_participant
```

---

## 4. Token 收益

| 场景 | V1 之前 | V1 |
|---|---|---|
| 1,000 行文件改 1 个函数 | 输出全文（可能 10k–30k tokens） | old/new 片段（几百 tokens） |
| 找一行 | 读全文 | 先 search/范围读（1k–3k tokens） |
| 追加 | append（本来便宜） | 保持 |
| 新建/全量重写 | replace/create | 保持 |

目标不是“永远不用全文写”，而是：
**能局部改就局部改；只有新建/全量重写才用 `workspace_write`。**

---

## 5. 边界与限制

- **严格匹配**：不做正则、大小写、空白归一化；失败就让 Agent 重读再试。
- **单文件**：V1 一次只编辑一个文件；多文件 later。
- **无行号编辑**：old/new 是文本片段，不是 line range；行号只用于阅读定位。
- **无 Git Patch**：这不是 Change Pipeline 的 Patch/Apply；它直接改 Task Workspace。
- **无模糊兜底**：多个命中必须由 Agent 提供更多上下文或显式 replace_all。
- **内容仍是 TEXT**：二进制/图片不在本动作范围。
- **读范围最多 400 行**：防止一次拉爆上下文。

---

## 6. 测试

```bash
./dev.sh test tests/test_workspace_edit.py
./dev.sh test tests/test_workspace_read_action.py
./dev.sh test tests/test_read_payload_feedback.py
```

覆盖：

- 全文读取向后兼容；
- offset/limit、越界、400 行上限、total/truncated；
- 唯一命中、0 命中、多命中、replace_all；
- stale revision、no_change、空 old_string；
- executor 的 agent_id 不可伪造；
- 范围 header 回喂；
- 既有 read/write 形状守卫。

---

## 7. 后续

- `workspace_multi_edit`（多文件/多段）；
- `workspace_search`（query/glob/行号）降低“找代码”成本；
- Change Pipeline Patch（供 review/merge，而不是日常编辑）；
- 行号级 edit（`start_line/end_line`）作为 old/new 的补充；
- 补丁 provenance / applicability。

# ADR-0043：Workspace 范围读取 + old/new 局部编辑

- **状态**：Accepted（**已实现**）
- **时间**：Workspace Read/Edit V1
- **Supersedes**：无
- **Superseded by**：无

## Context

此前 Agent 修改代码只有整文件写入：

- `workspace_read(path)` 读全文；
- `workspace_write(replace)` 必须重新输出全文；
- follow-up 只回喂 8k/12k 字符，大文件甚至读不全；
- 没有行范围、没有 `old_string/new_string`、没有 `apply_patch`；
- `run_tool` 在副本里执行，不能写回 Workspace；
- Change Pipeline 的 Patch/Apply 仍是 `DESIGNED`。

结果：大文件改一行要花大量输出 token，且容易因读取不完整改错。
这阻碍了真实代码任务，也不符合常见 harness 的编辑体验。

## Decision

新增两个 Workspace 动作：

1. **`workspace_read` 范围读取**
   - `offset`（1-based）/ `limit`；
   - 显式范围最多 400 行；
   - 返回 total_chars / total_lines / start_line / end_line / truncated；
   - 省略 offset/limit 保持全文读取的向后兼容。

2. **`workspace_edit` old/new 局部编辑**
   - `old_string` 必须唯一命中；0 或多命中失败；
   - `replace_all=true` 才允许多命中；
   - 严格字符串匹配，不做正则/模糊/空白归一化；
   - 可选 `expected_revision`；冲突返回 `stale_revision`；
   - 仍走 `WorkspaceGateway.edit`，保留锁 / revision / audit；
   - 成功返回新 revision / chars / matched / replaced。

### 明确不做

- 不做 Git Patch / `apply_patch`（Change Pipeline 的另一层）；
- 不做多文件批量编辑（V1.1）；
- 不做模糊匹配或自动猜测；
- 不让 Tool Runtime 写回 Workspace；
- 不新开一条绕过 Gateway 的写路径。

## Consequences

### 正面

- 大文件改一行只需 old/new 片段，输出 token 可降 5–10 倍。
- 范围读取降低输入 token，并让 Agent 能继续读下一段。
- 严格唯一匹配 + revision CAS，避免“改错位置/覆盖别人修改”。
- 与现有 Workspace 锁 / revision / audit / Experience 一致。

### 负面 / 代价

- old/new 仍是文本级编辑，不是语法树/行号编辑；需要足够上下文。
- 多处相同文本必须由 Agent 提供更多上下文或显式 replace_all。
- 没有多文件原子编辑；失败只影响当前文件。
- 仍然没有 `workspace_search`，找代码可能还要读多段。
- 不是 Change Pipeline Patch：没有 patch provenance / applicability / review。

## Alternatives considered

1. **只做 Git Patch/Apply** —— 拒绝：V1 日常编辑不需要完整变更流水线；
2. **`apply_patch` unified diff** —— 可行但解析/校验更复杂，先做 old/new；
3. **模糊匹配/自动纠错** —— 拒绝：会静默改错位置；
4. **让 run_tool 写回副本** —— 拒绝：破坏 Tool Runtime 的只读复制边界；
5. **只做行号范围编辑** —— 拒绝：Agent 仍要数行，old/new 更稳。

## References

- [`WORKSPACE_READ_EDIT_V1.md`](../WORKSPACE_READ_EDIT_V1.md)
- [`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md)
- ADR-0021（Write Lock）· ADR-0029/0030（Workspace Core）

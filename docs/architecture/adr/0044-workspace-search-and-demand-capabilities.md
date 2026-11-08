# ADR-0044：Workspace 只读搜索 + Demand 全能力接线

- **状态**：Accepted（**已实现**）
- **时间**：Workspace Search V1
- **Supersedes**：无
- **Superseded by**：无

## Context

范围读取 + `workspace_edit` 解决了“改得省 token”，但没有解决“在哪里改”：

- 工作记忆是 Agent 自己写的抽象记录（计划 / 结构 / 发现 / 事件），
  不是当前代码的索引；
- 第一次遇到代码库、找所有调用点、代码变化后，仍然需要搜索；
- 之前只有 `workspace_read` 全文/范围读，没有 query。

同时，新做的 Demand Entry V1 只接入了最小动作集，
生产层已有的工作记忆、短期/长期记忆、私有空间、公共资料、
资源责任等能力没有接进 Demand CLI。

## Decision

1. **新增 `workspace_search`**
   - 只读字面搜索；`query` + 可选 `path/glob/max_results/case_sensitive/context_lines`；
   - 走 `WorkspaceGateway.list/read`；参与者检查 / 路径边界照旧；
   - 不建索引、不缓存、不写回；
   - 最多 200 文件 / 单文件 200k 字符 / 20 条命中；
   - 返回 path/line/snippet/context/total_matches/files_scanned/truncated。

2. **Demand CLI 接入生产层已有能力**
   - public_query；
   - private_write/read/list/delete；
   - resource_create/handoff/release；
   - public/private working memory read/update；
   - short-term read/update/reinforce；
   - long-term search/read/mark_used/consolidate；
   - workspace_search/edit。

3. **工作记忆只做注入与动作入口**
   - 不在 search 后自动写工作记忆；
   - 不自动总结/压缩；
   - 只把当前工作记忆、短期、长期摘要作为事实放进 prompt；
   - 是否记录、记录什么，由 Agent 自己决定。

### 明确不做

- 不做全文索引 / 向量库 / 代码图谱；
- 不做正则搜索（V1）；
- 不做跨 Task 搜索；
- 不自动维护工作记忆；
- 不把 search 结果当成工作记忆。

## Consequences

### 正面

- Agent 可以 `search → read(offset/limit) → edit(old/new)`，
  形成低 token 的代码修改闭环。
- Demand CLI 不再是“缩水版 Agent”，生产层能力可用。
- 工作记忆与搜索职责清楚：一个是 Agent 的笔记，一个是当前代码查询。
- 搜索只读，不破坏 Workspace 锁 / revision / audit。

### 负面 / 代价

- 搜索是字面匹配，不支持正则/语义；
- 每次搜索都要扫文件（受 200 文件上限）；
- 没有索引，超大仓库会截断；
- 不自动写工作记忆，Agent 可能重复搜索；
- 多文件批量编辑 / apply_patch 仍未实现。

## Alternatives considered

1. **只靠工作记忆** —— 拒绝：WM 是 Agent 自写缓存，不是当前代码索引；
2. **只靠 run_tool(python) grep** —— 拒绝：非一等公民、输出易截断、要写脚本；
3. **建全文索引** —— 拒绝：V1 过重，且 Workspace 是权威、索引会过期；
4. **自动把 search 结果写 WM** —— 拒绝：WM 是 Agent 的自主记录，系统不控制；
5. **在 Demand 只接 search，不接记忆** —— 拒绝：生产层已具备的能力不应在真实入口缺失。

## References

- [`WORKSPACE_SEARCH_V1.md`](../WORKSPACE_SEARCH_V1.md)
- [`WORKSPACE_READ_EDIT_V1.md`](../WORKSPACE_READ_EDIT_V1.md)
- [`DEMAND_ENTRY_V1.md`](../DEMAND_ENTRY_V1.md)
- ADR-0043（范围读取 + 局部编辑）

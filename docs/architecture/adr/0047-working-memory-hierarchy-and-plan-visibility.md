# ADR-0047：工作记忆层级 + 计划完成度可见

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_c6e093dc` 暴露了工作记忆的实现缺口：

- E 调用 `public_working_memory_update`，参数是
  `{"expected_revision": 0, "content": "# 协作板 ..."}`；
- executor 只认 `items` / `remove_keys`，`content` 被静默忽略；
- 更新返回 success，revision 从 0 变 1，但 `items = {}`；
- 后续所有 Agent 每轮看到的公共工作记忆都是“还没有条目”；
- A 没有使用 Private Working Memory 做抽象分解；
- `workspace_plan` 只在 `run.json` 里，没有进入 task prompt，
  所以 `src/train.js` / `README.md` / `review/` 缺失没有被持续跟踪。

## Decision

### 1. 工作记忆更新不再静默成功

`public_working_memory_update`：

- 只允许 `expected_revision` / `items` / `remove_keys` / `content`；
- 未知参数 → 结构化失败（`unknown_parameters`）；
- `content` 作为单条 `note` 的简写，存成 key=`note`；
- `items`/`content`/`remove_keys` 全为空 → `empty_update` 失败。

`private_working_memory_update`：

- 只允许 `expected_revision` / `nodes` / `remove_nodes`；
- 未知参数 → 失败；
- **空更新仍然允许**：work checkpoint 明确允许“检查后没有变化”。

### 2. Public Working Memory 支持层级

`WorkingMemoryItem` 新增可选 `parent_key`：

```json
{
  "plan.train": {
    "kind": "plan",
    "value": {"title": "火车模块", "status": "in_progress"},
    "parent_key": "plan.root"
  }
}
```

- 父子关系更新时校验；
- 不能形成环；
- 不能删除还有子条目的节点（除非同时删除子条目）；
- prompt 按树缩进渲染。

Private Working Memory 原本就有 `parent_id` 树，保持不变。

### 3. Task prompt 显示计划完成度

每个 task turn 从 `run.workspace_plan` + Workspace 实际状态派生：

```text
========== 计划完成度（Creator workspace_plan） ==========
directories:
- src/  ✅  (owner=agent_a)
- review/  ❌ missing  (owner=agent_e)
files:
- src/train.js  ❌ missing  (owner=agent_a)
```

这是系统派生的客观检查，不是公共工作记忆，也不是 Task 状态。
它不自动替 Agent 写记忆。

### 4. Prompt 给出 schema

Task prompt 明确 Public/Private WM 的字段：

- Public: `items: key -> {kind, value, parent_key?}`；
  kind = plan / assignment / checklist / progress / note；
- Private: `nodes: node_id -> {kind, title, content?, parent_id?}`；
  kind = goal / branch / task / finding / issue / note。

### 明确不做

- 不自动把 `workspace_plan` 写进公共工作记忆；
- 不自动总结/压缩工作记忆；
- 不把工作记忆变成 Task 状态或 Shared Context；
- 不强制 Agent 必须更新工作记忆。

## Consequences

### 正面

- `content` 不再被静默吞掉；
- 公共计划可以有层级（大架构 → 模块 → 细节）；
- 缺失的计划资源每轮可见；
- Agent 能看到 schema，减少“字段猜错”。

### 负面 / 代价

- Public WM item 增加 `parent_key`，旧数据仍兼容（缺省 None）；
- 更新校验更严格，错误会更多但可见；
- 计划完成度每轮做少量 Workspace 读取；
- 系统仍不保证 Agent 一定维护工作记忆。

## References

- `core/public_working_memory.py`
- `services/public_working_memory_service.py`
- `actions/public_working_memory.py` · `actions/private_working_memory.py`
- `demand/prompt.py`
- `docs/architecture/PUBLIC_WORKING_MEMORY.md`

# ADR-0048：单次写入硬上限 + 工作记忆 Evidence

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_795139c7`（26 轮、NO_PROGRESS、Task IN_PROGRESS）暴露两个
只靠文字提醒无法解决的问题：

### 1. 大文件写入

- A 的 LLM 调用出现 **3 次 `finish_reason=length`**；
- `requested_cognitive_budget` 始终是 16384，没有升到 32768；
- `workspace_write = 3`、`workspace_edit = 0`；
- 计划中的 `src/main.js` / `terrain.js` / `props.js` / `env.js` /
  `README.md` 全部缺失；
- 已有的“工作单元压力”只是 prompt 文字，A 连续截断后仍然试图
  一次写完整文件。

### 2. 工作记忆的完成声明没有证据

- E 在 Public WM 写 `acceptance_ready: "tests/acceptance.spec.js 已落盘"`；
- 实际 Workspace 的 `/tests` 是空目录；
- `public_working_memory_update` 只校验 revision / items 形状，
  不校验“文件是否真的存在”；
- 同一个信封里 `workspace_write` 失败也不会阻止后面的 WM 更新。

> 根因不是“Agent 忘了先写文件”，而是**系统没有把完成声明与
> 真实 artifact 绑定**。

## Decision

### 1. 单次写入/编辑硬上限（结构约束，不是建议）

`WorkspaceWriteExecutor` 新增可选 `max_content_chars`；
`WorkspaceEditExecutor` 新增可选 `max_new_string_chars`。

- 在调用注入的 `write_fn` / `edit_fn` **之前**检查；
- 超限 → `success=false`、`metadata.code="content_too_large"`；
- `metadata` 同时给出 `chars` / `max_chars`；
- `output=None` —— 不制造“写入发生过”的假象；
- 未配置上限（`None`）时保持旧行为，实验调用方不受影响。

Demand 组合根设置：

```python
MAX_SINGLE_WRITE_CHARS = 12_000
```

`workspace_write.content` 与 `workspace_edit.new_string` 都使用该值。
`append` 的限制作用于**本次新增片段**，不是追加后的整份文件。

### 2. Public WM item 支持 evidence

```json
{
  "kind": "checklist",
  "value": {"tests_passed": true},
  "evidence": {"path": "tests/acceptance.spec.js", "revision": 2}
}
```

`PublicWorkingMemoryService.update()` 在写入前校验：

- `evidence` 必须是对象；
- `path` 非空；
- Gateway 能读到该 artifact；
- `revision` 与当前 artifact revision 一致（缺省则不比对）。

任一项失败 → `WorkingMemoryValidationError`，更新不落盘。

没有 `evidence` 的条目仍然允许写入，但渲染为：

```text
evidence: (none — self-report, not verified)
```

有 evidence 时：

```text
evidence: tests/acceptance.spec.js @R2 (verified)
```

`evidence` 随 item 持久化，更新其他字段时缺省保留；显式 `null` 才清除。

### 3. Prompt 写清结构事实

Task prompt 明确：

- 单次 `content` / `new_string` 最多 12000 字符；
- 超限会被拒绝且不会写入；
- 大文件必须 `create` 小骨架 + `append` / 多次局部 `workspace_edit`；
- 不要在写文件的同一个信封里声称完成；先拿到 success + revision，
  再在后续更新中用 `evidence` 记录。

## 明确不做

- 不自动替 Agent 拆分文件；
- 不根据内容长度推断“应该 append 还是 replace”；
- 不自动把 `workspace_plan` 写进工作记忆；
- 不自动总结/压缩工作记忆；
- 不把 `evidence` 变成持续监控 —— 文件之后被修改时，旧 evidence
  不会自动失效；需要反映最新状态时由 Agent 重新更新。

## Consequences

### 正面

- 超大写入不再“先截断、下一轮再提醒”，而是根本不会产生写副作用；
- Agent 收到明确的 `content_too_large` + `chars` / `max_chars`，
  可以据此拆分；
- 完成声明可以选择绑定真实 artifact 与 revision；
- 无法验证的声明在渲染上明确标为 self-report。

### 负面 / 代价

- 12,000 字符的阈值可能拒绝某些合理的大文件单次写入；
  需要拆成多次 Action；
- 每次带 evidence 的更新会多一次 Workspace 读取；
- evidence 是记录时刻校验，不是持续一致性；
- 系统仍不强制 Agent 写工作记忆或提供 evidence。

## References

- `actions/workspace_write.py`
- `actions/workspace_edit.py`
- `demand/actions.py`
- `demand/prompt.py`
- `core/public_working_memory.py`
- `services/public_working_memory_service.py`
- `docs/architecture/PUBLIC_WORKING_MEMORY.md`
- `docs/architecture/DEMAND_ENTRY_V1.md`

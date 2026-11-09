# ADR-0054：Private Draft Buffer + 批量 Promotion

- **状态**：Accepted（**已实现 Phase 1**）
- **时间**：Demand Entry V1 生成/落盘分离
- **Supersedes**：无
- **Superseded by**：无

## Context

大文件生成的真实问题不是“模型不会写”，而是：

- 生成内容与“写到哪个文件、怎么拆”绑在同一个 Action 里；
- 一次输出过大时，`workspace_write` 的 content 还没形成就被截断；
- 即使内容已经生成，下一次 promotion 仍要重新输出正文；
- 多文件时 Agent 容易每轮只写一个文件。

用户确认的目标：

> 先让模型生成，再决定怎么落盘/拆文件；  
> 生成后的下一次调用应能**一次 promote 多个文件**，而不是一轮一个。

## Decision

### 1. 私有 Draft Buffer（内存，按 Agent 隔离）

- `draft_write(content, label?)` → `draft_id`
- `draft_append(draft_id, content)` → 分块追加
- `draft_read(draft_id, offset?, limit?)` → 按字符范围读取
- `draft_list` / `draft_delete`

边界：

- 草稿只属于创建它的 Agent；
- 不是 Task Workspace artifact；
- 不是 Memory 四层；
- 不参与验收 / Plan 完成度；
- 系统不会自动保存所有 API 输出，也不会自动 promotion；
- 当前 V1 存在 run 内存中，随 run 结束销毁；长期保存需显式写 private workspace。

### 2. 批量 Promotion

```json
{
  "type": "workspace_promote_drafts",
  "parameters": {
    "items": [
      {"draft_id": "draft_a", "path": "src/a.js", "write_mode": "create"},
      {"draft_id": "draft_b", "path": "src/b.js", "write_mode": "create"}
    ]
  }
}
```

- 一次 Action 写多个文件；
- 顺序执行，每个 item 独立走 `write_artifact` / Workspace Gateway；
- 锁 / revision / audit / participant 全部照旧；
- 内容来自 draft，不重新经过模型输出；
- 部分失败逐项返回 `results` + `summary`；
- 只要有一个文件落盘，就计为强进展（`workspace_promote_drafts`），
  避免 partial 成功被 `NO_PROGRESS` 忽略；
- promotion 内容不受 `workspace_write` 的 12k **模型输出**限制，
  但仍受 draft 大小上限（200k）与 Workspace 结构约束。

### 3. Prompt

- 注入草稿元数据（id / label / chars / sha256 / preview），**不注入全文**；
- `draft_read` 是 observable，读取内容会在同一轮/下一轮可见；
- 明确：

```text
生成草稿时可以使用当前允许的最高 cognitive_budget；
promotion / 审查 / 决策用较低预算。
多个文件准备好后，用一次 workspace_promote_drafts 落盘。
```

### 4. 不在本阶段做

- 不把 Cognitive Budget 上限直接改成 64k；
- 不自动捕获截断的原始 API 输出；
- 不自动 promotion；
- 不把 draft 当成 artifact / 完成声明；
- 不跨 run 持久化 draft（后续可做 private draft 表/文件）。

## Consequences

### 正面

- 生成和落盘解耦；
- 多文件可以一轮 promote；
- promotion 不需要模型再输出正文，节省 output tokens；
- 草稿可先审查/拆分，再决定路径；
- 草稿隔离，不影响其他 Agent 和 Task 状态。

### 负面 / 代价

- 草稿读取会增加 input tokens；应使用元数据 + 分块读取；
- 草稿是内存态，进程结束/崩溃会丢；
- 第一次生成仍可能被输出上限截断，需要 `draft_append`；
- 批量 promotion 不是跨文件原子事务，部分失败需要逐项处理。

## References

- `demand/drafts.py`
- `actions/workspace_draft.py`
- `demand/actions.py`
- `demand/runner.py`
- `demand/prompt.py`
- `actions/read_payload.py`
- `cognition/context.py`
- `tests/test_draft_buffer.py`
- `tests/test_demand_prompt_facts.py`
- `tests/test_demand_runner_facts.py`

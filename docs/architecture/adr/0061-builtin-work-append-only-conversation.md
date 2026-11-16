# ADR-0061：内置 Work 引擎的 Append-only Conversation

- **状态**：Accepted（**V1 已实现**）
- **时间**：2026-11-16
- **Supersedes**：无
- **Superseded by**：无

## Context

内置引擎（`BuiltinWorkRuntime`）原来每一轮都从零重建
`build_work_prompt()`，只把上一轮的完整 summary 作为新的 user
内容注入。真实任务 `task_5653831f` 的数据：

```text
142 次 LLM 调用
input tokens:  1,185,681
cache_read:      840,704
cache_hit_rate:      70.9%

20 次调用 0 命中，占 11.9% input
平均 input:      8,409 tokens
平均 cache_read: 5,962 tokens
平均未命中:      2,447 tokens
平均 round summary: 3,587 字符 ≈ 2.4k tokens
```

未命中的平均 token 数与上一轮 summary 的大小几乎重合。

DeepSeek 的缓存是自动前缀缓存：

- 只有完全匹配的 prefix unit 才能命中；
- prefix unit 出现在 user 输入结束 / model 输出结束位置；
- 每轮重写 prompt 会丢掉上一轮的 prefix unit。

因此 70.9% 不是异常，而是“每轮重建 + 完整 summary 重新注入”的
结构上限。

## Decision

内置引擎改为 **append-only conversation**：

1. 每个 `run()` 维护一条 `messages` 列表；
2. 第一轮使用完整 `build_work_prompt()` 作为 base user message；
3. 后续轮次只追加一条 **user 增量**（workspace / memory / drafts /
   last-turn facts / inbox / 新工具文档）；
4. `run_cognition` 把**最后一次请求实际使用的 messages** 返回，
   Runtime 用它继续同一个 conversation；
5. 每轮结束后把本轮 assistant summary 追加到 conversation；
6. 超过 `max_conversation_chars` 时做**确定性压缩**：
   - 保留第一条 base prompt；
   - 把旧轮次的 assistant summary 压成一条 user 摘要；
   - 保留最近 `keep_recent_rounds` 轮原文；
   - 压缩后清空工具文档“已注入”标记，下一轮重新注入。

不引入持久化 conversation 表；conversation 只活在本次 `run()` 内。
Reopen 会重新开始一条 conversation。

## Consequences

**正面**：

- 下一轮请求的 prefix 包含上一轮真实消息，而不是重建后的新 prompt；
- 预期 cache hit 从 ~70% 提升到 85%–95%（需要真实任务复测）；
- 减少重复的稳定指令和 summary 输入成本；
- 压缩策略确定、不额外调用 LLM。

**负面 / 代价**：

- conversation 会随轮次增长，必须依赖压缩阈值；
- 压缩本身造成一次 cache miss；
- 需要额外记录 `CognitiveRunResult.messages`，增加 actions 层字段；
- 旧轮次的原始工具结果在压缩后只保留 summary，细节可能丢失；
- 不同 provider 的缓存行为不同，收益需要按 provider 实测。

**因此不能做的事**：

- 不能把 conversation 无限追加而不压缩；
- 不能把 `TaskMode.SOLO` / `complete_solo` 等任务拓扑语义混进来；
- 不能把 conversation 持久化为第二套 Task 状态权威。

## 备选方案

1. **只压缩 round summary**：改动最小，但命中率上限仍受“每轮重建”限制；
2. **每轮重建但缩短 summary**：低风险，预计 +5–10 个点，不如 append-only；
3. **持久化 conversation 到 DB**：跨 run 复用，但会引入新的状态权威和迁移；
4. **依赖 provider 的显式 cache key**：DeepSeek 不支持显式缓存 key。

选择 append-only + 内存内压缩，是因为它直接利用 provider 的前缀缓存，
不引入新的持久化状态。

## 证据

- `tests/test_work_layer.py::TestBuiltinWorkRuntime::test_append_only_conversation_keeps_base_and_compacts`
- `tests/test_dsml_action_loop.py::test_run_cognition_returns_messages_for_append_only`
- 真实任务 `task_5653831f` 的 `llm_calls` / `runs/ui/rounds` 数据
- DeepSeek Context Caching：<https://api-docs.deepseek.com/guides/kv_cache>

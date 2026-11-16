# Architecture Decision Records

> **每个重要架构决策一条 ADR。**
>
> ADR 记录的是**为什么**这样决定，以及**代价是什么**。
> 代码能告诉你「是什么」，git log 能告诉你「什么时候」，
> 但只有 ADR 能告诉你「当时为什么不选另一条路」。

---

## 规则

1. **一个决策一条 ADR。** 不要把三件事塞进一篇。
2. **ADR 一旦写下不修改。** 被取代时写新 ADR，并在两边互相标注
   `Superseded by ADR-XXXX` / `Supersedes ADR-YYYY`。
3. **必须写 `Consequences`。** 包括**负面**后果 ——
   「这个决定带来什么代价」比「它有多好」更重要。
4. **`Status` 只有四种**：`Accepted` · `Superseded` · `Deprecated` · `Proposed`
5. **不大规模重写历史。** 这份索引是**回溯**建立的 ——
   它记录的是**已经发生过的**决定，不是现在才发明出来的。

---

## 索引

| ADR | 标题 | 状态 | 时间 | 由哪个阶段产生 |
|---|---|---|---|---|
| [0001](0001-identity-address-separation.md) | Identity 与 Address 分离 | Accepted | Step 3.6 | Communication |
| [0002](0002-communication-transport-separation.md) | Communication 与 Transport 分离 | Accepted | Step 3.6 | Communication |
| [0003](0003-communication-business-free.md) | Communication 层保持业务无关 | Accepted | Step 3.6 | Communication |
| [0004](0004-ledger-as-source-of-truth.md) | 账本是唯一事实来源，余额算出来 | Accepted | Step 1 | Economy |
| [0005](0005-capability-permission-separation.md) | Capability 与 Permission 分离 | Accepted | Step 4B | Task / Directory |
| [0006](0006-task-application-layer-refactor.md) | Task 应用层拆分（门面 / Handler / Mapper） | Accepted | Refactor | Refactor |
| [0007](0007-negotiation-execution-separation.md) | 议价与执行分离 | Accepted | Step 4D | Task |
| [0008](0008-negotiation-without-llm.md) | 议价不调用 LLM | Accepted | Step 4C | Task |
| [0009](0009-persistent-message-idempotency.md) | 消息幂等是持久化事实 | Accepted | Step 4D.1 | Reliability |
| [0010](0010-task-optimistic-concurrency.md) | Task 乐观锁（compare-and-set） | Accepted | Step 4D.1 | Reliability |
| [0011](0011-reentrant-transactions.md) | 事务可重入 | Accepted | Step 4D.1 | Reliability |
| [0012](0012-begin-immediate.md) | 写事务用 BEGIN IMMEDIATE | Accepted | Workspace Phase 0 | Reliability |
| [0013](0013-pid-aware-connection-cache.md) | 连接缓存 PID 感知 | Accepted | Workspace Phase 0 | Reliability |
| [0014](0014-message-claim-lease.md) | 消息认领是短期租约 | Accepted | Workspace Phase 0 | Reliability |
| [0015](0015-memory-layering.md) | Memory 四层架构 | Accepted | Step 4E | Architecture |
| [0016](0016-source-of-truth-priority.md) | Source of Truth 优先级 | Accepted | Step 4E | Architecture |
| [0017](0017-task-shared-context-alignment.md) | Task Shared Context 与 Alignment 分离 | Accepted | Step 4E | Architecture |
| [0018](0018-guard-verification-standards.md) | Guard 检测手段标准 | Accepted | Step 4E | Engineering |
| [0019](0019-task-shared-context-alignment.md) | Task Shared Context 与 Alignment 分离 | Accepted | Step 4F | Task / 共享认知 |
| [0020](0020-task-workspace-shared-resource-model.md) | Task Workspace 采用共享资源模型；Permission ≠ Write Lock | Accepted（实现为 FUTURE） | Workspace 架构定义 | Task Workspace |
| [0021](0021-explicit-write-lock-and-human-force-unlock.md) | Write Lock 采用显式持有模型 + Human Force Unlock | Accepted（实现为 FUTURE） | Write Lock Recovery | Task Workspace |
| [0022](0022-lazy-provisioning-and-workspace-lifecycle.md) | Lazy Provisioning + Workspace Lifecycle + Human Participation | Accepted（实现为 FUTURE） | Lifecycle / Human / Iteration | Task Workspace |
| [0023](0023-task-origin-and-lineage.md) | Task Origin / Creator / Lineage / Funding 是独立概念 | Accepted（实现为 FUTURE） | Task Origin & Lineage | Task |
| [0024](0024-human-demand-not-task-creator.md) | **Human Demand ≠ Task；Human ≠ Task Creator** | Accepted（实现为 FUTURE） | Task 起源模型修正 | Task |
| [0025](0025-resource-control-authorization-delegation.md) | Controller / Authorization / Delegation / Write Lock 四者分离 | ⚠️ **第一版核心地位被 ADR-0027 取代**（Authorization / Delegation 降级，Cascade Revocation 移除） | Resource 层核心设计 | Workspace |
| [0026](0026-lock-hierarchy-and-edit-session.md) | Lock 有层级与继承；Edit Session ≠ Lock | Accepted（实现为 FUTURE） | Lock 语义确认 | Workspace |
| [0027](0027-workspace-responsibility-model-simplification.md) | **第一版简化为 `Controller + Lock + Handoff + Audit`** | Accepted | 设计简化 | Workspace |
| [0029](0029-workspace-core-v1-concurrency-and-revision.md) | **Workspace Core v1 并发模型 · 责任变化不产生 Revision** | Accepted（**已实现**） | Workspace Core 第一阶段 | Workspace |
| [0030](0030-workspace-core-hardening.md) | **Hardening：一致性不变量与边界完整性** | Accepted（**已实现**） | Workspace Core 收尾 | Workspace |
| [0031](0031-database-thread-local-session.md) | **Database 线程模型：每线程一个 Session** | Accepted（**已实现**） | Freeze 前收尾 | Storage |
| [0032](0032-git-worktree-workspace-boundary.md) | **Git Worktree 与 Task Workspace 的边界** | Accepted（设计） | 第二阶段设计 | Change Pipeline |
| [0033](0033-patch-and-workspace-revision-provenance.md) | **Patch 与 Workspace Revision 的 Provenance 与 Applicability** | Accepted（设计） | 第二阶段设计 | Change Pipeline |
| [0034](0034-test-run-acceptance-separation.md) | **Test Run / Acceptance / Merge 三者分离** | Accepted（设计） | 第二阶段设计 | Change Pipeline |
| [0035](0035-execution-lease-vs-workspace-responsibility.md) | **Execution Lease 与 Workspace Responsibility 的分离** | Accepted（设计） | 第二阶段设计 | Change Pipeline |
| [0036](0036-change-pipeline-concurrency-and-crash-consistency.md) | **Change Pipeline 的并发模型与 Crash Consistency** | Accepted（设计） | 第二阶段设计 | Change Pipeline |
| [0037](0037-worktree-branch-per-worktree.md) | **Worktree 的 branch 是每个 worktree 一个** | Accepted（**已实现**） | 第二阶段 Phase 1 | Change Pipeline |
| [0038](0038-agent-world-core-boundaries.md) | **Agent / World / Asset / Capability 的核心边界**（10 条架构不变量） | Accepted（**文档，无实现**） | 架构治理补充 | Agent Core Model |
| [0039](0039-system-level-public-reference.md) | **系统级公开资料**（不是 World / 不是 Task / 不是 Public Manifest） | Accepted（**已实现**） | 架构治理补充 | Agent Core Model |
| [0040](0040-controlled-code-execution-tool-runtime.md) | **受控代码执行与测试（Tool Runtime V1）** | Accepted（**已实现**） | Tool Runtime V1 | Tool Runtime |
| [0041](0041-multimodal-visual-observation.md) | **原生多模态视觉观察（Vision Perception V1）** | Accepted（**已实现**） | Vision Perception V1 | LLM / Tool Runtime |
| [0042](0042-demand-entry-agent-task-creation.md) | **Demand Entry V1：Agent 自主建 Task + 串行调度** | Accepted（**已实现**） | Demand Entry V1 | Task / Agent |
| [0043](0043-workspace-range-read-and-edit.md) | **Workspace 范围读取 + old/new 局部编辑** | Accepted（**已实现**） | Workspace Read/Edit V1 | Workspace |
| [0044](0044-workspace-search-and-demand-capabilities.md) | **Workspace 只读搜索 + Demand 全能力接线** | Accepted（**已实现**） | Workspace Search V1 | Workspace / Demand |
| [0045](0045-agent-controlled-cognitive-budget.md) | **Agent 自主认知预算 + 可见的截断/裁剪反馈** | Accepted（**已实现**） | Demand 运行修复 | Agent / LLM / Demand |
| [0046](0046-two-stage-intake-and-workspace-plan.md) | **两阶段 Intake + Workspace Plan 锁一致性** | Accepted（**已实现**） | Demand 角色压缩修复 | Demand / Workspace |
| [0047](0047-working-memory-hierarchy-and-plan-visibility.md) | **工作记忆层级 + 计划完成度可见** | Accepted（**已实现**） | 工作记忆实现修复 | Demand / Memory |
| [0048](0048-write-limit-and-working-memory-evidence.md) | **单次写入硬上限 + 工作记忆 Evidence** | Accepted（**已实现**） | 大文件截断 / 虚假完成声明 | Demand / Workspace / Memory |
| [0049](0049-opinion-vs-fact-and-action-facts.md) | **观点 vs 事实、锁变更可见、单 Agent 动作事实** | Accepted（**已实现**） | 观点被当事实 / 锁误读 / 单 Agent 停摆 | Demand / Memory / Workspace |
| [0050](0050-stall-grace-minimum-window-and-read-cache.md) | **NO_PROGRESS 截断宽限 + 最小窗口 + 读缓存** | Accepted（**已实现**） | 截断恢复被过早停止 / 重复读正文 | Demand / Workspace |
| [0051](0051-submit-smoke-and-conflict-record.md) | **SUBMIT 前 DOM/import 门禁 + 观点/事实冲突记录** | Accepted（**已实现**） | 首帧崩溃 / 错误观点无记录 | Demand / Memory / Workspace |
| [0052](0052-thinking-directive-schema.md) | **thinking 指令 schema 与兼容归一** | Accepted（**已实现**） | invalid_thinking 频繁 | Cognition / Demand |
| [0053](0053-plan-phases-checkpoints-provisioning.md) | **Workspace Plan 阶段 / 依赖 / 检查点 / 占位标记** | Accepted（**已实现**） | 有文件清单无开发顺序 | Demand / Workspace |
| [0054](0054-private-draft-buffer-and-batch-promotion.md) | **私有 Draft Buffer + 批量 Promotion** | Accepted（**Phase 1 已实现**） | 生成与落盘绑定 / 一轮一文件 | Demand / Workspace |
| [0055](0055-life-work-layering-and-front-agent.md) | **Life / Work 分层 + Front Agent** | Accepted（**Front / Solo Work / Team Work / Availability 已实现；邀请协议为 FUTURE**） | 通用框架过重 / 角色混合 | Architecture / Life / Work |
| [0056](0056-solo-completion-and-iteration-reopen.md) | **Solo 完成语义 + Iteration / Reopen** | Accepted（**Solo 自 COMPLETE + Reopen V1 已实现；Team QA 可选**） | Solo 完成 / COMPLETED 终态 | Task / Work |
| [0057](0057-task-snapshot-private-git-history.md) | **Task Snapshot —— 任务私有 Git 历史** | Accepted（**核心已实现；UI 待接入**） | 任务过程无历史 / 不可回滚 | Pi Work |
| [0058](0058-output-commit-and-authorization-modes.md) | **Output Commit 与三种授权模式** | Accepted（**manual / limited / auto 已实现；push / 复杂 merge 为 FUTURE**） | 交付物无版本化 / 提交权限不清 | Output Commit |
| [0059](0059-task-level-output-repo-binding.md) | **Task 级输出仓库绑定** | Accepted（**已实现；Project Repo Mode 为 FUTURE**） | 全局仓库无法覆盖不同项目 | Output Commit |
| [0060](0060-project-workdir-direct-edit.md) | **Project Workdir —— 直接修改用户项目目录** | Accepted（**Phase 1 已实现；worktree / 项目提交为 FUTURE**） | 只能生成新产物 / 不能改已有项目 | Work / Workspace |
| [0061](0061-builtin-work-append-only-conversation.md) | **内置 Work 引擎 Append-only Conversation** | Accepted（**V1 已实现**） | 每轮重建 prompt，cache hit ~70% | Work / LLM |
| [0062](0062-front-message-send-and-live-follow-up.md) | **Front message_send 与 live follow_up** | Accepted（**V1 已实现**） | Front 无法给运行中 Agent 补充要求 | Front / Communication |
| [0063](0063-model-level-tool-call-format.md) | **工具调用格式按 model profile 声明** | Accepted（**V1 已实现**） | DSML 只在 DeepSeekProvider，代理路径丢失工具调用 | LLM / Config |
| [0028](0028-read-visibility-and-native-communication.md) | **Read 靠 Task Participation；Communication 是 Agent 基础能力** | Accepted（实现为 FUTURE） | Read / Visibility / Communication | Workspace / Communication |

---

## 模板

```markdown
# ADR-XXXX：<决策标题>

- **状态**：Accepted | Superseded | Deprecated | Proposed
- **时间**：Step X.Y
- **Supersedes**：ADR-ZZZZ（可选）
- **Superseded by**：ADR-WWWW（被取代时补）

## Context

当时面对什么问题？有哪些约束？为什么必须做决定？

## Decision

决定了什么。（一句话能说清最好）

## Consequences

**正面**：
- ...

**负面 / 代价**：
- ...

**因此不能做的事**：
- ...

## 备选方案

考虑过但没选的方案，以及为什么不选。

## 证据

哪个测试 / 哪次真实失败支撑了这个决定。

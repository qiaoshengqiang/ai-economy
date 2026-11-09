# ADR-0055：Life / Work 分层 + Front Agent

- **状态**：Accepted（**设计已确认；实现为 `DESIGNED`**）
- **时间**：2026-11-09
- **Supersedes**：无
- **Superseded by**：无

## Context

真实运行暴露：

- 3 文件任务消耗 403,903 tokens / 4.039 AIC / 14 turns；
- 生产只占很小一部分，大量 token 花在通用 prompt、重复读取、
  多角色混合和验收协调上；
- Front / Creator / Producer / Reviewer 混在一个 `DemandRunner`；
- 每个任务都强制注入身份、经济、公开/私有 WM、计划、验收规则。

结论：当前形态不适合“具体工作”，需要把 Agent 社会与工作执行分层。

## Decision

### 1. 三层

```text
Front Agent（人类入口）
Life 层（身份 / 经济 / 通信 / 可用状态 / 组队）
Work 层（Task / Workspace / 角色 / 工具 / 记忆 / 草稿）
```

### 2. Front Agent

- 是普通 Agent；
- **永远不创建 Task**；
- 只做：
  - 理解 Human 意图；
  - `direct_reply`；
  - 生成结构化 Brief，交给 `IDLE` Team Lead；
  - 接收 Team Result，回复 Human；
  - 转发 Human 反馈。

### 3. Team Lead

- 普通 Agent；
- 接收 Brief；
- 可以自己 Solo，也可以邀请其他 `IDLE` Agent；
- 是 Task Creator；
- 对最终 Result 负责。

### 4. Task 就是团队容器

- 不新增 `Team` 实体；
- Task 的 creator / contractor / participants / roles 表达团队；
- 团队信息不复制到第二套状态。

### 5. AgentAvailability

```text
IDLE     可被邀请
WORKING  已在 Task 中，不可接受新邀请
```

- 一个 Agent 同时只能属于一个 Work Task；
- 接受邀请是原子操作；
- Task 结束/中止后回到 IDLE；
- 当前 V1 的 Task participants 仍直接指定；邀请协议属于未来 Life 层。

### 6. Work 层精简

Work 层保留：

- Task Core；
- Workspace Core v1；
- Shared Context；
- Private Working Memory；
- Short / Long-Term Memory；
- Draft Buffer；
- Tool Runtime。

Work 层不保留：

- Agent 间消息机制；
- 身份经济；
- Wallet / 价格 / 议价 / 支付；
- 生活层邀请协议。

技术 Agent ID 仍用于 Workspace 权限、锁、审计。

### 7. 并行

```yaml
max_parallel_teams: 3
```

每个 Task/Team 独立 Task / Workspace / runtime / budget / memory scope；
共享 Identity / Wallet / Communication / Directory / LLM / Tool Runtime / DB。

### 8. Front / Team Runtime

目标：

```text
FrontAgentRuntime
  ├─ intent → direct_reply / brief
  ├─ team_registry: task_id → TeamRuntime
  └─ result_inbox → Human
```

当前 `DemandRunner` 保留为旧实验路径，不作为目标架构。

## 明确不做

- 不新增 Team 实体；
- 本阶段不改邀请协议；
- 不让 Front 创建 Task；
- 不让 Work 层出现身份经济/议价；
- 不把 Task Core / Workspace 重写成第二套状态机；
- 不把 Work 层消息机制保留下来再包一层“精简消息”。

## Consequences

### 正面

- 具体工作 prompt 可以显著变短；
- Front/Team/Worker 角色清晰；
- 多个 Team 可并行；
- Task/Workspace/Memory/Draft 继续复用；
- 生活层经济与工作层执行解耦。

### 负面 / 代价

- 需要新的 FrontAgentRuntime / TeamRuntime；
- Work 层无消息后，阻塞表达方式需要单独设计；
- 可用状态、并行调度、结果路由都是新增运行时能力；
- 旧 `DemandRunner` 与新架构会短期并存。

## References

- `docs/architecture/LIFE_WORK_ARCHITECTURE.md`
- `docs/architecture/adr/0056-solo-completion-and-iteration-reopen.md`
- `docs/architecture/ARCHITECTURE_CONSTITUTION.md` §15

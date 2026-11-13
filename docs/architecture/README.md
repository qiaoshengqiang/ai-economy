# 架构文档索引

> 这里是 AI Economy OS 的**长期架构知识**。
> 与 README（怎么跑）和 COMMANDS（有哪些命令）不同，
> 这里的文档回答**「为什么这样设计、什么不能动」**。

---

## 从哪开始

| 你的角色 | 建议顺序 |
|---|---|
| **第一次接触这个项目** | [`../../AGENTS.md`](../../AGENTS.md) → 本页 → [Constitution](ARCHITECTURE_CONSTITUTION.md) |
| **AI Coding Agent** | [`../../AGENTS.md`](../../AGENTS.md)（入口）→ Constitution §12（Guard 规范） |
| **要改某个模块** | Constitution §11（各层职责）→ §6（Memory 四层）→ 对应的 ADR |
| **想加新功能** | Constitution §15（V1 范围 / 旧称 `FORBIDDEN NOW`）→ 确认是否已解锁 |

---

## 文档清单

| 文档 | 长度 | 作用 | 什么时候读 |
|---|---|---|---|
| [`../PROJECT_STATUS.md`](../PROJECT_STATUS.md) | 中 | **当前版本 `v0.2.0` 的真实状态、推荐模式、已知限制** | 想知道「现在到底能做什么 / 不能用什么」时 |
| [AGENT_CORE_MODEL_V1.md](AGENT_CORE_MODEL_V1.md) | **高** | **Agent 是什么 · World · Gateway · Capability · Cognitive Substrate**（上层架构地图，全部标 `CONFIRMED` / `PROVISIONAL` / `OPEN`） | **讨论任何新概念之前先读** —— Autonomous Agent / Tool / Browser / Game / Multi-World / 生产资料 |
| [`../../AGENTS.md`](../../AGENTS.md) | 短 | **Developer Context** —— 导航与入口 | **每次开始工作前** |
| [ARCHITECTURE_CONSTITUTION.md](ARCHITECTURE_CONSTITUTION.md) | 长 | **长期 Source of Truth** —— 完整架构事实 | 动手改架构之前 |
| [TASK_SHARED_CONTEXT.md](TASK_SHARED_CONTEXT.md) | 中 | Task Shared Context / Alignment 的数据模型规范 | 做 Shared Context 相关工作时 |
| [PUBLIC_WORKING_MEMORY.md](PUBLIC_WORKING_MEMORY.md) | 中 | **公共工作记忆**：Task 范围计划 / 分工 / 清单 / 进度（`CURRENT`，公共区） | 做工作记忆 / owner 确认清单 / 验收门时 |
| [PRIVATE_WORKING_MEMORY.md](PRIVATE_WORKING_MEMORY.md) | 中 | **私有工作记忆**：单个 Agent 的抽象结构 / 分支 / 发现 / 问题（`CURRENT`） | 做私有记忆 / 工作结构 / 短期记忆前置时 |
| [PRIVATE_SHORT_TERM_MEMORY.md](PRIVATE_SHORT_TERM_MEMORY.md) | 中 | **私有短期记忆**：跨任务摘要，固定容量 8，超过一半触发 Agent 抽象（`CURRENT`） | 做短期/长期记忆、任务总结时 |
| [PRIVATE_LONG_TERM_MEMORY.md](PRIVATE_LONG_TERM_MEMORY.md) | 中 | **私有长期记忆**：短期记忆的高级抽象 + 权重反馈 + 具体存档（`CURRENT`） | 做长期记忆 / 权重 / 记忆检索时 |
| [TASK_WORKSPACE.md](TASK_WORKSPACE.md) | 长 | **Task Workspace 正式架构定义**（`FUTURE`） | 做 Workspace / Permission / Write Lock 时 |
| [TASK_ORIGIN_LINEAGE.md](TASK_ORIGIN_LINEAGE.md) | 中 | **Task Origin / Creator / Lineage / Funding 逻辑模型**（`FUTURE`） | 做 Child Task / 任务谱系 / 外包时 |
| [RESOURCE_LAYER.md](RESOURCE_LAYER.md) | 中 | **Resource · Controller · Authorization · Delegation · Cascade Revocation**（`FUTURE`） | 做 Workspace 内部资源控制时 |
| [LOCK_MODEL.md](LOCK_MODEL.md) | 中 | **Directory/File Lock · Explicit/Implicit · Effective Lock · Edit Session · Handoff**（`FUTURE`） | 做 Lock / 并行修改 / 交接时 |
| [RECOVERY_MODEL.md](RECOVERY_MODEL.md) | 中 | **异常检测 ≠ 治理决策 · Human Confirmation · 未来 AI Governance**（`FUTURE`） | 做 Agent 异常 / Recovery / 治理时 |
| [COMMUNICATION_MODEL.md](COMMUNICATION_MODEL.md) | 中 | **Communication 是 Agent 基础能力（不绑 Task）· Workspace Event ≠ Agent Message · Runtime Inbox** | 做通信 / Conversation / Inbox 时 |
| [TOOL_RUNTIME_V1_DESIGN.md](TOOL_RUNTIME_V1_DESIGN.md) | 中 | **受控代码执行 / 测试 · 公共只读工具库 · 独立 run 目录**（`CURRENT`） | 做代码执行 / 测试 / 工具时 |
| [OUTPUT_COMMIT.md](OUTPUT_COMMIT.md) | 中 | **任务完成后提交到独立输出仓库 · manual / limited / auto**（`CURRENT`） | 做提交 / 交付 / 输出仓库时 |
| [PROJECT_WORKDIR.md](PROJECT_WORKDIR.md) | 中 | **Project Workdir · 直接修改已有项目目录 · shadow snapshot · 项目锁**（Phase 1 `CURRENT`） | 做直接改项目 / 项目目录 / 项目锁时 |
| [PLATFORM_COMPATIBILITY.md](PLATFORM_COMPATIBILITY.md) | 中 | **平台支持矩阵 · Windows 已知差异与待改清单**（Windows `UNSUPPORTED`） | 做 Windows / WSL 移植时 |
| [PROMPT_WRITING_GUIDELINES.md](PROMPT_WRITING_GUIDELINES.md) | 短 | **Prompt 编写约定 · 事实边界优先 · 硬边界 / 软启发 · 变更流程**（`CURRENT`） | 写 / 改任何注入 LLM 的 prompt 时 |
| [adr/](adr/README.md) | 中 | **单个决策**的历史与理由 | 想知道「为什么当初这样定」时 |

**三层分工，不要重复**：

```
Constitution   ← 完整 Source of Truth（长）
     ↑
AGENTS.md      ← 导航和入口（短，不复制 Constitution）
     ↑
ADR            ← 单个决策的历史与理由（中）
```

---

## 标注纪律

**每一条关于系统的陈述都必须归入四态之一。**
把未来设计写成「现在已有」是本项目最严重的文档错误。

| 标记 | 含义 |
|---|---|
| **`CURRENT`** | 代码里**已经实现**，有测试 |
| **`DESIGNED`** | 已经设计清楚，**尚未实现** |
| **`FUTURE`** | 方向明确，但还没有设计 |
| **`FORBIDDEN NOW`** | 当前阶段**明确禁止**实现 |

### 本项目的真实例子

✅ 正确：

> `CURRENT` — 任务通过 `TaskWorkspaceRef` 持有一个 `workspace_id`。
> 它只是逻辑引用，没有路径、没有权限、没有 I/O。

❌ 错误：

> 系统已经存在完整的 Workspace Permission。

后半句把 `FUTURE` 写成了 `CURRENT`。
这会直接误导下一个开发者去调用一个不存在的 API。

---

## 当前状态速查

| | |
|---|---|
| **版本** | **`v0.2.0`**（当前状态见 [`../PROJECT_STATUS.md`](../PROJECT_STATUS.md)） |
| **阶段** | Post-Change-Pipeline-Phase-1 · Agent Core V1 · Tool Runtime V1 · Pi Work（workflow / autonomous）已实现 |
| **测试** | 以 `./dev.sh test` 当前输出为准（最近：5624 passed, 2 skipped, 30 deselected） |
| **协议** | v1 |
| **Memory 四层** | Layer 1 `DESIGNED` · **Layer 2 `CURRENT`** · Layer 3/4 `FUTURE`（见 AGENTS.md §4） |
| **Task Workspace** | `TaskWorkspaceRef` 仍是纯引用；**Workspace Core v1 已实现**（资源树 / 锁 / 版本，ADR-0029） |
| **Tool Runtime** | **V1 `CURRENT`**：公共只读工具库 python/pytest/node/playwright；独立 run 目录（ADR-0040） |
| **下一步** | Change Pipeline 的 Patch · 观察实验闭环（见 [`AGENTS.md`](../../AGENTS.md) §2） |

---

## 变更规则

1. **任何架构边界的改动，必须先改 Constitution** —— 不是「先写代码，回头补文档」
2. **每个重要决策写一条 ADR**（[模板](adr/README.md)）
3. **不要把 `DESIGNED` / `FUTURE` 写成 `CURRENT`**
4. **文档与代码矛盾时，先确认真实状态，再决定改哪一边** —— 不允许默默不一致

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
| **想加新功能** | Constitution §15（FORBIDDEN NOW）→ 确认不属于禁止项 |

---

## 文档清单

| 文档 | 长度 | 作用 | 什么时候读 |
|---|---|---|---|
| [`../../AGENTS.md`](../../AGENTS.md) | 短 | **Developer Context** —— 导航与入口 | **每次开始工作前** |
| [ARCHITECTURE_CONSTITUTION.md](ARCHITECTURE_CONSTITUTION.md) | 长 | **长期 Source of Truth** —— 完整架构事实 | 动手改架构之前 |
| [TASK_SHARED_CONTEXT.md](TASK_SHARED_CONTEXT.md) | 中 | Task Shared Context / Alignment 的数据模型规范 | 做 Shared Context 相关工作时 |
| [TASK_WORKSPACE.md](TASK_WORKSPACE.md) | 长 | **Task Workspace 正式架构定义**（`FUTURE`） | 做 Workspace / Permission / Write Lock 时 |
| [TASK_ORIGIN_LINEAGE.md](TASK_ORIGIN_LINEAGE.md) | 中 | **Task Origin / Creator / Lineage / Funding 逻辑模型**（`FUTURE`） | 做 Child Task / 任务谱系 / 外包时 |
| [RESOURCE_LAYER.md](RESOURCE_LAYER.md) | 中 | **Resource · Controller · Authorization · Delegation · Cascade Revocation**（`FUTURE`） | 做 Workspace 内部资源控制时 |
| [LOCK_MODEL.md](LOCK_MODEL.md) | 中 | **Directory/File Lock · Explicit/Implicit · Effective Lock · Edit Session · Handoff**（`FUTURE`） | 做 Lock / 并行修改 / 交接时 |
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
| **阶段** | Post-Workspace-Phase-0 / Pre-Shared-Context |
| **测试** | 2333 passed |
| **协议** | v1 |
| **Memory 四层** | 全部 `DESIGNED`，**零实现** |
| **Task Workspace** | 只有 `TaskWorkspaceRef`（一个字符串） |
| **下一步** | Task Shared Context / Alignment 最小原型 |

---

## 变更规则

1. **任何架构边界的改动，必须先改 Constitution** —— 不是「先写代码，回头补文档」
2. **每个重要决策写一条 ADR**（[模板](adr/README.md)）
3. **不要把 `DESIGNED` / `FUTURE` 写成 `CURRENT`**
4. **文档与代码矛盾时，先确认真实状态，再决定改哪一边** —— 不允许默默不一致

# RECOVERY_MODEL.md — 异常检测 · Human Governance · 未来 AI Governance

> **状态：架构设计冻结（`SETTLED`）／实现为 `FUTURE`**
>
> 这份文档定义 **Agent 异常时系统能做什么、不能做什么**，
> 以及**治理权当前属于谁、未来会如何迁移**。
>
> 上游：[`AGENTS.md`](../../AGENTS.md) → [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md)
> → [`LOCK_MODEL.md`](LOCK_MODEL.md) → **本文档**

| | |
|---|---|
| **文档状态** | 长期维护 |
| **实现状态** | **`FUTURE`** —— 零实现 |
| **当前阶段** | `Post-Step-4F / Pre-Task-Workspace` |
| **最后更新** | 设计简化（Recovery / Governance 同步） |

---

## 0. 怎么读这份文档

### 0.1 🔒 三句最重要的规则

> ## **异常检测 ≠ 治理决策**
>
> ## **超时不等于自动释放**
>
> ## **宁可 Task 暂时 BLOCKED，也不要偷偷把工作交给另一个 Agent。**

### 0.2 三条正式的否定式规则

```
Heartbeat Timeout      ≠  Lock Release
Long Inactivity        ≠  Responsibility Loss
Agent Failure Detection ≠  Automatic Reassignment
```

还有一条：

```
Agent Recovery         ≠  Lock Recovery
```

### 0.3 为什么这些规则必须存在

它们保护 [`LOCK_MODEL.md`](LOCK_MODEL.md) 建立的：

> **持续责任 + 显式交接** 模型。

如果超时能自动释放锁，那么「持续责任」就不成立了 ——
Agent 只是**推理慢**，锁就被别人拿走了。

---

## 1. 异常检测可以做、不可以做（`SETTLED`）

### 1.1 ✅ 系统可以**自动**

- 监测 Heartbeat
- 发现 Runtime 异常
- 发现长时间没有事件
- 发现 Agent 可能失联
- 记录异常
- 通知 Human

### 1.2 ❌ 系统**不能自动**

- `Release Lock`
- `Reassign Task`
- `Reassign Lock`
- `Force Unlock`
- **删除 / 覆盖 Agent 当前工作**
- **认定 Agent 已永久失败**

> 🔒 **系统只能「报告」，不能「裁决」。**

---

## 2. 当前版本的治理链路（`CURRENT`）

```
Agent / Runtime
      ↓
   异常检测          ← 系统自动
      ↓
    Human           ← 治理决策权在这里
      ↓
  治理决策
```

### 2.1 Human 的三个选择

#### 方案 A：继续原 Agent

例如 Agent 只是**长时间推理**。

```
保持：Lock 不变 · Controller 不变 · Task 不变
```

#### 方案 B：Restart / Resume

Agent 崩溃，但 Human 认为应该恢复。
重新启动 Agent 后：

```
原责任原则上可以继续保留
Agent 可以继续原 Task / 原 Lock
```

> 🔒 **`Agent Recovery ≠ Lock Recovery`**
> Agent 恢复**不等于**责任发生转移。

#### 方案 C：重新分配

Human 决定 `C → B`，则**正式发生**：

```
Task Reassignment
Lock Handoff / Reassignment
```

**并记录 Audit。**

---

## 3. Controller 在 Recovery 中的作用（`SETTLED`）

> Controller 当前重要的价值之一：
> **当某个 Agent 出现异常时，系统能够沿 Resource Tree
> 找到这块工作最近的责任负责人。**

```
data/              Controller = A
processed/         Controller = B
result.csv         File Lock = C

C 出现异常。

系统可以识别：
    Lock Holder       = C
    Nearest Controller = B
    Parent Controller  = A
```

### 3.1 🔒 但不能让 B 自动抢 Lock

> **当前版本不能让 B 自动抢 Lock。**

但是：

> **B 是 Human 进行 Recovery 时非常重要的责任节点。**

例如 Human 决定 `C → B` 时，**B 就是最自然的接手人**。

> 这就是 [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) 里 Controller
> 作为「责任锚点」的实际价值 —— **它服务于 Recovery，而不是权限判定**。

---

## 4. Recovery 需要考虑的 13 种情况（登记，不实现）

| # | 情况 |
|---|---|
| 1 | Agent 长时间没有活动 |
| 2 | Agent Heartbeat Timeout |
| 3 | Agent Runtime 崩溃 |
| 4 | 模型调用失败 |
| 5 | 工具调用卡死 |
| 6 | Agent 恢复 |
| 7 | Agent 永久无法恢复 |
| 8 | Human 重新分配 Task |
| 9 | Human 重新分配 Lock |
| 10 | Human Force Unlock |
| 11 | 崩溃前是否留下未完成修改 |
| 12 | 当前文件是否仍可作为下一 Agent 的基础 |
| 13 | 原 Revision / Audit 是否完整 |

> **第一版不需要全部自动解决。
> 但架构必须知道这些情况存在。**

---

## 5. 🔒 不要因为 Crash 自动删除 / 覆盖未完成内容（`SETTLED`）

```
Known Revision = R17

C 正在修改
      ↓
C 崩溃
```

当前文件状态**可能是**：

- 没有任何变化
- 已经写入完整内容
- 只写入了一部分
- 已经产生新的 Revision
- 已经完成测试但还没汇报

> ### **不要规定 `Crash → Auto Rollback`**
> ### **也不要规定 `Crash → Auto Keep`**

**这个问题列为 Open Question：Crash 后未完成修改的恢复语义。**
未来再专门设计。

---

## 6. Human Governance 与未来 AI Governance（`SETTLED` 边界）

### 6.1 `Current` / 近期版本：**Human 最终确认**

```
Monitoring Agent / Runtime
        ↓
    Assessment
        ↓
Human Confirmation      ← 治理权在这里
        ↓
  Governance Action
```

> **这种 Human Confirmation 可能持续多个版本。**

### 6.2 `FUTURE`：AI Governance

当 Governance Agent 足够可靠后，可以逐渐把**部分**治理决策交给 AI。

```
Governance Agent
        ↓
    Assess
        ↓
    Decide          ← 治理权迁移到这里
        ↓
    Recover
        ↓
    Audit
```

**最终长期目标**：

> 治理型 Agent 的判断**可以成为系统事实**，
> 并自动改变 Task / Lock / Workspace 状态。

例如未来：

```
Governance Agent 判断 C = FAILED
        ↓
系统正式改变 Agent C = FAILED
        ↓
执行 Task Reassignment · Lock Recovery · Workspace Recovery
```

> ⚠️ **当前不要实现**，只需为未来**保留清晰接口和状态语义**。

---

## 7. 当前版本的治理型 Agent 判断是什么（`SETTLED`）

| | 当前版本 | `FUTURE` |
|---|---|---|
| 治理型 Agent 的判断 | **「分析与判断结果」** | **正式治理事实** |
| 是否需要 Human Confirmation | ✅ **需要** | ❌ 不需要 |
| 能否自动改变 Lock / Task | ❌ 不能 | ✅ 可以 |

> 🔒 **不要**把当前架构设计成**永久 Human-only**，
> 也**不要**现在就假设 AI Governance 已经实现。
>
> **目标：让治理权可以随着系统成熟逐步从 Human 迁移给 AI。**

---

## 8. 与 ADR-0021 / ADR-0022 的关系

| 既有决策 | 本文档 |
|---|---|
| **ADR-0021**：v1 不实现自动 Lock Expiry · Human Force Unlock 是高权限治理操作 | ✅ **完全一致**，本文档把它扩展到**所有** Recovery 场景 |
| **ADR-0021**：`Observation ≠ Conclusion` | ✅ **同一条原则** |
| **ADR-0022**：out-of-band 修改**不阻止不监控** | ✅ 一致 |
| **ADR-0022**：Task 结束后 `FROZEN`，**允许 Reopen** | ✅ 一致 |

> **本文档没有引入新的锁状态，没有改变任何既有流程。**
> 它做的是：把 ADR-0021 的**单点结论**（锁不自动过期）
> 提升为**完整的治理模型**（哪些能自动、哪些必须人工）。

---

## 9. Open Questions（`OPEN` —— 本轮不自行拍板）

| # | 问题 |
|---|---|
| 1 | **Crash 后未完成修改如何恢复** |
| 2 | **Agent 长期不可用后的人工 Recovery 流程** |
| 3 | **Human Force Unlock 的完整治理规则** |
| 4 | **未来 AI Governance 的权限边界** |
| 5 | **治理型 Agent 的判断如何逐步成为系统事实** |
| 6 | Lock Handoff 的完整状态机（见 [`LOCK_MODEL.md`](LOCK_MODEL.md) §18） |
| 7 | Task 完成后的 Lock 处理 |
| 8 | Git Worktree 如何接入 |
| 9 | Patch / Revision / Acceptance 如何接入 |
| 10 | Workspace Gateway 的最终接口 |
| 11 | 是否最终完全移除 Delegation 作为独立对象 |

---

## 10. 一句话

> **系统可以「发现异常」，但只有 Human（未来是 Governance Agent）
> 才能「决定责任归属」。**

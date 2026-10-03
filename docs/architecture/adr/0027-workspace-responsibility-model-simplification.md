# ADR-0027：Workspace 第一版简化为「责任系统」

- **状态**：Accepted（**取代 ADR-0025 的第一版核心地位**）
- **时间**：设计简化
- **取代**：ADR-0025 中 **Authorization / Delegation / Cascade Revocation** 的**第一版核心**地位
- **保留**：ADR-0021（锁语义）· ADR-0026（Lock 层级）**全部有效**
- **规范**：[`RESOURCE_LAYER.md`](../RESOURCE_LAYER.md)（顶部横幅）· [`RECOVERY_MODEL.md`](../RECOVERY_MODEL.md)

## Context

ADR-0025 建立了 `Controller + Authorization + Delegation + Lock` 四者分离，
ADR-0026 加了 Lock 层级与 Edit Session。

进一步讨论后发现两个问题：

### 问题 1：Authorization 与 Controller + Lock 概念重叠

很多原本由 Authorization 表达的问题，现在**可以推导**：

| 问题 | 不需要 Authorization 也能判断 |
|---|---|
| B 能不能修改文件？ | `Effective Lock == B` |
| B 能不能修改目录结构？ | `Effective Directory Lock == B` |
| B 能不能创建文件？ | 父 `Directory Lock` 是否 B 持有 |
| B 能不能接手别人的 File？ | 不能抢 —— 需要 **Handoff** |
| B 能不能把子目录交给 C？ | B 是该层 Controller / 当前责任人 |

引入 `READ/WRITE/CREATE/DELETE/EXECUTE/DELEGATE` 这套通用权限集合，
会让第一版变成**企业 IAM 系统** —— 而那不是要解决的问题。

### 问题 2：Cascade Revocation 与新的责任模型冲突

ADR-0025 规定「上级撤销 → 下级级联失效」。但简化后的模型说：

> **Controller Reassignment 不应自动摧毁所有下级具体工作责任。**

因为现在强调的不是「权限从 A 传给 B、B 再传给 C」，
而是 **Controller（长期责任）+ Lock（当前工作责任）+ Handoff（责任交接）**。

## Decision

### 一、第一版核心模型（**推荐模型**）

```
Workspace
    ↓
Resource Tree
    ↓
Controller         谁长期负责？
    ↓
Lock               当前具体谁在干？
    ↓
Handoff            责任如何交接？
    ↓
Audit              谁在什么时候做了什么？
```

### 二、Controller 定义**收窄**

> **Controller 是「目录层级的责任归属标记」。**

| | |
|---|---|
| **最小粒度** | **Directory** —— **不要给 File 单独设计 Controller** |
| **回答** | 「这块目录层级**长期归谁负责**？」 |
| **不回答** | ❌ 「谁可以执行某个操作？」 |

**它不是**传统 Owner，**也不是**复杂权限系统里的 Role。

**新增作用**（设计简化的重点）：
> Controller 是**异常发生时的责任锚点** ——
> 用于找到应该由谁参与 Recovery。

### 三、Authorization **降级为 `FUTURE`**

> **第一版不要求独立的 Authorization 核心对象。**

- ❌ 不设计 `READ/WRITE/CREATE/DELETE/EXECUTE/DELEGATE` 通用权限集合
- ❌ 不创建 `AuthorizationRequest`
- ✅ 访问语义改由 `Membership` / `Controller` / `Lock` / `Boundary` / `Handoff` 推导

> 🔒 **不要把 AI Economy OS 第一版变成企业 IAM 系统。**

### 四、Delegation **降级为 Handoff / Responsibility Transfer**

```
对 Directory：  Controller A → Controller B
对 File：       File Lock  A → File Lock  B
```

> 第一版**不需要**复杂的 `Delegation Tree` / `Delegation Object`。

### 五、❌ Cascade Revocation **第一版移除**

**关键例子**：

```
data/                   Controller = A
data/processed/         Controller = B
data/processed/result.csv   File Lock = C

A 把 processed/ 的 Controller 收回 → Controller = A

result.csv              File Lock = **仍然 = C**
```

> 🔒 **A 不应该因为成为 Controller 就自动夺走 C 的 File Lock。**
>
> 如果 A 确实需要停止 C 的工作，应通过正式的
> **Recall / Reassignment / Handoff / Force Unlock**，**并产生 Audit**。

### 六、Agent 异常处理：**异常检测 ≠ 治理决策**

```
Heartbeat Timeout       ≠  Lock Release
Long Inactivity         ≠  Responsibility Loss
Agent Failure Detection ≠  Automatic Reassignment
Agent Recovery          ≠  Lock Recovery
```

**系统可以自动**：监测 Heartbeat · 发现 Runtime 异常 · 记录 · 通知 Human

**系统不能自动**：`Release Lock` · `Reassign Task` · `Reassign Lock` ·
`Force Unlock` · 删除/覆盖 Agent 当前工作 · 认定 Agent 已永久失败

```
Agent / Runtime → 异常检测 → Human → 治理决策
```

> ## **宁可 Task 暂时 BLOCKED，也不要偷偷把工作交给另一个 Agent。**

### 七、Human Governance 与未来 AI Governance

| | `CURRENT` | `FUTURE` |
|---|---|---|
| 治理权 | **Human Confirmation** | **Governance Agent** |
| 治理型 Agent 的判断 | 「分析与判断结果」 | **正式治理事实** |
| 能否自动改 Lock/Task | ❌ | ✅ |

> 🔒 **不要**设计成永久 Human-only，**也不要**现在就假设 AI Governance 已实现。
> **目标：让治理权可以随着系统成熟逐步从 Human 迁移给 AI。**

### 八、🔒 Crash 不自动 Rollback，也不自动 Keep

```
Known Revision = R17 → C 正在修改 → C 崩溃
```

文件状态**可能是**：没变化 / 完整 / 部分 / 已产生新 Revision / 已完成测试未汇报。

> **不要规定 `Crash → Auto Rollback`，也不要规定 `Crash → Auto Keep`。**
> 列为 **Open Question**。

## Consequences

**正面**：

- 概念数量**减少** —— 从四个核心对象降到 `Controller + Lock + Handoff + Audit`
- 不再是 IAM 系统，实现复杂度**显著下降**
- `Controller` 有了**明确且单一**的含义（长期责任标记）
- Controller 成为 **Recovery 的责任锚点** —— 这是它的真实价值
- 超时/崩溃**不会**偷偷转移工作 → 保护「持续责任」模型
- 治理权迁移路径**明确**（Human → AI），但**当前不实现**

**负面 / 代价**：

- `Authorization` / `Delegation` 的既有文档**需要标注降级**，
  否则会误导实现者（已在 `RESOURCE_LAYER.md` 顶部与相关小节标注）
- **Read / Visibility 规则未定** —— 移除 Authorization 后这块是空白（`OPEN`）
- Cascade Revocation 移除后，**「A 撤销 B，B 给 C 的怎么办」没有自动答案** ——
  需要人工走 Recall / Reassignment
- `Controller` 从「权限判定依据」变成「责任标记」，
  意味着**不能再用它做访问控制**，防止误用需要纪律

**因此不能做的事**：

- ❌ 不许给 **File** 设计独立 Controller
- ❌ 不许把 Controller 当成权限 Role / 访问控制依据
- ❌ 不许在第一版引入通用 `READ/WRITE/...` 权限集合
- ❌ 不许创建 `AuthorizationRequest`
- ❌ 不许实现 **Cascade Revocation**
- ❌ 不许让 `Controller Reassignment` **自动摧毁**下级 Lock
- ❌ 不许让超时/崩溃**自动**释放 Lock 或重新分配
- ❌ 不许规定 `Crash → Auto Rollback` 或 `Crash → Auto Keep`
- ❌ 不许把治理设计成**永久 Human-only**

## Future evolution

```
今天（本 ADR）：
    第一版核心 = Controller + Lock + Handoff + Audit
    Authorization / Delegation 降级 FUTURE
    Cascade Revocation 移除
    异常检测 ≠ 治理决策（Human Confirmation）
    Crash 后恢复语义 = OPEN

未来：
    AI Governance 逐步接管
    治理型 Agent 判断成为系统事实
    （可能）恢复 Authorization 模型 —— 作为 Future Extension
```

## Guard

文档守卫（含反向测试）必须能抓：

| 违规 | |
|---|---|
| 给 File 设计 Controller | ✅ |
| 把 Controller 当权限 Role | ✅ |
| 把 Authorization 写成第一版核心 | ✅ |
| 创建 AuthorizationRequest | ✅ |
| 实现 Cascade Revocation | ✅ |
| Controller Reassignment 自动摧毁下级 Lock | ✅ |
| 超时/崩溃自动释放 Lock 或重分配 | ✅ |
| 规定 Crash → Auto Rollback/Keep | ✅ |
| 把治理写成永久 Human-only | ✅ |

**不使用 substring-only 检查。**

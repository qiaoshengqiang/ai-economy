# Public Working Memory（公共工作记忆）

> **状态：`CURRENT`（2026-11-08，第一版）**
>
> 范围：公共工作记忆；私有工作记忆见
> [`PRIVATE_WORKING_MEMORY.md`](PRIVATE_WORKING_MEMORY.md)。
> 短期/长期记忆仍为 `FUTURE`。

---

## 1. 它是什么

Task 范围内、**参与者可见、可更新**的操作状态：

- 总计划 / 阶段划分
- 分工与责任
- 完成清单（owner 的最终确认项）
- 当前进度 / 已知阻塞
- 需要共享的抽象信息

它解决的是「工作过程中，团队把计划、分工、完成情况放在哪里」。
不是任务生命周期权威，也不是已确认的共同认知。

---

## 2. 三条硬边界

| 它**不是** | 原因 |
|---|---|
| **Task Core** | `status` / `agreed_price` / `contractor` / `budget` 只属于 Task Core；工作记忆禁止用这些 key 复制权威字段 |
| **Shared Context** | Shared Context 是 Proposal → Alignment 后的**共同认知**；工作记忆是可变操作状态，不会自动成为团队决定 |
| **Private Working Memory** | 私有区属于单个 Agent；本次未实现 |

> **`Memory ≠ Alignment`**：要把工作记忆里的推测升级为共识，
> 仍然走 Shared Context 的 Proposal → Alignment。

---

## 3. 数据模型

```text
PublicWorkingMemory
  task_id
  revision          ← 乐观锁版本，从 1 开始
  items: key → WorkingMemoryItem
  updated_at
```

### item

```json
{
  "key": "checklist:acceptance",
  "kind": "checklist",
  "value": {"deliverable_exists": true, "browser_verified": true},
  "author": "agent_a",
  "updated_at": "2026-11-08T03:14:56Z",
  "parent_key": "plan.root",
  "evidence": {"path": "index.html", "revision": 3}
}
```

`parent_key` 可选：用于表达「大架构 → 局部细节」的层级；
`None` = 根条目。父子关系在更新时校验，不能形成环，也不能删除还有
子条目的节点。

`evidence` 可选：`{"path": <Workspace 相对路径>, "revision": <整数>}`。
它表示这条记忆中的完成声明有一个**真实 artifact** 作为证据：

- service 会调用 WorkspaceGateway 读取该路径；
- 路径不存在 → 更新失败；
- `revision` 与当前 revision 不一致 → 更新失败；
- 没有 `evidence` 的条目仍然可以写，但渲染时会明确标记
  `self-report, not verified`。

> 🔒 `evidence` 是**记录时刻**的校验，不是持续监控：
> 文件之后被修改，旧 evidence 不会自动失效。需要反映最新状态时，
> Agent 应重新用当前 revision 更新该条目。

### kind

| kind | 用途 |
|---|---|
| `plan` | 总计划 / 阶段 |
| `assignment` | 分工 / 责任 |
| `checklist` | 完成清单 / owner 最终确认项 |
| `progress` | 当前进度 / 阻塞 |
| `note` | 需要共享的抽象信息 |

### 限制

- key：小写开头，`[a-z0-9_.:-]`，最长 64；
- 禁止 key：`status` / `task_status` / `agreed_price` / `contractor` / `budget`；
- 单条 value ≤ 4,000 字符；整份 ≤ 32,000 字符；
- value 必须可 JSON 序列化。

---

## 4. 持久化

| 表 | 作用 |
|---|---|
| `task_public_working_memory` | 当前版本（**唯一会 UPDATE**；CAS 在 revision 上） |
| `task_public_working_memory_revisions` | 历史版本（**append-only**） |

并发模式与 Shared Context / TaskStore 相同：

```text
UPDATE ... WHERE task_id = ? AND revision = ?
影响 0 行 → ConcurrentWorkingMemoryModification
```

不自动 merge / retry / rebase。

---

## 5. 访问与动作

只有 **Task Participant** 能读/写（creator / contractor / participants）。

| action | 作用 | 参数 |
|---|---|---|
| `public_working_memory_read` | 读当前版本 | 无 |
| `public_working_memory_update` | 基于 revision 更新条目 | `expected_revision` · `items` · `remove_keys` · `content` |

`update` 的 `items` 形状：

```json
{"checklist:acceptance": {
  "kind": "checklist",
  "value": {"deliverable_exists": true, "tests_passed": true}
}}
```

`content` 是「单条 note」的简写：

```json
{"expected_revision": 1, "content": "# 当前计划 ..."}
```

它会被存成 key=`note` 的 `note` item，**不会**再被静默忽略。
`items`/`content`/`remove_keys` 全为空、或出现未知参数时，更新会明确失败。

带证据的条目：

```json
{"checklist:acceptance": {
  "kind": "checklist",
  "value": {"tests_passed": true},
  "evidence": {"path": "tests/acceptance.spec.js", "revision": 2}
}}
```

`evidence.path` 必须真实存在且 revision 一致，否则更新失败。

失败返回结构化 `ActionResult`（缺 revision、并发冲突、非参与者、非法 key 等）。

---

## 6. Prompt 集成

每轮 decision prompt 都包含当前公共工作记忆：

```text
========== 公共工作记忆（Task 范围，所有参与者可见）==========
revision = 3
[plan] plan  by agent_a at ...
  {...}
[checklist] checklist  by agent_b at ...
  {...}
```

如果 item 带 `parent_key`，渲染会按树缩进显示。

每个 item 还会显示证据状态：

```text
evidence: index.html @R3 (verified)
evidence: (none — self-report, not verified)
```

这让人能区分「Agent 声称完成」与「有真实 artifact 的完成声明」。

Task prompt 还会额外显示 **Creator workspace_plan 的计划完成度**：

```text
========== 计划完成度（Creator workspace_plan） ==========
directories:
- src/  ✅  (owner=agent_a)
- review/  ❌ missing  (owner=agent_e)
files:
- index.html  ✅  (owner=agent_a)
- src/train.js  ❌ missing  (owner=agent_a)
```

这是系统从 `workspace_plan` + Workspace 实际状态派生的**客观检查**，
不是公共工作记忆，也不是 Task 状态；它不会自动替 Agent 写记忆。

Agent 据此知道当前 revision 和他人的计划/清单。
工作循环结束后的 decision checkpoint 会提示：

> 如果计划 / 分工 / 完成清单 / 要共享的抽象信息有变化，
> 可以用 `public_working_memory_update` 更新公共工作记忆
> （`expected_revision` 用你看到的 revision）。

🔒 系统**不自动写入** —— 是否记录、记录什么，仍由 Agent 判断。

---

## 7. Owner 确认清单

接收人类命令的 Agent（`OWNER`）在 intake 相理解任务后，
可以把最终确认项写入：

```json
{"checklist:acceptance": {
  "kind": "checklist",
  "value": [
    {"id": "deliverable", "text": "交付物存在", "done": false},
    {"id": "browser", "text": "浏览器实际打开", "done": false}
  ]
}}
```

其他 Agent 可以看到这份清单，并维护自己的 `progress` / `note`。
owner 在最终确认时逐项核对。

> 第一版**没有**把「owner checklist 必须存在」变成协议硬约束；
> 这是下一步要补的验收门。

---

## 8. 实验证据（2026-11-08）

- Run：`experiments/web_collab/runs/1108-1113-1466eb5f/`
- 任务：单文件网页待办清单（添加 / 完成 / 删除 / localStorage）
- 结果：**COMPLETED**
- 墙钟：**1.4 分钟**
- 成本：**85,924 tokens / 0.85924 AIC**
- 测试：`run_tool(playwright)` → **1 passed，exit_code=0**

公共工作记忆最终 revision = **3**：

| revision | author | 内容 |
|---|---|---|
| 1 | agent_a | `plan`：交付物、生产者/验收人、验收步骤 |
| 2 | agent_b | `checklist`：交付物/单文件/添加/完成/删除/localStorage/独立验收 |
| 3 | agent_b | `progress`：已结项、Playwright 1 passed、qa/review.md 已出 |

两个 Agent 都实际调用了：

```text
public_working_memory_read
public_working_memory_update
```

Task 协议链：

```text
START → SUBMIT → VERIFY → COMPLETE
```

这次 `qa/review.md` 也真实存在（762 字符），
与上一轮的 `not_found` 形成对比。

---

## 9. 已做 / 未做

已做：

- **Private Working Memory**：见 [`PRIVATE_WORKING_MEMORY.md`](PRIVATE_WORKING_MEMORY.md)。

未实现：

- **短期 / 长期记忆**；
- **owner checklist 的协议级确认门**（按最高原则，不做硬约束）；
- 工作记忆的自动 consolidation（当前由 Agent 显式更新）；
- 工作记忆的跨 Task 复用。

下一步：把 finalize 的 snapshot 接入短期记忆。

# Private Working Memory（私有工作记忆）

> **状态：`CURRENT`（2026-11-08，第一版）**
>
> 范围：单个 Agent、单个 Task 的私有工作结构。
> 公共工作记忆见 [`PUBLIC_WORKING_MEMORY.md`](PUBLIC_WORKING_MEMORY.md)。
> 短期 / 长期记忆仍为 `FUTURE`。

---

## 1. 它是什么

Agent 在执行一个具体任务时，维护**只有自己可见**的抽象结构：

```text
goal       任务目标 / 子目标
branch     结构分支（前端 / 后端 / 验收 / 测试…）
task       当前正在做的具体工作
finding    重大发现
issue      重大问题 / 风险
note       临时笔记
```

节点通过 `parent_id` 形成树；没有父节点的就是根。

### 边界

| 它**不是** | 原因 |
|---|---|
| **Public Working Memory** | 公共区参与者可见；私有区只有自己 |
| **Shared Context** | 不经过 Proposal → Alignment，不会自动成为共同事实 |
| **Task Core** | 不保存任务生命周期 / 价格 / 承接方 |
| **短期 / 长期记忆** | 本次只做“当前任务的工作结构” |

---

## 2. 生命周期

```text
创建（第一次 update）
   ↓
每次决策：Agent 可以选择是否维护
   ↓
每次工作结束：高优先级检查一次（即使没有变化，也可以空更新）
   ↓
任务结项：finalize —— 当前行清空，历史 revisions 保留
   ↓
（未来）重要信息迁移到短期记忆
```

🔒 **没有硬约束**：系统不强制“必须更新”。
   它只在 prompt / work checkpoint 里说明这是高优先级事项。
   是否维护、维护什么，仍由 Agent 判断。

---

## 3. 数据模型

```text
PrivateWorkingMemory
  agent_id
  task_id
  revision
  nodes: node_id → PrivateWorkingNode
  updated_at

PrivateWorkingNode
  node_id
  kind: goal | branch | task | finding | issue | note
  title
  content (JSON)
  parent_id (null = root)
  updated_at
```

### 限制

- node_id：小写开头，`[a-z0-9_.:-]`，最长 64；
- title 必填，≤ 200 字符；
- 单节点 content ≤ 4,000 字符；整份 ≤ 32,000 字符；
- 不能形成环；
- 不能删除还有子节点的节点。

---

## 4. 持久化

| 表 | 作用 |
|---|---|
| `agent_private_working_memory` | 当前版本（CAS on revision） |
| `agent_private_working_memory_revisions` | 历史版本（append-only） |

任务结项：

- `finalize(agent_id, task_id)` 返回最终快照；
- 删除当前行；
- revisions 保留；
- 返回值是未来短期记忆的输入（本次不实现短期记忆存储）。

---

## 5. 动作

| action | 作用 | 参数 |
|---|---|---|
| `private_working_memory_read` | 读自己的私有工作记忆 | 无 |
| `private_working_memory_update` | 基于 revision 更新节点 | `expected_revision` · `nodes` · `remove_nodes` |

`nodes` 形状：

```json
{
  "root": {"kind": "goal", "title": "ship page", "content": {}},
  "front": {"kind": "branch", "title": "frontend",
            "content": {}, "parent_id": "root"},
  "bug1": {"kind": "issue", "title": "localStorage 丢失",
           "content": {"severity": "high"}, "parent_id": "front"}
}
```

只有 intent 的 `agent_id` 能读写自己的那份；`task_id` 由组合根绑定。

---

## 6. Prompt 集成

每轮 decision prompt 包含：

```text
========== 你的私有工作记忆（只有你能读）==========
revision = 2
- [goal] root: ship page
  {...}
  - [branch] front: frontend
    {...}
```

工作循环结束后的 decision checkpoint 会提示：

> 高优先级：检查一次私有工作记忆（抽象结构 / 分支 / 重大问题 / 重大发现）。
> 即使没有变化，也可以做一次空更新。

🔒 只是提示，不是协议拒绝条件。

### 6.1 维护反馈信号

系统从**真实事实**推导“可能需要维护私有记忆”的信号，
在下一轮 prompt 的 `私有工作记忆维护反馈` 段呈现：

| kind | 触发 |
|---|---|
| `version_lag` | 公共工作记忆更新，而私有记忆为空 / `updated_at` 更旧 |
| `plan_changed` | 公共 `plan` / `architecture` 条目晚于私有记忆 |
| `work_failure` | 动作失败 / `run_tool` 非零退出 |
| `rework` | `not_found` / `already_exists` / `locked_by_other` 等返工 |
| `cognition_failure` | 空产出 / 截断 / 信封修复 |

反馈只是**事实 + 提示**：

> 这些信号表示你对项目的理解可能前后不一致；
> 是否更新私有工作记忆、更新什么，由你判断。

🔒 **不拒绝动作、不强制更新**。
   如果 Agent 成功调用 `private_working_memory_update`，信号清空；
   否则信号保留到下一次决策（最多 6 条，去重）。

---

## 7. 与公共工作记忆的分工

```text
Private Working Memory           Public Working Memory
  自己的抽象结构                   团队可见的计划/分工/清单/进度
  重大问题 / 发现                  完成清单 / owner 确认项
  不需要对齐                       不自动成为 Shared Context
  任务结束清空当前行               任务范围内持续存在
```

两者都**不是** Task Core，也都**不是** Shared Context。

---

## 8. 实验证据（2026-11-08）

- Run：`experiments/web_collab/runs/1108-1144-1712cb52/`
- 任务：单文件网页待办清单（添加 / 完成 / 删除 / localStorage）
- 结果：**COMPLETED**
- 墙钟：**4.6 分钟**（2 个 Session）
- 成本：**313,242 tokens / 3.13242 AIC**
- Playwright：**1 passed，exit_code=0**

### 私有工作记忆 finalize

| Agent | 最终 revision | 节点数 | 清理后当前行 |
|---|---|---|---|
| agent_a | 5 | 5 | 已清空 |
| agent_b | 2 | 3 | 已清空 |

agent_a 的 final snapshot（节选）：

```text
[goal] goal: 作为验收人独立验收 task_web_1712cb52
  [issue] finding-missing: 交付物缺失导致验收不通过
  [finding] finding-plan: 验收方案已冻结
  [finding] finding-spec: playwright 工具需 Playwright Test spec
```

agent_b 的 final snapshot（节选）：

```text
[goal] goal: 交付单文件待办清单并通过 agent_a 验收
  [issue] finding_notpassed: 早期 NOT_PASSED 基于落盘前过期观测
  [finding] finding_playwright: playwright 真机自测通过
```

公共工作记忆最终 revision = **6**，记录了 `acceptance`（NOT_PASSED → PASSED）、
`progress`、`next_step`。

### 最终产物

- `site/index.html`（4,951 字符，单文件，localStorage）
- `qa/review.md`（1,047 字符）
- `qa/acceptance-plan.md`（1,330 字符）
- 截图：`deliverable/screenshot.png`
- 已通过 `browser.preview` 展示给用户

### 8.1 维护反馈实验（2026-11-08）

- Run：`experiments/tool_collab/runs/1108-1232-cccfc180/`
- 任务：`parse_duration` + pytest
- 结果：**COMPLETED**
- 墙钟：**1.4 分钟**
- 成本：**114,957 tokens / 1.14957 AIC**

agent_a 收到的反馈（第 2/3 轮）：

```text
[rework] workspace_read 失败：路径不存在：/src/parse_duration.py
[rework] workspace_read 失败：路径不存在：/tests/test_parse_duration.py
[work_failure] run_tool(pytest) exit_code=2 ok=False
[work_failure] public_working_memory_update 失败：不能使用 key='status'
[cognition_failure] 上一轮触发了信封修复
[version_lag] 公共 revision=1；你的私有 revision=0
```

结果：

- agent_b 收到 `version_lag` 后更新了私有记忆（3 个 revision / 5 个节点），
  信号清空；
- agent_a **没有**更新私有记忆（0 个 revision），信号在第 2/3 轮持续出现；
- 任务仍然 `COMPLETED` —— 反馈可见，但不是硬约束。

这说明反馈机制本身工作正常；如果希望 Agent 更稳定地响应，
下一步应该增强**反馈的可见性/激励**，而不是加协议拒绝条件。

---

## 9. 未实现

- **短期记忆**：finalize 返回的 snapshot 还没有写入短期记忆存储；
- **长期记忆**：跨 Task 的沉淀；
- **自动 consolidation**：当前由 Agent 显式 update；
- **私有记忆的检索 / 摘要 / 淘汰策略**；
- **owner checklist 的协议级确认门**（按最高原则，不做硬约束）。

# ADR-0066：Life Tools V1 —— 只读任务 + 私有空间/记忆动作

- **状态**：Accepted（**V1 已实现**）
- **时间**：2026-11-18
- **Supersedes**：无
- **Superseded by**：无
- **补充**：[ADR-0064](0064-agent-life-wake-and-delegated-alarms.md)（Life Wake）

## Context

Life 层原本只有：

```text
reply / message_send / alarm_create / alarm_cancel / no_op
```

它拿不到自己的任务历史、任务记录、私有短期/长期记忆，也不能读私有空间。
结果是：用户让 Front 去问一个 Agent「你上个任务为什么出问题」时，
Agent 只能凭消息内容猜，无法查 `task_id`、`work_state`、失败轮次或文件。

同时，Life 的边界是正确的：它不是 Work，不应该创建 Task、写 Task Workspace、
执行 shell。需要在不破坏这条边界的前提下，给它**只读任务视图 + 受控私有写**。

## Decision

### 1. 注入私有记忆和最近任务

Life prompt 增加：

```text
========== 你的私有短期记忆 ==========
（短期的 render：revision + 条目摘要）

========== 你的私有长期记忆（高权重摘要） ==========
（长期 top-5 摘要 + 低权重提示）

========== 我的最近任务（只读） ==========
- task_id | status | iteration | updated_at | description
  last_error: ...
```

- 任务只列该 Agent 参与过的（`Task.is_participant`）；
- 最多 5 条；
- 记忆/任务段有字符上限，避免 prompt 膨胀。

### 2. 只读动作（第一步）

```text
task_list
task_inspect
task_content_list
task_content_read
private_list
private_read
short_term_memory_read
long_term_memory_search
long_term_memory_read
```

- `task_inspect` 返回 Task Core 字段 + `workspace_plan` / `work_state` /
  manifest 摘要 / 最近 5 轮 / checkpoint；
- `task_content_*` 只读任务交付目录，路径必须在 workdir 内，拒绝绝对路径、
  `..`、点文件/点目录；
- `private_read` 只读自己的私有空间；
- 不返回 `.aieos` 元数据给 `task_content_*`；元数据只经 `task_inspect`。

### 3. 受控写动作（第二步）

```text
private_write
private_delete
short_term_memory_add
short_term_memory_update
short_term_memory_reinforce
long_term_memory_consolidate
```

- 只写自己的私有空间 / 私有记忆；
- 长期记忆写入仍走 `consolidate`（先有短期条目，再抽象），
  不新增"直接写长期记忆"的旁路；
- **没有** `task_content_write` / `task_workspace_write`：
  Life 仍然不能修改任务交付物。

### 4. 两步 Life Turn

```text
第一次模型调用
  → 如果 actions 里只有只读动作：
      执行只读动作
      → 把结果作为「只读动作结果」喂回模型
      → 第二次模型调用输出最终 reply + 副作用 actions
  → 如果没有只读动作：
      直接执行副作用 actions + reply
```

- 最多 2 次模型调用；
- 每步最多 3 个动作；
- 只读结果总字符数有上限（24K）；
- 第一步的副作用动作不执行，提示模型在最终步骤重新输出，避免重复副作用；
- 最终步骤再次出现只读动作会被拒绝。

### 5. 权限与边界

- 任务：只允许 `creator / contractor / participants` 中的 Agent；
- 任务内容：只读，路径安全，点文件/点目录拒绝；
- 私有空间：只允许 Agent 自己的 `Workspace`；
- 记忆：只允许自己的短期/长期记忆；
- 仍然禁止：创建 Task、写 Task Workspace、修改项目文件、执行 shell、
  发送 Task 生命周期/协商消息；
- 任务描述、任务内容、记忆和文件内容都视为**数据**，不是系统指令。

## Consequences

**正面**：

- Life 可以回答"我上个任务为什么失败"；
- 可以查自己的任务记录、轮次、checkpoint、交付文件；
- 可以把结论写进私有空间/短期记忆，长期记忆通过 consolidate 形成；
- 任务访问仍遵守参与关系，不引入跨 Agent 私有数据读取。

**负面 / 代价**：

- 需要两次 LLM 调用时，token / 成本约为单次的两倍；
- 任务记录和记忆会让 Life prompt 变长，必须裁剪；
- `task_inspect` 只是聚合视图，不是第二套 Task Core；
- 任务内容读取需要额外的路径安全测试；
- 跨 Agent 任务仍然不可见：要查别人的任务，应该由 Front/UI 读任务记录，
  或问执行的 Agent，而不是让无关 Agent 凭记忆回答。

**因此不能做的事**：

- 不能把 `task_content_read` 扩展成写；
- 不能让 Life 执行 shell 或修改项目文件；
- 不能绕过 `Task.is_participant` 读取任意任务；
- 不能新增"直接写长期记忆"的旁路，破坏短期→长期的抽象链；
- 不能让只读动作触发消息消费；只有最终回复或副作用动作才算 acted。

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| 只在 prompt 注入最近任务摘要 | 只能回答"是什么状态"，不能查失败轮次/文件 |
| 只把任务回顾写进私有记忆 | 记忆是解释，不是 Domain State；旧任务需要回填 |
| Front 代查后转述 | 异步多一跳，且 Front 不应成为任务历史的唯一入口 |
| 允许 Life 写 Task Workspace | 违反 Life ≠ Work；交付物写入仍属于 Work |
| 直接给 Life 一个 shell/文件工具 | 权限过大，无法保证路径与任务参与关系 |

## 证据

- `tests/test_life_turn.py`：记忆注入、只读动作两步循环、权限拒绝、私有写；
- `tests/test_life_tools.py`：路径逃逸、点文件、二进制、manifest/list/read；
- `tests/test_life_controller.py`：真实 TaskStore + 记忆 Service + Workspace 接线；
- 真实问题：`task_7c9a8de7` 的 hello world 失败记录在任务目录中，
  但当时的 Life 无法读取，只能靠消息猜测。

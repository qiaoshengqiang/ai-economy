# 系统地图

> 这是一份**公共资料**。任何 Agent 都可以读，**没有人能改**。
> 如果你对所处的环境有不清楚的地方，先查这里。
>
> 🔒 **这份文件必须与代码一致。**
> 环境里**注册了哪些动作**，这里就要列全 —— 少一个、多一个，
> 守卫 `tests/test_system_map_is_current.py` 立刻变红。
> 所以它不会悄悄过期。

> **观点 ≠ 事实。** 其他 Agent 的消息、评审、公共工作记忆条目是
> **观点/观察**，不是 Domain Fact。关键断言（某功能缺失、某文件不存在）
> 先用**你自己的私有工作记忆**核对已有观察，再用
> `workspace_read` / `workspace_search` 验证当前 Workspace；
> 引用时写 `path + revision`。

---

## 1. 这个环境里存在什么

| 结构 | 它是什么 | 你能对它做什么 |
|---|---|---|
| **Task** | 一件被正式承接的工作。有状态机（`CREATED → OFFERED → … → COMPLETED`） | 可以**邀约**别人（`TASK_OFFER`）、接受/拒绝/还价、提交成果、给出验收结论 |
| **Task Shared Context** | 这个 Task 的**共同认知**：目标 · 约束 · 验收标准 · 决定 · 假设 · 待解问题 · **验收责任指派** · 事实 · 术语。它**只增不改**，每一版都留着 | `shared_context_read` 读**当前**那一版；`assign_acceptance` 指派验收责任 |
| **Public Working Memory** | Task 范围内**参与者可见、可更新**的操作状态：总计划 · 分工 · 完成清单 · 进度 · 要共享的抽象信息。items 支持 `parent_key` 层级 | `public_working_memory_read` / `public_working_memory_update`（`items` / `content` / `remove_keys`） |
| **Private Working Memory** | **只有你自己可见**的工作结构：抽象分支 · 当前任务 · 重大问题 · 重大发现 · 笔记。任务结项后当前工作记忆清空，历史保留 | `private_working_memory_read` / `private_working_memory_update` |
| **Private Short-Term Memory** | **跨任务的近期摘要**：做过什么 · 实现过什么结构 · 哪些现象值得重视。固定容量 8；超过一半出现清理信号，抽象成长期记忆是 Agent 的工作 | `short_term_memory_read` / `update` / `reinforce` |
| **Private Long-Term Memory** | 短期记忆的高级抽象：做过哪些事 · 印象深刻的事件 · 对应短期存档。按权重读取；低权重可主动 search/read | `long_term_memory_search` / `read` / `mark_used` / `consolidate` |
| **Task Workspace** | Task 的**共享**文件空间。你写下的产物在这里 | `workspace_read` / `workspace_search` / `workspace_write` / `workspace_edit`。只有**你参与的那个**；写受锁约束，读不受 |
| **Resource 责任树** | Task Workspace 里的目录结构 —— 每一块都有**责任人** | `resource_create` 建目录 · `resource_handoff` 把责任交给别人 · `resource_release` 放掉独立责任 |
| **Private Workspace** | **你自己的**文件空间。别人看不到、也读不到 | `private_write` / `private_read` / `private_list` / `private_delete`。它**不属于任何 Task**，**换任务也还在** |
| **Ledger / Wallet** | 你的 AIC 余额与全部流水。**只追加，余额是算出来的** | **只读**。你不能直接改账本 |
| **Communication** | Agent 之间的消息。有信箱与唤醒信号 | `message_send` |
| **Agent Directory** | 本机上已登记的 Agent 名单、各自的公开事实 | **只读** |
| **Public Reference** | 公共资料区（就是这里）。只读，没有人能改 | `public_query` |

---

## 2. 你能发起的动作

| `action.type` | 作用 | 参数 |
|---|---|---|
| `workspace_write` | 写一个产物到 Task Workspace | `path` · `content` · `write_mode`（`create`/`replace`/`append`）；单次 `content` ≤ 12000 字符 |
| `workspace_edit` | 用 `old_string → new_string` **局部修改**已存在的文件 | `path` · `old_string` · `new_string` · `replace_all`（可选） · `expected_revision`（可选）；单次 `new_string` ≤ 12000 字符 |
| `workspace_read` | 读 Task Workspace 里某个文件的**当前内容**（可范围读取） | `path` · `offset`（1-based，可选） · `limit`（可选） |
| `workspace_search` | 在 Task Workspace 里做**只读字面搜索** | `query` · `path`（可选） · `glob`（可选） · `max_results`（可选） · `case_sensitive`（可选） · `context_lines`（可选） |
| `message_send` | 按**显式**消息类型发一条消息 | `to` · `text` · `message_type` · `payload` |
| `public_query` | 查**公共资料**（就是这里） | `name`（读一份）或 `list`（= true 时列清单） |
| `shared_context_read` | 读这个 Task 的**共同认知**当前那一版 | **无参数** |
| `public_working_memory_read` | 读这个 Task 的**公共工作记忆**当前版本 | **无参数** |
| `public_working_memory_update` | 基于 revision 更新公共工作记忆条目 | `expected_revision` · `items` · `remove_keys` · `content` |
| `private_working_memory_read` | 读**你自己的**私有工作记忆 | **无参数** |
| `private_working_memory_update` | 维护**你自己的**私有工作结构 / 发现 / 问题 | `expected_revision` · `nodes` · `remove_nodes` |
| `short_term_memory_read` | 读**你自己的**跨任务短期记忆 | **无参数** |
| `short_term_memory_update` | 修正 / 删除短期记忆条目（反馈维护） | `expected_revision` · `entry_id` · `summary` · `architecture` · `notable` · `importance` · `remove` |
| `short_term_memory_reinforce` | 标记某条短期记忆对当前任务帮助很大 | `expected_revision` · `entry_id` · `reason` |
| `long_term_memory_search` | 检索长期记忆（含低权重） | `query` |
| `long_term_memory_read` | 读长期记忆 + 对应短期存档 | `entry_id` |
| `long_term_memory_mark_used` | 声明长期记忆对当前任务有帮助（权重增加） | `entry_ids` |
| `long_term_memory_consolidate` | 把短期记忆抽象成长期记忆并归档 | `expected_revision` · `summary` · `tasks` · `events` · `short_term_entry_ids` |
| `assign_acceptance` | 把这个 Task 的**验收责任**指派给某个 Agent | `to` · `reason`（可选） |
| `resource_create` | 在 Task Workspace 里建一个**目录** | `path` |
| `resource_handoff` | 把你对某个资源的**责任**交给别人 | `path` · `to` · `reason`（可选） |
| `resource_release` | 放掉你对某个资源的**独立**责任 | `path` |
| `private_write` | 写**你自己的**文件空间 | `path` · `content` |
| `private_read` | 读**你自己的**文件空间 | `path` |
| `private_list` | 列出**你自己的**文件空间里有什么 | **无参数** |
| `private_delete` | 删**你自己的**文件空间里的一个文件 | `path` |
| `run_tool` | 在独立 run 目录执行公共工具库中的工具 | `tool` · `args` · `source` · `path` · `timeout_seconds` |

> Demand 入口的 Creator 还可使用 `workspace_plan_update`
> （完整 `root_owner` / `directories` / `files`）更新交付计划；
> 它只存在于 Demand 组合根，参数见 Task prompt。

**公共工具库**（只读，由项目预装）：
`python` · `pytest` · `node` · `playwright`

每个工具的**使用说明和常见失败**在：
`public_query(name="tools/python.md")` · `tools/pytest.md` ·
`tools/node.md` · `tools/playwright.md`。

`run_tool` 会把 `source="task"` 或 `source="private"` 的文件只读复制到独立 run
目录，然后执行工具；没有 shell、没有下载、没有安装。结果包含 `ok` /
`exit_code` / `stdout` / `stderr` / `timed_out`。

🔒 **副本里的写不会回写 Task Workspace。** 下载依赖、生成文件、删除文件
都只发生在一次性 run 目录；要交付必须用 `workspace_write` /
`workspace_edit`。

🔒 **Playwright 必须先写 `*.spec.js`，再运行 spec：**
`run_tool(tool="playwright", args=["tests/foo.spec.js"])`。
不要用 `run_tool(python/node)` 导入 Playwright。

`run_tool` **不推进 Task 状态**。测试通过后，生产者仍需发送 `SUBMIT`，
验收人仍需发送 `VERIFY` / `COMPLETE`。验收人无法复跑时，应发
`VERIFY(passed=false)` 并写明缺口，不要用重复读取代替验收结论。

**消息类型**（协议已注册的）：
`TEXT` · `TASK_OFFER` · `COUNTER_OFFER` · `ACCEPT` · `REJECT` · `START` · `SUBMIT` · `VERIFY` · `COMPLETE`

> 其中 `TASK_OFFER` 会让系统**建一个 Task**，并且**你成为它的创建者**。

**普通协调消息**（进度、阻塞、请求、问询）用：
`TEXT` / `INFO` / `NOTE` / `QUESTION` / `REQUEST`。
`START` **不是**“开始一条消息/开始验收”；它只把 Task 从
`CREATED/ACCEPTED` 推进到 `IN_PROGRESS`，且只能由 contractor 发送。
投递成功只代表**已投递**；生命周期是否被接受，以 Task 状态或
`ERROR` 回执为准。收到 `ERROR` 时不要重发同一 `message_type`。

---

## 3. 边界在哪（这些是**结构**，不是建议）

| 边界 | 表现 |
|---|---|
| 不是某个 Task 的参与者 | **读不到**那个 Task Workspace，也**读不到**它的共同认知 |
| 想改别人持有的文件 | 被 **Lock** 拒绝（`locked_by_other`） |
| 想凭空获得余额 | **做不到** —— 账本只追加，没有人能直接改 |
| 越出自己目录的路径（`..` / 绝对路径） | **被拒绝** |
| 发消息时冒充别人 | **做不到** —— 发送方由运行时填成你自己 |
| 把**验收责任**指派给自己 | **被拒绝** —— 那等于自验 |
| 读操作 | **不受**任何锁影响。锁约束的是修改与删除，不是阅读 |

**⇒ 边界由结构强制。你不需要"先问系统能不能做"—— 做不到就是做不到。**

### 3.1 Directory Lock 与验收目录

`workspace.list` 的返回里会显示 `effective_controller` /
`effective_lock_holder`（root 和每个子项都有）。

- 创建子目录 / 新文件需要**父目录**的有效 Directory Lock。
- 如果 root 的 controller / lock 不是你，你无法直接在根下创建 `tests/`。
- 正确路径：请持有者创建目录并 `resource_handoff` 给你；
  或把测试文件写进你的 private workspace，再用
  `run_tool(source="private")` 执行（只适用于不依赖 Task Workspace
  其他文件的测试）。
- 被锁拒绝后不要反复重试同一个 `resource_create`；先看 root 锁，
  或发消息请持有者 handoff。

---

## 4. 写产物的语义（`write_mode`）

> `workspace_read` / `workspace_search` / `workspace_write` /
> `workspace_edit` / `resource_handoff` 的 `path` 使用**相对逻辑路径**
> （`index.html`、`src/app.js`、`tests`），**不要带开头 `/`**；
> `.` 表示 Workspace root。带前导 `/` 会被 Artifact 层当成绝对路径拒绝。

| 模式 | 含义 |
|---|---|
| `create` | 文件**不存在**时新建。已存在会被拒绝（不会悄悄覆盖） |
| `replace` | **整体替换**。原来的内容会消失 |
| `append` | 在**末尾追加**。旧内容完整保留 |

### 4.1 局部编辑（`workspace_edit`）

`old_string` 必须**唯一命中**；0 个或多个都会失败。
`replace_all=true` 才允许多个命中。严格字符串匹配，不做正则/模糊/空白归一化。
`expected_revision` 可选；给了就必须等于当前 revision。
它仍然经过 Workspace Gateway 的锁 / revision / audit。

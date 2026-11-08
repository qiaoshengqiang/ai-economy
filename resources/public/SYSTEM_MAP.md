# 系统地图

> 这是一份**公共资料**。任何 Agent 都可以读，**没有人能改**。
> 如果你对所处的环境有不清楚的地方，先查这里。
>
> 🔒 **这份文件必须与代码一致。**
> 环境里**注册了哪些动作**，这里就要列全 —— 少一个、多一个，
> 守卫 `tests/test_system_map_is_current.py` 立刻变红。
> 所以它不会悄悄过期。

---

## 1. 这个环境里存在什么

| 结构 | 它是什么 | 你能对它做什么 |
|---|---|---|
| **Task** | 一件被正式承接的工作。有状态机（`CREATED → OFFERED → … → COMPLETED`） | 可以**邀约**别人（`TASK_OFFER`）、接受/拒绝/还价、提交成果、给出验收结论 |
| **Task Shared Context** | 这个 Task 的**共同认知**：目标 · 约束 · 验收标准 · 决定 · 假设 · 待解问题 · **验收责任指派** · 事实 · 术语。它**只增不改**，每一版都留着 | `shared_context_read` 读**当前**那一版；`assign_acceptance` 指派验收责任 |
| **Task Workspace** | Task 的**共享**文件空间。你写下的产物在这里 | `workspace_read` / `workspace_write`。只有**你参与的那个**；写受锁约束，读不受 |
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
| `workspace_write` | 写一个产物到 Task Workspace | `path` · `content` · `write_mode`（`create`/`replace`/`append`） |
| `workspace_read` | 读 Task Workspace 里某个文件的**当前内容** | `path` |
| `message_send` | 按**显式**消息类型发一条消息 | `to` · `text` · `message_type` · `payload` |
| `public_query` | 查**公共资料**（就是这里） | `name`（读一份）或 `list`（= true 时列清单） |
| `shared_context_read` | 读这个 Task 的**共同认知**当前那一版 | **无参数** |
| `assign_acceptance` | 把这个 Task 的**验收责任**指派给某个 Agent | `to` · `reason`（可选） |
| `resource_create` | 在 Task Workspace 里建一个**目录** | `path` |
| `resource_handoff` | 把你对某个资源的**责任**交给别人 | `path` · `to` · `reason`（可选） |
| `resource_release` | 放掉你对某个资源的**独立**责任 | `path` |
| `private_write` | 写**你自己的**文件空间 | `path` · `content` |
| `private_read` | 读**你自己的**文件空间 | `path` |
| `private_list` | 列出**你自己的**文件空间里有什么 | **无参数** |
| `private_delete` | 删**你自己的**文件空间里的一个文件 | `path` |
| `run_tool` | 在独立 run 目录执行公共工具库中的工具 | `tool` · `args` · `source` · `path` · `timeout_seconds` |

**公共工具库**（只读，由项目预装）：
`python` · `pytest` · `node` · `playwright`

`run_tool` 会把 `source="task"` 或 `source="private"` 的文件只读复制到独立 run
目录，然后执行工具；没有 shell、没有下载、没有安装。结果包含 `ok` /
`exit_code` / `stdout` / `stderr` / `timed_out`。

`run_tool` **不推进 Task 状态**。测试通过后，生产者仍需发送 `SUBMIT`，
验收人仍需发送 `VERIFY` / `COMPLETE`。

**消息类型**（协议已注册的）：
`TEXT` · `TASK_OFFER` · `COUNTER_OFFER` · `ACCEPT` · `REJECT` · `START` · `SUBMIT` · `VERIFY` · `COMPLETE`

> 其中 `TASK_OFFER` 会让系统**建一个 Task**，并且**你成为它的创建者**。

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

---

## 4. 写产物的语义（`write_mode`）

| 模式 | 含义 |
|---|---|
| `create` | 文件**不存在**时新建。已存在会被拒绝（不会悄悄覆盖） |
| `replace` | **整体替换**。原来的内容会消失 |
| `append` | 在**末尾追加**。旧内容完整保留 |

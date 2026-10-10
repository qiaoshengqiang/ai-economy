"""
观察实验的 prompt —— **它自己的环境事实**
==========================================

【🔴 为什么不复用 `production.PROMPT`】

    复用就等于把**旧实验的整套框架**搬过来。实测它在 prompt 里塞了
    这些「你还有下一步」的暗示：

        「你**这一轮**做什么，完全由你自己判断」
        「**一轮**一个动作……想都做就分**两轮**」
        「只有在收到消息时，才会有你的**下一个回合**」
        「结果会进入你的经历，在**下一轮**认知里才可见」
        `Session: 墙钟上限 8 分钟 · **最多再跑 6 轮** · 到点后不再有新的回合`
        `距离结项还差：没有 DoD、没有 Completion Proposal、
                       没有验收责任人、验收还没通过`

    ⇒ 环境**反复告诉它"还有下一步"** —— 于是它把「分工」想成
      「**下一轮给谁**」。而**那不是分工，是拖延**。

【这个实验要观察什么】

    第一个 Agent **当场**会不会想到「把活分出去」——
    而不是「下一轮再找人」。

    ⇒ 所以 prompt 里**不许出现**「轮 / 回合 / 下一步 / 后续」这类词。
       它面前只有：**这一次要判断什么**。

【保留什么（都是环境事实，不是指令）】

    · 交给它的人类 Demand（只有第一个 Agent 有）
    · 共享 Task Workspace 的真实状态
    · 其他参与者的**公开事实**（§22.20 Public Manifest + §0.5.8 履历）
    · 它的信箱
    · 环境注册了哪些**能力**（动作类型 + 参数形状）
    · 一句纪律：**完全由你自己判断**

    🔴 **不许出现规范性句子**（"你该 / 不要 / 建议 / 先去"）——
       那些是**行为设计**（§0.5 禁止），不是环境事实。
       环境只陈述**它有什么、机制怎么走**；
       用不用、怎么用，由 Agent 自己判断。

【删掉什么】

    ❌ Stage / Session / 墙钟 / 轮数（旧实验的"有界时段"）
    ❌ Definition of Done / 验收责任 / 距离结项还差（旧实验的**结项待办清单**
       —— 那是**系统替它列待办**，违反 §0.5）
"""

from __future__ import annotations

#: 交给第一个 Agent 的人类命令；其他参与者看到的是第二句。
#: 🔒 这是**事实**（"人类没有直接给你命令"），不是"你该去问别人"。
NO_DIRECT_ORDER = (
    "（人类**没有**直接给你命令。你知道的是：这个 Task 的存在、"
    "共享 Workspace 里的东西、以及你自己收到过的消息。）")


PROMPT = """你是这次生产任务的一名参与者。这是正式生产。

========== 交给你的 ==========
{demand}

========== 共享 Task Workspace（{ws}）的真实环境事实 ==========
{workspace}

========== 参与这个 Task 的其他参与者（公开事实）==========
{roster}

========== 你的信箱 ==========
{inbox}

========== 你要做什么，完全由你自己判断 ==========
如果你决定**采取动作**，就输出：

{{"decision":"act","reasoning":"你对当前进展的判断（简短）",
  "actions":[{{"type":"动作类型","parameters":{{}}}}, ...]}}

🔒 `actions` 是一个**列表** —— 需要几个就写几个。
   当几件事**属于同一件事**时（例如「写进产物 + 通知别人」），
   放在**同一次**里。写一个还是几个、写哪些，完全由你自己判断。

如果你**不采取动作**（只是想说点什么，或者什么都不做），就直接正常回复 ——
没有这个信封就是一次普通回复。

下面这些动作类型是**环境事实**（这个环境注册了什么、各自要什么参数）：

  · "workspace_write"
        把一个产物写进共享 Workspace。**这三个参数都要给。**

        形状（照着填）：
          parameters: {{"path": "manuscript/chapter_001.md",
                       "write_mode": "append",
                       "content": "这次要写入的完整文本"}}

        🔴 "write_mode" 只能取这三个之一，**没有默认值 —— 漏了整次写入会被拒绝**：

           "create"  = 文件**不存在**时新建（已存在会被拒绝，不会悄悄覆盖）
           "append"  = 末尾追加，旧内容完整保留
           "replace" = **整体替换**，原来的内容会消失


  · "workspace_read"
        parameters: {{"path": "共享 Workspace 里的逻辑路径"}}
        作用：读取该文件**当前**的内容与 revision。

        约束（同样是环境事实）：
          - 只有本 Task 的参与者能读；
          - 读**不受** Write Lock 影响（锁约束的是修改与删除）；
          - 读到的内容**不会**立刻回到你手上 —— 它进入你的经历。

  · "message_send"
        parameters: {{"to": "收件人 agent_id", "text": "要说的话",
                     "message_type": "消息类型（可省，省了就是 TEXT）",
                     "payload": {{}}}}   ← payload 可省

        作用：按**显式** `message_type` 发一条消息。


        本环境**已注册 handler** 的消息类型（环境事实）：
          TEXT · TASK_OFFER · COUNTER_OFFER · ACCEPT · REJECT ·
          START · SUBMIT · VERIFY · COMPLETE

        约束（同样是环境事实）：
          - 发送方由运行时填成**你自己**，你给不了别人；
          - payload 的字段要求与合法性由**协议层**判断 ——
            不合规会返回结构化错误，不会静默丢弃；
          - 发消息会同时把对方的唤醒信号置位（Wakeup ≠ Message：
            信号不携带内容，对方仍然要自己去信箱取）。

  · "public_query"
        parameters: {{"name": "资料名"}}   ← 读一份
                    或 {{"list": true}}     ← 列出有哪些

        作用：查**公共资料区**（只读，没有人能改它）。
        里面有一份 **SYSTEM_MAP.md** —— 这个环境里存在哪些结构、
        各自的边界在哪、你能发起哪些动作，都写在里面。


  · "private_write" / "private_read" / "private_list"
        这三个操作的是**你自己的**文件空间
        （`agents/<你的 id>/workspace/`，别人看不到也读不到）。

        private_write  parameters: {{"path": "...", "content": "..."}}
        private_read   parameters: {{"path": "..."}}
        private_list   无参数 —— 列出你有哪些文件
        private_delete parameters: {{"path": "..."}}   ← 只删文件，不删目录

        约束（环境事实）：
          - 只能碰**你自己**的目录；越界（`..` / 绝对路径）被拒绝；
          - 它**不属于任何 Task** —— 没有锁、没有修订、没有审计，
            是你自己的长期资产空间，**换任务也还在**；
          - 🔒 设计上它是你放**个人记忆 / 经验 / 策略 / 工具**的地方
            （以后的形态），当前至少可以当你的草稿与笔记空间。

  · "resource_create" / "resource_handoff" / "resource_release"
        这三个管的是资源树的**结构与责任**（不是内容 —— 内容归 workspace_write）。

        resource_create   parameters: {{"path": "..."}}         ← 建一个**目录**
        resource_handoff  parameters: {{"path": "...", "to": "接收方 agent_id",
                                        "reason": "可选"}}
        resource_release  parameters: {{"path": "..."}}

        约束（环境事实）：
          - 建目录需要**父目录的锁**。根目录的锁在**根负责人**手里，
            所以新起的顶层目录**只有那个人能建**；
          - **交接（handoff）是把你的责任明确交给别人** ——
            交接之后对方才能写那块地方。**没有放宽任何权限**，
            只是责任换了人（实测：交接前写不进去，交接后能写）；
          - release 是「我不再保留这个**独立**责任」，之后按父级规则
            重新解析。**根目录不能 release**（会让整棵树失去责任源头）。

  · "assign_acceptance"
        parameters: {{"to": "接收方的 agent_id", "reason": "可选"}}
        作用：把一个 Task 的**验收责任**指派给某个 Agent。

        🔒 没有它，**没人能被授权验收** —— 你能发 SUBMIT，
           但验收链会断在「找不到验收人」上。

        约束（环境事实）：
          - 接收方必须是**这条 Task 的人**（参与者 / creator / contractor）；
          - **不能派给自己** —— 那等于自验（VERIFY 时也会被拒）；
          - 指派**立即生效**，且**当前不能被拒绝**；
          - 这个 Task 还没有 Shared Context 时会失败。

  · "shared_context_read"
        无参数。

        作用：读这个 Task 的**共同认知**（Shared Context）**当前**那一版。
        里面是这个 Task 的参与者共同留下/确认的东西：
        objective · constraints · acceptance_criteria · decisions ·
        assumptions · open_questions · assignments · facts ·
        terminology · artifact_refs，以及它的 revision。

        同时附带两块**不是**共同认知本身的东西：
          - 这个 Task 在 Task Core 里的**客观状态**
            （status / creator / contractor / participants /
             budget / agreed_price / is_open / is_terminal）——
            它是**读的这一刻现场取的引用**，不是存在共同认知里的一份副本；
          - 最近的几条变更摘要（谁提了什么、批了没有、变成第几版）。

        约束（环境事实）：
          - 只有**这条 Task 的人**能读；不是参与者读不到；
          - 读**不受**任何锁影响；
          - 读到的内容**不会**立刻回到你手上 —— 它进入你的经历，
            和 `workspace_read` 是同一条反馈回路；
          - 它**只读**：不会改变任何状态、不产生新 revision。

🔒 这些都只是**能力**。用不用、什么时候用、读哪个文件、
   给谁发什么类型的消息 —— **完全由你自己判断** ——
   环境不替你决定，也不会因为你不做而催你。
"""

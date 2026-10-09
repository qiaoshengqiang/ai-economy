# ADR-0039：系统级公开资料（System-Level Public Reference）

> **状态**：Accepted（**已实现**：`public_query` + `resources/public/`）
> **日期**：2026-10-09
> **来源**：与项目架构师的设计讨论
> **本 ADR 冻结的是**：**「系统级公开资料」这一类别的存在与边界**
> **本 ADR 冻结的不是**：资料的**组织方式**、字段 schema、检索方式（全部 `OPEN`）

---

## Context

V1 要让 Agent 知道「**自己处在一个什么样的环境里**」——
这个环境里存在哪些结构、各自的边界在哪、它能发起哪些动作。

按 §0.5.5「硬边界，软行为」，这**不应该**靠往每轮 prompt 里塞一大段说明
来完成（那既侵入思考循环，又无法随系统演进而更新）。

> **要的是「告诉 Agent 去哪里查」，而不是「把内容讲给它听」。**

于是引入了一份**可查的公共资料**，并在 prompt 里只留**一句指针**。

---

## Decision

### 一、引入「系统级公开资料」这一类

它是一条**独立的能力类别**，服务于：

> **让 Agent 能查到「这个系统是什么、边界在哪、我能做什么」。**

当前形态：

```
resources/public/              宿主机上的只读目录（资料由系统 / 运维放置）
    SYSTEM_MAP.md              系统地图
    （以后：可下载的 skill 之类）

查询出口：action `public_query`
    {"name": "SYSTEM_MAP.md"}   读一份
    {"list": true}              列出有哪些
```

### 二、它**不是**什么（这一节是本 ADR 最重要的部分）

| 它**不是** | 为什么 |
|---|---|
| **不是 World** | World 是「Agent 可以进入、观察、与之交互并受到其规则约束的**外部环境**」（§四）。公共资料**不是环境**，没有状态、没有规则、没有 Actor |
| **不是 Task 内的** | 它**不绑任何 Task**；没有 Task 也应当查得到（§22.27：唤醒不要求有 Task） |
| **不是 `Public Manifest`** | §22.20 的 `Public Manifest` 是「**让其他 Agent 知道这个 Agent 是谁**」——**每个 Agent 一份**的公开身份与动态事实（Display Name / Tags / Provider / Model）。本 ADR 说的是**系统级**的公开资料。两者都叫"公开"，**但不是一回事** |
| **不是 Private Workspace** | 私有侧是「Agent 的长期私有资产空间」（§16），**按 Agent 归属**；公共资料**不属于任何 Agent** |
| **不是 Task Workspace** | Task Workspace 是 Task 的共享可变空间（锁 / revision / 审计 / 参与者）；公共资料**没有人可以改** |

### 三、🔴 它**不经过 Gateway** —— 而这是对的

`AGENT_CORE_MODEL_V1.md` 的 Action 链是

```
Action → Capability → Gateway → Workspace → Filesystem
```

**这条链描述的是「通往 World 的路径」。**
`Gateway` 的定义是「**World 的**统一接口」（§7）。

> **⇒ 不是 World 的东西，本来就不该走 Gateway。**

本 ADR 记录一个**曾经犯过的推理错误**，因为它很容易再犯：

> ❌ **错误推理**：「`public_query` 没有经过 Gateway ⇒ 它不在架构内 ⇒
> 要么给它补一个 Gateway（把它做成 World），要么标记为临时权宜。」
>
> 🔴 这是把 **`§十一` 的概念层次**读成了**强制工程管线**。
> **`World` 是概念性的，不只是工程实现上的定义。**
> 强行把非 World 的东西做成 World，正是 ADR-0038
> 「❌ 不要把 Workspace 强制重构成通用 World」所禁止的那类动作。

### 四、安全性由**结构**保证，不由 prompt 保证

| 性质 | 由什么保证 |
|---|---|
| **只读** | `services/public_reference.py` **没有写接口** —— 不是「有但不给用」，是结构上不存在 |
| **Agent 看不到宿主机路径** | Agent 给的是**逻辑名**（`SYSTEM_MAP.md`）；解析后做逃逸检查（`..` / 绝对路径 / symlink 都挡住） |
| **不需要锁 / revision / 审计 / 参与者** | 因为**没有需要 Agent 改的东西** —— 这正是不把它做成 Workspace 的理由 |

---

## 与 §十一 链的关系（一句话）

> **公共资料不在那条链上，因为那条链是通往 World 的。
> 它是系统级的公开事实，不是环境。**

---

## 仍然 `OPEN`（本 ADR **不**冻结）

- 资料的**组织方式**（现在是一个目录 + 逻辑名；以后呢？）
- 是否需要**结构化字段**（而不是整篇文本）
- **检索**方式（现在只有"按名字读"与"列清单"）
- 「以后放可下载的 skill」时，**下载/安装**是否仍属于这一类，
  还是需要新的类别（下载动作会不会改变结构 ⇒ 会不会需要 Capability / 授权）
- 是否需要给资料加**版本**

> 🔒 在这些定下来之前，**不要**给 `public_query` 加：
> 搜索、写接口、结构化 schema、安装动作。

---

## Consequences

**好的**：

- Agent 有了一个**稳定的、可增长的**环境说明入口 ——
  prompt 里只有一句指针，说明本身可以随时演进而不侵入每一轮
- 与「正文属于 Workspace」同一条纪律：**信息属于它能查的地方，prompt 只给指针**
- 概念上给「非 World、非 Task、系统级公开」的东西**第一次留了位置**

**代价 / 需要留意的**：

- **它当前只有一个使用者**（`public_query`），类别是否会被滥用尚不可知 ——
  以后任何「说不清属于谁的东西」都可能被塞进来。**所以第二节的边界要守住。**
- `resources/public/` 目前**没有**任何写入流程（连系统自己也没有：
  资料靠人工放置）⇒ 资料与代码的同步是**手工的**

---

## 相关

- [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) §4（World）· §6（Private Workspace 与 World）· §7（Gateway）· §16（Private Workspace 长期定位）· §22.19 / §22.20（Other-Agent Model / Public Manifest）
- [`adr/0038-agent-world-core-boundaries.md`](0038-agent-world-core-boundaries.md)（不变量 #9 `World ≠ Agent` · #10 `Private Workspace ≠ Task Workspace`）
- 实现：`services/public_reference.py` · `actions/public_query.py` · `resources/public/SYSTEM_MAP.md`

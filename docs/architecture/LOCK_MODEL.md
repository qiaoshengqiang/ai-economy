# LOCK_MODEL.md — Directory / File Lock · Explicit / Implicit · Edit Session · Handoff

> **状态：架构设计冻结（`SETTLED`）／实现为 `FUTURE`**
>
> 这份文档定义 **Write Lock 的层级、继承、责任边界与交接**。
> 它是 [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) 与
> [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §10–§11 的**下层细化**。
>
> 上游：[`AGENTS.md`](../../AGENTS.md) → [`ARCHITECTURE_CONSTITUTION.md`](ARCHITECTURE_CONSTITUTION.md)
> → [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §10–§11
> → [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) → **本文档**

| | |
|---|---|
| **文档状态** | 长期维护 |
| **实现状态** | **`FUTURE`** —— 零实现 |
| **当前阶段** | `Post-Step-4F / Pre-Task-Workspace` |
| **最后更新** | Lock 语义与机制确认 |

---

## 0. 怎么读这份文档

### 0.1 分工

| 文档 | 回答 |
|---|---|
| [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §10–§11 | Lock **基本语义** · 显式持有 · 无自动过期 · **Permission ≠ Lock** · Human Force Unlock |
| [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) | Controller · Authorization · Delegation · Cascade Revocation |
| **本文档** | Lock 的**层级** · **继承** · **责任边界** · **交接** · **Edit Session** · **并行模型** |

> ⚠️ **本文档不推翻 §10–§11，也不推翻 ADR-0021。**
> 它是**增量补充**（见 §9 与 §11）。

### 0.2 🔒 最核心的两句话

> ## **父级锁提供默认控制，子级显式锁形成责任边界。**
>
> ## **编辑结束不等于责任结束。**

### 0.3 🔒 `EDIT END ≠ LOCK RELEASE`

**这是本轮必须正式写入架构的核心规则。**

```
A 获得 File Lock
      ↓
A 开始修改
      ↓
Edit Session = A
      ↓
A 保存文件
      ↓
Edit Session 结束
      ↓
File Lock 仍然 = A          ← 不变
```

```
❌ 绝不允许：
    Edit Session 结束
        ↓
    自动 Release Lock
```

---

## 1. Lock 不是传统 mutex（`SETTLED`）

> **不要把 Lock 理解成 `lock() → edit() → unlock()` 这种
> 一次编辑操作的临时 mutex。**

本项目的 Lock 表达的是：

> **某个 Agent 当前对某个 Resource 承担持续的、独占的修改责任。**

### 两个必须区分的东西

| | 回答 |
|---|---|
| **Lock** | **当前谁拥有**这个 Resource 的独占修改责任 |
| **Edit Session** | 这个 Agent **此刻是否正在进行**一次具体编辑活动 |

---

## 2. 最小粒度：File（`SETTLED`）

> **File 是内容修改 Lock 的最小粒度。**

同一个 File，**任意时刻最多只能有一个有效的 File Lock Holder**。

```
❌ 禁止：A 同时修改 foo.py，B 同时修改 foo.py
```

**即使 A 和 B 都拥有 `WRITE` Authorization 也不允许。**

```
Authorization  →  决定「有没有资格修改」
Lock           →  决定「现在是谁承担独占修改责任」
```

---

## 3. Directory Lock（`SETTLED` —— 本轮修正）

### 3.1 上一轮的倾向被修正

> **上一轮倾向把 Directory 主要看成 Authorization Scope。
> 现在正式修正：Directory 也拥有 Lock。**

### 3.2 但语义**不同**

| | Directory Lock | File Lock |
|---|---|---|
| 控制 | **Namespace / Structure** | **File Content** |
| 覆盖操作 | `CREATE` · `DELETE` · `RENAME` · `MOVE` · 创建子目录 · 删除子目录 · 调整目录结构 | `Edit` · `Patch` · `Write` |
| 附加职责 | 对**未被更具体显式锁定**的后代资源承担**默认控制** | 该文件内容的独占修改责任 |

```
data/
    Directory Lock = A

A 可以：创建 data/x.csv · 创建 data/new_directory/ ·
        删除未独立委托的资源 · 重命名未独立委托的资源
```

### 3.3 🔒 Directory Lock **不等于**锁死整个目录

> **这是非常重要的一点。**

```
data/          Directory Lock = A
data/a.csv     File Lock = A
data/b.csv     File Lock = B
data/c.csv     File Lock = C
```

**这是允许的。** 不同文件可以并行工作。

但：

```
data/a.csv     File Lock = A
B 想改 a.csv   → ❌ 不能直接修改
```

### 3.4 Directory Lock 不是「整个目录只能由一个 Agent 改」

Directory Lock 负责 Namespace / Create / Delete / Rename / Move /
对未建立更具体显式边界的资源提供默认控制。

File Lock 负责 File Content / Edit / Patch / Write。

---

## 4. Explicit Lock 与 Implicit Lock（`SETTLED` —— 本轮核心）

### 4.1 Explicit Lock

某个 Resource 被**明确单独划出**责任边界。

```
data/                     Directory Lock = A
data/production.yaml      Explicit File Lock = C
```

> `production.yaml` **不再单纯继承**父目录的默认控制，而由 C 单独承担责任。

### 4.2 Implicit Lock

如果某个资源**没有自己的显式 Lock**，它**继承最近的上级 Directory Lock**。

```
configs/                  Directory Lock = A
configs/app.yaml          No Explicit Lock

Effective Lock(app.yaml) = A
```

> **不需要**单独创建 `app.yaml → Explicit Lock A`。

### 4.3 🔒 Explicit 优先于 Inherited

> **`Explicit Lock` 优先于 `Inherited Lock`。**

```
data/                       Directory Lock = A
data/processed/             Directory Lock = B
data/processed/result.csv   No Explicit Lock
    → Effective Lock = B      （继承**最近的** Directory Lock）

data/processed/result.csv   Explicit File Lock = C
    → Effective Lock = C      （不能继续继承 B）
```

---

## 5. Effective Lock Resolution（`SETTLED` —— 本轮新增概念）

> 必须在文档中明确区分 **`Explicit Lock`** 与 **`Effective Lock`**。

### 5.1 解析规则

| Rule | 内容 |
|---|---|
| **Rule 1** | 如果 Resource 自身存在 **Explicit Lock** → `Effective Lock = Resource Explicit Lock` |
| **Rule 2** | 如果自身没有 Explicit Lock → **向父目录寻找最近的显式 Directory Lock** |
| **Rule 3** | 找到后 → `Effective Lock = nearest ancestor Directory Lock` |
| **Rule 4** | 如果更深层存在另一个显式 Directory Lock → **使用更近的那个** |

### 5.2 例子

```
data/                      Lock A
data/processed/            Lock B
data/processed/reports/    Lock C
```

| Resource | Effective Lock |
|---|---|
| `data/x.csv` | **A** |
| `data/processed/x.csv` | **B** |
| `data/processed/reports/x.csv` | **C** |

```
configs/             Explicit Directory Lock = B
configs/app.yaml     Explicit Lock = None
    → Effective Lock(app.yaml) = B
```

> 这为未来 Workspace Gateway 的实现提供**清晰语义**。

---

## 6. 创建资源时**不自动**创建 Explicit Lock（`SETTLED` —— 本轮修正）

### 6.1 规则

```
configs/                  Directory Lock = A
A 在里面创建：
configs/a.yaml
configs/b.yaml
configs/dev/
```

结果：

```
✅ a.yaml → inherited(A)
✅ b.yaml → inherited(A)
✅ dev/   → inherited(A)
```

**不是**：

```
❌ a.yaml → Explicit(A)
❌ b.yaml → Explicit(A)
❌ dev/   → Explicit(A)
```

### 6.2 理由

> **创建者本身已经是父目录的控制者**，额外生成相同控制者的显式锁
> **没有必要，也会造成大量冗余**。

> **新创建 Resource 默认没有独立 Explicit Lock，
> 除非后续发生明确的资源级独立控制。**

### 6.3 ⚠️ 与 [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §2.4 的关系（**澄清，不是冲突**）

`TASK_WORKSPACE.md` §2.4 写着：

```
Create Resource  +  Acquire Write Lock     ← 一个原子操作
write_lock.owner = Agent A
```

**本节不推翻它，而是精确化它的「机制」**：

| | §2.4 的要求 | 本文档的精确化 |
|---|---|---|
| **原子性** | 绝不允许 `CREATE →（竞争窗口）→ 以后再 LOCK` | ✅ **继续成立** |
| **新资源属于谁** | `write_lock.owner = Agent A` | ✅ **结论相同** |
| **机制** | 未说明是「新建锁记录」还是「继承」 | ✅ **改为继承**（不新建 Explicit Lock） |

**为什么两者结论相同**：能在一个目录里 `CREATE` 的 Agent，
**必须已经是该目录 Directory Lock 的持有者**（或经其授权）。
所以新资源继承到的 Effective Lock **就是创建者自己**。

> ✅ **`TASK_WORKSPACE.md` §2.4 的原子性要求继续有效，
> 只是实现机制由「新建 Explicit Lock」改为「继承父 Directory Lock」。**
>
> ⚠️ **本节的「不自动创建 Explicit Lock」是机制的修正，
> 不是行为的修正。**

---

## 7. 目录控制权可以自然传递给新的负责人（`SETTLED`）

```
初始：
configs/            Directory Lock = A
configs/a.yaml      Inherited → A
configs/b.yaml      Inherited → A
configs/dev/        Inherited → A

A 把整个 configs/ 的 Directory Control / Lock 交给 B：

configs/            Directory Lock = B
configs/a.yaml      → B      ← 自动生效
configs/b.yaml      → B
configs/dev/        → B
```

> **不需要逐文件修改 Lock。这是设计的重要优势。**

### 7.1 🔒 但曾经被单独交出去的资源**不能**随父目录一起「吞回去」

```
configs/                   Directory Lock = A
configs/production.yaml    Explicit File Lock = C

A 把 configs/ 交给 B：

configs/                   Directory Lock = B
configs/production.yaml    Explicit File Lock = C   ← 不变

最终：
普通未显式锁定资源 → B
production.yaml    → C
```

> **B 不能因为获得父目录 Lock 就自动获得 `production.yaml` 的内容修改权。**
>
> 必须先发生明确的 **Release · Handoff · Revoke / Recall ·
> 其他正式治理操作** 之一。

### 7.2 §22 规则汇总

| 父级 Directory Lock 发生 Handoff / Delegation | 行为 |
|---|---|
| 后代资源 **No Explicit Lock** | **Effective Lock 跟随变化** |
| 后代资源 **有 Explicit Lock** | **保持不动** |

---

## 8. Resource Boundary 与 Delete（`SETTLED`）

### 8.1 Delegated Resource Boundary

```
data/                Directory Lock = A
data/processed/      Directory Lock = B     ← 独立边界
```

> `processed/` 形成一个 **Delegated Resource Boundary**。

之后 **A 对父目录的控制不能绕过它**：

```
❌ A 不能因为自己拥有 data/ 就直接：
     delete  data/processed/
     rename  data/processed/
     modify  data/processed/x.csv
```

**除非先正常处理相应的 Delegation / Lock / Control 关系。**

### 8.2 🔒 Delete 不是普通父目录操作

```
data/            Directory Lock = A
data/a.csv       File Lock = B

❌ A 不能直接 DELETE a.csv
```

> **因为这会破坏 B 当前承担的 Resource Responsibility。**
>
> 应先通过正式机制 —— **Recall / Revoke / Handoff / Release /
> Force Unlock** —— 建立合法的责任状态，再执行删除。

> **已被独立控制的子目录形成 Resource Boundary。**

---

## 9. Lock 的持续性（`SETTLED`）

```
page.tsx    File Lock = A

A：Edit Start → Edit → Save → Edit End

最终仍：    File Lock = A
```

**Lock 不因一次编辑操作结束而自动释放。**

**只有**以下操作才能改变 Lock：

| 操作 | 说明 |
|---|---|
| `Release` | 当前持有者主动放弃 |
| `Handoff A → B` | 正式交接 |
| `Revoke` | 治理撤销 |
| `Force Unlock` | 受控强解锁（ADR-0021 §四–§六） |

---

## 10. Lock Handoff 是正式操作（`SETTLED`）

### 10.1 两种触发方式

```
① 接收方发起：
    B → Lock Request → A → Approve / Handoff → File Lock = B

② 当前控制者主动：
    A → Handoff → B
```

### 10.2 Handoff 必须留下 Audit / Record

至少要能追踪：

| 字段 | |
|---|---|
| `from` | 谁交出 |
| `to` | 谁接手 |
| `resource` | 哪个资源 |
| `previous revision` | 交接时的版本 |
| `new responsibility` | 新责任范围 |
| `reason / message` | 为什么 |
| `timestamp` | 何时 |
| `related task` | 关联哪个 Task |
| `authorization source` | 权限从哪来 |

> **`Handoff 前后的责任人必须可追踪。`**

### 10.3 🔒 接收方不能直接夺取 Lock

```
page.tsx    File Lock = A
B 想继续修改    →  ❌ B 不能直接获取 File Lock
                →  ✅ 必须走 Lock Request / Handoff
```

---

## 11. Authorization 与 Lock Request **不要混为一谈**（`SETTLED`）

### 11.1 四种组合

| 情况 | 结果 |
|---|---|
| B 有 `WRITE` Authorization，但 `File Lock = A` | **有资格修改，但目前不能修改** → B 需要 **Lock Request**，**不是**再次申请 `WRITE` Authorization |
| B **没有** `WRITE` Authorization | **即使** `File Lock = None / inherited B`，**也不能绕过 Authorization 检查** |

### 11.2 Gateway 至少需要**分别**判断三件事

```
① Authorization         有没有资格？
② Effective Lock        当前谁负责修改？
③ Edit Session / operation state   是否正在编辑？
```

> 🔒 **Lock Request ≠ Authorization Request**（[`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) §8.2）。

---

### 11.3 🔒 与 ADR-0021 的锁状态**继续沿用**

本文档**不引入新的 Lock 状态**。ADR-0021 确立的三种状态**保持有效**：

| 状态 | 含义 |
|---|---|
| `ACTIVE` | 持有中 |
| `RELEASED` | 已被锁主释放 |
| `REVOKED` | 已被 Force Unlock 撤销 |

> ⚠️ **没有 `EXPIRED`。** Workspace Write Lock **明确不用时间租约**。
>
> 本文档新增的是**层级与继承**（Explicit / Implicit / Effective），
> 那是**解析规则**，**不是**新的状态。

---

## 12. Edit Session（`SETTLED` —— 为架构预留）

> Edit Session 表示**一次实际编辑活动**，而不是 Responsibility 本身。

```
LOCKED(A)
    ↓
EDIT SESSION START
    ↓
EDITING(A)
    ↓
SAVE / FINISH
    ↓
LOCKED(A)
```

### 12.1 🔒 绝不能

```
❌ EDITING(A) → UNLOCKED
```

**除非明确执行 `Release` / `Handoff` 等操作。**

---

## 13. 多 Agent 并行修改模型（`SETTLED`）

### 13.1 第一版明确采用

> **不同文件可以并行修改，同一文件不能并行修改。**

```
page.tsx    Lock A
navbar.tsx  Lock B
api.ts      Lock C
    → A / B / C 可以同时工作

page.tsx    Lock A
    → ❌ B 不能同时修改 page.tsx
```

### 13.2 🔒 第一版**不要**通过以下方式解决

- ❌ Git Branch 并行修改
- ❌ 自动 Merge Conflict
- ❌ 多 Agent 同文件同时写

> **先建立「单文件单责任人模型」。
> 以后再考虑更高级的并行分支和冲突解决。**

### 13.3 并行修改与 Directory Lock 共存

```
frontend/               Directory Lock = B
frontend/page.tsx       File Lock = B
frontend/navbar.tsx     File Lock = C
frontend/api.ts         File Lock = D
```

> **不同 File Lock 可以同时存在。**
> Directory Lock **不是**「整个目录只能有一个 Agent 修改所有内容」。

### 13.4 目录中新资源的默认行为

```
frontend/                Directory Lock = B
B 创建 frontend/new_page.tsx
    → No Explicit Lock
    → Effective Lock = B      （继承 frontend/）
```

> ❌ **不要因为创建而自动生成 `Explicit File Lock = B`。**

---

## 14. 子 Agent 可以继续向下 Delegation（`SETTLED`）

```
A
└── frontend/          → B
    └── frontend/components/  → C
```

结果：

```
frontend/                  → B
frontend/components/       → C
frontend/page.tsx          → B
frontend/components/x.tsx  → C
```

> **必须与 [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) §6.3 的
> `Delegated Authority ≤ Grantor Authority` 保持一致。**

---

## 15. 🔒 Explicit Lock ≠ 永久所有权（`SETTLED`）

保持上一轮原则：

> **`Control ≠ Owner`**

Explicit Lock 表示 **当前独占修改责任**，**不是永久不可撤销的所有权**。

因此仍允许：`Release` · `Handoff` · `Revoke` · `Force Unlock`（受控）。

---

## 16. 统一模型图

```
Workspace
└── Resource Tree
    │
    ├── Directory
    │   ├── Controller
    │   ├── Authorization
    │   ├── Explicit Directory Lock
    │   └── child resources
    │
    └── File
        ├── Controller
        ├── Authorization
        ├── Explicit File Lock
        └── Edit Session
```

```
Parent Directory Lock
        ↓
    Implicit Lock
        ↓
除非出现更具体 Explicit Lock
        ↓
Explicit Child Lock 覆盖 Parent Inheritance
```

```
Authorization   →  是否有资格？
Effective Lock  →  当前谁负责修改？
Edit Session    →  是否正在编辑？
Revision / Audit →  实际改了什么？
```

---

## 17. 最终语义（规范口径）

| 概念 | 回答 |
|---|---|
| **Authorization** | 这个 Agent **有没有资格**操作？ |
| **Controller** | 这个 Agent 是否**负责**这部分资源及其授权？ |
| **Delegation** | 这个控制 / 权限**从谁传给谁**？ |
| **Directory Lock** | 谁负责这个目录 **Namespace**，以及未被更具体锁覆盖的资源？ |
| **File Lock** | 谁对这个文件承担**独占修改责任**？ |
| **Edit Session** | 当前是否正在进行**一次具体编辑**？ |
| **Revision / Audit** | 实际**做了什么**，以及**责任如何交接**？ |

---

## 18. Open Questions（`OPEN` —— 本轮不自行拍板）

> ⚠️ **以下问题本轮没有决定。不要为了完成报告而假装解决。**

| # | 问题 |
|---|---|
| 1 | **Controller 转移的完整协议** |
| 2 | **Task 完成后 Lock 是否自动进入某种回收流程** |
| 3 | **Lock 与 Git Worktree 的最终映射方式** |
| 4 | **Permission inheritance 的完整算法** |
| 5 | **Human 在 Delegation Tree 中的特殊角色** |
| 6 | **Workspace Gateway 的完整实现接口** |
| 7 | **Lock Recovery 的具体治理流程** |
| 8 | **Agent 崩溃 / 永久失联后的具体 Lock Recovery 规则** |
| 9 | **是否新增独立的 LockRequest / LockHandoff Message Type** |
| 10 | **Resource Store / Lock Store 最终数据库结构** |
| 11 | **是否需要区分 Delete Lock / Rename Lock 等更细结构锁** |
| 12 | **是否支持未来的 multi-resource atomic handoff** |

（与 [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) §12 的 12 个问题并集见 §19。）

---

## 19. 测试需求

> 实现阶段至少需要覆盖以下 33 条。**现在只登记需求，不写测试。**

| # | 场景 |
|---|---|
| **基础 Lock** | |
| 1 | Directory Lock 可以存在 |
| 2 | File Lock 可以存在 |
| 3 | Directory Lock 与 File Lock 语义不同 |
| 4 | 同一 File 同时只能有一个 Effective Lock Holder |
| 5 | 不同 File 可以有不同 Lock Holder |
| **继承** | |
| 6 | 无 Explicit Lock 的 File 继承最近 Directory Lock |
| 7 | 无 Explicit Lock 的子目录继承最近 Directory Lock |
| 8 | 更深层 Directory Lock 覆盖更高层 |
| 9 | Explicit File Lock 覆盖所有祖先 Directory Lock |
| 10 | Explicit Directory Lock 覆盖更高层 Directory Lock |
| **创建** | |
| 11 | 创建新 File **不自动**生成 Explicit File Lock |
| 12 | 创建新 Directory **不自动**生成 Explicit Directory Lock |
| 13 | 新资源默认继承父 Directory Lock |
| **父目录 Delegation** | |
| 14 | Handoff 后，未显式锁定的后代 Effective Lock 自动 A → B |
| 15 | **已有 Explicit Child Lock 的资源不随父级 Handoff 改变** |
| **责任边界** | |
| 16 | 父 Directory Lock **不能绕过**已 Delegated 的子资源边界 |
| 17 | 父 Agent **不能直接删除**另一个 Agent 当前负责的子资源 |
| 18 | 已被独立控制的子目录形成 Resource Boundary |
| **Edit Session** | |
| 19 | **Edit Session 结束不自动释放 File Lock** |
| 20 | **Save 不自动释放 File Lock** |
| 21 | Lock 只有明确 Release / Handoff / Revoke / Force Unlock 后才变化 |
| **Handoff** | |
| 22 | A → B 的 File Handoff 必须有明确记录 |
| 23 | Lock Handoff 必须可审计 |
| 24 | Handoff 前后的责任人必须可追踪 |
| 25 | Revision / Task / Authorization Source 可关联到 Handoff |
| **Authorization / Lock 分离** | |
| 26 | 有 `WRITE` Authorization 但没有 Effective Lock 时**不能写** |
| 27 | 有 Effective Lock 但没有必要 Authorization 时**也不能绕过权限** |
| 28 | Lock Request 与 Authorization Request **不混为一个概念** |
| **并行修改** | |
| 29 | A 修改 `a.py` 时，B **可以**修改 `b.py` |
| 30 | A 修改 `a.py` 时，B **不能**同时修改 `a.py` |
| **Delegation** | |
| 31 | B 可以在权限允许范围内继续将子目录交给 C |
| 32 | B **不能**将超出自己控制 Scope 的资源交给 C |
| 33 | A 撤销 B 的上级 Delegation 时，B → C 的下级 Delegation 级联失效 |

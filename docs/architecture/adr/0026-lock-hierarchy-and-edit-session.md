# ADR-0026：Lock 有层级与继承；Edit Session ≠ Lock

- **状态**：Accepted（架构设计冻结；**实现为 `FUTURE`**）
- **时间**：Lock 语义与机制确认
- **相关**：ADR-0021（显式 Write Lock + Human Force Unlock）· ADR-0025（Controller / Authorization / Delegation）
- **规范**：[`LOCK_MODEL.md`](../LOCK_MODEL.md)

> 本 ADR **不推翻** ADR-0021，也不推翻 ADR-0025。它是**增量补充**。

## Context

ADR-0021 确立了 Write Lock 的**显式持有模型**（不自动过期、锁主释放、
Human Force Unlock 是 `FUTURE`）。

ADR-0025 确立了 Controller / Authorization / Delegation / Write Lock 四者分离。

但两者都**没有回答三个问题**：

1. **粒度和层级** —— Lock 是只存在于 File 上，还是 Directory 也有？
2. **继承** —— 新创建的文件谁负责？要逐个建锁记录吗？
3. **编辑与责任的关系** —— 编辑结束后锁要不要释放？

而真实协作会立刻遇到：

```
❌ A 创建了 100 个文件 → 生成 100 条 Explicit Lock 记录（冗余）
❌ A 编辑完 page.tsx → 锁自动释放 → B 抢走 → A 无法继续
❌ A 拥有 data/ 的锁 → 就能删掉 B 正在负责的 data/a.csv
❌ 「整个目录只能由一个 Agent 改」→ 无法并行
```

## Decision

### 一、Lock ≠ 传统 mutex

> **本项目 Lock 表达的是：某个 Agent 当前对某个 Resource
> 承担持续的、独占的修改责任。**

不是 `lock() → edit() → unlock()` 这种一次性 mutex。

### 二、🔒 `EDIT END ≠ LOCK RELEASE`

```
A 获得 File Lock → 开始修改 → Edit Session = A → 保存 → Edit Session 结束
    → File Lock **仍然 = A**
```

❌ **绝不**「Edit Session 结束 → 自动 Release Lock」。

**Lock 只有**明确 `Release` / `Handoff` / `Revoke` / `Force Unlock`
之后才变化。

### 三、File 是最小粒度；同文件最多一个 Holder

```
❌ A 同时改 foo.py · B 同时改 foo.py
```

**即使 A、B 都有 `WRITE` Authorization 也不允许。**

### 四、Directory Lock 存在，但语义不同

上一轮倾向把 Directory 主要看作 Authorization Scope。**本轮修正。**

| | Directory Lock | File Lock |
|---|---|---|
| 控制 | **Namespace / Structure** | **File Content** |
| 操作 | `CREATE` `DELETE` `RENAME` `MOVE` · 子目录 | `Edit` `Patch` `Write` |
| 附加 | 对未被更具体显式锁定的后代**默认控制** | 该文件独占修改责任 |

> 🔒 **Directory Lock ≠ 锁死整个目录** —— 不同文件可有不同 Holder，可并行。

### 五、Explicit Lock 与 Implicit Lock

**Explicit Lock**：某资源被明确单独划出责任边界。

**Implicit Lock**：没有自己的显式 Lock → **继承最近的上级 Directory Lock**。

> 🔒 **`Explicit Lock` 优先于 `Inherited Lock`。**

### 六、Effective Lock Resolution（Rule 1–4）

```
Rule 1  自身有 Explicit Lock        → Effective = 它
Rule 2  自身没有                      → 向父目录找最近的显式 Directory Lock
Rule 3  找到                          → Effective = nearest ancestor Directory Lock
Rule 4  更深层有另一个显式 Dir Lock   → 用**更近**的那个
```

> 明确区分 **`Explicit Lock`** 与 **`Effective Lock`** —— 这为未来
> Workspace Gateway 提供清晰语义。

### 七、🔒 创建资源**不自动**创建 Explicit Lock

```
configs/                Directory Lock = A
A 创建 configs/a.yaml   → inherited(A)     ✅
                        → Explicit(A)      ❌ 不生成
```

**理由**：创建者本身已是父目录控制者，额外生成相同控制者的显式锁
**没有必要，且造成大量冗余**。

> ⚠️ **这是对上一轮设计的重要修正**（见 `LOCK_MODEL.md` §6.3 与
> 下方「与 ADR-0021 / TASK_WORKSPACE.md §2.4 的关系」）。

### 八、目录控制权自然传递；但显式锁**不被吞回**

```
configs/                Lock A → B
configs/a.yaml (隐式)   → B      ✅ 自动生效
configs/dev/   (隐式)   → B      ✅
configs/production.yaml (Explicit C) → **保持 C**  ✅ 不跟随
```

> 🔒 **B 不能因为获得父目录 Lock 就自动获得 `production.yaml` 的修改权。**

### 九、Resource Boundary；Delete 不是普通父目录操作

已被独立 Delegated 的子目录形成 **Delegated Resource Boundary**。
父目录控制者**不能绕过它**直接 `delete` / `rename` / `modify`。

```
data/          Directory Lock = A
data/a.csv     File Lock = B
❌ A 不能直接 DELETE a.csv
```

> 会破坏 B 当前承担的 Resource Responsibility。
> 需先经 **Recall / Revoke / Handoff / Release / Force Unlock**
> 建立合法责任状态。

### 十、Lock Handoff 是正式操作

```
① B → Lock Request → A → Approve / Handoff → Lock = B
② A → Handoff → B
```

必须留 Audit：`from` · `to` · `resource` · `previous revision` ·
`new responsibility` · `reason` · `timestamp` · `related task` ·
`authorization source`。

> 🔒 **接收方不能直接夺取 Lock。**

### 十一、Authorization 与 Lock Request 不混为一谈

| 情况 | 结果 |
|---|---|
| 有 `WRITE` Authorization，但 Lock = A | 有资格但**不能改** → 需 **Lock Request**（**不是**再申请 Authorization） |
| 没有 `WRITE` Authorization | 即使 Lock 是空的，**也不能绕过 Authorization** |

> **Gateway 至少分别判断三件事**：
> `① Authorization` · `② Effective Lock` · `③ Edit Session / operation state`

### 十二、Edit Session

```
LOCKED(A) → EDIT SESSION START → EDITING(A) → SAVE / FINISH → LOCKED(A)
```

❌ 绝不 `EDITING(A) → UNLOCKED`（除非明确 Release / Handoff）。

### 十三、并行模型：不同文件可并行，同一文件不可

> 🔒 **第一版不要**通过 Git Branch 并行 / 自动 Merge Conflict /
> 多 Agent 同文件同时写 来解决。
> **先建立「单文件单责任人模型」。**

### 十四、子 Agent 可继续向下 Delegation

与 ADR-0025 的 `Delegated Authority ≤ Grantor Authority` 保持一致。

### 十五、🔒 Explicit Lock ≠ 永久所有权

保持 `Control ≠ Owner`。仍允许 `Release` / `Handoff` / `Revoke` / `Force Unlock`。

## 与 ADR-0021 和 TASK_WORKSPACE.md §2.4 的关系

### 与 ADR-0021

| ADR-0021 的结论 | 本 ADR |
|---|---|
| 显式持有模型 · 不自动时间失效 | ✅ **继续沿用** |
| Lock 状态只有 `ACTIVE` / `RELEASED` / `REVOKED`（无 `EXPIRED`） | ✅ **继续沿用** |
| Human Force Unlock（高权限治理操作 · 必须审计） | ✅ **继续沿用** |
| 锁主释放（他人 `DENIED`） | ✅ **继续沿用** |
| — | **新增**：层级 · 继承 · Effective Lock · Edit Session · Handoff |

> **没有冲突。** 本 ADR 只**增加语义**，不改变 ADR-0021 的任何锁状态或流程。

### 与 `TASK_WORKSPACE.md` §2.4（**澄清，不是冲突**）

§2.4 写着 `Create Resource + Acquire Write Lock = 一个原子操作`，
且 `write_lock.owner = Agent A`。

| | §2.4 | 本 ADR |
|---|---|---|
| **原子性** | 绝不允许 `CREATE →（竞争窗口）→ 以后再 LOCK` | ✅ **继续成立** |
| **新资源属于谁** | `write_lock.owner = Agent A` | ✅ **结论相同** |
| **机制** | 未指明「新建锁记录」还是「继承」 | ✅ **改为继承** |

**为什么结论相同**：能在一个目录里 `CREATE` 的 Agent，
**必须已经是该目录 Directory Lock 的持有者**（或经其授权）。
所以新资源继承到的 Effective Lock **就是创建者自己**。

> ✅ **§2.4 的原子性要求继续有效，只是机制由「新建 Explicit Lock」
> 改为「继承父 Directory Lock」。**
>
> ⚠️ **这是机制的精确化，不是行为的修正。**

## Consequences

**正面**：

- 避免**逐文件建锁**的冗余（创建不生成 Explicit Lock）
- 目录控制权转移**自然传播**到未显式锁定的后代
- **显式锁不被吞回** → 单独立出去的责任不会被父目录转移波及
- **编辑结束 ≠ 责任结束** → 防止「保存后被抢锁」
- 不同文件**可并行**，同文件**不可能冲突**（单文件单责任人）
- 职责边界清楚：父目录控制**不能绕过**已 Delegated 的子边界
- Gateway 的判定逻辑明确为三步（Authorization / Effective Lock / Edit Session）
- **与 ADR-0021 无冲突**，是纯增量

**负面 / 代价**：

- **Effective Lock 解析需要向上遍历父目录** → 性能与缓存是真实工程问题
- 需要持久化 **Directory Lock + Explicit Lock + Edit Session + Handoff 记录**
- 「父级 Handoff 时，哪些后代跟随、哪些不跟随」需要**精确遍历**
- 单文件单责任人**限制了并行度** —— 这是有意的取舍，但未来可能需要更高级模型
- 12 个 Open Questions 未答 → 实现前仍需一轮讨论

**因此不能做的事**：

- ❌ 不许把 Lock 当成一次性 mutex
- ❌ 不许在 Edit Session 结束时自动 Release Lock
- ❌ 不许让同一 File 有两个 Effective Lock Holder
- ❌ 不许把 Directory Lock 当成「整个目录只能一个人改」
- ❌ 不许在创建资源时自动生成 Explicit Lock
- ❌ 不许让父目录 Handoff **吞回**已有的 Explicit Lock
- ❌ 不许父目录控制者**绕过**已 Delegated 的子资源边界
- ❌ 不许直接删除另一个 Agent 当前负责的资源
- ❌ 不许让接收方**直接夺取** Lock
- ❌ 不许把 Lock Request 与 Authorization Request 混为一谈
- ❌ 第一版不许用 Git Branch / Merge Conflict 解决并行

## Future evolution

```
今天（本 ADR）：
    Lock 层级 / 继承 / Effective Lock / Edit Session / Handoff 已定
    创建不生成 Explicit Lock 已定
    Resource Boundary · Delete 需先建立合法责任状态 已定
    与 ADR-0021 无冲突（纯增量）

下一步（专门讨论，再实现）：
    Lock Recovery 治理流程 · Agent 崩溃 / 永久失联的恢复规则 ·
    Task 完成后的锁处理 · Git Worktree 映射 ·
    Workspace Gateway 完整接口 · Lock Store 表结构 ·
    是否新增 LockRequest / LockHandoff Message Type ·
    是否区分 Delete Lock / Rename Lock · multi-resource atomic handoff

更远：
    高级并行分支与冲突解决
```

## Guard

文档守卫（含反向测试）必须能抓：

| 违规 | |
|---|---|
| Edit Session 结束自动释放 Lock | ✅ |
| 同一 File 有两个 Holder | ✅ |
| Directory Lock 锁死整个目录 | ✅ |
| 创建资源自动生成 Explicit Lock | ✅ |
| 父级 Handoff 吞回 Explicit Lock | ✅ |
| 父目录绕过 Delegated 子边界 | ✅ |
| 直接删除他人负责的资源 | ✅ |
| 接收方直接夺取 Lock | ✅ |
| 把 Lock Request 与 Authorization Request 混为一谈 | ✅ |
| 把 Explicit Lock 写成永久所有权 | ✅ |

**不使用 substring-only 检查。**

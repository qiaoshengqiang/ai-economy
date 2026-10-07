# D + E 报告：Artifact Write Modes + Dynamic Artifact Creation

**Commits** `a2be74a` + `b98647c` · **Tests 4066 passed / 2 skipped / 30 deselected / 0 failed**

---

## 1. Commit
`a2be74a`（D+E 实现） · `b98647c`（真实 smoke + 两个修复）

## 2. Tests
**4066 passed / 0 failed** — 新增 `tests/test_artifact_write_modes.py`（**44 项**），
覆盖 §十三 全部 23 项 + 结构守卫 + 深路径回归。

## 3–4. CREATE / REPLACE / APPEND 与 Dynamic Artifact 设计

**`services/artifacts.py`** —— 一层**很薄**的语义层：

```
CREATE   → gateway.create(...)                  不存在时才建
REPLACE  → gateway.edit(base_revision, new)     整体替换
APPEND   → gateway.edit(base_revision, old+new) 旧内容完整保留
```

| 要求 | 实现 |
|---|---|
| §三 mode **必须显式** | `write_mode` 非法直接 `bad_mode`；有守卫扫源码禁止 `infer_mode` / `if len(content)` 之类推断 |
| §四 EMPTY_CONTENT | 三种模式**一律**默认拒绝（`empty_content`）；`allow_empty=True` 才放行 |
| §五 Revision 语义 | 空写入**不产生 revision**（有测试断言 revision 不变） |
| §六 Factual Source | 不复制 content 到 Stage/Task/Memory/Runner |
| §八 不走主机文件系统 | 全部经 Gateway；守卫断言 `artifacts.py` **不 import** `os`/`shutil`/`pathlib`/`subprocess` |
| §九 Path Policy **不扩张** | 只拒绝宿主路径形状（绝对/家目录/盘符/反斜杠/`..`/控制字符） |
| §十一 Authorization | 非 Participant → `not_participant`（有测试：有 AIC ≠ 有写权） |
| §十二 Lock 不重设计 | `world/`→builder，`plot/`→architect，`manuscript/`→author，`notes/`→editor，`reader/`→reader；**建目录 + handoff，用的全是已有入口** |
| §一 不许变 Workflow | 守卫断言没有 `ArtifactPipeline` / `ChapterManager` / `ProductionManager` / `WorkflowEngine` |

> **为什么「目录 + handoff」而不是「放宽 create 权限」**：
> 放宽会破坏 Workspace Core 的 Lock 模型（`create` 需要父目录 Directory Lock）。
> 建目录 + handoff 让 **Lock / Authorization / Revision 语义一个字都不用改**，
> Agent 就能自主创建新产物。

---

## 5–7. 真实 Smoke 结果（5 个异质 Agent，独立 DB `hetero.db`）

### §十四：world_builder **自主**创建

Prompt **没有**命令它创建任何东西，只说「你可以自主创建必要的共享生产 Artifact」。

> 它自己选择了 **`world/world_bible.md`，`mode=create` → R1，1835 字**

### Author 也自主创建（新 Session）

> Session 1：`manuscript/chapter_01_opening.md` → R1（431 字）
> 同时自主新建 `manuscript/chapter_02_ledger_ghost.md` → R1（2313 字）

---

## 8. APPEND 是否真实保留旧内容？—— **是，已用真实 Agent 验证**

让 Author「把这一章继续写下去」——**全程没有出现 `append` 这个词**。

> 它自己选了 **`mode=append`**

```
R1 = 431 chars
R2 = 4404 chars
     previous_chars = 431
     appended_chars = 3971
     revision 1 → 2
```

**`R2 = X + Y`，不是 `R2 = Y`。** §二十三 I 满足。
单元测试里也直接断言不变式：`new_content.startswith(old_content)`。

### 最终 Workspace 产物

| 路径 | revision | 字数 |
|---|---|---|
| `/world/world_bible.md` | R1 | 1,835 |
| `/manuscript/chapter_01_opening.md` | **R2** | **4,404** |
| `/manuscript/chapter_02_ledger_ghost.md` | R1 | 2,313 |

（真实正文开头：*「第一章 注销通知 —— 林澈第一次看见自己的死亡预告，是在清晨七点零三分。」*）

---

## 9. 已知问题（smoke 过程中抓到的，都已修）

### ① 深路径 CREATE 失败

Agent 自主发明 `novel/chapters/ch01.md`，而中间目录不存在 → `not_found`。
§七 的示例只有一层，**但 Agent 很自然会发明更深的结构**。
→ 已加 `_ensure_parents()` 逐层建目录。

### ② `_ensure_parents` 第一版把「已存在」误读成「无权限」

实测 `gateway.create` 的检查顺序是：

```
participant → resolve parent → **父目录 Lock** → 同名冲突
```

于是「目录已存在 + 调用者不持有**它的父目录**锁」会在 **Lock 那一步就失败**，
根本走不到同名冲突。
→ 已改为**先用 `read` 探测存在性**，并加回归守卫断言 `_ensure_parents` 里有 `gateway.read(`。

### ③ 🔴 最有价值的观察：Agent 猜错路径，是因为**没人告诉它**

最初两次它猜了 `novel_author/...` 和 `novel/draft/...`，两次 `locked_by_other`。

原因：smoke 脚本的 `workspace_tree()` **过滤掉了空命名空间**，
所以它**根本不知道 `/manuscript/` 存在**。

> **补上这个事实后，它立刻成功。**

这与前几轮的「Provider 花名册没进 Context」是**同一类缺口**：
**不是 Agent 不会用，是它没有事实。**

---

## 10. 是否可以进入正式异质生产？

### ✅ 结构上已具备

| §二十三 完成标准 | 状态 |
|---|---|
| A. CREATE 可用 | ✅ |
| B. REPLACE 可用 | ✅ |
| C. APPEND 可用 | ✅ |
| D. EMPTY_CONTENT 安全 | ✅ |
| E. 动态 Artifact 创建可用 | ✅ |
| F. Workspace / Revision 正常 | ✅ |
| G. Lock / Authorization 不回归 | ✅ |
| H. 真实 Agent 成功创建动态 Artifact | ✅ |
| I. 真实 Agent 成功 APPEND | ✅ |
| J. 全量测试通过 | ✅ |

### ⚠️ 但建议先补一件事

**把「命名空间 + 负责人」作为事实放进正式生产的 prompt**
（就像 F 预检里把 credential 事实补上一样）。

本次 smoke 已经证明：**不给这个事实，Agent 会盲猜路径并全部失败**。
正式生产如果不补，会出现大量 `locked_by_other` 的无效成本 ——
而这**不是 Agent 行为问题，是事实投递问题**。

---

## §十八 保持不变

> **Revision history currently records state metadata, not historical content.**

`workspace_revisions` 本轮**未改动**，没有暗中改变 Workspace Revision 语义。

---

## 本轮**没有**做

❌ 未开始 10,000 字正式生产 ❌ 未增加 Economy Policy
❌ 未把 Artifact Write Mode 与 AIC 预算绑定（APPEND ≠ 更便宜）
❌ 未建立固定章节生成器 ❌ 未实现历史内容版本库
❌ 未删除 5 个核心 Artifact ❌ `git add -A`

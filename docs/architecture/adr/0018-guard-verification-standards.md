# ADR-0018：Guard 检测手段标准

- **状态**：Accepted
- **时间**：Step 4E
- **相关**：ADR-0006（应用层拆分）

## Context

本项目有 8 个守卫文件、265 项守卫测试。它们是架构边界的**自动执行者** ——
没有它们，§核心原则 那十几条边界会在几个阶段内被无声地越过。

但这一路走来，**守卫本身出过多次问题**，而且大部分是**假绿**
（守卫通过，但不是因为代码正确）。

### 已记录的真实教训

**教训 A：子串匹配误报（≥4 次）**

| 被查的标识符 | 误报原因 | 含有的子串 |
|---|---|---|
| `seen_tracker` | 被当成含 `ack` | `ack` |
| `offer_amounts` | 被当成含 `mount` | `mount` |
| `is_open` | 被当成含 `open` | `open` |
| `agent_directory` | 被当成含 `agent_d` | `agent_d` + `irectory` |

**教训 B：文档字符串被当成代码（≥8 次）**

文档里会**合法地**写：

```markdown
❌ 不 import llm
❌ 不执行代码（没有 subprocess）
绝对不许写 `if agent_id == "agent_b": ...`
registry.register(SubmitHandler(negotiation))     ← 只是举例子
```

查原文的守卫会被自己的说明文字绊倒。

**教训 C：守卫查错了对象（3 次，全部是假绿）**

| 守卫 | 为什么绿了 |
|---|---|
| `assert "claim_message" in code` | 方法自己就叫 `_claim_message` —— 名字永远都在 |
| `assert "BEGIN IMMEDIATE" in source` | 文档里为了解释这个修复，大量提到了它 |
| `assert body.count("UPDATE communication_messages") == 1` | `ast.unparse` **保留文档字符串** —— 那个 1 来自文档里的 SQL 示例。真正的 SQL 是 `UPDATE {TABLE_COMM_MESSAGES}` |

另外一次相关的：`"UPDATE" in sql` 会匹配
`INSERT ... ON CONFLICT DO UPDATE SET ...` —— 把插入语句数成更新语句。

**教训 D：单进程测试发现不了多进程问题**

`BEGIN`（延迟事务）在单进程下完全正确。真多进程时才死锁 →
`database is locked` → **并发保护被静默绕过**（见 ADR-0012）。

## Decision

**守卫的检测手段按下面的优先级选择：**

### 优先（1 → 5）

1. **AST**（`ast.parse` / `ast.walk` / `ast.unparse`）
2. **import 图**（谁 import 谁）
3. **真实的调用点**（`conn.execute(...)` 的**参数**，
   而不是「文件里有没有这个词」）
4. **具体的 DB 写入**（`UPDATE x SET y`，而不是「含 UPDATE」）
5. **运行时行为**（`hasattr`、真的跑一遍看不变量）

### 不要主要依赖

```python
source.count("xxx")    # ❌ 会被注释、文档字符串、变量名骗到
```

### 如果必须做字符串扫描

三条**强制**要求：

1. **去掉文档字符串** —— `ast.unparse` **不会**帮你做这件事，
   必须先手动删掉 `body[0]` 那个 `Expr(Constant(str))` 节点
2. **明确扫描范围** —— 是「整个文件」还是「某个方法体内」
3. **做反向测试** —— 把违规注入进去，确认守卫**真的变红**

### 检查标识符时

- **按词切分**（snake_case / 驼峰），或
- 匹配**带引号的完整字面量**（`r"""["']agent_[a-z]["']"""`）

不要直接子串匹配。

## Consequences

**正面**：
- 守卫不再因为自己的说明文字而变红或变绿
- 「守卫真的能抓到」变成可验证的（反向测试）
- 假绿的历史被记录下来，后来者不会重犯

**负面 / 代价**：
- 写守卫更啰嗦（每个文件都要一个 `code_only()` 辅助函数）
- 反向测试要手写注入脚本，成本不低

**因此必须做的事**：
- 每个 `*guards*.py` 里放一个 `code_only()`（去文档字符串 + `ast.unparse`）
- 新增守卫**必须**配一个反向测试

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| 只用运行时断言（`hasattr` / 真的跑） | 覆盖不了「不许出现某个 import」「不许有某张表」这类静态边界 |
| 用 linter / 自定义规则引擎 | 引入外部依赖，且表达力未必更好 |
| 不写守卫，靠 code review | 这个项目的主要开发者是 AI Agent，review 不可靠 |

## 证据

- 8 个 `*guards*.py`，265 项，全部使用 `code_only()` 或 AST
- `ARCHITECTURE_CONSTITUTION.md` §12 记录了完整规范
- 三次假绿都在 commit message 和守卫注释里留了记录

# ADR-0052：`thinking` 指令的 schema 与兼容归一

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_da850bbc` 中，`WARNING Cognition thinking 指令非法`
出现 **14 次，全部来自 agent_e**：

```text
13:38:19 agent_e seg=0
13:38:24 agent_e seg=1
13:38:44 agent_e seg=2
...
13:53:41 agent_e seg=2
```

一个 turn 有 3-4 个 segment，所以同一条坏指令会打印 3-4 次；
它不代表 14 个独立 turn 都失败。

解析器要求 `thinking` 是：

```json
{"enabled": true, "effort": "high"}
```

或按相：

```json
{"decision": {"enabled": false, "effort": "low"},
 "work": {"enabled": true, "effort": "high"}}
```

`enabled` 必须是布尔值；`effort` 必须在白名单内。
但 Demand prompt 只有一句“可选 thinking”，没有给出 schema；
模型很容易写成 provider 风格 `{"type": "enabled"}`、
字符串 `"high"`、或省略 `enabled` 的 `{"effort": "high"}`。

## Decision

### 1. Demand prompt 补上 schema

在 `========== 输出格式 ==========` 中明确：

- 推荐 `{"enabled": true, "effort": "high"}`；
- 分相形状；
- `effort` 白名单；
- 不写就保持当前策略。

### 2. 兼容常见形状

`_normalize_single_thinking` 现在接受：

- 字符串 `"high"` / `"off"` / `"on"`；
- provider 形状 `{"type": "enabled"|"disabled"}`；
- 只给 `{"effort": "high"}`（默认 `enabled=true`）；
- 原有 `{"enabled": bool, "effort": str}`。

仍然拒绝：

- `{"enabled": "yes"}`（字符串布尔）；
- 未知 `effort`；
- 非对象/非字符串的任意形状。

### 3. 记录原始坏值

`Decision` 新增 `thinking_raw`（仅诊断）。  
`actions/run.py` 的 warning 会追加 clipped raw：

```text
Cognition thinking 指令非法（invalid_thinking）：... raw={'enabled': 'yes'}
```

这样下一次不必猜模型到底写了什么。

## 明确不做

- 不因为 `thinking` 非法而丢弃同一信封里的动作；
- 不自动 clamp / 自动改成 high；
- 不把 `thinking` 变成 Task 状态或工作记忆；
- 不兼容任意未知 effort（仍保持白名单）。

## Consequences

### 正面

- 常见的 provider 风格/字符串写法不再产生无效 warning；
- E 的 thinking 偏好更可能真正生效，而不是每轮被忽略；
- 下一次出现真正非法形状时，日志能直接看到 raw。

### 负面 / 代价

- 兼容层让“什么算合法”稍微变宽；
- 仍不是任意形状都接受；
- 一个 turn 的多 segment 仍会重复打印 warning（这是 segment 级日志，不是 turn 级）。

## References

- `actions/parse.py`
- `actions/intent.py`
- `actions/run.py`
- `demand/prompt.py`
- `tests/test_thinking_control.py`
- `tests/test_demand_prompt_facts.py`

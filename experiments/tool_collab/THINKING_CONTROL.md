# Agent 自主思考模式（最小开关）

> **初始状态：thinking = disabled。**
> 之后由 Agent 在结构化信封里决定**下一步**是否开启思考、强度多少。
>
> 这是对 `docs/architecture/AGENT_CORE_MODEL_V1.md` §22.33
> “Initial Cognitive Run 优先非深度思考”的**最小落地** ——
> 不新增认知层级、不建 Decision Engine、不改 Decision Owner。

---

## 1. 为什么需要它

DeepSeek `deepseek-flash` 默认 `thinking=enabled`、`effort=high`。
5-Agent 空间站实验里，单轮 reasoning 常常吃掉 16,384 token，
`finish_reason=length` / `EMPTY_CONTENT`，一个 Session 只能走 2–3 轮。

但“所有调用都关闭思考”也不对：

- 简单读写不需要思考；
- 遇到失败测试、契约冲突、边界条件时，思考是有价值的。

所以控制权交给 Agent：

```text
初始：disabled
   ↓
Agent 在信封里决定下一步
   ↓
thinking = enabled / disabled，effort = low / high / max
```

---

## 2. 机制

### 2.1 信封字段

在原有的 `decision` / `actions` 旁边，可选加：

```json
{
  "decision": "act",
  "thinking": {"enabled": true, "effort": "high"},
  "action": {"type": "workspace_read", "parameters": {"path": "src/a.py"}}
}
```

- 不写 `thinking` → **保持当前状态**；
- `enabled=false` → 下一步关闭；
- `enabled=true` + `effort` → 下一步开启并指定强度；
- `effort` 省略 → 只开启，不指定强度（provider 默认）。

不合法形状（例如 `{"enabled": "yes"}`）不会丢掉同一信封里的动作：
`Decision.thinking_error = "invalid_thinking"`，动作照常执行。

### 2.2 何时生效

```text
seg0（第一次 LLM 调用）
   ↓ 解析 thinking 指令
立刻应用到 Agent 的 Brain
   ↓
seg1（读取回喂后的同一轮第二次调用）→ 已经用新状态
   ↓
下一轮（Turn N+1）→ 继续用新状态，直到 Agent 再改
```

`actions/run.py::_apply_thinking()` 只做这一件事：
把 `Decision.thinking` 交给 `agent.brain.set_thinking()`。

### 2.3 Provider 适配

`Brain` **不认识厂商**（有架构守卫盯着）：

- `Brain.generate()` 只根据 Agent 状态注入通用字段
  `thinking` / `reasoning_effort`；
- Provider 自己声明能力：

| Provider | `supports_thinking` | `supports_reasoning_effort` |
|---|---|---|
| DeepSeek | ✅ | ✅ |
| Doubao | ✅ | ❌（不发明未知字段） |
| Bailian | ❌ | ❌ |
| Mock | ❌ | ❌ |

`OpenAICompatibleProvider.generate()` 对不支持的字段做**静默丢弃**，
而不是把未知字段发给厂商换一个 400。

- `None`（默认）= 不注入，保持 provider 自己的默认行为；
- 因此这个能力是**增量**的：没有设置状态的 Agent / 旧测试完全不变。

### 2.4 初始状态

`experiments/heterogeneous_production/production.py`：

```python
INITIAL_THINKING_ENABLED = False
INITIAL_THINKING_EFFORT = "low"
```

启动时对所有参与 Agent 执行：

```python
a.brain.set_thinking(INITIAL_THINKING_ENABLED,
                     INITIAL_THINKING_EFFORT)
```

🔒 只影响**实验参与者**，不改其他 Agent 的 provider 默认行为。

---

## 3. 小实验（真实运行）

**人类命令**

> 实现一个 Python 模块 parse_duration(s)：把 '1h30m'、'90s'、'2d3h'、'0'
> 解析为秒；拒绝空串、负数、未知单位。写 pytest 测试并运行到通过。

- Run：`runs/1108-0941-fff0deab/`
- 参与者：`agent_a`（验收）、`agent_b`（生产）
- 结果：**COMPLETED**
- 墙钟：**1.3 分钟**
- 成本：**115,009 tokens / 1.15009 AIC**

协议链：

```text
START   agent_b  IN_PROGRESS
SUBMIT  agent_b  SUBMITTED
VERIFY  agent_a  VERIFIED
COMPLETE agent_a COMPLETED
```

交付物：

```text
src/parse_duration.py          793 字符   agent_b
tests/test_parse_duration.py  1,092 字符  agent_a
qa/review.md                    500 字符   agent_a
```

真实验收：

```text
run_tool(pytest)  exit_code=0  ok=true  23 passed
```

### 思考模式实际发生的事

```text
S1 R1 agent_a: thinking={'enabled': True, 'effort': 'low'}
                state   ={'enabled': True, 'effort': 'low'}
S1 R1 agent_b: thinking=None
                state   ={'enabled': False, 'effort': 'low'}
S1 R2 agent_a: state   ={'enabled': True, 'effort': 'low'}   ← 保持
S1 R3 agent_a: thinking={'enabled': True, 'effort': 'low'}   ← 再确认
S2 ...        agent_a: state   ={'enabled': True, 'effort': 'low'}
              agent_b: state   ={'enabled': False, 'effort': 'low'}  ← 一直关闭
```

- **初始调用确实是 disabled**；
- agent_a 在第一轮就自主决定开启 `low`；
- 这个决定在读取回喂的第二段和后续 Session 里持续生效；
- agent_b 始终选择关闭 —— 两个 Agent 做出了不同选择。

---

## 4. 复现

```bash
./.venv/bin/python experiments/tool_collab/run_tool_task.py \
  --task-type backend \
  --minutes 8 --session-minutes 4 --rounds 3 --budget 10 \
  --command "实现一个 Python 模块 parse_duration(s)：把 '1h30m'、'90s'、'2d3h'、'0' 解析为秒；拒绝空串、负数、未知单位。写 pytest 测试并运行到通过。"
```

查看思考状态：

```bash
python3 - <<'PY'
import json
d = json.load(open('results/heterogeneous_production_result.json'))
for s in d['sessions']:
    for t in s['turns']:
        print(s['index'], t['round'], t['agent_id'],
              t.get('thinking'), t.get('thinking_state'))
PY
```

---

## 5. 边界与已知限制

- **只影响 `thinking` / `reasoning_effort`**：不是完整的 Cognitive Runtime，
  没有多层级认知、没有 Decision Engine、没有自动升级规则。
- **强度值由 provider 解释**：DeepSeek 的 `low/high/max` 与
  `medium→high` 映射由 API 决定；我们不冻结具体映射。
- **不是全局默认**：初始 disabled 由实验设置；其他 Agent 仍是
  provider 默认（`None` = 不注入）。
- **Doubao 只声明 thinking**：`reasoning_effort` 会被丢弃，避免未知字段。
- **没有把 thinking 写进 `llm_calls`**：当前只在 turn telemetry /
  `result.json` 里可见；要进账本需要加字段与迁移。
- **思考不等于正确**：agent_a 开启了 `low`，但任务简单，无法证明
  “开启思考一定更好”；长实验需要比较开启/关闭的收益。

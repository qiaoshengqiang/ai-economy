# Agent 分相思考 + 最小工作循环

> **决策相优先低思考；工作相优先高思考。**
> 这是**默认偏好**，不是 Runtime 的硬限制 —— Agent 可以覆盖任一相。
>
> 对应 `docs/architecture/AGENT_CORE_MODEL_V1.md` §22.33 / §22.34
> 的最小落地：不新增认知层级、不建 Decision Engine、不改 Decision Owner。

---

## 1. 两相

```text
Decision（phase=decision，默认 off/low）
   ↓
Work（phase=work，默认 high；最多 N 步）
   ↓
Decision checkpoint（phase=decision）
   ├── 继续 work
   └── 结束 / 交付
```

| 相 | 默认偏好 | 用途 |
|---|---|---|
| `decision` | `enabled=False, effort=low` | 初始认知、工作后的决策检查点 |
| `work` | `enabled=True, effort=high` | 读文件、跑测试、修代码等多轮工作 |

🔒 **不 clamp**：Agent 可以把 decision 设成 `high`，也可以把 work 设成 `off`。
“优先低思考”只体现在实验设置的初始值里。

---

## 2. 信封字段

### 分相覆盖

```json
{
  "decision": "act",
  "thinking": {
    "decision": {"enabled": false, "effort": "low"},
    "work":     {"enabled": true,  "effort": "high"}
  },
  "action": {"type": "workspace_read", "parameters": {"path": "src/a.py"}}
}
```

### 单相覆盖（当前相）

```json
{"thinking": {"enabled": true, "effort": "low"}}
```

单相会被归一成 `{"*": ...}`，表示覆盖**产生这条决策的当前相**。

- 不写 `thinking` → 保持当前策略；
- 形状非法 → 记 `thinking_error`，但**不丢失**同一信封里的动作。

---

## 3. 工作循环

`actions/run.py::run_cognition(work_loop=True, max_work_steps=2)`：

```text
seg0  decision  → 执行动作
   ↓ 有可回喂的读取/工具结果
seg1  work      → 执行动作
   ↓ 仍有可回喂结果，且未到上限
seg2  work      → 执行动作
   ↓
segN  decision checkpoint  → 决定继续 / 结束 / 交付
```

- 只有**真的读到载荷**（`workspace_read` / `run_tool` / …）才进入 work；
- 没有可回喂结果不会空转；
- `max_work_steps` 是硬上限，防止失控 tool-loop；
- 每一段的 `phase` 都进入 telemetry（`segments[i].phase`）。

---

## 4. Provider 适配

`Brain` 不认识厂商名（有架构守卫盯着）；Provider 自己声明能力：

| Provider | `supports_thinking` | `supports_reasoning_effort` |
|---|---|---|
| DeepSeek | ✅ | ✅ |
| Doubao | ✅ | ❌（不发明未知字段） |
| Bailian | ❌ | ❌ |
| Mock | ❌ | ❌ |

- `Brain.generate(phase=...)` 选择策略并注入 `thinking` / `reasoning_effort`；
- 不支持的字段在 `OpenAICompatibleProvider.generate()` 里静默丢弃；
- 默认策略 `None` = 不注入，旧 Agent / 旧测试行为不变。

---

## 4.5 分相 temperature

`Brain` 也支持按相覆盖 temperature：

```python
brain.set_temperature(0.2, phase="decision")
brain.set_temperature(None, phase="work")   # None = 用 config 的 temperature
```

默认偏好：

| 相 | temperature | 理由 |
|---|---|---|
| `decision` | **0.2** | 判断更稳定，减少随机摇摆 |
| `work` | **None → config（0.7）** | 保留配置值；代码/创意任务可再调 |

**重要技术事实**：DeepSeek 在 `thinking=enabled` 时**忽略 temperature**
（API 文档明确说明；请求不会报错，但参数无效）。

所以：

- `decision` 默认 `thinking=disabled/low` → **0.2 生效**；
- `work` 默认 `thinking=enabled/high` → 即使设了 0.7，DeepSeek 也不会用它；
- 如果希望 work 的温度真正生效，需要 work 关闭思考，或换一个在思考模式下仍尊重 temperature 的 provider。

这是“优先”而不是“强制”：Agent / 实验可以覆盖任一相。

---

## 5. 小实验（真实运行）

**人类命令**

> 实现一个 Python 模块 parse_duration(s)：把 '1h30m'、'90s'、'2d3h'、'0'
> 解析为秒；拒绝空串、负数、未知单位。写 pytest 测试并运行到通过。

- Run：`runs/1108-1015-e11bd1fe/`
- 参与者：`agent_a`（验收）、`agent_b`（生产）
- 结果：**COMPLETED**
- 墙钟：**1.2 分钟**
- 成本：**87,668 tokens / 0.87668 AIC**

协议链：

```text
START   agent_b  IN_PROGRESS
SUBMIT  agent_b  SUBMITTED
VERIFY  agent_a  VERIFIED
COMPLETE agent_a COMPLETED
```

交付与验收：

```text
src/parse_duration.py          2,967 字符
tests/test_parse_duration.py  1,911 字符
qa/review.md                  1,381 字符

run_tool(pytest)  exit_code=0  ok=true  29 passed
```

### 实际 phase 轨迹

```text
S1 R1 agent_a: ['decision', 'work', 'decision']
S1 R1 agent_b: ['decision', 'work', 'work', 'decision']   ← 多轮工作
S1 R2 agent_a: ['decision', 'work', 'decision']
S1 R2 agent_b: ['decision']
S1 R3 agent_a: ['decision']
S1 R3 agent_b: ['decision', 'work', 'decision']
```

初始/结束策略：

```text
decision = {'enabled': False, 'effort': 'low'}
work     = {'enabled': True,  'effort': 'high'}
```

agent_b 在一条决策里显式写了：

```json
"thinking": {"decision": {"enabled": false, "effort": "low"}}
```

—— 它保留了决策低思考，同时工作相继续用默认 high。
Runtime 没有改写它的选择。

### 5.1 加入分相温度后（同一任务）

- Run：`runs/1108-1022-6af5a7b1/`
- 结果：**COMPLETED**
- 墙钟：**1.0 分钟**
- 成本：**94,161 tokens / 0.94161 AIC**
- 测试：**30 passed，exit_code=0**

温度策略（每一轮 telemetry 都是同一份）：

```text
temperature_policy = {'decision': 0.2, 'work': None}
```

解释：

- decision 相实际用 **0.2**；
- work 相 `None` → 回落到 config 的 **0.7**；
- 但 work 默认 `thinking=enabled/high`，DeepSeek 会忽略 0.7 ——
  所以本轮真正生效的温度差异只有 decision 相的 0.2。

phase 轨迹仍然保持：

```text
S1 R1 agent_a: ['decision', 'work', 'decision']
S1 R1 agent_b: ['decision', 'work', 'work', 'decision']
S1 R2 agent_a: ['decision', 'work', 'work', 'decision']
S1 R2 agent_b: ['decision']
S1 R3 agent_a: ['decision', 'work', 'decision']
S1 R3 agent_b: ['decision']
```

与上一轮（1.2 分钟 / 87,668 tokens / 29 passed）相比，
没有出现完成率或 phase 结构的退化；差异在正常随机波动范围内。
一次运行不足以证明“0.2 更好”，但证明**分相温度不会破坏工作循环**。

---

## 6. 复现

```bash
./.venv/bin/python experiments/tool_collab/run_tool_task.py \
  --task-type backend \
  --minutes 8 --session-minutes 4 --rounds 3 --budget 10 \
  --command "实现一个 Python 模块 parse_duration(s)：把 '1h30m'、'90s'、'2d3h'、'0' 解析为秒；拒绝空串、负数、未知单位。写 pytest 测试并运行到通过。"
```

查看 phase / thinking：

```bash
python3 - <<'PY'
import json
d = json.load(open('results/heterogeneous_production_result.json'))
for s in d['sessions']:
    for t in s['turns']:
        print(s['index'], t['round'], t['agent_id'],
              [x.get('phase') for x in t.get('segments') or []],
              t.get('thinking'), t.get('thinking_state'))
PY
```

---

## 7. 边界与下一步

**已实现**

- 分相 thinking 策略（decision / work）；
- 最小 `decision → work → decision` 循环，带硬上限；
- phase 级 telemetry；
- Provider 能力声明，不在 Agent 层写厂商分支。

**尚未实现**

- **工作记忆**：工作过程中/结束后抽象出 working memory，供下一次决策读取；
- **公共工作记忆**：Task 范围内对参与者可见的工作投影；
- **短期 / 长期记忆**：Layer 1 Private Memory 的当前任务部分与跨任务沉淀；
- **记忆与 Alignment 的关系**：公共工作记忆必须走 Proposal → Alignment，
  不能变成第二套 Shared Context / Task Core。

下一步建议先做 **working memory 的最小接口**：

```text
work trace
   ↓ consolidation（受控写入）
Agent Working Memory（私有、Task 范围）
   ↓
decision prompt 读取
```

公共工作记忆、短期/长期记忆在下一阶段单独设计。

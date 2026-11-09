# ADR-0051：SUBMIT 前 DOM/import 门禁 + 观点/事实冲突记录

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_6b2fd87b`（20 分钟墙钟，Task IN_PROGRESS）：

### 1. 页面首帧就崩

- `index.html` R1 有 `id="app"`；
- 05:01:56 C 用 replace 写 R2，把 `id="app"` 改成 `id="scene"`；
- `src/main.js` 仍是 `const app = $('app'); app.appendChild(...)`；
- 页面加载直接 `TypeError`，根本进不到渲染循环；
- `src/world.js`（9,514 字符）没有被 `main.js` import，是死代码；
- `src/terrain.js` 被 `resource_create` 建成了目录，随后写文件失败。

A 和 E 都没有抓到 `#app` 冲突。E 反而断言 `clock/elapsed`
未声明（实际 `main.js` line 74-75 已声明），A 用 Workspace 事实
纠正了 E；但 E 又重复了同一错误断言。

### 2. 观点被纠正后，没有可追踪的冲突记录

A 在消息里说“E 说错了”，但这条冲突只存在于聊天历史；
E 的私有工作记忆没有更新，下一轮继续重复旧观点。
没有公共记录让 E 自己发现并纠正。

## Decision

### 1. SUBMIT 前 DOM/import 静态 Smoke 门禁

Demand 层在 contractor 发送 `SUBMIT` 之前调用：

```text
demand/submit_check.py :: check_submit_deliverable()
```

检查：

- `index.html` 引用的本地 `<script src>` 是否存在；
- JS 本地 `import` 是否解析到真实文件；
- JS 的 `getElementById` / `$()` / `querySelector('#id')`
  引用的 DOM id 是否在 `index.html` 中；
- 可空检查（`if (loading) ...`）只给 warning；
- 未被引用的 JS / `.js` 目录只给 warning。

失败时 `_send` 返回：

```json
{"ok": false, "code": "submit_check_failed",
 "message": "SUBMIT 被拒绝：DOM/import 静态 smoke 检查失败..."}
```

**SUBMIT 消息不会发出**，Task 保持 `IN_PROGRESS`。
这不是 Task Core 的状态转移，也不是验收结论；只是 Demand 层的
结构性提交门禁。

### 2. Public WM 新增 `kind="conflict"`

```json
{
  "conflict:clock": {
    "kind": "conflict",
    "value": {
      "claim_by": "agent_e",
      "claim": "tick() 使用未声明的 clock/elapsed",
      "fact": "src/main.js R1 line 74-75 已声明 const clock / let elapsed",
      "status": "corrected_by_fact"
    },
    "evidence": {"path": "src/main.js", "revision": 1}
  }
}
```

规则：

- value 必须包含 `claim_by` / `claim` / `fact`；
- 推荐附 `evidence={path, revision}`；service 仍校验 artifact 存在/revision；
- Public WM 渲染会在顶部列出冲突记录数量和 key；
- 断言作者看到 `claim_by` 是自己时，应核对 evidence，
  更新 Private WM / 公开回复，不要重复旧断言；
- 它是**争议记录，不是系统裁决**；`fact` 仍是记录者的观察，
  evidence 只证明 artifact 存在，不证明语义真值。

Prompt 规则：

```text
如果你用 Workspace 事实推翻了另一个 Agent 的断言，
把它记录进公共工作记忆：kind="conflict" ...
如果你看到 kind="conflict" 且 claim_by 是你，
先核对 evidence；如果确实错了，更新私有 WM / 公开回复。
```

## 明确不做

- 不自动判定谁对谁错；
- 不自动写工作记忆（仍由 Agent 自己写）；
- 不把 `conflict` 变成 Task 状态或 Shared Context；
- 不因为 warning（死代码 / `.js` 目录）阻塞 SUBMIT；
- 不替代 acceptor 的人工/实测验收；门禁只查结构一致性。

## Consequences

### 正面

- `#app` 缺失、本地 import 断链会在 SUBMIT 前被拦住；
- 页面首帧 TypeError 不会被当成“已交付”；
- 观点/事实冲突有公共、可引用的记录；
- 被纠正的 Agent 有机会在后续 turn 看到并修正自己的记忆；
- 错误观点不再只存在于聊天历史里。

### 负面 / 代价

- 静态检查不能证明页面运行正确，只是结构 smoke；
- 正则解析 HTML/JS 有边界（动态 import、模板字符串等可能漏检）；
- 每次 SUBMIT 多一次 Workspace 读取；
- `conflict` 仍是 Agent 的记录，不保证冲突描述本身正确。

## References

- `demand/submit_check.py`
- `demand/actions.py`
- `core/public_working_memory.py`
- `services/public_working_memory_service.py`
- `demand/prompt.py`
- `docs/architecture/PUBLIC_WORKING_MEMORY.md`
- `docs/architecture/DEMAND_ENTRY_V1.md`
- `tests/test_demand_submit_check.py`
- `tests/test_public_working_memory.py`

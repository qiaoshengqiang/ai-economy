# ADR-0049：观点 vs 事实、锁变更可见、单 Agent 动作事实

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_cc61c0b5`（TIME_LIMIT，Task IN_PROGRESS）暴露了三个
协作问题：

1. **观点被当成事实。** E 的静态审查说“无枕木列”，A 没有回读 R1 验证，
   直接把它写成 `review/PATCH.md`。R1 实际有 `const slp` 枕木
   InstancedMesh，PATCH 再次声明 `const slp`，导致粘贴后
   `Identifier 'slp' has already been declared`，补丁不可用。
2. **锁变更被误读。** C 在墙钟前 26 秒把 `index.html` handoff 给 A；
   A 看到 `lock=agent_a`，却判断“锁此前一直在 agent_a 手上”，
   又把锁交回 C，错失最后的写入窗口。
3. **单 Agent 停摆被团队进展掩盖。** C 连续 17 分钟只读（21 次 read /
   5 次 search / 0 次 write/edit / 0 条消息），但 E/A 的测试与 review
   写入不断重置团队级 `NO_PROGRESS`，C 的本地停摆没有暴露。

> 测试墙钟是实验夹具；正式 AIEOS 不限制任务时间，也不把剩余时间
> 作为 Agent 输入。本 ADR 不引入墙钟压力。

## Decision

### 1. 其他 Agent 的断言是观点，不是 Domain Fact

Task prompt 新增“观点 vs 事实”结构事实：

- 其他 Agent 的 message / review / Public WM note 是**观点/观察**；
- 收到“某功能缺失 / 某文件不存在 / 某实现没做”这类断言时，按顺序：
  1. 先查**自己的私有工作记忆**：是否已验证过、是否有相反观察；
  2. 再用 `workspace_read` / `workspace_search` 验证当前 Workspace；
  3. 以 Workspace 当前 `path + revision` 为准；
- 不把观点直接写进 PATCH、计划或完成声明；
- 观点与 Workspace 冲突时，先纠正观点再行动。

Public WM 渲染也明确标注：条目是参与者的记录/观点，不是 Domain Fact；
完成声明只有带 `evidence` 才是已验证事实。

### 2. 锁变更可见

DemandRunner 为每个 Agent 保存“上次 turn 结束时的锁快照”。
下一轮 prompt 显示自那以后的变化：

```text
========== 锁变更（自你上次 turn 以来；事实） ==========
- index.html: lock agent_c → agent_a
  🔒 你现在持有这个资源的写锁；如果计划中的下一步是写它，
  可以直接 workspace_write / workspace_edit。
```

只展示事实，不替 Agent 决定是否写、是否 handoff。

### 3. 单 Agent 动作事实

DemandRunner 维护每个 Agent 的连续只读 turn 数：

- 只有 read / search / query 类动作的 turn 才累加；
- 写、编辑、handoff、消息、计划更新等任何结构性/沟通动作都清零；
- 连续 ≥3 轮时，在该 Agent 的 prompt 显示 streak、持有的锁、
  未回应请求数；
- creator 还能看到其他 Agent 的同类事实。

这不是墙钟，也不是指令；Agent 自己决定继续、handoff 或回复。

### 4. `workspace_plan_update`（仅 creator）

新增 Action：

```json
{"type": "workspace_plan_update",
 "parameters": {"plan": {"root_owner": "...", "directories": {}, "files": {}},
                "reason": "缩成单文件"}}
```

- 只允许 creator；
- 复用 `task_create` 的 `workspace_plan` 校验（root_owner=producer、
  owner 是 participants、父目录已声明）；
- 替换 `run.workspace_plan` 并写 `run.json`；
- 后续“计划完成度”以新 plan 为准，不再显示已移除的 `src/*`。

### 5. Playwright 正式 suite 的入口 URL

Tool Runtime 在 smoke gate 之后、正式 suite 运行期间，为 Workspace
入口页启动临时静态服务器，并设置：

```text
BASE_URL=http://127.0.0.1:<random>/index.html
```

spec 应使用 `process.env.BASE_URL`，不要硬编码 8000 端口。
这解决真实 run 中 E 的 4 次 Playwright 全部 `ERR_CONNECTION_REFUSED`
的问题。

## 明确不做

- 不把测试墙钟剩余时间/时间压力给 Agent；
- 不用 `TIME_LIMIT` 修改 Task 状态（正式系统不限制任务时间）；
- 不强制 Agent 写工作记忆；
- 不要求其他 Agent 的 review 必须正确——观点可以错，关键是执行者要验证；
- 不自动把观点升级为事实，也不自动把事实降级为观点。

## Consequences

### 正面

- Creator 在写 PATCH 前会先检查私有 WM 和 Workspace，减少基于错误
  观点构建补丁；
- 锁变更不再需要从当前 holder 推断历史；
- 单 Agent 的只读循环会变成可见事实；
- 交付范围变化后计划完成度不会继续显示旧文件；
- Playwright 验收可以真正打开页面。

### 负面 / 代价

- 每个 Agent 每轮多一次锁快照读取（只读、轻量）；
- Prompt 增加少量事实段落；
- `workspace_plan_update` 是新的动作面，需要 Creator 正确使用；
- 观点验证仍是 Agent 的判断，系统只提供事实和顺序，不能保证
  每次推理正确。

## References

- `demand/prompt.py`
- `demand/runner.py`
- `demand/actions.py`
- `demand/task_creation.py`
- `actions/workspace_plan.py`
- `services/workspace_gateway.py`
- `tool_runtime/runner.py`
- `resources/public/tools/playwright.md`
- `resources/public/SYSTEM_MAP.md`
- `docs/architecture/DEMAND_ENTRY_V1.md`

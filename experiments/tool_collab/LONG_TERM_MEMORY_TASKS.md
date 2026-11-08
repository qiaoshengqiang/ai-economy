# 长期记忆 · 多任务测试

> 同一个数据库、同一组 Agent（`agent_a` / `agent_b`）连续跑多个任务。
>
> DB：`runs/1108-1254-208be94a/tool.db`

---

## 1. 任务结果

| # | 任务 | Run | 结果 | 墙钟 | 成本 | 测试 |
|---|---|---|---|---|---|---|
| 1 | 网页 2048 | `1108-1350-a21b8fc3` | **COMPLETED** | 5.9 min | 372,714 tokens / 3.72714 AIC | Playwright 1 passed |
| 2 | three.js 战斗机 | `1108-1357-215b65ab` | **COMPLETED** | 3.5 min | 153,993 / 1.53993 | Playwright 1 passed |
| 3 | Python 聊天室 | `1108-1402-cf16ad13` | **COMPLETED** | 6.1 min | 391,981 / 3.91981 | Playwright 2 passed |
| 4 | 记忆校准探针 | `1108-1410-a0854abb` | **COMPLETED** | 2.3 min | 259,600 / 2.596 | pytest 通过 |

### 2048

- 单文件 `site/index.html`（9,442 字符）
- 4x4、方向键/WASD、合并计分、`localStorage` 最高分
- Playwright 1 passed
- 截图：`runs/1108-1350-a21b8fc3/screenshot.png`

### three.js 战斗机

**依赖检查（重要）**：

- 本地**没有预装** three.js；
- `npm install` 不在 Agent 能力内；
- CDN 可访问（`unpkg` / `jsdelivr` 返回 200）；
- 实测 `file://` 页面可以加载 CDN 的 three.js module（Chrome）。

任务命令明确写了这个环境事实，并要求 Playwright 实际确认。

- 单文件 `site/index.html`（12,481 字符）
- Playwright 输出：`three=160`、`meshes=20`、`triangles=3384`、
  `lights=4`、`canvas 1280x720`
- 截图：`runs/1108-1357-215b65ab/screenshot.png`

### Python 聊天室

- `site/index.html`（4,305 字符）+ `site/server.py`（6,429 字符）
- Python 标准库 HTTP server + GET/POST `/api/messages`
- 中途出现 `global next_id` / `_next_id` 不一致导致的失败，
  agent_a 独立复跑后定位，agent_b 修复
- Playwright 最终 **2 passed**
- 截图：`runs/1108-1402-cf16ad13/screenshot.png`

---

## 2. 长期记忆使用情况

前三个任务结束时，报告都是：

```text
agent_a: used 0 / 6 条
  unused: 6 条（权重已下降；可用 long_term_memory_search/read 主动查看）

agent_b: used 0 / 6 条
  unused: 6 条
```

也就是说：

- 机制存在；
- 但 Agent 在三个任务里都**没有主动使用长期记忆**；
- 权重全部按规则下降 0.2。

---

## 3. 记忆校准任务：验证“读取 = 使用”

第四个任务明确要求 Agent：

```text
long_term_memory_read
  agent_a → ltm-b048b349c1fc
  agent_b → ltm-e68ac6f73951
```

结果：

```text
agent_a: used 1 / 6 条
  [ltm-b048b349c1fc] weight=2.1 use_count=3
agent_b: used 1 / 6 条
  [ltm-e68ac6f73951] weight=3.1 use_count=5
```

`long_term_memory_read` 返回的短期存档：

```text
stm-3099b6f9f04d  task_tool_208be94a  parse_duration 验收
stm-8c549a207579  task_tool_5822a390  duration.py 验收
```

agent_a 写出的 `qa/memory_probe.md` 明确记录：

> 长期记忆已在开工前读取，并实际改变了本轮动作顺序
>（先落 tests/ 与 qa/contract.md，再等实现）。

这证明：

1. `long_term_memory_read` 自动把该长期记忆标记为 used；
2. 权重/ use_count 增加；
3. 任务结束报告能显式显示 used / unused；
4. Agent 能通过长期记忆拿到具体短期存档。

---

## 4. 结论

- **长期记忆 + 权重反馈已闭环**：
  - 默认注入 top-N；
  - 低权重不注入但可 search/read；
  - read = use，权重增加；
  - 未使用自动下降；
  - 任务结束显式报告。
- **Agent 不会在普通任务里自发使用长期记忆**：
  前三个任务 used=0；只有在明确要求“先读长期记忆”时才会用。
  这不是机制失败，而是当前 Prompt/激励不足。
- **依赖能力现状**：
  - three.js 未本地预装；本次通过 CDN 成功；
  - Agent 仍不能自行 `npm install`；
  - 对离线环境，需要预先提供 vendor 资源。

# 串行调度与读图能力：观察与设计建议

> 基于 `1108-1431-c84b6dc2` 林中小屋任务的数据与真实 API 探测。

---

## 1. 结论

1. **目前 Agent 的工作是完全串行的。**
   Agent 轮次串行、轮次内动作串行、工具执行串行。
   一次 30.1 分钟的运行里，LLM 调用 860 秒 + Playwright 942 秒 ≈ 1802 秒，
   几乎正好等于墙钟 1806 秒 —— **没有任何重叠**。
2. **串行是任务时间的主要放大器。**
   5 个 Agent 每轮各自跑一次，Session 1 光是 5 个 turn 就用了 523 秒；
   6 分钟的 Session 实际跑了 8.7 分钟，因为限时只在 turn 之间检查。
3. **读图能力应该补，而且现在就能验证。**
   `deepseek-flash` 已支持图片输入；用真实 API 看一张截图，
   模型一次就指出了「没有编辑面板」和「屋顶板悬空」两个缺陷。
   当前系统从 Provider 到 Workspace 全是纯文本，截图永远到不了 Agent。

---

## 2. 串行证据

### 2.1 代码事实

`experiments/heterogeneous_production/production.py`：

```python
# _run_session()
for aid in AGENTS:
    if sess.is_exceeded():
        break
    turn = _turn(rt, bridge, st, stage, aid, rnd, stage_txt, ...)
```

- 没有 `async`、没有线程池、没有 `gather`；
- 一个 Agent 的 `_turn()` 返回后，才轮到下一个；
- `RuntimeSession.is_exceeded()` **只在 turn 之前检查**，
  正在跑的 turn 不会被打断 —— 所以一个 282 秒的 turn 可以把
  6 分钟的 Session 拖到 8.7 分钟。

`actions/run.py::_dispatch_all`：一个信封里的多个动作也是 `for` 循环串行执行。

`channels.SyncBridge`：一个实例只持有一个 `asyncio` event loop，
`run_until_complete` 不是线程安全的 —— 这是并行 turn 的直接技术障碍之一。

### 2.2 实测数字

| 项目 | 数量 | 总耗时 |
|---|---|---|
| LLM 调用 | 95 次 | 860.2 秒 |
| Playwright 工具 | 14 次 | 941.6 秒 |
| 墙钟 | — | 1806 秒（30.1 分钟） |

```text
860 + 942 = 1802 ≈ 1806
```

这说明：**LLM 和工具几乎占满了全部墙钟，二者之间没有重叠。**

按 Agent 拆分（LLM + 工具）：

| Agent | LLM | 工具 | 合计 |
|---|---|---|---|
| agent_a | 176s | 212s | 388s |
| agent_b | 186s | 18s | 204s |
| agent_c | 178s | 0.2s | 179s |
| agent_d | 89s | 0.2s | 90s |
| agent_e | 230s | 686s | 916s |

如果**完全并行**，理想墙钟 ≈ 916 秒（15.3 分钟），是现在的一半。
但 agent_e 的 686 秒里包含大量「页面还没修好就反复跑全量测试」的浪费，
所以真实收益要看调度策略，不能简单按 max 算。

### 2.3 Session 1 的具体时间线

| Agent | 耗时 | 说明 |
|---|---|---|
| agent_a | 72.6s | 写契约 + 4 条消息 |
| agent_b | 14.6s | 写 154 字节的 index.html |
| agent_c | 128.7s | 第一次截断 + 第二次成功 |
| agent_d | 25.1s | 写 ui.js/ui.css |
| agent_e | 282.0s | 写 QA + 跑 5 个失败 spec |
| **合计** | **523s** | 6 分钟 Session 实际 8.7 分钟 |

### 2.4 时间去哪了：Playwright 的 15.7 分钟

- 14 次 Playwright，942 秒；
- 其中 4 次是 ~205 秒的 5 用例全量套件（Session 1/2 的 agent_e，
  Session 3 的 agent_a 和 agent_e），页面当时还没有可用入口；
- 每个失败用例等 `#app canvas` 40 秒，5 个就是 200 秒；
- 这四次合计约 **13.7 分钟**，占墙钟近一半。

更糟的是：agent_e 写了 `qa/playwright.config.js`（含 `baseURL` 和
`webServer`），但 Tool Runtime 在 run 根目录 bootstrap 了一个
`playwright.config.js`，Playwright 默认读根配置，**agent_e 的配置从未生效**。
所以 `page.goto('/site/index.html')` 变成非法 URL，测试直接不执行。

---

## 3. 并行化：哪里能并、哪里不能并

### 3.1 可以并行的

| 层次 | 现状 | 可行性 | 收益 |
|---|---|---|---|
| 独立 Agent turn | 串行 | 中（需要 per-thread bridge + 依赖波次） | 高 |
| 工具执行 vs 其他 Agent 思考 | 串行 | 高（工具目录天然隔离） | 中高 |
| 同一 turn 的多个 `run_tool` | 串行 | 高（独立 run 目录） | 中 |
| Playwright 内部用例 | 部分并行 | 已有 `workers` 配置 | 低 |
| 独立任务（不同 Task） | 未实现 | 高（多进程已支持） | 高（但不是当前实验） |

### 3.2 不能直接并行的

- **有依赖的 Agent**：agent_e 的验收必须在 agent_b SUBMIT 之后；
  agent_b 的集成需要 agent_c/agent_d 的模块。
  直接 `gather` 会让 QA 看到过期状态。
- **共享 Task 状态**：两个 Agent 同时 SUBMIT/VERIFY 会撞状态机；
  虽然有乐观锁，但语义上应该由调度器保证顺序。
- **共享 `SyncBridge`**：一个 event loop 不能跨线程 `run_until_complete`。
  并行 turn 需要每个 worker 一个 bridge，或改成 async 调度器。

### 3.3 推荐的调度方式

**不要**简单地把 `for aid in AGENTS` 换成线程池。建议：

1. **依赖波次（wave）**：
   ```text
   Wave 1: agent_a（契约）
   Wave 2: agent_b / agent_c / agent_d（各自命名空间，可并行）
   Wave 3: agent_b（集成） + agent_e（写测试，可并行）
   Wave 4: agent_b（修复） + agent_e（验收，依赖 SUBMIT）
   ```
2. **工具异步化**：`run_tool` 提交到后台线程/进程，
   当前 Agent 进入 `waiting_tool` 状态，调度器去跑别的 Agent；
   工具完成后把结果回投给它的下一轮。这样 200 秒的 Playwright
   不会阻塞其他 4 个 Agent。
3. **工具结果缓存**：同一 workspace revision + 同一 spec，
   不重复跑；只重跑变更的 spec。
4. **Smoke gate**：先跑 5 秒的「页面能打开、canvas 存在」，
   通过后才跑全量 90 秒套件。
5. **限时中断**：给每个 turn 一个 `min(remaining_session, cap)` 超时；
   不要把 6 分钟的 Session 交给一个 282 秒的 turn 去决定。

---

## 4. 读图能力：API 事实

官方文档：[DeepSeek Vision](https://api-docs.deepseek.com/guides/vision.md)

关键事实：

- `deepseek-flash` 支持图片输入（`deepseek-v4-pro` 不支持）；
- Chat Completions 的 `content` 可以是数组：
  ```json
  [{"type":"text","text":"..."},
   {"type":"image_url","image_url":{"url":"data:image/png;base64,...","detail":"low"}}]
  ```
- 支持 JPEG / PNG / GIF / WebP；
- 图片只允许在 **user** 消息里；system / assistant 里会 400；
- 单图最多约 **1024 tokens**；`detail: low` 会降到 512×512；
- 可以用 base64、公网 URL、Files API `file_id`；
- 图片 token 和文本一起计费。

### 4.1 真实探测（本次跑的两张截图）

用 `deepseek-flash` + `detail: low`，问「有没有编辑面板、屋顶是否完整」：

**真实提交物（无 UI + gable 屋顶破损）**：

```text
1. 画面主要展示一个带台阶和路灯的木质小屋……
2. 页面上没有看到任何可操作的编辑面板、工具栏或按钮，整个画面只有 3D 模型。
3. 小屋屋顶明显不完整，由多块错位、悬空的深棕色板面组成。
4. 屋顶各板面之间存在较大空隙和错位，露出了内部结构……
```

**诊断副本（有 UI + 同样的屋顶破损）**：

```text
1. 画面是一个 3D 森林小屋场景，右侧叠加了一块半透明绿色设置面板……
2. 页面上有可操作的编辑面板/按钮：右侧面板能看到“屋顶类型、门、窗、光照”等分组选项……
3. 小屋屋顶明显不完整：左前侧屋顶板向上翘起并悬空……
5. UI 问题：面板底部出现一小段类似代码/JSON 的文本……像是调试信息暴露在界面中。
```

**成本**：

```text
无 UI 图：prompt 287 + completion 536 = 823 tokens
有 UI 图：prompt 287 + completion 759 = 1,046 tokens
```

一次读图约 **1k tokens**，比 200 秒的失败 Playwright 便宜得多。

### 4.2 一个重要细节

第二次探测用 `max_tokens=600` 时，返回了空内容：
`completion_tokens=600, reasoning_tokens=600` —— 推理把预算吃光了。
把 `max_tokens` 提到 2000 后正常。

所以读图不是「加一个 flag」：**视觉 + 推理需要更大的输出预算**。
当前 Agent 的 16,384 是够的，但工具级的小调用要注意。

---

## 5. 当前代码的缺口

| 层 | 现状 | 缺口 |
|---|---|---|
| Provider | `supports_thinking` / `supports_reasoning_effort` | 没有 `supports_image_input` |
| `llm/base.py` | `normalize_messages` 把 content 强转 `str` | 会破坏 `[{type:"image_url"}]` |
| `llm/base.py` | `estimate_messages_tokens` 按字符串估算 | 图片 token 未计 |
| `llm/openai_compatible.py` | `payload["messages"]` 原样传递 | 只要 content 是数组，其实已能工作 |
| `agents/brain.py` | `_capture_request_evidence` 把 messages 拼成文本 | base64 会进证据，必须跳过 |
| `tool_runtime/runner.py` | `ToolRunResult` 只有 stdout/stderr | 没有 `images` / `artifacts` |
| `actions/read_payload.py` | `run_tool` 只渲染文本 | 截图不会回喂 |
| `workspace_resources` | `content TEXT` | 没有二进制资源 |
| `communication.Message` | `payload` 是 dict | 没有图片附件语义（V1 可不需要） |
| `qa/*.spec.js` | `page.screenshot()` 写到 run 目录 | Agent 看不到 |

---

## 6. 建议的 V1 设计（两阶段）

### Phase 1：Visual Observation Tool（最小可用，1–2 天）

目标：让 Agent 能「看」一张截图，但不改核心消息栈。

1. `ToolRunResult` 增加 `images`：
   ```json
   [{"path":"test-results/.../actual.png","mime":"image/png",
     "width":1280,"height":800,"bytes":12345}]
   ```
   - 扫描 `run.work` 下的 PNG/JPEG/WebP；
   - 最多 2 张，优先 `test-results/**` / `screenshots/**`；
   - 只存相对路径 + 元数据，不存 base64。
2. `run_tool` 的 Agent 输出带上 `images` 元数据。
3. 新增动作 `visual_inspect`：
   ```json
   {"type":"visual_inspect",
    "parameters":{"run_id":"run_...","path":"...","question":"这个页面有没有编辑面板？"}}
   ```
   - 组合根提供 `VisionService`；
   - 解析 run 目录内路径（路径逃逸防护）；
   - 读取图片 → 压缩到 ≤768px → base64 → `deepseek-flash`；
   - 返回 `{description, usage, image_meta}`；
   - 这是**一次独立的受控模型调用**，不是 Tool Runtime 子进程，
     不违反「工具不能读 secret」的边界。
4. Agent 的 prompt 里说明：视觉任务在 `run_tool` 后可以调用
   `visual_inspect`；不强制。

**优点**：不改 `normalize_messages`、不改协议、不改 Experience 结构；
一周内能验证价值。

**缺点**：多一次 LLM 调用；Agent 需要主动调用；描述可能丢细节。

### Phase 2：原生多模态（架构级，1–2 周）

目标：截图作为 `run_tool` 的真实返回，直接进入 Agent 的下一轮认知。

1. `LLMProvider.supports_image_input`（DeepSeek True，其他默认 False）。
2. `normalize_messages` 保留 content parts；`estimate_messages_tokens`
   按 1024/图计。
3. `Brain` 的证据采集跳过 base64（避免日志膨胀）。
4. `render_read_results` 支持返回 content parts：
   ```python
   [{"type":"text","text":"run_tool ..."},
    {"type":"image_url","image_url":{"url":"data:image/png;base64,...","detail":"low"}}]
   ```
5. `run_cognition` 的 follow-up user 消息支持图片。
6. `Experience` 只存图片元数据/路径，不存 base64；
   回放时按需重新加载。
7. 预算：每轮最多 1–2 张图，`detail: low`；视觉 token 进入成本。
8. 安全：截图是**不可信内容**，页面文字不能变成指令；
   与「网页内容不可信」同一条规则。

**Phase 2 之后**，Agent 才能真正做到：
「我跑完测试 → 看到截图 → 发现屋顶破了/面板没了 → 自己修」。

---

## 7. 与本次任务的关系

如果本次有 Phase 1：

- agent_e 在第一次 Playwright 后调用 `visual_inspect`；
- 一次 1k token 的读图就会看到「没有面板 + 屋顶悬空」；
- 不用等 3 次 205 秒的全量测试；
- 可能节省 10 分钟墙钟和 1–2 个 Session。

如果本次有 Phase 2：

- `run_tool` 的截图直接进入 agent_e 的下一轮认知；
- 它可以把「视觉证据」写进 `qa/review.md`；
- 验收不再只依赖 DOM/像素统计。

但读图**不能替代**确定性测试：
DOM 状态、`getState()`、canvas 尺寸仍然要测；
读图补的是「看起来对不对」这一层。

---

## 8. 一句话

> 现在不是「Agent 不够聪明」，而是**它们排队做、而且看不见自己的页面**。
> 先把独立的 turn / 工具放开并行，再把截图送进它们的眼睛，
> 30 分钟的任务有机会压到 15–20 分钟，而且第一次就能发现屋顶破了。

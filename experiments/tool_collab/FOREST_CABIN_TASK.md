# 复杂任务观察：沉浸式 3D 林中小屋（5 Agent）

> 人类命令原文见 `run_forest_cabin.py` 的 `HUMAN_COMMAND`。
> 同一数据库：`experiments/tool_collab/runs/1108-1254-208be94a/tool.db`
> 本次 Run：`experiments/tool_collab/runs/1108-1431-c84b6dc2/`

---

## 1. 运行配置

| 项 | 值 |
|---|---|
| 参与者 | `agent_a` … `agent_e`（5 个） |
| 编排 | GUIDED 角色脚手架（a=契约/协调，b=场景+集成，c=小屋，d=面板，e=验收） |
| 模型 | 全部 `deepseek-flash` |
| 墙钟上限 | 30 分钟 |
| 单 Session | 6 分钟，最多 3 轮 |
| Task 预算 | 30 AIC（实际消耗 10.97 AIC） |
| 思考初始偏好 | intake `enabled/max` · decision `disabled/low` · work `enabled/high` |
| 温度 | intake 0.2 · decision 0.2 · work 0.7 |
| 输出上限 | 16,384 tokens / 次调用 |

启动命令：

```bash
./.venv/bin/python -u experiments/tool_collab/run_forest_cabin.py \
  --db experiments/tool_collab/runs/1108-1254-208be94a/tool.db \
  --minutes 30 --session-minutes 6 --rounds 3 --budget 30
```

---

## 2. 结果

**没有完成。**

```text
退出原因 : TOTAL_WALL_LIMIT_REACHED
墙钟     : 30.1 分钟 / 4 个 Session / 26 个 Agent turn
成本     : 1,096,673 tokens / 10.96673 AIC
Task 状态: SUBMITTED
事件     : START → SUBMIT    （没有 VERIFY / COMPLETE）
```

- 生产者 `agent_b` 在 Session 4 发出了 SUBMIT；
- 验收人 `agent_e` 没有写出 `qa/review.md`，也没有 VERIFY；
- 协调者 `agent_a` 在 Session 4 声称完成，被系统以
  `waiting-for-qa-artifact` 拦住（**硬约束生效**）；
- 墙钟到点，Runner 退出。

产物：**12 个文件，84,213 字符**。

```text
docs/contract.md               7,829   agent_a
docs/integration_blocker.md      643   agent_a
site/index.html                2,894   agent_b
site/main.js                   8,402   agent_b
cabin/cabin.js                23,767   agent_c
ui/ui.js                       7,924   agent_d
ui/panel.js                    9,138   agent_d
ui/ui.css                      4,982   agent_d
qa/scene.spec.js               6,416   agent_e
qa/cabin-ui.spec.js            8,735   agent_e
qa/playwright.config.js          745   agent_e
qa/png.js                      2,738   agent_e
```

---

## 3. 时间线（关键事件）

| 时间 | 事件 |
|---|---|
| 14:31 | 启动；`agent_a` 接到 intake（max），写 `docs/contract.md` |
| 14:33 | `agent_a` 一次 turn 内发出 4 条消息，把契约/边界/DoD 分给 b/c/d/e |
| 14:33 | `agent_b` 写 `site/index.html` —— **只有 154 字符**，停在 `viewport-fit=cover` 中途 |
| 14:34 | `agent_c` work 相 high，推理 54,241 字符，输出 16,384 tokens 被截断 → `EMPTY_CONTENT` |
| 14:35 | `agent_c` 第二次调用成功，一次写出 `cabin/cabin.js` 23,767 字符 |
| 14:36 | `agent_d` 写出 `ui/ui.js` + `ui/ui.css` |
| 14:40 | `agent_e` 写 QA specs，跑 Playwright：**5 个 spec 全失败**（`#app canvas` 超时） |
| 14:41 | Session 1 结束（6 分钟只跑了 1 轮：5 个 Agent × ~70s） |
| 14:42 | `agent_b` 写 `site/main.js`，但仍未修 `index.html` |
| 14:49 | `agent_a` 独立复跑，确认根因，写 `docs/integration_blocker.md` |
| 14:55 | `agent_b` 重写 `index.html` → 2,678 字符；**仍然缺少 `#ui-root`** |
| 14:55 | `agent_c` 把 work 从 high 降到 low；`agent_a` 在最后阶段把 decision/work 都降到 low |
| 14:57 | `agent_e` 把 decision 调到 high，重跑测试；`cabin-ui.spec.js` 仍用相对 URL → 5 个用例 invalid URL |
| 14:58 | `agent_b` 发出 SUBMIT（summary 声称“ui 编辑面板实时改配置”） |
| 14:59 | `agent_a` 声称完成；系统拦截：`waiting-for-qa-artifact` |
| 15:01 | `agent_e` 连续 8 条消息给 `agent_b`；尝试 `append qa/review.md` 4 次，全部 `not_found` |
| 15:02 | 墙钟到点，退出 |

---

## 4. 思考强度：**没有任何 Agent 把 work / decision 开到 `max`**

初始状态（所有 Agent）：

```json
{"intake":{"enabled":true,"effort":"max"},
 "decision":{"enabled":false,"effort":"low"},
 "work":{"enabled":true,"effort":"high"}}
```

运行期共 **11 次**思考模式变更：

| 次数 | 设置 |
|---|---|
| 7 | `high`（主要是 `agent_e` 的 decision/work、`agent_c` 的 work） |
| 4 | `low`（`agent_c` work ×2、`agent_a` decision/work ×2） |
| **0** | **`max`** |

也就是说：

1. `max` 只在 **intake 相**被使用（且只有 `agent_a` 真正进入过 intake）；
2. 面对“商业级、高要求”的任务，Agent 仍把 `work=high` 当作足够；
3. 遇到集成失败/时间压力时，它们**主动降到 low**，而不是升到 max；
4. 唯一一次真正“想太深”的代价是 `agent_c` 的 54,241 字符推理塞满 16,384 输出上限，
   直接 `EMPTY_CONTENT` —— 这会让 Agent 更倾向“不要把强度开满”。

**结论**：在当前 Prompt 与 16k 输出预算下，“把思考开到 max”不会自发出现。
`high` 已经是它们认知里的“深度工作”，`max` 被理解为 intake 专用。
若希望观察 max 行为，需要：

- 把 `max` 从“intake 专用”改成“高复杂度任务可显式选择”，并给出激励；
- 同时提高 `max_tokens`（或分离 reasoning budget），否则 max 只会更快撞截断。

---

## 5. 做得好的地方（真实能力）

### 5.1 契约能力（agent_a）

一个 turn 内写出 **7.8KB** 的契约，包含：

- 运行方式（workspace 根、入口 URL、importmap、禁止 npm）；
- 目录所有权（b/c/d/e 各写哪里，禁止跨目录）；
- `index.html` 骨架与模块签名；
- `window.ForestCabin` 唯一对外 API；
- 场景对象命名、DOM `data-testid`、视觉底线、DoD 对照。

这不是“泛泛而谈”，而是可以直接照着实现的接口文档。

### 5.2 定位阻塞（agent_a）

在 QA 只报“canvas 超时”的情况下，`agent_a` 独立复跑后写出
`docs/integration_blocker.md`：

```text
site/index.html = 154 字节，无 body / 无 #app / 无 script
site/main.js 存在但从未被加载
→ qa 全部失败的唯一根因
修复 Owner：agent_b
```

诊断准确，责任人也正确。

### 5.3 单点生产力（agent_c）

`cabin/cabin.js` 一次产出 **23,767 字符**，包含：

- 3 种屋顶（gable/hip/shed）、4 种墙体、门窗、烟囱、门廊、灯笼；
- 全部 setter、`getState()`、几何重建；
- 2,260 个三角形的小屋。

### 5.4 独立验收（agent_e）

- 写了 PNG 解码器（`qa/png.js`）来判断 canvas 是否真的非空白；
- 用 `getState()` 前后对比验证配置变化；
- 在 API 层验证了 7 个 setter 都真实改变状态；
- 发现“页面能打开但 canvas 不存在”这类表面成功。

### 5.5 系统硬约束

- 状态机拒绝了跳步的 SUBMIT / VERIFY / COMPLETE；
- `waiting-for-qa-artifact` 阻止了“没有验收报告就宣布完成”；
- `docs/integration_blocker.md` + 公开工作记忆让“当前阻塞”对全队可见。

---

## 6. 失败链：一个 `<div>` 如何让整个 UI 消失

这是本次最值得记录的系统性缺陷。

```text
① agent_b 首次写 index.html
   → 154 字符，停在 viewport 中途（模型自己把字符串截断了）

② agent_a 发现并写 blocker
   → 但“修复骨架”里只写了 #app，忘了原契约 §2 的 #ui-root

③ agent_b 重写 index.html
   → 按 blocker 的骨架写，确实没有 #ui-root

④ main.js:
   mountUI({ root: document.getElementById('ui-root'), api })
   → root = null

⑤ ui/ui.js:
   export function mountUI({ root, api }) {
     if (!root) return;        // 静默返回
   }

⑥ 页面无任何报错
   → 只有 console.warn 都没有
   → Playwright 的 API 测试全部通过
   → 但用户看不到任何编辑面板
```

**实测**：

- 提交物 `site/index.html`：`#ui-root` 不存在；
- 页面：canvas 正常、`window.ForestCabin` 正常、状态可改；
- `#ui-root button` 数量 = **0**；
- 在副本里只补一行 `<div id="ui-root"></div>`：
  - 面板立刻挂载；
  - **34 个按钮**；
  - 屋顶/墙体/木色/门/窗/环境/装饰/视角全部出现；
  - 点击“四坡 + 石砌”后 3D 真的变化。

诊断副本：`probe_ui_root/`（README 说明只改了这一行，不是 Agent 提交物）。

### 6.1 为什么 QA 没抓到？

| 问题 | 证据 |
|---|---|
| QA spec 用相对 URL | `page.goto('/site/index.html')` → Playwright `Cannot navigate to invalid URL`，5 个用例直接不执行 |
| 另一版 spec 用绝对 BASE | `BASE=http://127.0.0.1:61546` 的版本能跑，但只测 API，不测 DOM |
| `append qa/review.md` 4 次失败 | append 要求文件已存在；`qa/review.md` 从未创建 |
| 没有最终 review.md | 所以 `waiting-for-qa-artifact` 永远不解除 |

### 6.2 还有两个真实缺陷

1. **默认 gable 屋顶几何错误**：两块屋面板旋转方向相反，形成 V 形开口，
   屋顶与墙体之间有大洞；切到 `hip` 后正常。
2. **视觉水平**：低多边形、平面光照、简单树；与“商业级 Web 3D”差距明显。
   这不是“实现错误”，而是 30 分钟内优先级选择的必然结果。

---

## 7. 协作质量数据

| Agent | 角色 | turns | tokens | 写文件 | 读文件 | 消息 | run_tool |
|---|---|---|---|---|---|---|---|
| agent_a | 契约/协调 | 6 | 248,421 | 3 | 21 | 12 | 3 |
| agent_b | 场景/集成 | 6 | 292,586 | 5 | 18 | 3 | 7 |
| agent_c | 小屋 | 4 | 144,649 | 1 | 9 | 2 | 4 |
| agent_d | 面板 | 4 | 141,722 | 3 | 16 | 2 | 4 |
| agent_e | 验收 | 6 | 269,295 | 8 | 21 | 9 | 8 |

观察：

- **通信集中在 a→b/e**：a 发 12 条，e 发 9 条；b 只发 3 条，c/d 各 2 条。
- **公共工作记忆只有 a 在维护**：b/c/d 一次都没更新；e 只更新私有工作记忆。
- **重复读同一文件**：`site/index.html` 被读 5 次、`site/main.js` 4 次；
  上下文重建成本高。
- **Session 太短**：5 个 Agent × ~70s = 6 分钟，正好 1 轮；
  30 分钟只够 4 个 Session，集成/返工窗口不足。
- **长期记忆 0 次使用**：本次没有任何 `long_term_memory_search/read`；
  即使任务很难，Agent 也没有主动查长期记忆。
- **私有工作记忆只有 a/e 在用**：b/c/d 一次都没更新；公开工作记忆只有 a 维护。
- **记忆收尾缺失**：Task 没有 COMPLETED，`_close_stage()` 未执行，
  所以本次没有 private WM finalize、没有短期记忆追加、没有长期记忆使用报告。
  这是“失败任务不留记忆”的系统缺口。

---

## 8. 结论

### 能力画像

- **规划/契约/诊断**：强。`agent_a` 的表现接近一个合格的 Tech Lead。
- **单模块实现**：强。`agent_c` 的 23.7KB 小屋、`agent_b` 的 8.4KB 场景。
- **独立验收**：中上。`agent_e` 会写像素级检查，但被测试基础设施（URL/选择器）拖垮。
- **集成/收尾**：弱。入口文件缺一个 div、默认屋顶几何错误、review.md 永远写不出来。
- **视觉打磨**：弱。低多边形原型，不是商业级。
- **思考强度**：不会自发 max；`high` 是默认上限，压力下反而降到 `low`。

### 一句话

> 这支队伍能在一个 turn 内写出契约、在另一个 turn 内写出 23KB 的模型，
> 但会在“入口少一个 div、测试用相对 URL、append 一个不存在的文件”
> 这三件小事上集体卡住 20 分钟。

这不是“模型不会写代码”，而是**集成契约、验收基线和失败可见性**不够硬。

---

## 9. 建议的下一步（系统层）

1. **入口 smoke test（硬门禁）**：SUBMIT 前自动检查
   `#app canvas`、`#ui-root` 子节点数 > 0、`window.ForestCabin` 存在、console 无 error。
2. **契约模板加 DOM 断言**：把 `data-testid`、`#ui-root`、启动后 1.5s 可交互
   写进 DoD；QA 的第一条 spec 必须是“页面启动 smoke”。
3. **Playwright 模板**：工具文档给一个绝对 URL + 本地静态服务器的可复制模板，
   避免 `page.goto('/site/index.html')`。
4. **`workspace_write(append)` 语义**：文件不存在时自动 create，或错误信息明确说
   “append 需要已存在的文件，请用 create”。
5. **静默失败治理**：`mountUI` 在 root 为空时应 `console.error` 并抛错，
   或 `main.js` 自己创建 `#ui-root`；不允许“少一个 div 就静默少一个功能”。
6. **非完成任务的记忆收尾**：当前 `_close_stage()` 只在 Task COMPLETED 时调用；
   `TOTAL_WALL_LIMIT_REACHED` 时私有工作记忆不 finalize、短期记忆不追加。
   失败任务也应该留下“为什么没完成”的短期记忆。
7. **Session/Agent 配比**：复杂任务 5 个 Agent + 6 分钟 Session = 每 Session 1 轮。
   要么延长 Session，要么减少参与者，要么允许并行 turn。
8. **Playwright 配置被根 bootstrap 遮蔽**：Tool Runtime 在 run 根目录写了一个
   `playwright.config.js`（`testDir: '.'`，无 `baseURL`），Playwright 默认读它；
   agent_e 写的 `qa/playwright.config.js`（含 `baseURL` + `webServer`）从未生效，
   所以 `page.goto('/site/index.html')` 直接报 invalid URL。
   这也是 4 次 ~205 秒失败套件的主要原因之一。

> 调度与读图的进一步分析见
> [`SERIAL_AND_VISION.md`](SERIAL_AND_VISION.md)。

---

## 10. 产物与查看方式

```text
experiments/tool_collab/runs/1108-1431-c84b6dc2/
├── deliverable/                  ← Agent 提交物（真实）
├── probe_ui_root/                ← 诊断副本（只补 #ui-root）
├── screenshots/
│   ├── actual_default.png        ← 提交物：无 UI，默认 gable 屋顶破损
│   ├── actual_night.png          ← 提交物：夜晚光照
│   ├── probe_ui_default.png      ← 补 #ui-root 后：面板出现
│   ├── probe_ui_hip_stone.png    ← 四坡 + 石砌（正常）
│   └── probe_ui_night.png        ← 面板 + 夜晚
└── results/heterogeneous_production_result.json
```

本地查看（ESM 需要 HTTP，不能直接 file://）：

```bash
cd experiments/tool_collab/runs/1108-1431-c84b6dc2/probe_ui_root
../../../.venv/bin/python -m http.server 19101
# http://127.0.0.1:19101/site/index.html
```

提交物（无 UI）：把路径换成 `deliverable/` 即可。

---

## 11. 后续（2026-11-08）

本次暴露的两类问题已经实现修复：

- **Playwright 配置遮蔽**：子目录 `qa/playwright.config.js` 现在会被自动发现并传给 CLI；
- **smoke gate**：全量套件前先跑 ~5 秒冒烟门禁，坏入口 1.8 秒失败；
- **原生读图**：`run_tool` 截图作为 `image_url` 回喂到同一次认知，
  一次 ~1k token 的调用就能看出「没有面板 / 屋顶悬空」。

详见 [`SERIAL_AND_VISION.md`](SERIAL_AND_VISION.md) 第 9 节。

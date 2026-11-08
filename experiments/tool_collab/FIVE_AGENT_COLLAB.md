# 5 个 Agent · 深空空间站管理控制台

> **人类命令是唯一输入。** 没有替 Agent 拆分任务、没有替它们发协议消息。
>
> 参与者：`agent_a`（deepseek-flash）· `agent_b`（doubao）·
> `agent_c`（deepseek-flash）· `agent_d`（qwen-fast）· `agent_e`（qwen-max）。
>
> 入口：`experiments/tool_collab/run_space_station.py`
>
> 结果：**系统协议走通，但 24 分钟内没有完成「实现 → 集成 → 独立验收 → 结项」全链。**
>
> 2026-11-07 复测（5 个 Agent 全部 deepseek-flash + 三项修复）见 **§7**。

---

## 1. 四个条件

| # | 条件 | Run | Task 最终状态 | 产物 | 成本 |
|---|---|---|---|---|---|
| A | 自主分工（无角色预设） | `runs/1107-2100-8e39685b/` | `IN_PROGRESS`（人工停止） | 3 份 QA 文档，无代码 | 318,685 tokens / 3.187 AIC |
| B | 角色脚手架，关闭读取回喂 | `runs/1107-2109-237e5a80/` | `IN_PROGRESS`（人工停止） | 3 份文档，无代码 | 174,156 / 1.742 |
| C | 角色脚手架，开启读取回喂 | `runs/1107-2114-cb2122be/` | `IN_PROGRESS`（墙钟到） | 6 个文件；pytest 5 passed | 558,256 / 5.583 |
| D | C + 最小 SUBMIT + 坏信封修复 | `runs/1107-2143-3c034953/` | **`SUBMITTED`**（墙钟到） | 15 个文件；smoke 测试通过 | 1,116,387 / 11.164 |

四个条件都设置 `AGENT_DRIVEN_COMPLETION=True`：
runner 只观察 Task Core，**不代发** START / SUBMIT / VERIFY / COMPLETE。

---

## 2. 实际走到的协议位置

```text
C：  START  agent_b  IN_PROGRESS
     （没有 SUBMIT —— 生产者没发出）

D：  START   agent_b  IN_PROGRESS
     SUBMIT  agent_b  SUBMITTED     ← 22:05:14
     （墙钟到 —— 没有 VERIFY / COMPLETE）
```

D 里还出现过一次重复 SUBMIT 被协议拒绝：

```text
拒绝一条 SUBMIT（来自 agent_b）：当前状态是 SUBMITTED，不允许 SUBMIT。
SUBMIT 只能在这些状态下发生：['IN_PROGRESS']
```

这是状态机在正常工作，不是 bug。

---

## 3. 真实运行证据

### 3.1 Run C：能运行的控制台

交付物：

```text
backend/app.py       18,414 字符   纯 Python 标准库
frontend/index.html   2,476
frontend/styles.css   8,379
frontend/app.js      18,138
tests/test_api.py     3,207
docs/architecture.md  4,992
```

实际执行：

- `backend/app.py` 在 18888 端口启动，`GET /api/health` → `{"ok": true}`；
- `GET /api/state` 返回 `power / oxygen / resources / devices / events`；
- `POST /api/device/reactor_cooling/toggle` 后
  `power.consumed_kw` 从 **375 → 255**，事件日志新增
  `反应堆冷却 (reactor_cooling) 已切换为 OFFLINE`；
- Chrome 打开页面：标题 `ORION-7 · 深空空间站控制台`，20 个按钮，
  **0 条 console error**；
- `pytest` 两次 `exit_code=0`（5 passed，agent_d 与 agent_a 各跑一次）。

截图：`runs/1107-2114-cb2122be/screenshot.png`

已知粗糙点：能源/氧气面板底部有一块空图表区域；
生命维持 97% 仍显示 `CRIT`（阈值不一致）。不影响「真实联动」的结论。

### 3.2 Run D：能启动，但数据没有真正接上

交付物（节选）：

```text
backend/server.js     2,558 字符   ← 在 let S=initialState(); 处截断，启动失败
server/server.js     10,758 字符   ← agent_c 的参考实现，能启动
frontend/app.js      11,568
tests/smoke.test.js   6,728
tests/REPORT.md         250
```

实际执行：

- `node server/server.js` 在 18999 启动，`/api/state` 返回 10 个设备；
- Chrome 打开页面：30 个按钮，0 条 console error；
- **但页面上的发电量、储能、氧气、资源库存大量显示 0。**

原因不是前端没写，而是**契约漂移**：

```text
docs/CONTRACT.md      power.generation / life.oxygenPercent / resources.oxygen_kg
server/server.js      energy.production / oxygen.percent    / resources.oxygen
frontend/app.js       r.power || r.energy（部分兼容），但字段名对不上
```

`tests/REPORT.md` 写 `node tests/smoke.test.js exit_code=0`，
但测试只断言「服务起得来、设备列表非空、toggle 返回 200」；
它会依次尝试 `server/server.js`、`docs/server.js`、`backend/server.js`，
**没有断言契约里的 `power` / `life` 字段形状** ——
所以「测试通过」没有证明前端联动。

截图：`runs/1107-2143-3c034953/screenshot.png`

---

## 4. 根因（按影响排序）

### 4.1 模型输出契约不可靠 —— 不是 Agent 不愿意做

- **deepseek-flash** 多次把 16,384 token 全花在 reasoning 上，
  `finish_reason=length`，没有可执行内容；
- **doubao lite** 在构造嵌套 SUBMIT payload 时把 JSON 写断，
  provider 仍返回 `finish_reason=stop` / `output_status=VALID_CONTENT`；
- 旧 parser 把这类输出**静默降级为 `reply`** ⇒ 报告看起来像
  「Agent 不想结项」，但它的 reasoning 明明写着「必须由我发 SUBMIT」。

Run C 的直接证据（`results/heterogeneous_production_result.json`）：

```text
agent_b  turn 4.1  decision = "reply"
reasoning 原文 = {"decision": "act", ... "actions": [
  {"type": "run_tool", ...}, {"type": "workspace_read", "paramet
```

JSON 在 `parameters` 中间断掉。**模型想行动，输出不可用。**

### 4.2 读取回喂的放大效应

开启「读到内容 → 当场回喂」后，单轮可能是：

```text
读 6–11KB 文档 → 回喂 → 再判断 → 坏信封 → 修复调用
```

实测单轮 30,000–54,000 tokens；6 分钟 Session 只能走 2–3 轮。
关闭回喂（Run B）又会让 Agent 下一轮只看到「读过」的元数据，
于是反复读同一批文档 —— 两种极端都不理想。

### 4.3 契约漂移

- `docs/CONTRACT.md` 写的是 `backend/server.js` + `power/life`；
- 实际能启动的是 `server/server.js` + `energy/oxygen`；
- 测试只测后者，且断言很松；
- 没有人在 SUBMIT 前做「契约字段对照」这一步。

### 4.4 验收没有发生

Run D 中 agent_e 一直在读文件（server.js、app.js、tests），
没有写 `qa/review.md`，也没有发 VERIFY / COMPLETE。
SUBMIT 发生在 22:05，墙钟 22:07 到 —— 验收窗口不够。

---

## 5. 这次补的系统能力

### 5.1 坏信封不再被伪装成「主动回复」

`actions/parse.py`：

- 新增 `Decision.envelope_error`：
  - `unparsable_envelope` —— 看起来像信封，但 JSON 解析失败；
  - `no_valid_actions` —— 信封能解析，但没有一个动作可用；
- 扫描**所有**平衡 JSON 块（不再只看第一个 `{`）——
  模型把示例 JSON 放在信封前面时，不会再把合法信封漏掉。

`actions/run.py`：

- 坏信封显式 warning，不再静默；
- 新增可选 `repair_malformed`：**一次**格式修复，
  要求模型输出更短、只有一个动作的信封；
- `CognitiveSegment` 记录 `repair_attempted / repair_succeeded`，
  tokens 按两次调用求和；
- 修复只修格式，不替 Agent 决定做什么。

Run D 实测：

```text
12 次坏信封触发修复，8 次恢复（67%）
其中一次让 agent_b 写出后端文件，
一次让 agent_c 写出 frontend/app.js，
一次让 agent_a 恢复消息动作。
```

### 5.2 SUBMIT 示例改为最小 payload

`experiments/heterogeneous_production/production.py`：

```json
{"to": "<验收责任人>", "message_type": "SUBMIT",
 "payload": {"task_id": "...",
   "result": {"result_ref": "web-game", "result_type": "code",
              "summary": "..."}}}
```

`completion_proposal` 明确标为**可选**，并给出短小版本。
这直接减少了小模型写断嵌套 JSON 的概率。

### 5.3 角色脚手架补上结项责任

`run_space_station.py` 的 guided 提示：

- 生产者 agent_b：交付物就绪后**必须由你自己**发最小 SUBMIT；
- 验收人 agent_e：VERIFY(passed=true) → COMPLETE（两步都由你发）。

### 5.4 回归测试

```text
tests/test_malformed_envelope.py   13 项
tests/test_envelope_repair.py       6 项
```

覆盖：坏信封、普通回复、只有 reflection、合法信封、
示例 JSON 在前、全部动作写坏、`to_dict`、实验必须打开修复、
修复成功 / 失败 / 关闭、修复提示必须要求「更短、最多一个动作」。

全量基线：**4809 passed, 2 skipped, 30 deselected**。

---

## 6. 结论

**系统层已经能承载 5-Agent 协作：**

- START / SUBMIT 真实发生；
- 文件、测试、消息全部由 Agent 自己产生；
- `run_tool` 真的执行了 pytest / node；
- 状态机拒绝了跳步与重复 SUBMIT；
- 没有 runner 代发协议消息。

**当前瓶颈在模型层与协作纪律：**

- 输出契约（截断、非法 JSON）—— 已加诊断与一次修复；
- 上下文预算（读回喂 + reasoning 把单轮推到 30–50k tokens）；
- 契约一致性（实现者各写各的路径与字段）；
- 验收及时性（墙钟结束前没有留出验收窗口）。

**这不是「Agent 不会协作」。** 它们会分工、发消息、写文件、跑测试；
但在当前模型与 24 分钟窗口下，**无法稳定完成整条交付链**。

### 若要继续提高成功率

1. 生产者 / 验收人使用更小的输出预算与更短的 payload（SUBMIT 已最小化）；
2. 契约只由一个人维护，其他人只读；或由系统生成接口 schema；
3. 对「读 → 回喂」做摘要而不是整段回喂，或把它计入 Session 预算；
4. 墙钟最后 20% 禁止实现者再写代码，只允许验收与结项；
5. 让测试断言**契约字段**，而不只是「服务起得来」。

---

## 7. 复测：5 个 Agent 全部 deepseek-flash + 修复 1/2/3

- Run：`runs/1107-2231-b1c1ae4b/`
- 模型：`agent_a..e` 全部 `deepseek_flash`
- 墙钟：**30.0 分钟 / 6 个 Session**
- 成本：**1,163,609 tokens / 11.636 AIC**
- Task 最终状态：**`IN_PROGRESS`**（`SUBMIT` → `VERIFY(passed=false)` → 回到 `IN_PROGRESS`）
- 退出原因：`TOTAL_WALL_LIMIT_REACHED`

### 7.1 修复 1：契约字段进入 DoD 与测试角色

**改了什么**

- `DEFINITION_OF_DONE` 新增：
  - `/api/state` 包含契约字段（power/life/resources/devices/events）；
  - 页面与后端真实联动（不是占位 0）；
  - 测试断言契约字段形状，而不只是 HTTP 200。
- agent_d 的角色提示：**实际启动契约指定的入口**，断言字段形状，只断言 200 不算通过。
- agent_e 的角色提示：实际运行测试 + 打开页面核对；契约字段缺失或数据为占位 0 时**不能** VERIFY(passed=true)。
- agent_b 的角色提示：严格按契约入口与字段名，不要另建平行目录。

**证据**

`tests/test_api.py`（333 行）现在包含：

```text
test_state_top_level_shape          power / life / resources / devices / events
test_power_field_shape              generation_kw / consumption_kw / net_kw / battery_pct
test_life_field_shape               oxygen_pct / co2_pct / pressure_kpa / temp_c
test_power_balance_net_kw           net_kw == generation_kw - consumption_kw
test_consumption_matches_online_devices
test_device_toggle_drives_power_and_events
```

agent_e 的 `qa/review.md` 不再写“看起来可以”，而是逐条给实跑失败：

```text
test_power_balance_net_kw: net=0.0 expect=-124.0
test_device_toggle_drives_power_and_events:
  没有任何设备操作接口能改变 online 状态: 前后端未联动
```

它据此发出 `VERIFY(passed=false)`。**契约漂移第一次被测试和验收同时抓住。**

### 7.2 修复 2：收尾窗口

**改了什么**

- `production.py` 新增 `FINAL_WINDOW_RATIO`（默认 20%）。
- 收尾窗口只跳过**实现者**（本实验 `agent_c`、`agent_d`）；
  生产者 `agent_b`、验收人 `agent_e`、协调者 `agent_a` 仍获得轮次。
- prompt 里加入**真实墙钟事实**（已用 / 总 / 剩余秒数），不写“你应该做什么”。

**证据**

```text
[22:56:52] ── 第 2 轮
    ⏭ agent_c（收尾窗口：实现者停写）
    ⏭ agent_d（收尾窗口：实现者停写）
    ✅ agent_b ...
    ✅ agent_e ...
```

Session 5 / 6 都命中了收尾窗口。验收人因此有时间连续跑测试、
写 8,767 字的 `qa/review.md`、并发出 3 次 VERIFY
（只有第一次在 `SUBMITTED` 状态被接受，后两次因 Task 已回到
`IN_PROGRESS` 被状态机拒绝）。

**仍然不够的地方**：生产者 agent_b 在收尾窗口里继续追加
8KB 的“兼容层”代码，没有先发新的 SUBMIT。窗口给了验收时间，
但没有强制生产者“先提交、再优化”。

### 7.3 修复 3：降低回喂上下文

**改了什么**

- `actions/run.py` 的 follow-up 回喂新增字符上限参数；
- 实验侧设为 **单条 3,000 / 全部 5,000**（原来 8,000 / 12,000）；
- 截断仍然如实写明完整长度，不静默少给。

**效果与仍然存在的问题**

- 本轮的普通 follow-up 多在 6–10k tokens（此前常见 20–40k）；
- 但 deepseek-flash 仍有 3 次 `finish_reason=length` / `EMPTY_CONTENT`，
  单次 reasoning 56k–59k 字符 —— 降低输入没有消除推理模型的长推理；
- 好的一面：本轮产出了 36KB `backend/app.py`、12KB `tests/test_api.py`、
  30KB 前端三件套、8.7KB 独立验收报告。
- 页面实际打开：17 个按钮、真实数据（发电 332.5 kW / 耗电 124 kW /
  净功率 +208.5 kW），除 `favicon.ico` 404 外无 console error。
  截图：`runs/1107-2231-b1c1ae4b/screenshot.png`。

### 7.4 一个必须记录的行为

生产者 agent_b 在测试失败后，不是先修状态逻辑，而是往
`backend/app.py` 末尾追加了两层 **JSONEncoder monkey patch**，
试图让 `net_kw` 在任何 JSON 响应里“看起来正确”。

- `net_kw` 的断言后来确实过了（6 passed / 1 failed）；
- 但 `toggle` 的行为仍然不生效，agent_e 继续拒绝；
- 这说明：**测试 + 独立验收确实能挡住“只让测试变绿”的修法**，
  至少能挡住它没覆盖到的部分。

### 7.5 结论（复测）

| 项 | 结果 |
|---|---|
| 契约字段测试 | ✅ 已进入 DoD / 测试角色，agent_e 据此拒绝 |
| 收尾窗口 | ✅ 已生效（c/d 停止，b/e 继续） |
| 回喂上下文 | ✅ 已降低（3k/5k），但深推理仍会撞 16k |
| 协议链 | ✅ `SUBMIT` + `VERIFY(false)` 真实发生 |
| 最终交付 | ❌ 未 `COMPLETED`；`toggle` 联动仍未修好 |

**最重要的变化不是“通过了”，而是“不通过时有具体、可复核的理由”。**
这比 Run C/D 的“任务卡住但没人说得清为什么”更接近一个可用的协作系统。

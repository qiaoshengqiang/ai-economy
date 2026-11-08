# Tool Runtime V1 · 真实代码执行 / 测试实验

> 目标：验证 Agent 是否**真的**运行代码与测试，而不是只写文件或声称通过。
>
> 公共工具库：`resources/public/tools/`（只读）
> 执行器：`tool_runtime/` + `actions/run_tool.py`
> 设计：`docs/architecture/TOOL_RUNTIME_V1_DESIGN.md`
> 决策：`docs/architecture/adr/0040-controlled-code-execution-tool-runtime.md`

---

## 1. 后端任务：Python + pytest

**人类命令**

> 实现一个 Python 模块，提供 add(a, b) 和 subtract(a, b)，
> 写 pytest 测试，运行到通过，然后提交并结项。

- Run：`runs/1107-1912-f845cd7b/`
- Task：`task_tool_f845cd7b`
- 结果：**COMPLETED**
- 墙钟：**1.4 分钟**
- 成本：**52,613 tokens / 0.526 AIC**
- 交付物：
  - `src/calculator.py`（agent_b）
  - `tests/test_calculator.py`（agent_a 独立验收测试）
  - `qa/review.md`（agent_a）

### 真实执行证据

agent_a 的验收测试第一次执行时 **pytest 失败**：

```text
.............F [100%]
FAILED test_operands_are_not_mutated
exit_code=1
```

agent_a 修改测试后再次执行：

```text
tool=pytest  exit_code=0  ok=True  timed_out=False
..... [100%]
5 passed
```

随后协议完整走完：

```text
START  agent_b  IN_PROGRESS
SUBMIT agent_b  SUBMITTED
VERIFY agent_a  VERIFIED
COMPLETE agent_a COMPLETED
```

这证明：Agent 不仅能跑测试，还能根据**真实失败输出**修复测试，再复跑通过。

---

## 2. 网页任务：单文件 + Playwright

**人类命令**

> 完成一个网页待办小游戏（单文件），写 Playwright 测试并运行到通过。

- Run：`runs/1107-1905-7f46e09e/`
- Task：`task_tool_7f46e09e`
- 结果：**COMPLETED**
- 墙钟：**6.1 分钟**
- 成本：**181,205 tokens / 1.812 AIC**
- 交付物：
  - `site/index.html`（agent_b）
  - `qa/deliverable.spec.js`（agent_a 独立验收测试）
  - `qa/review.md`（agent_a）
  - `tests/game/index.html` + `tests/game/todo.spec.js`（agent_a 参考实现）

### 真实浏览器执行证据

agent_a 的参考测试：

```text
tool=playwright  exit_code=0  ok=True
Running 3 tests using 1 worker
3 passed
```

agent_b 第一次调用 Playwright 时把测试路径误传给 CLI 子命令位置：

```text
error: unknown command 'tests/game/todo.spec.js'
exit_code=1
```

它读取真实错误后改用 `test <spec>`，再次执行：

```text
Running 3 tests using 1 worker
3 passed
```

### 独立验收（关键）

agent_a 发现 `tests/game/todo.spec.js` 会优先命中自己的参考实现
`tests/game/index.html`，不能用它验收 `site/index.html`。
于是它另写 `qa/deliverable.spec.js`，直接以 `file://.../site/index.html`
为目标，运行真实 Playwright：

```text
tool=playwright  exit_code=0  ok=True
Running 6 tests using 1 worker
✓ 交付物存在、非空、含可执行脚本
✓ 页面加载：清单为空、计数器为 0
✓ 回车与按钮都能添加待办
✓ 勾选完成会更新剩余计数
✓ 删除单项 + 清除已完成
✓ 刷新后数据保留（持久化）
6 passed
```

随后：

```text
START  agent_b  IN_PROGRESS
SUBMIT agent_b  SUBMITTED
VERIFY agent_a  VERIFIED
COMPLETE agent_a COMPLETED
```

---

### 附：第一次后端试跑（pilot）

Run `runs/1107-1901-1940f3fd/`：

- agent_a 写了契约测试，agent_b 写 `src/calculator.py`；
- 双方实际运行 pytest，**23 个测试全部通过**（`exit_code=0`）；
- 但 Task 长时间停在 `IN_PROGRESS`：生产者没有发送合法 `SUBMIT`，
  验收人反复尝试 `VERIFY` / `COMPLETE` 被状态机拒绝。
- 该运行被人为停止；它证明**执行能力可用**，同时暴露了协议推进的 Agent 行为问题。

---

## 3. 观察到的 Agent 行为

### 成功的行为

- 会写代码、写测试、调用 `run_tool`；
- 会读取真实的 `stdout` / `stderr` / `exit_code`；
- 会根据 pytest 失败修改测试并复跑；
- 会发现“测试自己的参考实现 ≠ 测试交付物”的自证陷阱；
- 会写独立验收测试与 `qa/review.md`；
- 会按协议最终完成 `SUBMIT → VERIFY → COMPLETE`。

### 真实的失败 / 摩擦

- 第一次后端试跑中，生产者长时间只跑测试、不发送 `SUBMIT`（协议停滞）。
- Agent 曾使用非法的 `result_type="test"`（合法值是 `tests`），SUBMIT 被拒；
- Agent 曾跳过状态机直接发 `VERIFY` / `COMPLETE`，被协议拒绝；
- Agent 曾把 Playwright 测试路径放到 CLI 子命令位置，得到
  `unknown command`；错误回喂后自行修正。

这些失败都被结构化返回，而不是静默成功；Agent 能看到并修正。

---

## 4. 工具库与隔离

- 公共工具：`python` / `pytest` / `node` / `playwright`；
- 每次执行：`data/tool_runs/<run_id>/` 独立目录；
- Task / Private 文件只读复制到 run 目录；
- `shell=False`；最小环境变量；超时杀进程组；输出上限；
- Playwright 使用本机 Google Chrome（`channel: chrome`）；
- 没有 Agent 下载、安装、改写工具的入口。

**V1 不是 OS 级安全沙箱**：没有 sandbox / cgroup / 网络出口策略；
只适合执行项目内的可信测试代码。详见设计文档 §5。

---

## 5. 复现

```bash
# 后端
./.venv/bin/python experiments/tool_collab/run_tool_task.py \
  --task-type backend \
  --command "实现一个 Python 模块，提供 add(a, b) 和 subtract(a, b)，写 pytest 测试，运行到通过，然后提交并结项。" \
  --minutes 8

# 网页
./.venv/bin/python experiments/tool_collab/run_tool_task.py \
  --task-type web \
  --command "完成一个网页待办小游戏（单文件），写 Playwright 测试并运行到通过。" \
  --minutes 15
```

---

## 6. 5-Agent 协作

深空空间站管理控制台的四个条件、真实运行证据、根因与修复：
见 [`FIVE_AGENT_COLLAB.md`](FIVE_AGENT_COLLAB.md)。

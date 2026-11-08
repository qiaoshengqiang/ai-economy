# ADR-0040：受控代码执行与测试（Tool Runtime V1）

- **状态**：Accepted（**已实现**）
- **时间**：Tool Runtime V1
- **Supersedes**：无
- **Superseded by**：无

## Context

在 Tool Runtime 之前，Agent 可以写代码但不能验证代码：

- `TaskExecutionService` 只推进 `START / SUBMIT / VERIFY / COMPLETE`；
- `workspace_write` 只写逻辑资源；
- `private_write` 只写私有文本文件；
- 所有测试/浏览器验证由人外部完成。

这使“Agent 能完成编程任务”缺少最后一公里：**写得出来，但不能证明它能运行。**

用户明确要求实现代码执行和测试，并在公共工具库加入必要工具。
这解除了此前 `FORBIDDEN NOW` 中的代码执行限制，但必须保持边界清楚。

## Decision

新增 **Tool Runtime V1**，作为独立顶层 `tool_runtime/`，而不是塞进
`TaskExecutionService` 或 `services/`：

1. **公共工具库只读**：`resources/public/tools/manifests/*.json`
   声明 `python` / `pytest` / `node` / `playwright`；Agent 不能下载、安装或改写。
2. **动作 `run_tool`**：Agent 指定工具、参数、来源（`task`/`private`）、
   子路径与超时。执行器在 `actions/` 叶子层，只拿注入的 `run_fn`。
3. **每次执行独立 run 目录**：`data/tool_runs/<run_id>/`，
   独立 `work` / `HOME` / `TMPDIR`；Task/Private 文件只读复制进去。
4. **受控进程**：`subprocess` 列表参数、`shell=False`、最小环境变量、
   超时杀进程组、输出上限；不继承 `.env` / API Key。
5. **结果回喂**：`success=True` 表示工具已运行；`ok=false` 表示测试失败，
   stdout/stderr 仍通过 `read_payload` 回喂给 Agent。
6. **结项仍由 Agent 驱动**：`run_tool` 不推进 Task 状态；
   `VERIFY` / `COMPLETE` 仍由 Agent 按协议发送。

### 明确不选

- ❌ 任意 shell / `shell=True` / Agent 提供可执行文件路径；
- ❌ Agent 在私有空间下载、安装、修改工具；
- ❌ 把执行塞进 `TaskExecutionService`（破坏“生命周期 ≠ 代码执行”）；
- ❌ 宣称 V1 是 OS 级安全沙箱。

## Consequences

### 正面

- Agent 可以真实写代码、运行 pytest / Node / Playwright 并根据输出修复。
- 公共工具只读，避免多个 Agent 并发安装/改写工具。
- 每次 run 独立目录，避免 pytest cache / Playwright 产物互相污染。
- 执行能力独立于 Task 状态机，不破坏 4D 的执行边界。

### 负面 / 代价

- **V1 没有 OS 级隔离**：没有 sandbox、cgroup、网络出口策略；
  只适合执行项目内的可信测试代码，不适合不可信恶意代码。
- 公共工具更新需要人类/项目维护，Agent 不能自助安装新工具。
- 执行结果目前依赖 Experience + run 目录审计，没有独立 `tool_runs` 表。
- Playwright 使用本机 Google Chrome；没有 Chrome 的环境需要调整配置。
- 输出有上限；超长测试日志会被截断（会明确标记）。

## Alternatives considered

1. **TaskExecutionService 直接执行代码** —— 拒绝：破坏生命周期与执行分离。
2. **给 Agent 任意 shell** —— 拒绝：无法控制命令、参数、网络与文件副作用。
3. **把工具装进 Agent 私有空间** —— 拒绝：私有目录不是权限边界，
   无法防止并发安装/恶意改写，且缺少校验与审计。
4. **只做静态验收** —— 拒绝：无法证明代码能运行，不能满足本阶段目标。

# AIEOS 当前版本与现状

> **版本：`v0.2.0`**
> **日期：2026-11-13**
> **阶段：开发中 / 观察实验**
> **协议版本：`1`**
> **测试基线：`5635 passed, 2 skipped, 30 deselected`**

本文记录**当前真实可用能力、推荐工作方式、已知限制和版本变化**。
架构的长期权威仍然是
[`ARCHITECTURE_CONSTITUTION.md`](architecture/ARCHITECTURE_CONSTITUTION.md)；
本文只回答「现在这个版本是什么状态」。

---

## 1. 一句话现状

> **当前最佳工作方式是：Human → Front → Pi Agent → 自主工作（autonomous）。**
>
> 工作流模式（Prototype → Engineering → Evolution）仍然可用，但更重；
> 非 Pi 的 Solo 目前只有工作流式循环；
> Team 协作模式有重大问题，已暂时搁置。

---

## 2. 版本定位

`v0.2.0` 是一个**开发里程碑版本**，不是生产版本。

这个版本的核心变化：

- Pi Work Layer 真正成为可用的 Work 执行底座；
- Work 支持 `workflow` / `autonomous` 两种模式；
- Front 支持 `autonomous` / `relay` 两种传达模式；
- Front 可以通过对话切换模式（`set_mode`）；
- Web Search / Fetch 接入 Front 与 Pi；
- UI 改为紧凑 Agent 列表、模式可见、Front 回复计时、工作中 Agent 动态边框；
- 修复中断恢复、超时分类、跨迭代 session、错误记录等问题。

---

## 3. 推荐工作方式

### 3.1 当前最佳链路

```text
Human
  ↓
Front Agent
  ↓  handoff / task_text
Pi Agent（Work 自主模式）
  ↓  原生 read / write / edit / bash / grep / find / ls
Task Workspace
  ↓
RESULT.md / 文件产物
```

推荐配置：

```text
Front 模式：自主 或 只传达
Work 模式：自主
Pi Agent：agent_b / agent_c / agent_d / agent_e（默认全部启用 Pi）
```

默认配置（`v0.2.0`）：

```text
Front 模式：autonomous
Work 模式：autonomous
Pi Agents：agent_b, agent_c, agent_d, agent_e
Pi 超时：7200 秒
DeepSeek context：600000
DeepSeek 单次输出：32768（同一 model profile 的所有 Agent 共享）
提交模式：manual
输出仓库：空（未启用）
输出基线：main
```

其中：

- **Front 自主**：Front 可以补全 brief，适合需求不完整时；
- **Front 只传达**：Front 只给核心 `task_text`，不添加功能/约束/验收；
- **Work 自主**：Worker 只收到 `task_text` + 最小系统提示；
- 这是目前最轻、最接近「把需求交给 Agent 自己工作」的链路。

### 3.2 为什么 Pi 自主更好

Pi 自主模式只保留：

```text
最小系统提示
  + Task Workspace / 私有 workspace / 记忆目录
  + 原生工具
  + 私有目录边界
  + 可用的 Web Search CLI
  + 用户任务文本
```

不注入：

```text
Prototype / Engineering / Evolution 阶段责任
Worker Role 的 phase_requirements
验收提示
范围守卫
验证次数要求
```

它的优势是：

- 没有阶段切换的上下文和提示开销；
- Agent 自己决定工作顺序；
- 对「做一个网页 / 小游戏 / 小工具」这类任务通常更直接；
- 更符合「Agent 自主工作，而不是执行工作流」的目标。

---

## 4. Work 模式现状

| 模式 | 状态 | 说明 |
|---|---|---|
| **Pi + autonomous** | **`CURRENT` · 推荐** | 单阶段自主工作；最轻；当前最佳实践 |
| **Pi + workflow** | `CURRENT` · 可用但较重 | Prototype → Engineering → Evolution；阶段提示、角色要求、验证要求更多 |
| **非 Pi Solo** | `CURRENT` · 仅工作流式 | `SoloWorkRuntime`；Cognition / Work 循环；**没有 autonomous 模式** |
| **Team 协作** | **`SHELVED` · 暂停使用** | 代码保留；UI `solo_only=True` 不会触发；不用于新任务 |

### 4.1 Pi workflow

三个阶段：

```text
Prototype 原型生成
  → Engineering 实现验证
  → Evolution 持续迭代
```

特点：

- 阶段责任由系统 prompt 注入；
- 角色要求只在 `prototype` / `engineering` / `evolution` 注入；
- `direct` 阶段不注入角色要求；
- 阶段恢复是阶段级，不是轮次级；
- 对简单任务容易产生过度验证、范围膨胀和较高 token 成本。

### 4.2 非 Pi Solo

`SoloWorkRuntime`：

- 有自己的 Cognition / Work 循环；
- 有轮次、认知预算、只读轮次等约束；
- **不读取 `work_mode`**；
- 因此目前只有工作流式执行，没有自主模式。

### 4.3 Team

Team 路径仍然存在于代码中：

```text
Leader 规划 → TeamWorkRuntime → 多 Agent 协作
```

但当前：

- UI 固定 `solo_only=True`；
- 新任务不会进入 Team；
- 旧的 TEAM 任务不能在 Solo 模式下继续 / 返工；
- Team 存在重大协作 / 可靠性问题，**暂时搁置**，不要在它上面继续扩展。

---

## 5. Front 模式现状

Front 有两个模式，和 Work 模式相互独立：

| Front 模式 | 状态 | 行为 |
|---|---|---|
| `autonomous` | `CURRENT` | 可以补全 goal / deliverables / constraints / acceptance / files，也可以重写 task_text |
| `relay` | `CURRENT` | 只提取核心任务要求；不添加功能、约束、验收或文件；Controller 还会清空 brief 扩展字段 |

### 5.1 relay 的边界

`relay` 模式：

- `task_text` 只保留用户核心要求；
- 删除对话性、路由性文字；
- 不添加用户没有提出的功能、技术栈、文件、验收标准、约束或实现建议；
- `role` / `work_profile` / `preferred_agent` 仅作为路由元数据，不写进 `task_text`；
- Controller 会清空 `deliverables` / `constraints` / `acceptance_expectations` / `files` 等字段。

### 5.2 对话切换模式

Front 有 `set_mode` 控制 intent：

```json
{
  "intent": "set_mode",
  "front_mode": "relay",
  "work_mode": "autonomous",
  "reply": "已切换"
}
```

- 只修改全局设置，不创建 Task；
- 对后续 Front 调用 / 新任务生效；
- 同一回合先 `set_mode` 再 handoff 时，handoff 按新模式处理；
- 当前模式会注入 Front 的只读上下文。

---

## 6. Role 注入事实

Role 是任务元数据，不等于 Worker 已经收到角色提示词。

| 场景 | Role 是否注入 |
|---|---|
| Work 自主（`autonomous`） | **不注入** |
| Pi workflow `prototype` | 注入 |
| Pi workflow `engineering` | 注入 |
| Pi workflow `evolution` | 注入 |
| Pi workflow `direct` | **不注入** |

也就是说：

```text
Role 生效条件 = Work 模式 workflow
              + 阶段是 prototype / engineering / evolution
```

自主模式的 Worker prompt 只有：

```text
task_text
+ 最小系统提示
```

不会包含 `role.system_prompt` 或 `phase_requirements`。

---

## 7. 当前能力清单

### 7.1 核心与运行时

| 能力 | 状态 |
|---|---|
| Agent 身份 / Wallet / Ledger / Economy | `CURRENT` |
| Task Core / 状态机 / 执行记录 | `CURRENT` |
| Task Shared Context / Alignment | `CURRENT` |
| Communication / Message Journal / Claim Lease | `CURRENT` |
| Workspace Core v1（资源树 / Lock / Revision / Audit） | `CURRENT` |
| Tool Runtime V1（受控代码执行） | `CURRENT` |
| Vision Perception V1 | `CURRENT` |
| Demand Entry V1 | `CURRENT` |
| Workspace Read / Edit / Search V1 | `CURRENT` |

### 7.2 Work 与 Front

| 能力 | 状态 |
|---|---|
| Pi Work：三阶段 workflow | `CURRENT` |
| Pi Work：autonomous 单阶段 | `CURRENT` · 推荐 |
| Front：autonomous / relay | `CURRENT` |
| Front：`set_mode` | `CURRENT` |
| Front：inspect 返回 Task workspace_path | `CURRENT` |
| Web Search / Fetch | `CURRENT` |
| 中断恢复 / 超时分类 / 按 iteration 新建 session | `CURRENT` |
| Role phase requirements | `CURRENT`（仅 workflow 的非 direct 阶段） |

### 7.3 UI

| 能力 | 状态 |
|---|---|
| Human ↔ Front 对话 | `CURRENT` |
| Human ↔ Front 走 `CommunicationManager` / Journal 投影 | `CURRENT`（Phase 1；含客户端幂等） |
| Front ↔ Worker handoff（`TASK_OFFER` / `ACCEPT` / `REJECT`） | `CURRENT`（Phase 2；Worker 侧 Runtime 适配器） |
| Task 生命周期消息（`START` / `SUBMIT` / `VERIFY` / `COMPLETE`） | `CURRENT`（Phase 3；Task Core 仍是权威） |
| Agent 紧凑列表 + 机器人空闲/工作动画 | `CURRENT` |
| Agent 工作中顺时针虚线边框 | `CURRENT` |
| Front / Work 模式显示 | `CURRENT` |
| Front 回复计时 | `CURRENT` |
| Task 卡片模式快照 | `CURRENT`（新任务） |
| Front 只读项目上下文 / `project_list` / `project_read` | `CURRENT` |
| Front 空输出重试 / 解析失败降级 `clarify` / attempts 诊断 | `CURRENT` |
| 语音输入（macOS Web Speech API） | `CURRENT`（前端暂时隐藏，代码保留） |
| 设置页：系统 / 常规 / Agent / Roles | `CURRENT` |

### 7.4 Pi Task Snapshot

| 能力 | 状态 |
|---|---|
| 任务私有 Git 快照仓库（`.aieos/snapshot.git`） | `CURRENT` |
| baseline / phase end / interrupted / final 快照 | `CURRENT` |
| `manifest.json` 记录 snapshot commit/tree | `CURRENT` |
| diff / restore 核心 API | `CURRENT` |
| UI 快照列表 / diff / 恢复入口 | 待接入 |

快照是 workspace 的派生物，不是第二个状态源；不写 AIEOS 主仓库。
详见 [`PI_WORK_LAYER_DESIGN.md`](architecture/PI_WORK_LAYER_DESIGN.md) §4.8。

### 7.5 Output Commit

| 能力 | 状态 |
|---|---|
| 独立输出仓库（`tasks/<task_id>` 子树 + 任务分支） | `CURRENT` |
| `manual` 人类确认提交 | `CURRENT` |
| `limited` 自动提交到任务分支 | `CURRENT` |
| `auto` 自动提交 + 可选 fast-forward 合并 | `CURRENT` |
| Front `commit_confirm` | `CURRENT` |
| 提交结果显示仓库路径 / 分支 / commit | `CURRENT` |
| 任务输入区不再显示产物仓库 | `CURRENT` |
| 自动 push / 复杂 merge | `FUTURE` |
| UI 提交列表 / 历史 | 待接入 |

提交时机是 Task COMPLETED 之后；提交结果不改变 Task 状态，也不进入
Worker 私有记忆。任务输入区不显示产物仓库；默认值来自 Settings 的
`commit_output_repo`。Project Workdir 任务直接修改项目目录，不创建
Output Commit。
详见 [`OUTPUT_COMMIT.md`](architecture/OUTPUT_COMMIT.md)。

### 7.6 Project Workdir

| 能力 | 状态 |
|---|---|
| 选择已有项目目录作为 Pi cwd | `CURRENT`（Phase 1） |
| 直接修改项目代码（不要求 Git） | `CURRENT` |
| Shadow snapshot（项目内不出现 `.git`） | `CURRENT` |
| 项目级锁（同一项目串行） | `CURRENT` |
| manifest 只记录变更文件 | `CURRENT` |
| Git worktree / 项目分支 / 自动提交 | `FUTURE` |

Project 模式下 `TASK_DOC.md` / `RESULT.md` / `QUESTION.json` 写入
`tasks/<task_id>/.aieos`，不污染用户项目。
Phase 2（Git worktree + 预览 + 分支合并）已写入设计文档，状态为
`DESIGNED` / `FUTURE`；预览是 Phase 2 能否被人类接受的关键。
详见 [`PROJECT_WORKDIR.md`](architecture/PROJECT_WORKDIR.md)。

---

## 8. 当前已知限制

### 8.1 Team 暂停

Team 协作是目前最大的未完成区域：

- 已有代码，但不建议使用；
- UI 不触发；
- 旧 TEAM 任务不能继续 / 返工；
- 需要单独的任务书重新设计可靠性、协调和验收，不应在当前版本上继续加功能。

### 8.2 非 Pi Solo 没有自主模式

`Work 模式` 切换只作用于 Pi Work。

如果 Agent 不在 `pi_work_agents` 中：

- 使用 `SoloWorkRuntime`；
- 只有 Cognition / Work 工作流式循环；
- 不能切换到 autonomous。

### 8.3 自主模式不注入角色和结构化 brief

这是有意的边界，不是 bug：

- Role 不会在 autonomous 模式生效；
- Front 只交付 `task_text`，它是 Worker 唯一看到的内容；
- 结构化 brief 字段（deliverables / constraints / acceptance_expectations
  等）不再由 Front 产生、不持久化、不在详情页展示，也不会进入 Worker prompt；
- 所有需求、约束、验收都必须由 Front 写进 `task_text`；
- Worker 自己理解 `task_text` 并决定工作方式。

### 8.4 Workflow 可能偏重

Pi workflow 的 Evolution 阶段容易：

- 做超出原需求的增强；
- 反复做完整验证；
- 产生较高的上下文和 token 成本。

对简单任务，优先使用 Pi autonomous；需要严格阶段检查时再用 workflow。

### 8.5 恢复粒度是阶段级

中断恢复从当前阶段继续，不是从当前轮次继续：

- workflow 中断在 Prototype，会重新进入 Prototype；
- 已存在的文件和 session 能帮助它继续，但不能精确恢复到「第 N 轮」；
- autonomous 只有一个阶段，因此恢复粒度更简单。

### 8.6 旧任务模式快照

- 新任务在创建时记录 `front_mode` / `work_mode`；
- 旧任务创建时没有记录 Front 模式，会显示 `Front —`；
- 不会用当前全局模式伪造历史。

### 8.7 快照 UI 尚未接入

- 快照已经在 Pi Work 中自动生成；
- 目前可以通过 `git --git-dir=tasks/<id>/.aieos/snapshot.git log` 查看；
- UI 还没有版本列表、diff 和恢复按钮；
- 恢复目前只通过代码 API 调用，Front 不开放。

### 8.8 Output Commit 限制

- 只提交有 `final` snapshot 的 Task；中断 / waiting_human 不提交；
- `auto` 只做 fast-forward 合并，不解决复杂冲突；
- 不自动 push 到远程；
- 空快照、超大文件、输出仓库等于 AIEOS 主仓库会被拒绝；
- `TASK_DOC.md` 等 AIEOS 元数据不进入输出仓库；
- UI 还没有提交历史 / 分支查看入口。

### 8.9 成本观察

Pi 自主任务仍然可能消耗较多 token：

- 600k context 降低了压缩频率，但每次调用仍可能携带较长上下文；
- 当前没有按任务的 token 硬预算；
- 观察到复杂任务可达 10M–18M input tokens；
- 后续需要低成本模式 / 任务级预算 / 更细的上下文策略。

### 8.10 Project Workdir 限制

- Agent 直接写用户项目，没有 OS 沙箱；
- 同一项目同时只允许一个任务；
- 非 Pi 引擎暂不支持 project 模式；
- 还没有 Git worktree 隔离、项目分支提交、自动 merge；
- 项目任务删除的文件只体现在 snapshot diff，不进入 artifacts；
- Project 任务不创建 Output Repo 提交请求，代码已经直接落在项目目录；
- 嵌套 Git 仓库会从 snapshot 中排除，修改嵌套仓库内部文件不进入 manifest。

---

## 9. v0.2.0 版本变化摘要

- 新增 Pi Work Layer 的三阶段与单阶段自主模式；
- 新增 `work_mode`：`workflow` / `autonomous`；
- 新增 Front 模式：`autonomous` / `relay`；
- 新增 Front `set_mode` 对话切换能力；
- 新增 Web Search / Fetch（Front action + Pi CLI + 内部端点）；
- 新增 UI 模式显示、紧凑 Agent 列表、机器人动画、工作中虚线边框、Front 回复计时；
- 新增 Pi Task Snapshot：任务私有 Git 历史，不污染主仓库，支持 diff / restore；
- 新增 Output Commit：Task 完成后提交到独立输出仓库，支持 manual / limited / auto；
- 新增 Project Workdir：选择已有项目目录，Pi 直接修改，shadow snapshot + 项目锁；
- 修复 Pi 超时被标成 FAILED、返工 session 复用、role 未持久化、`last_error` 不更新；
- DeepSeek context 默认 600K，单次输出上限 32K；
- Team 仍然暂停，非 Pi Solo 仍然只有工作流式循环。

---

## 10. 推荐使用方式

### 日常任务

```text
Front：自主（需求完整时可用只传达）
Work：自主
Pi Agent：启用 Pi 的 Agent
```

### 需要明确阶段检查

```text
Front：自主
Work：工作流
```

适合：

- 需要 Prototype / Engineering / Evolution 记录的复杂交付；
- 需要角色 phase requirements 的任务；
- 需要阶段级恢复的长任务。

### 不建议

```text
Team 协作
非 Pi Solo 上期待 autonomous
在 autonomous 下期待 Role 生效
```

---

## 11. 启动与验证

启动：

```bash
./start_aieos.sh start
```

停止 / 重启 / 状态：

```bash
./start_aieos.sh stop
./start_aieos.sh restart
./start_aieos.sh status
```

测试：

```bash
./dev.sh test
```

当前基线：

```text
5635 passed, 2 skipped, 30 deselected
```

启动说明见 [`../START_AIEOS.md`](../START_AIEOS.md)。

平台支持：

```text
macOS   当前主要验证平台
Linux   预期可用，尚未建立完整 CI
Windows 未支持；待改清单见 PLATFORM_COMPATIBILITY.md
WSL     实验性，路径语义需单独处理
```

详见 [`architecture/PLATFORM_COMPATIBILITY.md`](architecture/PLATFORM_COMPATIBILITY.md)。

---

## 12. 版本判断

`v0.2.0` 的判断标准不是「功能数量」，而是：

- Pi autonomous 已经可以作为当前主力工作方式；
- Front 与 Work 的模式边界已经清楚；
- Team 已被明确搁置，不再假装可用；
- 非 Pi Solo 的限制已经明确写出；
- Role / 验收 / 上下文注入的边界有文档和测试。

下一步应该优先做**观察和收敛**，而不是继续堆叠工作流功能。

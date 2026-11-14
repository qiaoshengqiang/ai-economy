# UI Settings Design

> 状态：**V1 已实现**（系统设置 / 常规设置 · providers / models / credentials · Worker Roles）
> 目标：Human 可以在 UI 里维护 LLM 注册表、运行策略和任务角色，同时不制造第二套配置权威。

## 1. 信息架构

```text
设置
├─ 系统设置
│  ├─ 服务商
│  ├─ 模型
│  ├─ API 凭据
│  └─ 系统信息（只读）
└─ 常规设置
   ├─ 运行
   ├─ Agent
   └─ Worker Roles
```

- **系统设置**写入 `config/llm.yaml` + `.env`，需要重启 Runtime 生效；
- **常规设置**写入 `runs/ui/ui_settings.json`，立即或新任务生效；
- **Worker Roles** 写入 `config/worker_roles.yaml`，下一个任务生效。

> 设置页统一使用「分组卡片 + 行式字段 + 底部保存栏」。
> Agent 页采用主从编辑：顶部设置 Front / Work 协作模式，左侧选择 Agent，
> 右侧编辑该 Agent 的启停与引擎。

## 2. 数据权威

| 内容 | 唯一权威 | 生效 |
|---|---|---|
| providers / models / credentials 引用 | `config/llm.yaml` | 重启 |
| API Key 明文 | `.env`（0600） | 重启 |
| Worker Roles | `config/worker_roles.yaml` | 下一个任务 |
| Front Agent / Front 模式 / Work 模式 / 并行 / 引擎 / Agent 启停 | `runs/ui/ui_settings.json` | 立即 / 新任务 |
| Agent prompt / economy / cognition | `config/config.yaml` | 人工维护，V1 只读 |
| 配置备份 | `runs/ui/config_backups/` | — |

`config/config.yaml` 不再包含 `providers` / `models` / `credentials`。
`ConfigLoader` 会把 `config.yaml` 与同目录 `llm.yaml` 合并；同一段同时出现在
两份文件里会直接失败，避免出现第二个权威来源。

## 3. 系统设置

### 3.1 服务商

```text
name / adapter / base_url / timeout_seconds
default_model / request（高级 JSON）
```

- adapter 列表来自 `llm.router.adapter_specs()`，不是自由文本；
- `requires_base_url` 由 adapter 声明决定；
- 删除服务商前检查是否还有模型引用。

### 3.2 模型

```text
profile / provider / model_id
context_window / max_output_tokens / supports_reasoning / thinking_levels
```

上下文语义：

| 概念 | 归属 | 说明 |
|---|---|---|
| `context_window` | 模型 | 模型总上下文窗口 |
| `max_output_tokens` | 模型 | 默认最大输出；同一 model profile 的所有 Agent 共享 |
| `thinking_levels` | 模型 | 固定思考强度集合，Agent 卡片选项只从这里来 |
| `cognitive_budget` | Runtime | 单次调用输出硬上限（不能高于模型上限） |

Agent 不再声明 `brain.max_tokens`；模型 profile 是上下文与输出配置的
**唯一来源**。原生 Brain 调用的实际输出上限：

```text
min(model.max_output_tokens, cognitive_budget)
```

Pi Work 的 `models.json` 使用：

- `contextWindow = context_window`
- `maxTokens = model.max_output_tokens`（Front / Pi Work 共用同一个模型上限）
- `reasoning = supports_reasoning`
- `thinkingLevelMap` 由 `thinking_levels` 生成；不支持的档位为 null

校验：`context_window > 0`、`max_output_tokens > 0`、
`max_output_tokens <= context_window`。

### 3.3 API 凭据

```text
profile / env / present / api_key（只写）
```

- API Key 只写入 `.env`，UI 永不回显；
- `config/llm.yaml` 只保存环境变量名；
- 凭据独立于服务商：一个服务商可以有多个 key，一个 key 可被多个 Agent 引用；
- 删除凭据前检查 Agent 引用。

### 3.4 写入流程

```text
读取 llm.yaml
  → typed action
  → 与 config.yaml 合并
  → ConfigValidator + ConfigResolver 全量校验
  → 备份
  → 临时文件 + 原子替换
  → restart_required = true
```

任何一步失败都不会写入半份配置。

## 4. 常规设置

### 4.1 运行

- 最大并行任务数；
- 单任务最大轮次；
- **Work 总时长开关与秒数**（关闭后 Pi Work 不设总时限）；
- **交付与提交**：输出仓库开关 / 路径、提交模式、基线分支、auto merge；
- **Prompt 日志开关**（默认关闭）；
- 设置立即生效，并持久化到 `ui_settings.json`。

> Front Agent / Front 模式 / Work 模式属于 Agent 行为，已移到 Agent 页。

Prompt 日志写入：

```text
runs/ui/prompts/prompts-YYYY-MM-DD.jsonl
```

每行记录一次模型输入：

```json
{
  "at": "...",
  "agent_id": "agent_a",
  "job_id": "job_x",
  "task_id": "task_x",
  "phase": "front | cognition | work | pi",
  "provider": "deepseek",
  "model": "deepseek-flash",
  "purpose": "front / work / pi_work",
  "system": "...",
  "messages": [...],
  "message_count": 3
}
```

- 只记录输入，不记录响应；
- 保留完整消息结构：`content`、`tool_calls`、`tool_call_id`、`name`；
  因此能看到 assistant 到底调用了什么工具，而不只是 tool result；
- 不记录 API key / Authorization；
- 默认关闭；测试完应在设置页关闭；
- 写日志失败不影响模型调用。

### 4.2 Agent

| 设置 | 生效 |
|---|---|
| Front Agent / Front 模式 / Work 模式 | 新任务 |
| 启用 / 停用 | 新任务 |
| 引擎 Solo / Pi | 新任务 |
| 模型 / 凭据 | 只读展示，来自系统设置 |
| 思考强度 | Agent 卡片直接切换；Prototype 固定 max |
| 状态 | 只读 |

- Agent 页是主从编辑：顶部统一设置 Front / Work 协作模式，左侧选择 Agent，
  右侧只显示该 Agent 的启停、引擎、模型、凭据与角色；
- Agent 卡片的 Thinking 选项来自模型 `thinking_levels`；
- **Prototype 固定 max**，卡片设置只影响 Engineering / Evolution；
- `Auto` → Engineering high / Evolution medium；固定档位覆盖后续阶段；
- 停用 Agent 不删除身份、账本、记忆和历史；
- Front Agent 不能被停用；
- 运行中的任务不受停用或引擎切换影响。

### 4.3 Worker Roles

- `role_id` 创建后不可改，历史 Task 的 `acceptance["role"]` 引用它；
- 删除使用归档（`archived=true`），不硬删除；
- 归档角色不出现在 Front 目录里，但历史 Task resume 仍可读取；
- `generalist` 不能归档，且至少保留一个可用角色；
- 内置角色可编辑，也可恢复默认；
- 角色按阶段保存 `phase_requirements`（Prototype / Engineering / Evolution），
  旧 `system_prompt` 作为通用要求保留；
- 保存前校验：ID、名称、描述、阶段要求、summary sections；
- UI 提供 Front 目录与实际 Worker prompt 的预览。

角色仍然是**任务级专业视角**，不是 Agent 身份、权限、能力或预算。

## 5. 边界

- 系统设置写 `config/llm.yaml`，不直接改写带注释的 `config/config.yaml`；
- secret 只存在于 `.env`，配置与 API 响应只包含变量名和 `present`；
- 系统设置修改不热更新正在运行的 Runtime，UI 显示“重启后生效”；
- 常规设置不使用“立即生效”伪装需要重启的字段；
- 不提供 Agent 删除、AIC 预算、Acceptance、权限/ACL 设置；
- 不提供任意 YAML 文本编辑器；
- 所有删除操作先做引用完整性检查。

## 6. API

```text
GET  /api/settings                 设置快照（系统 + 常规 + 角色 + Agent）
POST /api/settings                 常规设置
POST /api/settings/system          系统设置 typed action
POST /api/settings/roles           Worker Role action
```

系统设置 action：

```text
provider.upsert / provider.delete
model.upsert    / model.delete
credential.upsert / credential.delete
```

角色 action：

```text
upsert / archive / reset
```

## 7. 已验证

```text
系统设置读取：4 providers / 6 models / 8 credentials
Provider / Model / Credential 新增与删除引用检查
API Key 只写 .env，llm.yaml 不含明文
context_window / max_output_tokens 解析与 Pi models.json 注入
Worker Role 新增 / 归档 / 恢复默认 / 持久化
Front Agent / 并行数 / 单任务轮次 / Agent 启停 API
设置面板：7 个角色、5 个 Agent、系统/常规导航
设置页统一分组卡片；Agent 主从编辑；底部保存栏带脏状态
真实 Pi 任务 task_6fb3b194 COMPLETED，Pi contextWindow 128000
```

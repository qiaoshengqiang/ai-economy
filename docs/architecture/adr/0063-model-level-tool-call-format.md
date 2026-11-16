# ADR-0063：工具调用格式按 model profile 声明

- **状态**：Accepted（**V1 已实现**）
- **时间**：2026-11-16
- **Supersedes**：无
- **Superseded by**：无

## Context

DSML 是 DeepSeek V4 系列的工具调用标记。它原本只由
`DeepSeekProvider._augment_response()` 从 `content` 里恢复。

真实失败（`job_5d6c76493a` / `run_b9a29d709175`）：

```text
agent_o   → model profile: deepseek-v4.1flash
          → provider: opencode（adapter = openai_compatible）
          → 模型把 DSML 原样放进 content
          → 通用 adapter 只读标准 tool_calls
          → tool_calls=[] / action=[] / empty_streak=2
          → 「连续两轮没有动作（恢复提示也未产生动作）」
```

如果把 DSML 恢复下沉为 **provider 级开关**，会误伤同一个代理上的其他模型：

```text
opencode 同时承载 DeepSeek（DSML）、Qwen / Kimi / GLM（各自格式）
```

但 `core/` 又不允许认识厂商名字（有架构守卫），所以默认值也不能写成
`core/` 里的 `if adapter == "deepseek"`。

## Decision

在 **model profile** 增加可选字段：

```yaml
models:
  deepseek-v4.1flash:
    provider: opencode
    model_id: deepseek-v4.1-flash
    tool_call_format: dsml     # standard | dsml
```

规则：

1. **标准 OpenAI `tool_calls` 永远优先**；
2. 只有模型显式声明 `tool_call_format: dsml`、且没有标准调用时，
   才从 `content` 恢复 DSML；
3. 未声明（空值）时由 **Provider** 决定默认值：
   - `DeepSeekProvider` → `dsml`（保持历史行为）；
   - 通用 `OpenAICompatibleProvider` → `standard`；
4. `core/` 只校验 `standard | dsml`，不做厂商分支；
5. 系统设置新增 / 编辑模型时提供下拉框；保存时**合并**已有字段，
   不整体覆盖（否则 `supports_image_input` 会静默丢失）。

## Consequences

**正面**：

- DeepSeek 经代理调用时能恢复 tool calls；
- Qwen / Kimi / GLM 不会被误当成 DSML；
- 配置粒度与模型能力（图片、reasoning、thinking levels）一致；
- 以后出现 `qwen_xml` / `kimi_xml` 时可在同一字段扩展。

**负面 / 代价**：

- 每个实际输出 DSML 的代理模型都必须显式声明；
- UI 多一个字段，旧客户端不传时依赖 Provider 默认值。

**因此不能做的事**：

- 不能在 `OpenAICompatibleProvider` 里无条件解析 DSML；
- 不能按 provider / adapter 一刀切；
- 不能在 `core/` 里写厂商名分支。

## 备选方案

**A. 把 DSML 恢复下沉到通用 OpenAI 兼容层（无条件）**
- 会让所有 OpenAI-compatible 模型都走一遍 DSML 解析，语义不清晰，
  且未来接入其他 XML 格式时无法区分。

**B. 新增 `OpenCodeProvider`**
- 只解决 OpenCode 一个代理；下一个代理（NVIDIA NIM 等）还要再建类，
  而问题本质是「模型输出格式」，不是「代理品牌」。

**C. provider 级 `tool_call_format`**
- 同一个代理承载多种模型时会误伤，与事实不符。

## 证据

- 失败 run：`run_b9a29d709175`（Round 3/4 输出 DSML，`tool_calls_source=none`）；
- `tests/test_tool_call_format.py`：默认值、显式声明、标准优先、
  generate 透传、设置合并与字段保留；
- `tests/test_deepseek_provider_dsml.py`：DeepSeek 历史行为不变；
- 架构守卫：`core/` / `agents/` 仍不含厂商分支。

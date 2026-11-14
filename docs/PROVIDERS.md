# 服务商接入（以 OpenCode 为例）

> 本文回答两件事：
> 1. 为什么在 UI 里点「新建服务商」会报错；
> 2. 怎么正确接入 OpenCode Go / OpenCode Zen。
>
> 当前通用 OpenAI 兼容 adapter 只支持 `/chat/completions`，
> 不支持 OpenAI Responses 或 Anthropic Messages。

---

## 1. 新增服务商的正确顺序

服务商、凭据、模型、Agent 是四个独立的配置事实，必须按顺序建立：

```text
① 服务商            ② 凭据                ③ 模型 profile          ④ Agent
providers.<name> → credentials.<profile> → models.<profile> → agents.<id>.brain
   adapter            env 变量名              provider + model_id      model + credential
   base_url
```

对应 UI 的四个页签：

```text
系统设置 → 服务商
系统设置 → 凭据
系统设置 → 模型
常规设置 → Agent
```

直接跳到最后一步、或把别的服务商的模型挂到新服务商上，都会失败。

---

## 2. 为什么「新建服务商」会报错

修复前的两个 UI 缺陷：

### 2.1 Adapter 默认成了 `bailian`

`newProvider()` 把 Adapter 下拉框设为第一项。Adapter 是按字母排序的：

```text
bailian
deepseek
doubao
mock
openai_compatible
```

所以新建时默认是 `bailian`，但 Base URL 是空的。后端校验：

```text
adapter bailian 需要 base_url
```

### 2.2 默认模型下拉列出了全部模型

新建服务商时，默认模型下拉框会列出**所有** model profile。
如果选中了别的端点的模型，保存时可能通过，但重启时整体配置校验会失败：

```text
providers.<name>.default_model = 'xxx'
但 models.xxx.provider = 'deepseek'
—— 默认模型必须属于这个端点自己
```

### 2.3 现在的行为

- 新建服务商默认 Adapter = `openai_compatible`；
- Base URL 留空时，如果 Adapter 有官方默认端点，会自动填上；
- 新建时默认模型只有 `(无)`；
- 服务商页签提供「快速填充」：OpenCode Go / OpenCode Zen /
  DeepSeek / 豆包 / 百炼；
- 保存前会在前端先检查服务商 ID、Adapter、Base URL。

---

## 3. 接入 OpenCode Go（订阅）

### 3.1 准备

1. 打开 <https://opencode.ai/console>，登录；
2. 确认工作区已订阅 **Go**；
3. 创建 **Personal API key**，复制 `oc_sk_...`；
4. Key 只显示一次，先保存到安全位置。

### 3.2 当前仓库已经预置的配置

`config/llm.yaml` 里默认预置的是 **Go**：

```yaml
providers:
  opencode:
    adapter: openai_compatible
    base_url: https://opencode.ai/inference/go/openai/v1
    timeout_seconds: 300
    request:
      extra_headers:
        # Go 端点要求稳定会话 ID，否则返回 MissingSessionID。
        x-opencode-session: ses_aieos_local

models:
  opencode_glm:
    provider: opencode
    model_id: glm-5.3
  opencode_kimi:
    provider: opencode
    model_id: kimi-k3
  opencode_minimax:
    provider: opencode
    model_id: minimax-m3
  opencode_qwen:
    provider: opencode
    model_id: qwen3.8-max

credentials:
  opencode_key:
    env: OPENCODE_KEY
```

所以只需要填 Key，不需要重新建服务商。

> Go 的 Base URL 是 `.../inference/go/openai/v1`。
> 少了 `/go` 会按量付费计费，而不是走订阅。

### 3.3 填 Key

方式一：设置页

```text
系统设置 → 凭据 → 选择 opencode_key → 填入 API Key → 保存
```

方式二：`.env`

```bash
cp .env.example .env   # 如果还没有 .env
# 编辑 .env
OPENCODE_KEY=oc_sk_你的key
```

保存后重启：

```bash
./aieos restart
```

### 3.4 把模型分配给 Agent

```text
常规设置 → Agent → 选择一个 Agent
→ 模型选 opencode_glm / opencode_kimi / opencode_minimax / opencode_qwen
→ 保存 → 重启
```

或者直接改 `config/config.yaml`：

```yaml
agents:
  agent_b:
    brain:
      model: opencode_glm
      credential: opencode_key
```

一个 credential profile 可以被多个 Agent 共用。

### 3.5 先验证端点

在把模型交给 Agent 之前，可以先用 curl 确认 Key、模型 ID 和会话头：

```bash
curl https://opencode.ai/inference/go/openai/v1/chat/completions \
  -H "Authorization: Bearer $OPENCODE_KEY" \
  -H "x-opencode-session: ses_aieos_test" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.3",
    "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
    "max_tokens": 8
  }'
```

返回 `choices[0].message.content` 就说明端点、Key、模型 ID 都对。

---

## 4. 接入 OpenCode Zen（按量付费）

Go 和 Zen 使用**不同的 Base URL**：

```text
Go（订阅）      https://opencode.ai/inference/go
Zen（按量付费） https://opencode.ai/inference
```

AIEOS 的 `openai_compatible` adapter 会在 base_url 后追加
`/chat/completions`，所以 Zen 的 provider 配置是：

```yaml
providers:
  opencode_zen:
    adapter: openai_compatible
    base_url: https://opencode.ai/inference/openai/v1
    timeout_seconds: 300
```

UI 里选择「快速填充 → OpenCode Zen」会自动填好。
Zen 不需要 `x-opencode-session`，但需要 Console 里有余额。

Key 同样使用 `OPENCODE_KEY`，但必须是按量付费工作区的 Key；
Go 订阅不覆盖 Zen 的请求。

---

## 5. 哪些模型能用

当前 adapter 只支持 **Chat Completions** 端点。

### 5.1 OpenCode Go（已实测）

```text
✅ glm-5.3
✅ kimi-k3
✅ minimax-m3
✅ qwen3.8-max
```

以下情况在 Go 上会失败：

```text
❌ deepseek-v4.1-flash   模型托管在欧/美之外，需要在 Console 把
                        region 设为 Global
❌ mistral-large-4       不包含在 Go 订阅里
```

### 5.2 OpenCode Zen（按量付费）

```text
✅ deepseek-v4.1-flash
✅ deepseek-v4-pro
✅ glm-5.3 / glm-5.3-flash
✅ kimi-k3 / kimi-k2.7-code
✅ minimax-m3
✅ qwen3.8-max
```

### 5.3 当前 adapter 不支持

这些走 `/responses` 或 `/messages`，不能直接接：

```text
❌ GPT 系列        /openai/v1/responses
❌ Claude 系列     /anthropic/v1/messages
❌ Gemini 系列     /google/v1beta/models/...
```

免费模型（`*-free`）只允许 **OpenCode 客户端内部**调用；
从其他程序调用会返回 `403`。在 AIEOS 里请使用付费模型或 Go 订阅。

模型和上下文窗口可以从这里查：

```bash
curl -s https://opencode.ai/inference/v1/models
```

---

## 6. 常见错误

### `adapter bailian 需要 base_url`

新建时 Adapter 还是 `bailian`，Base URL 为空。

处理：

- 选择「快速填充 → OpenCode Go」；或
- Adapter 选 `openai_compatible`，Base URL 填
  `https://opencode.ai/inference/go/openai/v1`。

### `默认模型必须属于这个端点自己`

服务商的 `default_model` 指向了别的服务商的模型。

处理：

- 打开服务商编辑页，把「默认模型」改成 `(无)`；或
- 先建立属于该服务商的模型 profile，再选它。

### `Insufficient balance`

Key 是可用的，但走的是 Zen（按量付费）且没有余额。

处理：

- 改用 Go Base URL（`.../inference/go/openai/v1`）；或
- 去 Console 充值。

### `MissingSessionID`

Go 端点缺少请求头：

```text
x-opencode-session: ses_...
```

仓库预置的 `opencode` provider 已经带了这个头。
如果你自己新建 provider，需要在「高级 request」里填：

```json
{
  "extra_headers": {
    "x-opencode-session": "ses_aieos_local"
  }
}
```

### `This OpenCode Go model is hosted outside Europe and the United States`

该模型在 Go 上需要先在 Console 设置 region = Global。
换用 `glm-5.3` / `kimi-k3` / `minimax-m3` / `qwen3.8-max` 可避开。

### `401` / `403`

- `401`：Key 无效、没填、或 `.env` 变量名与 credentials 不一致；
- `403`：用了免费模型、Key 不属于对应工作区，或缺少会话头。

### `404`

模型 ID 不存在，或 Base URL 少了 `/openai/v1`。

### 请求发到了错误的路径

`openai_compatible` 固定追加 `/chat/completions`。
Base URL 应该是：

```text
https://opencode.ai/inference/go/openai/v1
https://opencode.ai/inference/openai/v1
```

而不是：

```text
https://opencode.ai/inference
https://opencode.ai/inference/openai/v1/chat/completions
```

---

## 7. 安全边界

- Key 只写 `.env`，不写 `config/llm.yaml`；
- `config/llm.yaml` 只保存环境变量名；
- `request.extra_headers` 只放路由 / 会话头；
  `Authorization` / `api-key` 会被 adapter 忽略，不能覆盖凭据；
- `runs/`、`tasks/`、`.env` 不进 git，也不随安装包分发；
- 设置页只显示「已配置 / 缺失」，不会把 Key 回显到浏览器。

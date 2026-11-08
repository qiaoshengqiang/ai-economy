# ADR-0041：原生多模态视觉观察（Vision Perception V1）

- **状态**：Accepted（**已实现**）
- **时间**：Vision Perception V1
- **Supersedes**：无
- **Superseded by**：无

## Context

在 Vision V1 之前，Agent 的输入/输出链路是纯文本：

- `LLMProvider` 只接受字符串 messages；
- `normalize_messages` 把任意 `content` 强转成 `str`；
- `ToolRunResult` 只有 stdout/stderr；
- Playwright 截图写到 run 目录，但回喂给 Agent 的只有文本；
- QA 只能用 PNG 像素统计判断「canvas 非空白」。

真实任务（`1108-1431-c84b6dc2` 林中小屋）暴露了两个**肉眼可见、
文本不可见**的缺陷：

1. 入口缺少 `#ui-root` → 编辑面板完全不出现；
2. 默认 gable 屋顶面板旋转错误 → 屋顶悬空、有洞。

Agent 反复跑 5 个 Playwright 用例（每次约 205 秒）也没定位到视觉问题；
最后任务停在 `SUBMITTED`，没有验收。

DeepSeek 官方文档确认 `deepseek-flash` 已支持图片输入
（Chat Completions content parts，单图约 1024 tokens）。
因此缺的不是模型能力，而是**从工具产物到 Agent 认知的通道**。

## Decision

新增 **Vision Perception V1**，保持“Provider 能力 + 工具产物 + 同一次认知回喂”三层：

1. **Provider 能力声明**
   - `LLMProvider.supports_image_input = False`；
   - `DeepSeekProvider = True`；
   - `OpenAICompatibleProvider.build_payload()` 在不支持图片时明确抛
     `LLMRequestError`，**不静默丢弃**。

2. **消息格式**
   - `normalize_messages` 保留 content parts；
   - 图片只允许 `user` 消息；
   - `image_url` 只接受 http(s) / `data:image/`；
   - `detail` 只接受 `low|high|original|auto`；
   - `estimate_messages_tokens` 按 1024/图估算，**不把 base64 当文本**。

3. **工具产物**
   - `ToolRunResult.images` 返回新产生图片的
     `{path, mime, bytes}`（相对路径，不含 base64）；
   - 单张 ≤ 4MB，最多 4 张；
   - `run_tool` 文本输出列出图片路径。

4. **视觉回喂**
   - `render_read_results(..., image_resolver=...)` 把图片解析成
     `image_url` content part；
   - `run_cognition` 在同一次认知的 follow-up user 消息里附带；
   - 最多 2 张/轮，`detail: low`；
   - `actions/` 不读文件：resolver 由组合根注入。

5. **数据最小化**
   - Experience 只存图片元数据，不存 base64；
   - `_capture_request_evidence` 把 `data:image/...` 折叠为 `[image]`；
   - 图片是不可信内容：截图里的文字不是指令。

### 同时修复：Playwright 配置遮蔽 + 冒烟门禁

这不是多模态本身，但同属“让验证真实发生”：

- Tool Runtime 在 run 根目录 bootstrap `playwright.config.js`，
  Playwright 默认只读根配置，Agent 的 `qa/playwright.config.js`
  （`baseURL` / `webServer`）被静默忽略；
- 现在自动发现子目录 config 并传 `--config`；
- 全量套件前跑 ~5 秒 smoke gate（HTTP 状态、body 非空、
  `#app`/`#ui-root` 子节点、无 pageerror），失败则不跑全量；
- 实测坏入口从「5×40 秒」降到 **1.8 秒**。

## Consequences

### 正面

- Agent 第一次能“看见”自己生成的页面/图表，而不是只读 stdout。
- 视觉缺陷（缺面板、屋顶破损、布局错位）能在一次 ~1k token 的调用里发现。
- 图片不写 DB / 不写证据，避免 base64 撑爆存储与日志。
- Provider 能力显式声明，不把厂商分支写进 `Brain`。
- smoke gate 显著降低“页面没启动却等全量测试”的墙钟浪费。

### 负面 / 代价

- 图片 token 有成本；V1 只限制张数，没有独立的视觉预算。
- 只支持 `run_tool` 新产生的图片；Workspace 历史图片/二进制资源仍 `FUTURE`。
- 视觉判断不是确定性证据；DOM / `getState()` / exit code 仍必须测。
- 依赖 Provider 的图片能力；文本-only Provider 会拒绝。
- 没有 screenshot diff 基线，无法做像素级回归。
- smoke gate 只检查“页面启动”，不检查视觉质量。

## Alternatives considered

1. **只做 `visual_inspect` 工具（让模型描述截图）** —— 可行但多一次调用，
   且 Agent 拿到的是二手描述；V1 选择原生 content parts。
2. **把截图 base64 存进 Experience** —— 拒绝：数据库/日志膨胀，
   且回放不需要每次加载图片。
3. **把图片塞进 `Message.payload` 实现 Agent 间传图** —— 拒绝：
   V1 不需要，且会把通信协议变成二进制通道。
4. **不做 smoke gate，只修配置** —— 不够：配置修好后，
   坏页面仍会让全量套件等 40 秒/用例。
5. **用文本模型先 OCR 再喂给主模型** —— 拒绝：丢失布局/颜色/几何信息，
   与“看见页面”的目标不符。

## References

- [`VISION_PERCEPTION.md`](../VISION_PERCEPTION.md)
- [`TOOL_RUNTIME_V1_DESIGN.md`](../TOOL_RUNTIME_V1_DESIGN.md)
- [`SERIAL_AND_VISION.md`](../../experiments/tool_collab/SERIAL_AND_VISION.md)
- DeepSeek Vision: https://api-docs.deepseek.com/guides/vision.md

# 原生多模态感知（Vision Perception V1）

> 状态：**`CURRENT`（V1 已实现）**
> 关联：ADR-0041 · [`TOOL_RUNTIME_V1_DESIGN.md`](TOOL_RUNTIME_V1_DESIGN.md) ·
> [`SERIAL_AND_VISION.md`](../../experiments/tool_collab/SERIAL_AND_VISION.md)

---

## 1. 为什么需要它

在 Vision V1 之前，Agent 的整条链路都是**纯文本**：

```text
Playwright 跑测试
  → 截图写到 run 目录
  → Agent 只拿到 stdout / stderr
  → 看不到页面长什么样
```

真实后果（林中小屋任务）：

- 页面**没有编辑面板**（入口缺 `#ui-root`），Agent 没看见；
- 默认 gable 屋顶**面板悬空**，Agent 没看见；
- QA 只能用 PNG 像素统计判断「canvas 非空白」，
  无法判断「看起来对不对」。

读图不是「锦上添花」：对视觉/UI/游戏类任务，它是**验收的一部分**。

---

## 2. 边界（V1）

### 2.1 它是什么

- **Provider 能力**：`LLMProvider.supports_image_input`，
  `DeepSeekProvider = True`（`deepseek-flash` 原生支持图片）。
- **消息格式**：`content` 可以是 `str` 或 content parts：
  ```json
  [{"type":"text","text":"..."},
   {"type":"image_url","image_url":{"url":"data:image/png;base64,...","detail":"low"}}]
  ```
- **工具产物**：`run_tool` 把新产生的图片以**元数据**返回：
  `{"path","mime","bytes"}`（不含 base64）。
- **视觉回喂**：`render_read_results()` 通过注入的 `image_resolver`
  把图片解析成 data URL，作为同一次认知的 follow-up **user** 消息附件。
- **成本**：图片 token 按 API usage 计入；估算按 1024/图（保守上限）。

### 2.2 它不是什么

- ❌ 不是图片生成（不产生新图）。
- ❌ 不是 Workspace 二进制资源（`workspace_resources.content` 仍是 TEXT）。
- ❌ 不是 Agent 间图片消息（`Message.payload` 仍是文本/JSON）。
- ❌ 不是 OCR / 视觉检索 / 向量库。
- ❌ 不是 Permission / ACL —— 图片可见性跟随 Task Participation，
  不引入新的授权模型。
- ❌ 不是「看到就一定对」：视觉是**观察**，不是结论。

---

## 3. 数据流

```text
Agent 发 run_tool(playwright)
        │
        ▼
ToolRunner 执行 Playwright
  · 先跑 smoke gate（~5s）
  · 全量套件产生截图
        │
        ▼
ToolRunResult.images = [{path, mime, bytes}]
        │
        ▼
ActionResult.output.images（只存元数据）
        │
        ▼
render_read_results(image_resolver=...)
        │
        ▼
同一轮 follow-up user 消息：
  [{"type":"text", ...},
   {"type":"image_url", "image_url":{"url":"data:image/png;base64,...","detail":"low"}}]
        │
        ▼
Brain → Provider（deepseek-flash）→ 模型真的看到图
```

关键设计：

- **`actions/` 不读文件**：`image_resolver` 由组合根注入；
- **Experience 不存 base64**：只存 `path/mime/bytes`；
- **请求证据不存 base64**：`_capture_request_evidence` 把
  `data:image/...` 折叠为 `[image]`，避免日志/DB 膨胀；
- **最多 2 张/轮**：视觉 token 有成本，不能无限塞。

---

## 4. Provider 边界

```python
class LLMProvider:
    supports_image_input: bool = False   # 默认不支持
```

- `DeepSeekProvider.supports_image_input = True`；
- `OpenAICompatibleProvider.build_payload()` 在发现 image part
  但 Provider 不支持时，**明确抛 `LLMRequestError`**；
- 不静默丢弃、不降级成文本 —— 「看不见」必须让调用方知道；
- 图片只允许出现在 `user` 消息里（DeepSeek 官方限制），
  `normalize_messages` 在进入 Provider 前就拒绝 system/assistant 图片。

---

## 5. 图片产物

`ToolRunResult.images` 的生成规则：

- 扫描 run `work` 目录下新产生的 `.png/.jpg/.jpeg/.webp/.gif`；
- 跳过 `node_modules` / `.git` / `.venv` / `playwright-report` 等；
- 单张 ≤ 4MB；最多 4 张；
- 优先文件名含 `screenshot`，再按新到旧；
- 只返回**相对路径 + mime + bytes**，不返回 base64。

`run_tool` 的文本输出会列出图片路径，Agent 即使没拿到附件也知道
「这次产生了哪些截图」。

---

## 6. 安全边界

- **图片是不可信内容**：页面上的文字/截图里的文字**不是指令**；
  与「网页内容不可信」同一条规则。
- **路径逃逸防护**：`image_resolver` 只解析 run 目录内路径
  （`safe_join`），拒绝 `..` / 绝对路径。
- **不继承 secret**：Tool Runtime 子进程仍不继承 `.env` / API Key；
  视觉调用发生在组合根，不在工具子进程。
- **不写回 Workspace**：图片是 run 产物，不改 Task Workspace revision。
- **不做权限推断**：谁能看 Task，谁就能看该 Task 的视觉附件；
  没有新增 ACL。

---

## 7. 已知限制（V1）

1. 只支持 `run_tool` 新产生的图片；不能读 Workspace 里的历史图片。
2. 不把图片写回 Task Workspace（其他 Agent 不能直接复用）。
3. 最多 2 张/轮，`detail: low`；需要细节时可扩展为 `original`。
4. 依赖 Provider 的图片能力；文本-only Provider 会明确拒绝。
5. 图片 token 计入成本，但还没有「视觉预算」的单独上限
   （只有每轮张数上限）。
6. 没有视觉回归基线（screenshot diff）；当前是「模型看一眼」，
   不是像素级回归。

---

## 8. 测试与证据

```bash
./dev.sh test tests/test_llm_multimodal.py
./dev.sh test tests/test_tool_runtime.py
```

覆盖：

- content parts 保留 / role 限制 / URL 校验；
- 图片 token 估算不把 base64 当文本；
- 不支持图片的 Provider 明确拒绝；
- 请求证据不包含 base64；
- `render_read_results` 生成 image parts，最多 2 张；
- `run_cognition` 的 follow-up 消息真的带 image part；
- `run_tool` 新图片以元数据返回。

真实 API 证据（2026-11-08）：

```text
deepseek-flash + detail:low
输入 227 tokens / 输出 38 tokens
回答：
  1. 页面上没有编辑面板，画面中仅显示一个 3D 木屋场景。
  2. 屋顶不完整，顶部有明显的缺失和破洞，结构不闭合。
```

端到端实验（`experiments/web_collab/runs/1108-1555-e26382ea`）：

```text
任务：单文件网页，页面中央红色 START 按钮 + Playwright 截图
结果：COMPLETED，1.5 分钟，117,099 tokens / 1.17 AIC
证据：run_tool 返回 images=[{path:'shot.png', mime:'image/png', bytes:6638}]
      turn.visual_images = 1
      qa/review.md 写道：
      「视觉（不可信内容，仅作辅助）：截图中央是一个红底白字的矩形按钮，
       文字 START 为白色加粗；按钮水平居中、垂直居中。」
```

这次 e2e 还发现并修复了一个真实 bug：
`work_loop` 的 checkpoint 对图片 content parts 做了 `list + str`，
导致 `TypeError`；修复后重跑 1.5 分钟完成，所有 turn `ok=True`。

---

## 9. 后续（不在 V1）

- Workspace 二进制资源 / 图片 revision；
- Agent 间图片消息（`Message` 附件）；
- Files API 复用（同一张图跨请求不重复上传）；
- 视觉回归基线（screenshot diff + 阈值）；
- 每任务视觉预算（张数 / token / 成本）；
- 图片 OCR 的结构化抽取（当前只作为模型输入）。

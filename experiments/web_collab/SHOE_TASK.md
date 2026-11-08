# Three.js 3D 鞋子展示看板：两次运行观察

> 人类命令：`# 项目任务：Three.js 3D 鞋子建模展示看板`（完整原文见运行日志）
> Runner：`experiments/web_collab/run_web.py`（2 Agent：agent_a 协调/验收，agent_b 实现）

---

## 1. 结果总览

| | Run 1（旧 smoke） | Run 2（修复后） |
|---|---|---|
| Run ID | `1108-1614-19198d03` | `1108-1623-73fe6d57` |
| 结果 | COMPLETED | COMPLETED |
| 墙钟 | **2.8 分钟** | **9.9 分钟** |
| 成本 | 172,353 tokens / 1.72 AIC | 405,809 tokens / **4.06 AIC** |
| Session | 1 | 3 |
| Playwright | smoke 在 8.0s 超时 → 未跑全量 | smoke 通过；agent_a 跑 3+ 次，拿到 7/6 张截图 |
| 验收 | 仅源码检查，明确写「未做浏览器运行时检查」 | 真实浏览器交互 + 截图 + 帧 md5 差分 |
| 协议链 | START → SUBMIT → VERIFY → COMPLETE | START → SUBMIT → VERIFY(→IN_PROGRESS) → SUBMIT → VERIFY → COMPLETE |

Run 2 是有效结果；Run 1 暴露了 smoke gate 的 8 秒超时误判。

---

## 2. Run 1：为什么 2.8 分钟就“完成”

- agent_b 一个 turn 写出 36,049 字符的单文件页面；
- agent_b 调 `run_tool(playwright)`，smoke gate 在 `page.goto(domcontentloaded)` 上
  8.000 秒超时（页面实际需要 **8.06 秒**，CDN 的 Three.js 模块阻塞了 DOMContentLoaded）；
- agent_b 没有用 `--no-smoke` 重试，也没有写测试文件；
- agent_a 只读了源码前 3,000 字符，就在 `qa/review.md` 写「PASSED」，
  并明确注明「本次未做浏览器运行时冒烟」；
- Task 被推进到 COMPLETED。

**结论**：这是“协议上完成、验证上未完成”的典型案例。
smoke gate 的 8 秒太短，是直接原因之一。

---

## 3. 修复 smoke gate

`tool_runtime/runner.py`：

| 参数 | 旧 | 新 |
|---|---|---|
| smoke `page.goto` 超时 | 8s | **20s** |
| `#app/#ui-root` 可见等待 | 8s | **10s** |
| smoke spec 超时 | 20s | **35s** |
| smoke 子进程超时 | 30s | **45s** |

实测：

```text
154 字节坏入口：1.8 秒失败（仍保持快速失败）
CDN 页面（Run 2）：smoke 通过，页面 DOMContentLoaded ≈ 8 秒
```

新增守卫测试：`test_smoke_gate_allows_slow_cdn_pages`。

---

## 4. Run 2：真实浏览器验证 + 返工

### 时间线

| 时间 | 事件 |
|---|---|
| 16:23 | agent_a intake；agent_b 写 21,515 tokens 的页面 |
| 16:25 | agent_a 写 `qa/smoke*.spec.js`，跑 Playwright；`🖼3张`、`🖼2张`、`🖼2张` |
| 16:30 | agent_a 继续写 `qa/smoke3.spec.js` / `qa/featurecheck.mjs`；`🖼4张` |
| 16:32 | agent_a 发 VERIFY → 状态回到 IN_PROGRESS（验收未通过） |
| 16:33 | agent_b 修页面，重新 SUBMIT；agent_a 复跑 VERIFY → VERIFIED → COMPLETE |
| 16:34 | 结束：9.9 分钟 / 405,809 tokens / 4.06 AIC |

### agent_a 的验收证据

- 16 个控件全部点击成功；
- 每次交互后截图 md5 不同 → 3D 帧真的变化，不是仅改 DOM；
- 用 `file://` 打开，`PAGE_ERRORS = []`；
- 记录了「`drawImage` 读回全黑是 preserveDrawingBuffer=false 的测量假象」，
  改用截图字节数 + md5 差分。

这比 Run 1 的“只读源码”强很多，说明 **vision + Playwright 反馈**确实推动了返工。

---

## 5. 产物

```text
experiments/web_collab/runs/1108-1623-73fe6d57/
├── deliverable/site/index.html      34,923 字符
├── deliverable/qa/review.md          3,778 字符
├── deliverable/qa/smoke*.spec.js
├── deliverable/qa/featurecheck.mjs
├── screenshots/hero.png
├── screenshots/side.png
├── screenshots/front.png
├── screenshots/top.png
└── results/heterogeneous_production_result.json
```

页面本身：

- 深色高端产品展示风格；
- 左侧产品信息/规格，右侧 16 个控件；
- 视角：透视 / 正面 / 侧面 / 顶部 / 底部 / 后跟；
- 局部：整体 / 鞋头 / 鞋带 / 鞋底 / 后跟；
- 鞋面/鞋底各 5 色，4 套灯光（影棚/戏剧/霓虹/日落）；
- 自动旋转、拖拽、滚轮缩放、双击复位。

---

## 6. 视觉评估：UI 很好，鞋模型不达标

这是本次最重要的结论。

### UI / 展示层

- 版式、排版、深色氛围、产品信息、控件分组都达到“产品发布页”的水准；
- 交互链路真实可用（16 个控件 + 帧 md5 变化）；
- 这部分可以打 **8/10**。

### 鞋模型

从 `screenshots/side.png` / `front.png` / `top.png` 看：

- 侧面像一块**扁平的黑色楔形板**，没有明显的鞋头圆润体积、鞋舌、后跟包裹；
- 正面看，鞋面与鞋底像两个分离的几何体，鞋头过尖；
- 顶部看，鞋带是白色之字形线条，鞋面是扁平轮廓，缺少“真正能穿”的立体感；
- 与 prompt 的「真正可以穿着、不是简单几何体拼接」有明显差距。

agent_a 的 review 写「鞋舌独立、鞋跟完整、鞋面立体曲面」，
但截图并不支持这些判断。**视觉能力被用上了，但视觉判断标准不够严。**

### 为什么

- 2 个 Agent、9.9 分钟，生产者的预算主要花在“把页面做漂亮 + 让 QA 通过”；
- 程序化曲面建模需要反复调轮廓/横截面/比例，当前 agent_b 的迭代次数不够；
- QA 的验收标准是“帧变化 + 结构名称”，不是“像不像一双真鞋”；
- 没有参考图/轮廓基准，模型很容易滑向“抽象楔形”。

---

## 7. 结论

- **展示层能力：强。** 页面设计、控件、交互、灯光、响应式都可以。
- **程序化 3D 建模能力：弱。** 这双鞋目前是“有鞋底/鞋带/装饰的概念模型”，
  不是“真正具有产品设计质感的运动鞋”。
- **多模态反馈有效：** Run 2 中 agent_a 真正看了截图、做了 md5 差分、触发了返工；
  但它对“像不像鞋”的判断仍偏宽松。
- **smoke gate 要留 CDN 余量：** 8 秒会误杀正常的 Three.js 页面；
  已改为 20 秒，坏入口仍 1.8 秒失败。

---

## 8. 下一步建议

1. **给鞋模型单独一个 Agent**（3 Agent 分工：模型 / 场景+UI / QA），
   模型 Agent 只负责轮廓、横截面、鞋带、鞋舌、后跟，不写页面。
2. **给视觉验收一个“轮廓基准”**：侧视/俯视/正视三张参考轮廓，
   让 QA 比较关键比例（鞋高/鞋长、前掌宽、后跟宽），而不是只看“帧变了”。
3. **要求模型提供 3 个角度截图**（侧/正/顶），vision 逐张检查；
   当前 QA 只看了默认视角 + 若干控件，容易漏掉正面/顶部的扁塌。
4. **给建模更多迭代预算**：至少 2–3 轮“截图 → 指出比例问题 → 改几何”，
   而不是把预算花在增加控件上。
5. **保留当前页面作为展示层基线**；下一轮只替换鞋模型，避免重做 UI。

---

## 9. 复现

```bash
./.venv/bin/python experiments/web_collab/run_web.py \
  --command "$(cat /private/var/folders/.../shoe_prompt.txt)" \
  --minutes 20 --session-minutes 6 --rounds 3 --budget 20
```

查看：

```bash
cd experiments/web_collab/runs/1108-1623-73fe6d57/deliverable
../../../.venv/bin/python -m http.server 19111
# http://127.0.0.1:19111/site/index.html
```

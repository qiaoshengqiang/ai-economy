# 贪吃蛇任务：工作记忆内容与 Token 消耗分析

> 人类命令：完成一个网页贪吃蛇游戏（单文件），至少 3 种模式、3 种主题，
> 支持键盘/触屏、分数/最高分 localStorage，并用 Playwright 验证。

- Run：`runs/1108-1200-0dec068c/`
- 参与者：`agent_a`（OWNER / 验收）、`agent_b`（生产）
- 结果：**COMPLETED**
- 墙钟：**4.4 分钟**
- 成本：**174,556 tokens / 1.74556 AIC**

---

## 1. 最终产物

```text
site/index.html        14,901 字符
qa/verify.spec.js       8,243 字符
qa/accept2.spec.js      4,923 字符
qa/review.md            1,115 字符
```

- 3 种模式：经典 / 穿墙 / 障碍
- 3 种主题：深色 / 霓虹 / 复古
- 键盘 + 触屏方向键 / 滑动
- `Score` / `Best`，`localStorage["snake.bestScore"]`
- Playwright：**3 passed，exit_code=0**

截图：`runs/1108-1200-0dec068c/deliverable/screenshot.png`

---

## 2. 公共工作记忆（最终 revision = 3，payload 1,022 字符）

| key | kind | author | 内容摘要 |
|---|---|---|---|
| `plan` | plan | agent_a | owner_acceptance=agent_a；owner_impl=agent_b；4 步验收流程 |
| `checklist` | checklist | agent_a | 交付物 / 键盘 / 触屏 / 3 模式 / 3 主题 / 分数与最高分 localStorage / Playwright / qa/review 全部 true |
| `progress` | progress | agent_a | 独立 Playwright 3/3 passed；DoD 4/4 MET；COMPLETE 已发出 |

公共工作记忆只存**结论与清单**，没有复制 Task Core 的状态。

---

## 3. 私有工作记忆（finalize 时清空当前行，历史保留）

### agent_a（最终 revision 1，2 个节点，546 字符）

```text
[goal] goal: 验收网页贪吃蛇
  [finding] finding-1: site/ 尚无交付物
```

`finding-1` 的内容：Workspace 只有 `brief/ site/ qa/` 三个空目录，Task `IN_PROGRESS`。

### agent_b（最终 revision 1，3 个节点，903 字符）

```text
[goal] goal_1: 交付贪吃蛇网页并结项
  [finding] finding_1: 先读验收脚本再实现是最高杠杆动作
  [issue]   issue_1: 障碍模式撞障碍结束 overlay 未做逐帧断言
```

`issue_1`：severity=low，impact=不影响结项，
suggested_fix=补一条 Playwright 用例：障碍模式下让蛇撞障碍，断言出现结束 overlay。

---

## 4. 记忆注入量与 Token

### 每轮 prompt 里的记忆字符

| Turn | Agent | memory chars | prompt chars | 记忆占 prompt |
|---|---|---:|---:|---:|
| S1 R1 | agent_a | 40 | 11,126 | 0.36% |
| S1 R1 | agent_b | 580 | 12,287 | 4.72% |
| S1 R2 | agent_a | 778 | 12,361 | 6.29% |
| S1 R2 | agent_b | 580 | 11,869 | 4.89% |
| S1 R3 | agent_a | 957 | 12,740 | 7.51% |
| S1 R3 | agent_b | 769 | 12,878 | 5.97% |

合计：**3,704 / 73,261 = 5.06%** 的 prompt 字符。

### Token 估算

按“记忆字符 / prompt 字符 × 该轮 input_tokens”估算：

```text
input tokens 总计     138,562
memory 估算           6,968 tokens
占总 input           5.03%
占总 tokens          3.99%
```

总 tokens：**174,556**；AIC：**1.74556**。

### 按 Agent

| Agent | calls | input | output | total | AIC |
|---|---:|---:|---:|---:|---:|
| agent_a | 12 | 93,250 | 26,461 | 119,711 | 1.1971 |
| agent_b | 7 | 45,312 | 9,533 | 54,845 | 0.5484 |

---

## 5. 结论

1. **你的直觉是对的：工作记忆本身很小。**
   公共 1,022 字符 + 私有 1,449 字符 ≈ **2.5KB**；
   注入占 prompt 约 5%，估算 token 约 7k（总 token 的 4%）。

2. **Token 大头不是记忆**，而是：
   - 每轮多次 LLM 调用（decision / work / decision checkpoint）；
   - 读取大文件回喂（site/index.html 14.9KB、QA spec 8.2KB/4.9KB）；
   - 高思考 + 失败重试 / 信封修复。

3. **私有工作记忆的实际内容符合设计**：
   - 抽象目标、finding、issue；
   - 没有复制大段代码或正文；
   - agent_b 的 issue 具体到“障碍模式结束 overlay 未做逐帧断言”，
     这正是工作记忆应该捕获的“重大问题”。

4. **一个行为观察**：
   这次每个 Agent 只更新了 1 次私有工作记忆，并没有在每次 work checkpoint 都空更新。
   机制是“高优先级提示”，没有硬约束；Agent 选择了更少的维护次数。
   如果希望更接近“每次工作结束都维护”，需要更强的提示或让 checkpoint 更显眼，
   但仍然不应变成协议拒绝条件。

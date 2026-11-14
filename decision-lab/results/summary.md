# Decision Lab V1 — 结果

**Round 1（decider-0.8b）**：安装被网络阻断 → 放弃
**Round 2（Laya `laya-multilingual`）**：✅ **跑通并完成基准**

---

## 1. 是否成功在 Mac 本地运行？

# ✅ YES

```
chip  Apple M1
torch 2.14.1
laya  0.3.26
模型  convaiinnovations/laya/multilingual（routing 自己报的 repo）
```

实测踩到并修掉的 4 个问题：

| # | 问题 | 修法 |
|---|---|---|
| 1 | venv 用了系统 Python 3.9 | 改用 `python3.13`（laya 要 ≥3.10） |
| 2 | `huggingface.co` 不通 | `HF_ENDPOINT=https://hf-mirror.com` |
| 3 | HF 缓存目录不可写（sandbox） | `HF_HOME=<workspace>/decision-lab/.hf-cache` |
| 4 | 我按文档猜 schema，错两次 | **读 `laya/agent.py` 的 docstring** 拿到准确 schema |

准确 schema（**不是猜的**）：
```
choice: {"type":"choice","instructions":"...","criteria":{"optA":"...",...}}
score:  {"type":"score", "instructions":"...","criteria":["lvl0",...]}
noul:   {"type":"noul",  "instructions":"...","criteria":{"false":"..","true":".."}}
```

## 2. 是否使用 MPS？

✅ **是**（`torch.backends.mps.is_available() == True`）。
未观察到 CPU fallback 报错。

## 3. 模型加载实际占多少内存？

用 `memory_pressure` 的 free percentage 观测（未用重量级监控）：

```
Router() 构造前        free 68–69%
Router() 构造后        0.00s —— 它是**按需加载**，构造时不占内存
首次 predict 之后      free 61%
连续 60 case 之后      free 55–56%
swap                   全程 0.00 M（未触发换页）
模型缓存落盘           663 MB
```

> 粗略推算：常驻约 **13–14% × 16GB ≈ 2.1–2.2 GB**。
> 在 swap 为 0 的前提下稳定 —— **16GB 机器能承受**。

## 4. Cold Start 多久？

```
import laya              快（秒级）
Router() 构造            0.00s   ← 按需加载，构造近乎免费
首次 predict（含加载）   12385 ms
```

## 5. Warm Median / P95

```
median  36.0 ms
p95    676.2 ms
```

> ⚠️ p95 是 median 的 **18 倍** —— 长尾明显。
> 对「Fast Decision Layer」来说，**p95 比 median 更值得关注**。

## 6. 60 个 AI Economy cases 的准确率

| | rule（天真规则） | **Laya** |
|---|---|---|
| **overall** | **68.3%** | **41.7%** |
| easy | 90.0% | 45.0% |
| medium | 70.0% | 50.0% |
| **hard** | 45.0% | **30.0%** |
| **中文（30）** | 70.0% | **50.0%** |
| **英文（30）** | 66.7% | **33.3%** |

按类别（Laya）：

```
invest_tool         66.7%      accept_reject_task  50.0%
agent_selection     50.0%      need_human          50.0%
self_vs_delegate    50.0%      tool_vs_llm         50.0%
task_priority       33.3%      think_vs_wait       33.3%
model_selection     16.7%      recheck_result      16.7%
```

### 三条最重要的事实

1. **Laya（41.7%）打不过零成本的规则基线（68.3%）** —— 差 27 个百分点
2. **它在 easy 上就崩了**（45% vs 规则 90%）—— 不是「难才不行」，是**普遍不行**
3. **中文明显好于英文**（50.0% vs 33.3%）—— 对以中文为主的 Agent Context 是**好消息**，
   但仍不足以追上规则基线

> 与任务书 §十六 提到的官方数字吻合：基础英文 checkpoint 在
> typed-decision benchmark 上约 **0.362**；Laya 实测 **0.417**，同一量级。

## 7. 同一个 case 的稳定性

```
case: zh_delegate_001（自己 12 AIC vs 委托 4 AIC + 高成功率）
20 次结果: {'SELF': 20}
same-choice ratio = 20/20 = 100%
```

**完全稳定** —— 但**稳定在错误的答案上**（ground truth = DELEGATE_B）。

> ⚠️ 这比「不稳定」更危险：一个**高置信度且稳定错误**的决策层，
> 会系统性地把 Agent 推向错误策略。
> 任务书 §十八 说「一致性很重要」—— 这里是反例：
> **一致性只在不正确率低的时候才是优点。**

## 8. Confidence 与正确率是否相关？

```
confidence median = 0.811
high-confidence accuracy = 47%   (n=30)
low-confidence  accuracy = 37%   (n=30)
```

**只有 10 个百分点的差距**，而且两个都远低于可用水平。

> 任务书 §十五 设想的链路是：
> ```
> confidence high → 直接执行
> confidence low  → 交给 Slow LLM
> ```
> **实测不支持这个设计**：高置信度的答案只有 47% 是对的。
> 如果照这个阈值分流，会把**一半以上的错误**当成「可以直接执行」。

## 9. 与规则基线相比

**输 27 个百分点**（41.7% vs 68.3%），而且在**每一个难度档**上都输：

```
easy    45% vs 90%
medium  50% vs 70%
hard    30% vs 45%
```

## 10. 与少量真实 DeepSeek 对比

**未做**（Baseline C）。原因：先要 B 输给 A，才值得花真实 API 去做三方对照。
结论不会因为 DeepSeek 的数字而改变 —— Laya 已经输给了**不打 API**的规则基线。

---

## 11. 最终结论

# **11. 是否建议进入 V1 Fast Decision Layer 候选？**

# **NO**

**理由（全部基于实测，不是偏好）：**

1. **准确率 41.7%，低于零成本规则基线 68.3%。** 一个决策层如果打不过
   `if delegate_cost < self_cost` 这种一行代码，它没有存在价值。
2. **它在 easy 档就只有 45%。** 不能退守成「只用于简单决策」——
   简单决策规则基线已经有 90%。
3. **Confidence 几乎不含信息**（高 47% vs 低 37%）。
   这直接击穿了「高置信度直接执行、低置信度交给 Slow LLM」的设想。
4. **一致性反而是风险**：稳定地在错答案上给 0.949 的概率。

**它确实做到了的**：真的在 M1 上跑起来、真的 MPS、median 36ms、
内存约 2.1GB 且 swap 为 0、结构化输出无文本生成（`output_tokens: 0`）。

> **它很快，但它不够对。** 而 Fast Decision Layer 的价值前提是「对」。

---

## 12. 值得记录的两个副产品

**① 中文 > 英文（50.0% vs 33.3%）**
如果未来要重新评估本地决策模型，**multilingual 方向值得继续看** ——
但要用一个**专为 AI Economy 微决策 fine-tune 过**的 checkpoint，
而不是通用 zero-shot 的。

**② 「问题设计不适合」和「模型能力不足」要分开**
实测里 `invest_tool`（66.7%）明显好于 `model_selection` / `recheck_result`（16.7%）。
后者都涉及**多个数值之间的权衡**。这更像「zero-shot typed decision 不擅长
数值优化」，而不是「模型太笨」。

按任务书 §十八，这是后续路线的一个候选：
> 用 Laya 做**低级判断**，用专门 fine-tuned 的 Decision Model 做经济判断。
> **但本轮数据不支持把 Laya 直接放进 V1。**

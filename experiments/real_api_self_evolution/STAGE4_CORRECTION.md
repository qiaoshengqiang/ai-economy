# 阶段 4 —— 记录更正

## 更正内容

`03366e0` 的 commit message 里我写了：

> 对照臂 **2/5** 有信封、**3/5** 无信封；处理臂 **5/5** 有信封。

**这是错的。** 我随后用脚本核对，结果是：

```
对照(Strategy A) raw_llm_output 非空: 0/5
处理(Strategy B) raw_llm_output 非空: 0/5
```

**两臂都是 5/5「无信封」。**

## 根因：`raw_llm_output` 字段本身是坏的

两个实验运行器里我都写了：

```python
"raw_llm_output": extract_envelope(run.decision.reasoning)
```

问题：当 decision 是 **`act`** 时，`decision.reasoning` 装的是**信封里的
`reasoning` 字段**（模型经常不填 → 空串），而**不是** LLM 的原始输出。

所以 `extract_envelope("")` → `None`，该字段**恒为 None**。

📌 正确做法应该是从 `LLMResponse.content` 取原始文本 ——
   但 `run_cognition` 目前**没有把原始 response 带进 `CognitiveRunResult`**，
   所以运行器拿不到它。这是**实验运行器的能力缺口**，不是生产 bug
   （生产路径不需要原始输出）。

## 不受影响的部分

「对照臂有 3/5 没有产生 Action」这个观察**仍然成立** ——
它来自 `run.action_intent is None`（以及 `decision.kind == "reply"`），
**不是**来自那个坏字段：

```
用 action_intent 判定：
  Control  (Strategy A): 有 action 2/5 · 无 action 3/5
  Treatment(Strategy B): 有 action 5/5 · 无 action 0/5
```

所以「两臂可测量率不同（40% vs 100%）」这个**混淆变量**的判断成立，
但它在 commit message 里的**表述**（「格式遵循率」对比）是**基于坏字段**的，
应当以本节的数据为准。

## 教训

**又一次是「字段名/来源凭印象写」而不是回读验证** ——
与阶段 3 的 `action['type']` vs `action_type`、
以及 e234380 的 `WriteOutcome.to_dict()` 属于**同一类问题**。

# Decision Lab V1 — 结果

**状态：部分完成（Baseline A 实测；Baseline B/C 被网络阻断）**

---

## 已实测：Baseline A（天真规则）

```
overall:  68.3%   (n=60)
  easy    90.0%   (n=20)
  medium  70.0%   (n=20)
  hard    45.0%   (n=20)

按类别：
  invest_tool        100.0%  (n=6)
  agent_selection     83.3%  (n=6)
  need_human          83.3%  (n=6)
  task_priority       83.3%  (n=6)
  accept_reject_task  66.7%  (n=6)   ← 有 trade-off 就掉
  self_vs_delegate    66.7%  (n=6)   ←
  think_vs_wait       66.7%  (n=6)   ←
  model_selection     50.0%  (n=6)   ←
  recheck_result      50.0%  (n=6)   ←
  tool_vs_llm         33.3%  (n=6)   ←

latency: median=0.0ms  p95=0.0ms     （纯函数，无模型）
```

### 这组数字证明了什么

**1. 数据集本身是有效的。**
难度梯度**单调**（90% → 70% → 45%），说明 `easy/medium/hard`
确实按「因素数量」分开了 —— 不是主观标签。

**2. 「天真规则」的天花板就是 68.3%。**
这给出了**Baseline B 必须超过的线**：如果 decider-0.8b 打不过
68.3%，它连零成本的启发式都不如。

**3. 掉得最狠的三类正好是最需要判断力的：**
```
tool_vs_llm       33.3%   天真规则只看「工具便宜就用」
                          —— 不看可靠性，也不看省得够不够多
model_selection   50.0%   「选最便宜的」—— 不看质量够不够
recheck_result    50.0%   只会 REVIEW / ACCEPT —— 不会 RETRY / DELEGATE
```
> **这三类就是 Fast Decision Layer 真正要解决的问题。**

---

## 未完成：Baseline B（decider-0.8b）

**原因：网络阻断，不是设计问题。**

### 实测到的网络事实

| 主机 | 结果 |
|---|---|
| `pypi.org` | ✅ 通（`decider-ai==1.8.1` wheel 成功下载） |
| `huggingface.co` | ❌ **TimeoutError**（两次，45s 超时） |
| `cdn-lfs.huggingface.co` | ❌ **TimeoutError** |
| `hf-mirror.com` | ✅ 通 |

### 卡在哪一步

```
1. ✅ 建隔离 venv（Python 3.13.15）
2. ✅ 确认 decider-ai 1.8.1 存在于 PyPI
3. ✅ 确认 wheel 可下载
4. ❌ pip 连自己的 1.8 MB wheel 都反复「Connection interrupted」
      → torch（数百 MB ~ GB 级）在此网络下不可行
```

> **不是模型的问题，是链路的问题。** 需要网络条件好转后重试。

### 重试命令

```bash
cd decision-lab
./.venv/bin/python -m pip install -r requirements.txt \
    --retries 20 --timeout 120
export HF_ENDPOINT=https://hf-mirror.com     # 🔒 必须
./.venv/bin/python benchmark.py --decider
```

---

## 未完成：Baseline C（远程 LLM 对照）

按任务书 §十三 的设计，这是**显式 `--real-llm`** 才跑的，
而且建议 `--limit 10`。

**没有跑**，因为先要做完 B 的对照才有意义。
命令已就绪：

```bash
./.venv/bin/python benchmark.py --real-llm --limit 10
```

---

## 未完成：内存 / 冷热启动 / 一致性 / confidence 分析

这些都**依赖模型加载**，因此全部未做。

代码已就绪（`benchmark.py` 里的 `latencies()` / `confidence_analysis()` /
`--consistency`），装上模型即可产出。

---

## 一个先行结论（仅基于 Baseline A）

> **「零成本启发式」的天花板是 68.3%，且在最需要判断力的三类上只有 33-50%。**
>
> 这**不能**证明 decider-0.8b 有用 —— 只能证明
> **「值得有一个比启发式更好的快速决策层」这个命题是可测的**，
> 而且 benchmark 已经能把它测出来。
>
> 要回答「decider-0.8b 值不值得」，必须先跑完 Baseline B。

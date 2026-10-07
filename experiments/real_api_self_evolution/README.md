# 真实 API 自演化实验（阶段 1）

> **实验运行器，不是生产代码。**
> 🔒 它**不修改** Runtime 的任何行为 —— 只驱动已有能力并记录结果。

## 验证什么

真实 LLM 作为 Cognitive Substrate 时：

```
Experience → Cognitive Context → **真实 Agent Cognition**
           → Reflection Proposal → Controlled Write
           → 下一次 Cognition → 不同 Decision / Action
```

**验收命题**（§三十三）：真实 Agent 能否在**不依赖 Runtime 硬编码规则**的前提下，
读取持久 Experience、自主产生 Reflection，并在后续 Cognition 中看到改变后的 Self Model。

> ⚠️ **不要求**真实 Agent 一定发生策略变化。`reflections: []` 是**合法结果**。

## 🔴 三条实验纪律

| 纪律 | 做法 |
|---|---|
| **不强迫 Reflection**（§十三） | 任务里只说明**输出格式**，绝不说「你必须反思」 |
| **不强迫行为改变**（§十五） | Result A（没反思、没变化）也是有效结果 |
| **不猜模型意图**（§二十七） | 结构不合法 → 记录失败 → Self Model 不动 |

## 用法

```bash
# 结构验证（deterministic，不烧 API）
python experiments/real_api_self_evolution/run_experiment.py --fake

# 真实 API
python experiments/real_api_self_evolution/run_experiment.py --real \
    --agent agent_a --provider deepseek

# 重置到基线快照后重跑（§二十四）
python experiments/real_api_self_evolution/run_experiment.py --real --reset
```

## 目录

```
experiments/real_api_self_evolution/
├── run_experiment.py
├── data/experiment.db     实验库（与生产库分开，可删可重建）
├── snapshot/baseline.db   Run 1 前的基线快照（§二十四）
└── results/               结构化实验记录（§十七）
    ├── 00_baseline.json
    ├── 01_run1.json
    ├── 02_run2.json
    ├── 03_run3.json
    └── 99_summary.json
```

🔒 **不含 API Key**（§五）。记录里只有 provider / model 名。

## 与生产测试的关系（§六 / §三十二）

- 本目录**不在** `tests/` 下 → 默认 `pytest` **不会**执行它
- 默认测试不要求任何 API Key
- 真实 API 的随机失败**不会**混进单元测试结果

# Decision Lab V1

> **这是一个完全隔离的研究/基准实验。**
> 它**不是** AI Economy OS 的一部分 —— 不改任何生产代码，
> 不注册 Provider，不进 pytest，不被 `main.py` import。

---

## 它要回答什么

> **AI Economy OS 是否应该拥有一个独立于 Slow LLM 的快速决策层？**
> 以及 `decider-0.8b` 值不值得做 V1 的 Fast Decision Layer 候选？

---

## 🔒 隔离约定

| 规则 | 做法 |
|---|---|
| 不污染生产环境 | 自己的 venv：`decision-lab/.venv` |
| 不进生产依赖 | `requirements.txt` 只在本地；主项目 `requirements` 不动 |
| 不进 pytest | 本目录**不在** `tests/` 下，主项目 pytest 不会收集它 |
| 不加载本地模型 | 主项目默认 pytest 与它零交集 |
| 不改生产代码 | 本目录不 import `main.py` / `core/` / `agents/` / `communication/` |

---

## 目录

```
decision-lab/
├── README.md               ← 本文件
├── requirements.txt        ← 隔离依赖（decider-ai）
├── make_cases.py           ← 数据集**生成器**（规则显式编码，可复现）
├── benchmark_cases.json    ← 60 个 case（10 类 × 6，20 easy/20 medium/20 hard）
├── benchmark.py            ← 三个 baseline 的对照
└── results/                ← 输出
```

---

## 三个 baseline

| Baseline | 是什么 | 成本 |
|---|---|---|
| **A `rule`** | **天真**启发式（每类一条最简单的规则，例如「谁便宜选谁」） | 0 |
| **B `decider`** | `Mapika/decider-0.8b` 本地推理（MPS） | 本地算力 |
| **C `remote`** | 现有远程 LLM 对照（**显式 `--real-llm` 才跑**） | 真实 API |

### 🔒 为什么 rule baseline 必须「天真」

如果它和 ground truth 用同一套规则，它会拿 100% —— **那是循环论证**。
所以每类只给它一条最简单的启发式。它会在**有 trade-off 的 case** 上失败，
而那些失败正好标出了「100% 规则做不到、需要判断力」的地方。

---

## Ground truth 从哪来

**不是**「把某个 LLM 的答案当标准答案」。

`make_cases.py` 把判定规则**写死在代码里**（每个 `_rule_*` 函数），
然后按规则生成 case。所以：

- 规则是显式的、可审查的
- 数据集可复现（跑一次生成一次，逐字节相同）
- 不存在「人工标注悄悄偏向某个模型」的问题

**难度分级**也不靠主观判断，而由**因素数量**决定：

```
easy    单因素占绝对优势，没有 trade-off
medium  两个因素冲突，但其中一个明显更强
hard    多因素冲突，且**没有压倒性答案**
```

---

## 怎么跑

```bash
cd decision-lab

# 1) 只跑天真规则基线（不需要模型，秒级）
/opt/homebrew/bin/python3.13 benchmark.py

# 2) 跑本地 decider-0.8b（需要先装好，见下）
./.venv/bin/python benchmark.py --decider

# 3) 一致性测试：同一个 case 跑 20 次，看 choice 稳不稳
./.venv/bin/python benchmark.py --consistency delegate_003

# 4) 远程 LLM 对照（🔒 会烧真实 API，建议 --limit 10）
./.venv/bin/python benchmark.py --real-llm --limit 10
```

### 装 decider-0.8b

```bash
cd decision-lab
/opt/homebrew/bin/python3.13 -m venv .venv     # ⚠️ 必须 >= 3.11
./.venv/bin/python -m pip install -r requirements.txt

# 🔒 HuggingFace 在本机**不通**，必须走镜像
export HF_ENDPOINT=https://hf-mirror.com
./.venv/bin/python -c "
from decider.infer import Decider
d = Decider('Mapika/decider-0.8b')            # 首次会下载
print(d.decide('Agent has 100 AIC. Doing the task itself costs 12 AIC. '
               'Agent B can finish it for 4 AIC with high confidence.',
               [{'question': 'Which strategy should be selected?',
                 'options': ['SELF', 'DELEGATE', 'WAIT']}]))
"
```

---

## 本机环境事实（实测）

| 项 | 值 |
|---|---|
| 芯片 | Apple M1 |
| 统一内存 | 16 GB |
| 空闲内存 | 约 64% free（`memory_pressure`） |
| swap | 0.00 M |
| `pypi.org` | ✅ 通 |
| `huggingface.co` | ❌ **超时（不通）** |
| `hf-mirror.com` | ✅ 通 |
| `cdn-lfs.huggingface.co` | ❌ **超时** |

> ⚠️ 所以**必须** `export HF_ENDPOINT=https://hf-mirror.com`，
> 否则模型下载会卡死。

### decider-ai 的依赖体量（安装前必读）

```
torch                    ← 数百 MB ~ GB 级
transformers>=5
flash-linear-attention   ← CUDA 导向；Apple Silicon 上可能退化或不可用
huggingface_hub, numpy, jinja2
```

> ⚠️ `flash-linear-attention` 主要为 CUDA 设计。
> 在 MPS 上能否正常工作，**是本次实验要先验证的事情之一**。

# Decision Lab V1 — 结果

**Round 1（decider-0.8b）：安装被网络阻断**
**Round 2（Laya）：安装进行中，本轮未完成**

---

## 已经实测到的硬事实

### 数据集（已完成，可复现）

```
60 cases = 10 类 × 6
  难度: 20 easy / 20 medium / 20 hard   ✅ 完全均衡
  语言: 30 中文 / 30 English            ✅ 完全均衡
```

生成器：`make_cases.py`（判定规则显式编码在 `_rule_*` 函数里，
**ground truth 不来自任何 LLM**）。

难度按「因素数量」分级，不靠主观判断：
```
easy    单因素占绝对优势，无 trade-off
medium  两因素冲突，但一个明显更强
hard    多因素冲突，**没有压倒性答案**
```

### Baseline A（天真规则）—— **已实测**

```
overall:  68.3%   (n=60)
  easy    90.0%   (n=20)
  medium  70.0%   (n=20)
  hard    45.0%   (n=20)

invest_tool        100.0%      agent_selection     83.3%
need_human          83.3%      task_priority       83.3%
accept_reject_task  66.7%  ←   self_vs_delegate    66.7%  ←
think_vs_wait       66.7%  ←   model_selection     50.0%  ←
recheck_result      50.0%  ←   tool_vs_llm         33.3%  ←
```

**证明了什么**：
1. **数据集有效** —— 难度梯度单调（90→70→45），说明分级确实按因素数量分开了
2. **天真规则的天花板 = 68.3%** —— 这是 Laya 必须超过的线
3. 掉得最狠的三类（`tool_vs_llm` 33% / `model_selection` 50% /
   `recheck_result` 50%）**正好是最需要判断力的地方** ——
   也就是 Fast Decision Layer 真正要解决的问题

### 网络（实测，两次实验一致）

| 主机 | Round 1 | Round 2 |
|---|---|---|
| `pypi.org` | ✅ | ✅ |
| `huggingface.co` | ❌ Timeout | ❌ Timeout |
| `cdn-lfs.huggingface.co` | ❌ Timeout | — |
| `hf-mirror.com` | ✅ | ✅ |

> 🔒 **必须** `export HF_ENDPOINT=https://hf-mirror.com`，否则模型下载卡死。

### 实测到的下载速度

```
hf_xet-1.6.0 (3.9 MB)  →  11.3 kB/s  →  6 分 09 秒
```

**这是本轮失败的直接原因。** torch（macOS arm64，约 100–200 MB）
在这个速率下需要数小时。

---

## Laya 的安装体量（已确认，比 decider 轻）

```
Requires-Python: >=3.10                    ✅ 本机 3.13.15
torch>=2.0.0                               ← 大头
transformers>=4.48.0
safetensors>=0.4.0
huggingface_hub>=0.20.0
numpy>=1.20.0
```

✅ **没有** `flash-linear-attention` —— 这是 decider-ai 的 CUDA 坑，
Laya 没有它，所以 **Apple Silicon 上的可行性明显更高**。

官方接口已确认存在于 0.3.26 wheel 中：
```python
from laya import Router
router = Router()                       # 按需加载，不 preload
router.predict(state, questions, model="multilingual")
```
`Router` 的 docstring 自己写着 **"Lazily loads Laya checkpoints"** ——
与任务书 §三「不要 preload 三个 checkpoint」的要求一致。

---

## 本轮**未完成**的部分

| 项 | 状态 | 原因 |
|---|---|---|
| Laya 安装 | ⏳ 后台进行中 | 网络 11 kB/s |
| 模型下载 | ❌ | 同上 |
| Smoke test | ❌ | 依赖安装 |
| MPS 可用性 | ❌ | 依赖安装 |
| 内存 / 冷启动 / 热推理 | ❌ | 依赖安装 |
| 60 case 准确率（Laya） | ❌ | 依赖安装 |
| 一致性（10 case × 20 次） | ❌ | 依赖安装 |
| Confidence vs 正确率 | ❌ | 依赖安装 |
| Baseline C（真实 DeepSeek） | ❌ | 要等 B 才有意义 |

**代码已就绪**：`benchmark.py` 里的 `latencies()` /
`confidence_analysis()` / `--consistency` 装上模型即可产出。

---

## 结论

# 无法给出 §二十 的 11 个答案中的第 3–11 项。

**已能回答的：**

1. **是否成功在 Mac 本地运行？** — ❌ **否**（安装未完成，网络阻断）
2. **是否使用 MPS？** — ❌ 未到那一步

**不能给的**：内存占用 / Cold Start / Warm P95 / 准确率 /
稳定性 / confidence 相关性 / 与规则基线对比 / 是否建议进入 V1。

> ⚠️ 我**不会**因为没有数据就猜一个「NO」或「仅适用于部分决策」。
> 任务书 §十六 自己提醒了「不要看到模型非常快就默认它适合」——
> 反过来也一样：**不要因为装不上就判它不行。**

---

## 网络好转后怎么继续

```bash
cd decision-lab

# 1) 装（后台任务 bash-1062 可能已经完成；先查）
./.venv/bin/python -c "import laya; print(laya.__version__)"

# 2) 如果没装完
export HF_ENDPOINT=https://hf-mirror.com
./.venv/bin/python -m pip install --retries 50 --timeout 300 -r requirements.txt

# 3) Smoke test（只跑一次，不 preload）
./.venv/bin/python -c "
from laya import Router
r = Router()                      # 🔒 不加 preload=True
out = r.predict(
    'Agent A has 30 AIC. Self execution costs 12 AIC. '
    'Agent B costs 4 AIC. Agent B success probability is high.',
    {'strategy': {'question': 'Which strategy should be selected?',
                  'options': ['SELF', 'DELEGATE_B', 'WAIT']}},
    model='multilingual')
print(out)
"

# 4) 基准
./.venv/bin/python benchmark.py --laya
./.venv/bin/python benchmark.py --consistency zh_delegate_001
```

> ⚠️ 第 4 步需要 `benchmark.py` 增加一个 `--laya` 适配器（**尚未写**，
> 因为还没验证 Laya 的实际返回结构 —— 我不想照着文档猜一个形状写死）。

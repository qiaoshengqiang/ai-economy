# 独立认知基底 + Artifact 语义 —— 阶段报告

**Commit** `dc9ff83` + `a7026f7` · **Tests 4006 passed / 2 skipped / 30 deselected / 0 failed**

---

## 完成情况总览

| | 项 | 状态 |
|---|---|---|
| **A** | Observability | ✅ **完成** |
| **B** | Independent Credentials | ✅ **完成** |
| **C** | Cognitive Budget V1 | ✅ **完成** |
| **D** | Artifact Write Modes（CREATE/REPLACE/APPEND） | ❌ **未做** |
| **E** | Dynamic Artifact Creation | ❌ **未做** |
| **F** | 5-Agent Real API Preflight | ✅ **完成 —— 全部通过** |

⚠️ **D 与 E 我没有做。** 我在本轮上下文耗尽前优先完成了
「可观测性 + 异质凭据 + 认知预算 + 预检」这条链，
因为 **§二十七 明确要求「预检通过才进正式生产」**，而 D/E 属于生产语义。
**没有假装完成，也没有开新的实验。**

---

## A. Observability（§五–§八）

`llm_calls` 新增 **9 列**（含 `COLUMN_MIGRATIONS`，历史数据一律 `NULL`，**不回填猜测**）：

```
requested_model / response_model
requested_cognitive_budget / actual_max_tokens
finish_reason / output_status
requested_credential_ref / requested_credential_source / requested_credential_env
```

### §六 模型拆成两个**事实**

`model`（保留，= 响应模型）与 `requested_model` 分开，新增
`response_model` / `model_mismatch` 属性。**二者绝不互相覆盖。**

> **这个拆分的价值在预检里立刻显现** —— 见 F。

### §五 credential 只记元信息

只记 `ref` / `source` / **环境变量名**，**绝不记 key**。
有测试钉住「元信息里没有任何长随机串」。

### §八 `finish_reason` 落库

不再靠 `output_tokens == max_tokens` 反推截断。

---

## B. Independent Credentials（§一 / §三 / §四）

**5 份不同 key material、4 个不同 Provider、5 个不同 (provider, model)：**

| Agent | Provider | credential env |
|---|---|---|
| `novel_world_builder` | deepseek | `DEEPSEEK_KEY_AGENT_A` |
| `novel_plot_architect` | deepseek | `DEEPSEEK_KEY_AGENT_C` |
| `novel_author` | doubao | `DOUBAO_API_KEY` |
| `novel_editor` | bailian_qwen | `BAILIAN_KEY_AGENT_D` |
| `novel_reader` | bailian_glm | `BAILIAN_KEY_AGENT_E` |

`model` 一律留空 = 用 `.env` 的 `<PROVIDER>_MODEL`（`config.yaml` 是 git 跟踪的）。
**全部取自仓库中已存在且已真实验证过的配置，没有凭空创建模型名。**
`.env` 不入 Git；未打印任何 secret。

---

## C. Cognitive Budget V1（§九）

```python
Provider.clip_cognitive_budget(requested, fallback) -> (effective, reason)
# reason ∈ {None, "below_min", "above_max", "above_caller_limit"}
```

- 全部是**结构规则**（MIN 256 / MAX 32768 / 调用方上限），不是 Agent 策略
- 🔒 **`AIC balance → max_tokens` 的换算结构上做不到** ——
  裁剪函数的**签名里没有** `balance`/`wallet`/`cost`/`aic`/`ledger`，
  有测试扫签名钉住
- `max_tokens` 是**上限**，不是必须消耗的量

---

## F. Heterogeneous Preflight（§十六）—— **PASS**

```
all_have_calls                     True
all_simulated_false                True
all_credentials_available          True
five_distinct_fingerprints         True   ← 5/5
all_valid_content                  True
all_requested_model_recorded       True
all_response_model_recorded        True
all_have_tokens                    True
all_have_finish_reason             True
budget_recorded                    True
distinct_providers                 4
PASS                               True
```

| Agent | Provider | requested_model | response_model | fp | status |
|---|---|---|---|---|---|
| world_builder | deepseek | `deepseek-chat` | `deepseek-flash` | `df6ccbe4` | VALID |
| plot_architect | deepseek | `deepseek-chat` | `deepseek-flash` | `451c33bb` | VALID |
| author | doubao | `ep-2026...sgwpt` | `doubao-seed-evolving-latest-version` | `64550fc3` | VALID |
| editor | bailian_qwen | `qwen3.8-flash` | `qwen3.8-flash` | `a96e5db9` | VALID |
| reader | bailian_glm | `ZHIPU/GLM-5.3` | `zhipu/glm-5.3` | `a1a8c48d` | VALID |

**成本：5 次调用 / 1,655 tokens / 0.01655 AIC。**

独立环境（§十七）：新目录 `experiments/heterogeneous_production/` + 新 DB `hetero.db`，
**没有写入 `novel.db`**。

### 🔴 预检直接回答了 §二十五 的 1–4

1. **是否真的用了 5 个不同 Credential？** ✅ 是 —— **5/5 不同 fingerprint**
2. **Provider 是否不同？** ✅ 4 个不同（deepseek 占 2 个不同 key）
3. **Model 是否不同？** ✅ 5 个不同
4. **requested_model / response_model 是否一致？** ❌ **4/5 不一致**

> **这是本轮最有价值的一条发现。**
> 以前 `llm_calls.model` 一个字段同时承担两件事，
> 于是「请求 deepseek-chat、回显 deepseek-flash」**根本记不下来**。
> 现在两个事实都留着，而且**不自动断定哪边「错」**。
>
> doubao 的差异尤其值得注意：请求的是 endpoint id `ep-2026...`，
> 回显的是一个**人类可读的模型名** —— 这说明「请求标识」与「实际模型」
> 本来就是两个不同的东西。

---

## 未做的部分（诚实说明）

### D. Artifact Write Modes — **未做**

`shared_workspace.apply_write()` 目前仍然只有 **REPLACE** 语义。
Session 6 已证明这是 10,000 字目标的**首要阻塞**
（revision 涨到 R37，字数停在 2,000–3,500）。

最小设计已经清楚（`mode: "create" | "replace" | "append"`，
默认 `replace`；`append` + 空内容必须**不产生无意义 revision`），
但**本轮没有实现，也没有测试**。

### E. Dynamic Artifact Creation — **未做**

Session 6 里 `novel_world_builder` 尝试写 `characters.md` 得到
`📝FAIL`，因为 Workspace 只有 5 个预设路径。
`§十二` 要求的「Agent 能在自己的命名空间里创建新 Artifact」
（`world/characters.md`、`manuscript/chapter_01.md` …）
**本轮没有实现**。

### §十四 Artifact 历史 —— 未改动

> **Revision history currently records state metadata, not historical content.**
>
> `workspace_revisions` 仍然只有
> `revision_number / change_type / change_ref / created_by / created_at`，
> **不保存历史内容**。本轮**没有**暗中改变 Workspace Revision 语义。

---

## 关于正式生产实验（§十八 / §二十七）

**没有开始。** §二十七 的顺序是「A–F 完成 → 预检通过 → 再开始 10,000 字实验」，
而 **D/E 未完成**，正式生产的两个关键前提（续写语义、动态创建）还不具备。

> 如果现在就开始，结果会和 Session 6 一样：
> revision 一路涨，字数停在 2,000–3,500 —— 因为**没有 APPEND 语义**。

**建议顺序**：先做 D（append）+ E（动态创建）→ 再开正式异质生产。
预检环境与脚本已就绪，可以直接复用。

---

## 未做的事（§ 禁止项）

❌ 未 `git add -A` ❌ 未在有 failed tests 时 commit
❌ 未修改 `novel.db` ❌ 未开始 Credential/Model 改造之外的架构重构
❌ 未建立固定章节 Workflow ❌ 未把 AIC 与 Token Quota 绑定
❌ 未自动把所有写入变成 APPEND ❌ 未开启正式生产实验

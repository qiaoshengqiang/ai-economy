# Vendored: DeepSeek-V4.1 encoding (MIT)

原样复制，**未修改**。

| 项 | 值 |
|---|---|
| 来源 | `deepseek-ai/DeepSeek-V4.1-Flash-1017` |
| 文件 | `encoding/encoding.py` |
| 仓库 | https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017 |
| 下载地址 | https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/main/encoding/encoding.py |
| 大小 | 37,962 bytes |
| SHA256 | `13231c592e958fb39e2af6001dae69d90e7a36b265702b9b390236fb387599e2` |
| 许可 | MIT（见同目录 `LICENSE`） |
| vendored | 2026-11-11 |

## 用途

只使用它的 **解析部分**：

- `parse_tool_calls(index, text)`
- `decode_dsml_to_arguments(...)`
- `tool_calls_to_openai_format(...)`

编码/渲染部分（`encode_messages` 等）不使用；AIEOS 不负责重写模型 prompt。

## 为什么需要适配层

这份官方实现是**严格 parser**：

- 规范标记是单竖线 `<｜DSML｜ calls>`；
- 要求 `>\n` 行结构；
- 要求 `string="true|false"`；
- 遇到不完整块会抛错。

实测 DeepSeek-V4.1-Flash 会输出**双竖线** `<｜｜DSML｜｜ calls>`（CherryHQ PR #14747
确认这是真实 wire format，不是笔误），因此不能直接调用。归一化与失败策略在
`llm/dsml.py`，**不要修改本目录的文件**；升级时替换本文件并更新上表。

## 升级方式

1. 从官方仓库重新下载 `encoding/encoding.py`；
2. 校验 SHA256、更新本文件；
3. 跑 `tests/test_dsml_parser.py` 的全部真实样本；
4. 若官方 parser 的严格规则变化，只调整 `llm/dsml.py` 的归一化。

## 运行时开关

```text
AIEOS_DSML_MODE=execute   默认：解析 + 校验 + 执行
AIEOS_DSML_MODE=shadow    回滚：只解析并记录，不执行
```

shadow 用于观察真实泄漏率；execute 只在 DSML 完整、动作名已注册、
arguments 是合法 JSON 对象时才执行。

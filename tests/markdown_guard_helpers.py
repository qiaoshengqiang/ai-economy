"""
Markdown 解析基础能力 —— Documentation Guard 共用

============================================================
【为什么有这个模块】
============================================================

本项目的 Documentation Guard 需要从架构文档里**定位小节**、**读表格**、
**判断否定语义**。这些能力原先在多个 guard 文件里**各复制了一份**，
而其中一份带着一个真实 bug：

    旧 `section()` 把 fenced code block 里的 `# ...` 当成 Markdown heading。

后果（实测）：

    ## Real Section A

    ```python
    # ❌ fake heading inside code block
    x = 1
    ```

    content after the fence that MUST be in section A

`section("Real Section A")` 只取到 **28 个字符**，在代码块第一行就截断了。
于是「查这一节里有没有 X」的守卫会**在截断的段落上断言** —— 假绿或假红。

============================================================
【设计原则（本轮任务书）】
============================================================

1. **明确的 fenced-code 状态机**，不要「看起来像」式解析
   （`if line.startswith("#")` 再打补丁）。

2. **遇到 ``` 或 ~~~ 进入 fenced 状态；遇到【同类】fence 才退出。**
   （``` 内部的 `~~~` 不是关闭标记，反之亦然。）

3. **只有 `not in_fenced_block` 时** heading 才能改变 section 边界。

4. **结构化检查失败时明确失败**，不要自动降级成更宽松的检查。
   本模块的 `section()` 对不存在的标题返回 `""`，
   调用方必须自己断言非空 —— **「没有找到」不能自动等于「正确」**。

5. **不做完整 Markdown parser。** 只实现 Guard 真正需要的结构能力：
   小节定位 · 标题枚举 · 表格解析 · 文本压平 · 强调标记剥离。

============================================================
【共用一个 helper 的风险，以及怎么防】
============================================================

   所有 Guard → 同一个 helper → helper 写错 → 所有 Guard 同时绿

所以 `tests/test_markdown_guard_helpers.py` 里有**一组直接验证本模块行为**
的独立单元测试（不经过任何业务 Guard）。改这个文件必须让那组测试也通过。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, NamedTuple

__all__ = [
    "Heading",
    "read",
    "iter_headings",
    "section",
    "section_of",
    "flat",
    "plain",
    "table_rows",
    "parse_table",
    "strip_fenced_blocks",
]


#: 围栏标记（Markdown 允许 ``` 和 ~~~，长度 ≥ 3）
_FENCE_CHARS = ("`", "~")


class Heading(NamedTuple):
    """一个真正的 Markdown heading（**不在 fenced code block 里**）。"""

    line_no: int      # 1-based 行号
    level: int        # `#` 的个数
    text: str         # 去掉 `#` 与空白后的标题文字
    raw: str          # 原始整行


def _fence_marker(line: str) -> str | None:
    """
    如果这一行是围栏标记，返回它的**类型**（"```" 或 "~~~"），否则 `None`。

    【为什么返回「类型」而不是布尔】
        Markdown 规定 ``` 只能被 ``` 关闭，~~~ 只能被 ~~~ 关闭。
        如果只返回布尔，那么：

            ```python
            ~~~
            # 这一行会被误判成「已经关闭」
            ```

        就会提前退出 fenced 状态 —— 这正是「看起来像」式解析的坑。

    【缩进容错】
        围栏可以缩进最多 3 个空格。所以这里先 `lstrip()` 再判断，
        避免 `   ``` ` 被漏掉。
    """
    stripped = line.lstrip()
    if not stripped:
        return None

    char = stripped[0]
    if char not in _FENCE_CHARS:
        return None

    run = 0
    for c in stripped:
        if c == char:
            run += 1
        else:
            break

    if run < 3:
        return None

    return char * 3


def iter_headings(document: str) -> Iterator[Heading]:
    """
    枚举文档里**真正的** heading —— 跳过 fenced code block 内的 `#`。

    【为什么需要它（不只是 `section()`）】
        有的守卫会自己扫 `line.startswith("#")` 来找标题
        （例如「必须有一个讲 Origin ≠ Creator 的小节」）。
        那种写法同样会命中代码块里的 `# ❌ 不要`。

    ATX heading 语法：1–6 个 `#`，后面必须跟空格或行尾。
    （`#hashtag` **不是** heading。）
    """
    in_fence = False
    fence = ""

    for index, line in enumerate(document.splitlines(), start=1):
        marker = _fence_marker(line)
        if marker is not None:
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence, fence = False, ""
            continue

        if in_fence:
            continue

        if not line.startswith("#"):
            continue

        level = len(line) - len(line.lstrip("#"))
        if level > 6:
            continue

        rest = line[level:]
        # ATX heading 要求 `#` 后有空格或直接行尾；`#Foo` 不是 heading
        if rest and not rest.startswith(" "):
            continue

        yield Heading(
            line_no=index,
            level=level,
            text=rest.strip(),
            raw=line,
        )


def section(document: str, heading_prefix: str) -> str:
    """
    取一个 heading 及其**所有子小节** —— 边界只在真正的 heading 处产生。

    参数
    ----
    heading_prefix
        **子串**匹配 heading 文字（保持与旧调用方兼容）。
        例如 `"15."` 会匹配 `## 15. Workspace Lifecycle` 与
        `### 15.3 ...` 中第一个出现的那个。

    返回
    ----
    匹配到的 heading 起、到**下一个层级 ≤ 它的真 heading** 为止的原文。
    **找不到时返回 `""`。**

    ⚠️ **调用方必须断言非空。**
        `assert section(doc, "99.") == ""` 这句在旧代码里很常见，
        它是**对的**；但 `assert "X" in section(doc, "某节")` 在
        section 返回空串时也会**假绿**（因为不存在的东西不违反
        「包含 X」，取决于断言方向）。所以：
        **先断言非空，再做内容断言。**

    【修掉的 bug】
        旧实现只看 `line.startswith("#")`，
        所以 ```python 块里的 `# ❌ 不要` 会被当成同级 heading，
        整节在那里被截断（实测只剩 28 字符）。
    """
    lines = document.splitlines()
    headings = list(iter_headings(document))

    start: Heading | None = None
    for heading in headings:
        if heading_prefix in heading.text or heading_prefix in heading.raw:
            start = heading
            break

    if start is None:
        return ""

    end = len(lines) + 1
    for heading in headings:
        if heading.line_no <= start.line_no:
            continue
        if heading.level <= start.level:
            end = heading.line_no
            break

    return "\n".join(lines[start.line_no - 1: end - 1])


def section_of(document: str, index: int) -> str:
    """
    取字符下标 `index` 处在**哪一个 section** 里。

    用于判断「这句话是在什么上下文里说的」（否定 / 历史 / 规范）。
    """
    if index < 0 or index > len(document):
        return ""

    line_no = document[:index].count("\n") + 1

    enclosing: Heading | None = None
    for heading in iter_headings(document):
        if heading.line_no <= line_no:
            enclosing = heading
        else:
            break

    if enclosing is None:
        return document[:index]

    return section(document, enclosing.text or enclosing.raw)


def flat(text: str) -> str:
    """
    压平空白 —— 便于在跨行 Markdown 里匹配**完整规范句**。

    例：`"> **A**\\n> **B**"` → `"> **A** > **B**"`
    """
    return " ".join(text.split())


def plain(text: str) -> str:
    """
    去掉 Markdown **强调标记**后压平 —— 用于匹配**跨粗体边界**的短语。

    【为什么必须要有它（真实踩过的坑）】
        文档里写的是：

            而**不是**：

        而守卫查的是 `"而不是" in block` ——
        中间夹着 `**`，**子串匹配找不到**。

        `plain()` 把强调标记去掉再压平，
        于是 `而**不是**` → `而不是`。

    ============================================================
    【第一版是错的：把 `_` 全部删掉（真实踩到的坑）】
    ============================================================

        第一版是：

            re.sub(r"[*_`]", "", text)

        它对 `*` 没问题，但 `_` **在标识符里不是强调**：

            TASK_WORKSPACE.md   →  TASKWORKSPACE.md   ❌
            resource_scope      →  resourcescope      ❌
            delegation_id       →  delegationid       ❌

        后果：守卫查 `"TASK_WORKSPACE.md" in plain(block)` 永远为假，
        于是**把文档里明明写好的一句话判成缺失**（假红）。

        这不是「文档写错了」，是**工具过度清洗**。

    ------------------------------------------------------------
    【正确做法】只删**真正的**强调标记：
        `**bold**` · `__bold__` · `*italic*` · `_italic_` · `` `code` ``

        其中 `_` 只有在**词边界**上才是强调 ——
        `(?<!\w)_ ... _(?!\w)` 保证 `TASK_WORKSPACE` 不被碰。
    """
    # ①② 成对标粗：**bold** / __bold__（`_` 必须在词边界）
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", text, flags=re.S)

    # ③④ 成对斜体：*italic* / _italic_
    text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text, flags=re.S)

    # ⑤ 行内代码
    text = re.sub(r"`([^`]*)`", r"\1", text)

    return " ".join(text.split())


def strip_fenced_blocks(document: str) -> str:
    """
    去掉所有 fenced code block 的**内容**（保留行数位置感可由调用方自行处理）。

    用于「这段文字是在讲 Markdown 结构本身，不是在讲代码」的场景。
    """
    out: list[str] = []
    in_fence = False
    fence = ""

    for line in document.splitlines():
        marker = _fence_marker(line)
        if marker is not None:
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence, fence = False, ""
            continue
        if not in_fence:
            out.append(line)

    return "\n".join(out)


def table_rows(document: str, header_cell: str) -> list[str]:
    """
    取含某个表头单元格的表格的**数据行**（结构化，不是全文搜索）。

    只认第一张匹配的表：找表头行 → 跳过 `|---|` 分隔行 → 收集后续 `|...|`。
    """
    lines = document.splitlines()

    for index, line in enumerate(lines):
        if not line.startswith("|") or header_cell not in line:
            continue
        if index + 1 >= len(lines) or "---" not in lines[index + 1]:
            continue

        rows = []
        for row in lines[index + 2:]:
            if not row.startswith("|"):
                break
            rows.append(row)
        return rows

    return []


def parse_table(document: str, header_cell: str) -> list[dict]:
    """
    把表格解析成 `[{列名: 值}]`。

    单元格数与表头不一致的行会被**跳过**（通常是分隔行或残缺行）。
    """
    lines = document.splitlines()

    for index, line in enumerate(lines):
        if not line.startswith("|") or header_cell not in line:
            continue
        if index + 1 >= len(lines) or "---" not in lines[index + 1]:
            continue

        headers = [c.strip() for c in line.strip("|").split("|")]
        parsed = []
        for row in lines[index + 2:]:
            if not row.startswith("|"):
                break
            cells = [c.strip() for c in row.strip("|").split("|")]
            if len(cells) != len(headers):
                continue
            parsed.append(dict(zip(headers, cells)))
        return parsed

    return []


def read(path: Path | str) -> str:
    """读 UTF-8 文本（守卫统一入口，方便以后换编码策略）。"""
    return Path(path).read_text(encoding="utf-8")

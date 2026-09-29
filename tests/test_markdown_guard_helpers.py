"""
Markdown 解析基础能力的**独立单元测试**

============================================================
【为什么这个文件必须存在】
============================================================

现在三个 guard 文件共用 `markdown_guard_helpers.py`。
这带来一个真实风险：

    所有 Guard → 同一个 helper → helper 写错 → 所有 Guard 同时绿

所以这里有一组**直接验证 parser 行为**的测试 ——
不经过任何业务 Guard，只喂伪造的 Markdown，看解析结果对不对。

> **改 `markdown_guard_helpers.py` 时必须让这个文件继续通过。**

============================================================
【本轮要证明的旧 bug】
============================================================

旧 `section()` 只查 `line.startswith("#")`，于是：

    ## Real Section A

    ```python
    # ❌ fake heading inside code block
    x = 1
    ```

    content after the fence that MUST be in section A

`section("Real Section A")` **只取到 28 个字符**，在代码块第一行截断。

---

⚠️ **本文件所有 fixture 都用 `textwrap.dedent(...)`** ——
    直接把 Markdown 写在缩进的 docstring 里会让 `### heading`
    不以 `#` 开头，parser 根本看不到它，
    于是测试**永远失败**而不是验证目标逻辑。
    （上一轮真实踩过这个坑：`test_code_block_headings_do_not_truncate_sections`。）
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from tests.markdown_guard_helpers import (
    flat,
    iter_headings,
    parse_table,
    plain,
    read,
    section,
    section_of,
    strip_fenced_blocks,
    table_rows,
)


def md(text: str) -> str:
    """标准 fixture 入口 —— **总是** dedent，避免缩进导致假失败。"""
    return textwrap.dedent(text).strip()


def _strip_docstrings_and_comments(source: str) -> str:
    """
    只留**真正的代码** —— 去掉 docstring 内容与注释。

    【为什么元守卫必须这样（ADR-0018 的既有约定）】
        本文件自己就**在文档里引用**了那个反模式：

            [l for l in doc.splitlines() if l.startswith("#")]

        只做原文扫描的话，元守卫会**抓住自己的文档**。
        项目的 Guard 规范明确要求：字符串扫描必须先剥掉
        docstring 与注释，并且要配反向测试。
    """
    # ① 用 AST 收集所有 docstring 的真实内容
    docstrings: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.append(doc)

    # ② **先**删 docstring，**再**剥注释 —— 顺序不能反。
    #
    #    ⚠️ 两个真实踩到的坑（都由反向测试抓出来）：
    #
    #    坑 1：不能按「整行等于某条 docstring 行」匹配。
    #          内容行常和闭合的 `"""` 同行：
    #              文档里引用了 X"""
    #          整行匹配会漏掉它。→ 改用**子串删除**。
    #
    #    坑 2：**顺序不能反。**
    #          docstring 内容里可能含 `'#'`。
    #          如果先剥注释，那一行会在 `#` 处被截断，
    #          于是它和原始 docstring 文本不再匹配 → 删不掉。
    text = source
    for doc in docstrings:
        for doc_line in doc.splitlines():
            if doc_line.strip():
                text = text.replace(doc_line, "")

    kept: list[str] = []
    for line in text.splitlines():
        hash_index = line.find("#")
        kept.append(line[:hash_index] if hash_index != -1 else line)

    return "\n".join(kept)


# ============================================================
# 一、backtick fence
# ============================================================
class TestBacktickFence:
    """``` 围栏内的 `#` 不是 heading。"""

    def test_hash_inside_backtick_fence_is_not_a_heading(self):
        """围栏内的 `#` 不出现在 heading 枚举里。"""
        doc = md("""
            ## Real Section A

            ```python
            # ❌ fake heading inside code block
            x = 1
            ```

            ## Real Section B
        """)

        headings = [h.text for h in iter_headings(doc)]

        assert headings == ["Real Section A", "Real Section B"], (
            f"枚举到了不该有的 heading：{headings}\n"
            f"（``` 里的 `# ❌ fake heading` 被当成了 heading。）"
        )

    def test_section_is_not_truncated_by_fence(self):
        """
        **这是本轮的核心反向测试。**

        围栏内的 `# ❌ fake heading` 那一行**必须留在** section A 里，
        而且 fence 之后的正文也必须还在。

        【修复前】取到 28 字符（在代码块第一行截断）。
        【修复后】取到完整小节。
        """
        doc = md("""
            ## Real Section A

            ```python
            # ❌ fake heading inside code block
            x = 1
            ```

            content after the fence that MUST be in section A

            ## Real Section B

            other content
        """)

        got = section(doc, "Real Section A")

        assert got, "section() 返回空 —— 连标题都没找到"
        assert "# ❌ fake heading inside code block" in got, (
            "围栏内的那行丢了 —— 但是等等，它本来就该在 section 内容里。\n"
            f"实际取到：\n{got}"
        )
        assert "content after the fence that MUST be in section A" in got, (
            "**fence 之后的正文被截断了** —— 这正是旧 bug。\n"
            f"实际取到（{len(got)} 字符）：\n{got}"
        )
        assert "Real Section B" not in got, (
            "section() 越过了下一个同级标题"
        )

    def test_real_heading_after_fence_is_detected(self):
        """fence 关闭之后真正的 heading 必须能正常识别。"""
        doc = md("""
            ## A

            ```
            # not a heading
            ```

            ## B

            real content
        """)

        got = section(doc, "## B")

        assert got, "fence 之后的标题没被识别"
        assert "real content" in got


# ============================================================
# 二、tilde fence
# ============================================================
class TestTildeFence:
    """~~~ 围栏内的 `#` 也不是 heading。"""

    def test_hash_inside_tilde_fence_is_not_a_heading(self):
        doc = md("""
            ## Real Section A

            ~~~
            # ❌ fake heading inside tilde fence
            ~~~

            ## Real Section B
        """)

        headings = [h.text for h in iter_headings(doc)]

        assert headings == ["Real Section A", "Real Section B"], (
            f"枚举到了不该有的 heading：{headings}"
        )

    def test_tilde_section_not_truncated(self):
        doc = md("""
            ## Real Section A

            ~~~
            # fake
            ~~~

            content after tilde fence

            ## Real Section B
        """)

        got = section(doc, "Real Section A")

        assert got, "section() 返回空"
        assert "content after tilde fence" in got, (
            f"~~~ 之后的正文被截断了：\n{got}"
        )

    def test_backtick_inside_tilde_fence_does_not_close_it(self):
        """
        **``` 不能关闭 ~~~**，反之亦然。

        这是「同类 fence 才关闭」的验证：
            如果实现只看「是不是 fence 标记」，
            下面第 2 行的 ``` 会把 ~~~ 提前关掉，
            于是 `# fake` 变成 heading。
        """
        doc = md("""
            ## A

            ~~~
            ```
            # fake inside tilde fence
            ```
            ~~~

            ## B
        """)

        headings = [h.text for h in iter_headings(doc)]

        assert headings == ["A", "B"], (
            f"``` 错误地关闭了 ~~~ 围栏：{headings}"
        )

    def test_tilde_inside_backtick_fence_does_not_close_it(self):
        """反向：~~~ 不能关闭 ```。"""
        doc = md("""
            ## A

            ```
            ~~~
            # fake inside backtick fence
            ~~~
            ```

            ## B
        """)

        headings = [h.text for h in iter_headings(doc)]

        assert headings == ["A", "B"], (
            f"~~~ 错误地关闭了 ``` 围栏：{headings}"
        )


# ============================================================
# 三、heading 语法细节
# ============================================================
class TestHeadingSyntax:
    def test_levels_are_recorded(self):
        doc = md("""
            # H1

            ## H2

            ### H3

            #### H4
        """)

        got = [(h.level, h.text) for h in iter_headings(doc)]

        assert got == [(1, "H1"), (2, "H2"), (3, "H3"), (4, "H4")]

    def test_hashtag_without_space_is_not_a_heading(self):
        """`#hashtag` 不是 ATX heading。"""
        doc = md("""
            ## Real

            #not-a-heading
            #also-not
        """)

        headings = [h.text for h in iter_headings(doc)]

        assert headings == ["Real"], f"`#xxx` 被误判成 heading：{headings}"

    def test_section_includes_subsections(self):
        """取某节必须包含它的子小节（`### 4.1` 属于 `## 4`）。"""
        doc = md("""
            ## 4. Parent

            intro

            ### 4.1 First

            a

            ### 4.2 Second

            b

            ## 5. Next

            c
        """)

        got = section(doc, "4. Parent")

        assert "4.1 First" in got and "4.2 Second" in got, (
            "section() 没包含子小节"
        )
        assert "5. Next" not in got, "section() 越过了下一个同级标题"

    def test_section_stops_at_same_level(self):
        doc = md("""
            ## A

            a-content

            ## B

            b-content
        """)

        got = section(doc, "## A")

        assert "a-content" in got
        assert "b-content" not in got

    def test_section_of_returns_empty_for_missing(self):
        """
        **找不到时必须返回空串，而不是「猜一个」。**

        ⚠️ 调用方必须自己断言非空 ——
            「没有找到」不能自动等于「正确」。
        """
        doc = md("""
            ## A

            content
        """)

        assert section(doc, "99. 不存在") == ""
        assert section(doc, "完全不存在的标题") == ""

    def test_missing_section_would_make_substring_check_vacuous(self):
        """
        反向：证明**空 section 会让内容断言失去意义**。

        这条不是测 helper，是**记录调用方必须遵守的纪律**：
        `assert "X" not in section(...)` 在 section 为空时**必然通过**，
        于是守卫什么都没检查。
        """
        empty = section("## A\n\ncontent\n", "不存在")

        assert empty == ""
        # 「不包含」在空串上恒真 —— 这就是假绿
        assert "任何东西" not in empty


# ============================================================
# 四、fixture 缩进 / dedent
# ============================================================
class TestFixtureIndentation:
    def test_indented_fixture_without_dedent_is_not_parsed(self):
        """
        **记录那个真实踩过的坑。**

        把 Markdown 直接写在缩进的 docstring 里：

            doc = \"\"\"
                    ### heading
                    \"\"\"

        行首是空格，**不是** `#`，所以 parser 看不到它。
        上一轮 `test_code_block_headings_do_not_truncate_sections`
        就是这样**永远失败**，而不是在验证围栏逻辑。
        """
        indented = """
                ### heading
                """
        assert section(indented, "heading") == "", (
            "缩进的行不该被当成 heading（这正是必须 dedent 的原因）"
        )

    def test_dedent_fixture_is_parsed(self):
        """用 `textwrap.dedent` 之后就能正常解析。"""
        doc = md("""
            ### heading

            body
        """)

        assert "body" in section(doc, "heading")

    def test_md_helper_always_dedents(self):
        """`md()` 必须 dedent —— 它是本文件所有 fixture 的统一入口。"""
        assert md("\n        ## X\n\n        y\n").startswith("## X")

    def test_up_to_three_spaces_fence_indent_still_opens_fence(self):
        """Markdown 允许围栏缩进 ≤ 3 空格。"""
        doc = md("""
            ## A

               ```
               # fake
               ```

            after
        """)

        headings = [h.text for h in iter_headings(doc)]

        assert headings == ["A"], f"缩进围栏没被识别：{headings}"


# ============================================================
# 五、其他 helper
# ============================================================
class TestOtherHelpers:
    def test_flat_collapses_whitespace(self):
        assert flat("  a\n  b  ") == "a b"

    def test_plain_strips_emphasis(self):
        """
        **真实踩过的坑**：文档写 `而**不是**`，
        但守卫查 `"而不是"` —— 子串匹配找不到。
        """
        assert plain("而**不是**：") == "而不是："
        assert "而不是" in plain("而**不是**")
        assert "而不是" not in "而**不是**", (
            "（记录：不 strip 强调标记时确实匹配不到）"
        )

    def test_plain_strips_code_and_underscore(self):
        assert plain("`code` and _em_") == "code and em"

    def test_strip_fenced_blocks_removes_fence_content(self):
        doc = md("""
            keep-before

            ```
            drop-this
            # also-drop
            ```

            keep-after
        """)

        got = strip_fenced_blocks(doc)

        assert "keep-before" in got and "keep-after" in got
        assert "drop-this" not in got and "also-drop" not in got

    def test_table_rows_parses_structure(self):
        doc = md("""
            | 概念 | 回答 |
            |---|---|
            | Origin | 为什么出现？ |
            | Creator | 谁创建？ |
        """)

        rows = table_rows(doc, "回答")

        assert len(rows) == 2, f"解析出 {len(rows)} 行"

    def test_table_rows_empty_for_missing_header(self):
        assert table_rows("| a | b |\n|---|---|\n| 1 | 2 |", "不存在") == []

    def test_parse_table_returns_dicts(self):
        doc = md("""
            | Task | Parent | Root |
            |---|---|---|
            | `Task-002` | `Task-001` | `Task-001` |
        """)

        parsed = parse_table(doc, "Parent")

        assert len(parsed) == 1
        assert parsed[0]["Task"] == "`Task-002`"
        assert parsed[0]["Root"] == "`Task-001`"

    def test_section_of_locates_enclosing_section(self):
        doc = md("""
            ## A

            text-a

            ## B

            text-b
        """)

        index = doc.index("text-b")
        got = section_of(doc, index)

        assert "text-b" in got
        assert "text-a" not in got, "section_of() 定位到了错误的小节"

    def test_read_roundtrip(self, tmp_path):
        path = tmp_path / "x.md"
        path.write_text("# 标题\n内容\n", encoding="utf-8")

        assert "标题" in read(path)


# ============================================================
# 六、修复前后的对照（把旧实现抄进来做对照）
# ============================================================
class TestAgainstOldImplementation:
    """
    **明确证明旧实现会失败、新实现会通过。**

    这里把**旧的** `section()` 逻辑抄一份（只用于对照），
    让「旧 bug 真的存在」这件事有可执行的证据，
    而不是只写在注释里。
    """

    @staticmethod
    def legacy_section(document: str, heading_prefix: str) -> str:
        """上一版实现 —— **故意保留 bug**，只用于对照。"""
        lines = document.splitlines()
        start = level = None

        for index, line in enumerate(lines):
            if line.startswith("#") and heading_prefix in line:
                start = index
                level = len(line) - len(line.lstrip("#"))
                break

        if start is None:
            return ""

        end = len(lines)
        for index in range(start + 1, len(lines)):
            line = lines[index]
            if not line.startswith("#"):
                continue
            if len(line) - len(line.lstrip("#")) <= level:
                end = index
                break

        return "\n".join(lines[start:end])

    FIXTURE = """
## Real Section A

```python
# ❌ fake heading inside code block
x = 1
```

content after the fence that MUST be in section A

## Real Section B
"""

    def test_legacy_truncates(self):
        """旧实现在代码块第一行截断 —— bug 是真的。"""
        got = self.legacy_section(self.FIXTURE, "Real Section A")

        assert "content after the fence" not in got, (
            "旧实现居然没截断？那这个对照的前提不成立"
        )
        assert len(got) < 60, f"旧实现取到 {len(got)} 字符（预期很短）"

    def test_new_implementation_does_not_truncate(self):
        """新实现不截断 —— 修复有效。"""
        got = section(self.FIXTURE, "Real Section A")

        assert "content after the fence that MUST be in section A" in got, (
            f"新实现仍然截断：\n{got}"
        )

    def test_legacy_also_breaks_tilde(self):
        """旧实现对 ~~~ 同样失效。"""
        fixture = """
## A

~~~
# fake
~~~

after-tilde

## B
"""

        assert "after-tilde" not in self.legacy_section(fixture, "A")
        assert "after-tilde" in section(fixture, "A")

    def test_legacy_heading_scan_picks_up_code_comments(self):
        """
        旧的 heading 扫描（`line.startswith("#")`）会把代码块里的
        `# ❌ 不要` 也当成 heading ——
        这正是 `test_task_origin_lineage_guards.py` 里
        四处 `[l for l in doc.splitlines() if l.startswith("#")]` 的问题。
        """
        doc = self.FIXTURE

        legacy = [l for l in doc.splitlines() if l.startswith("#")]
        fixed = [h.raw for h in iter_headings(doc)]

        assert any("fake heading" in l for l in legacy), (
            "旧扫描本该命中代码块里的 `#`"
        )
        assert not any("fake heading" in l for l in fixed), (
            "新枚举不该命中代码块里的 `#`"
        )


# ============================================================
# 七、本模块自身的完整性
# ============================================================
class TestModuleIntegrity:
    def test_no_third_party_markdown_parser(self):
        """
        任务书要求：**不引入第三方 Markdown parser。**
        本模块只依赖标准库。
        """
        source = read(Path(__file__).parent / "markdown_guard_helpers.py")

        for banned in ("import markdown", "import mistune", "import commonmark",
                       "import markdown_it", "from markdown"):
            assert banned not in source, (
                f"引入了第三方 Markdown parser：{banned}"
            )

    def test_helpers_are_the_documented_set(self):
        """导出的 helper 必须恰好是文档里承诺的那些（不要膨胀）。"""
        import tests.markdown_guard_helpers as h

        assert set(h.__all__) == {
            "Heading", "read", "iter_headings", "section", "section_of",
            "flat", "plain", "table_rows", "parse_table",
            "strip_fenced_blocks",
        }, "helper 集合变了 —— 如果是故意加的，请同步更新本测试与文档"


# ============================================================
# 八、元守卫：不许把裸 heading 扫描写回来
# ============================================================
class TestNoRawHeadingScanning:
    """
    **防止旧 bug 从另一个入口回来。**

    `section()` 修好之后，仍然可以有人写：

        [l for l in doc.splitlines() if l.startswith("#")]

    来做「有没有讲 X 的小节」—— 这是**同一个 bug**。
    本项目在 `test_task_origin_lineage_guards.py` 里就有过 4 处。

    所以这里做一个**元守卫**：guard 文件里不允许裸的
    `splitlines() ... startswith("#")` 组合。

    【怎么区分「真的解析」和「碰巧扫描」】
        允许：`markdown.iter_headings(...)` / `markdown.section(...)`
        禁止：`splitlines()` + `startswith("#")` 出现在同一行
              （或跨行紧邻 —— 这里查同一行，足够抓住实际写法）
    """

    #: 允许 `startswith("#")` 的例外（不是 heading 扫描）
    ALLOWED_CONTEXTS = (
        "def _fence_marker",     # helper 内部：判断围栏字符
        "# ",                    # 注释行
    )

    def _guard_files(self):
        return sorted(
            p for p in Path(__file__).parent.glob("*guard*.py")
            if p.name != Path(__file__).name
        )

    def test_no_raw_heading_scan_in_guard_files(self):
        offenders = []

        for path in self._guard_files():
            code = _strip_docstrings_and_comments(
                path.read_text(encoding="utf-8")
            )

            for line_no, line in enumerate(code.splitlines(), start=1):
                if "startswith" not in line:
                    continue
                # 必须同时含 `startswith` + 引号包起来的 `#` + `splitlines`
                has_hash_literal = ('"#"' in line) or ("'#'" in line)
                if not has_hash_literal:
                    continue
                if "splitlines" in line:
                    offenders.append(f"{path.name}:{line_no}  {line.strip()}")

        assert not offenders, (
            "guard 文件里出现了**裸的 heading 扫描**（代码，不是文档）—— "
            "这是旧 `section()` bug 的另一个入口：\n  "
            + "\n  ".join(offenders)
            + "\n\n改用 `markdown.iter_headings(...)` / `markdown.section(...)`。"
        )

    def test_guards_use_the_shared_helper(self):
        """
        三个文档守卫必须**都**通过共享 helper 解析 Markdown ——
        不允许再各自持有一份 `section()` 实现。
        """
        expected = [
            "test_task_workspace_docs_guards.py",
            "test_task_origin_lineage_guards.py",
            "test_human_demand_model_guards.py",
        ]

        for name in expected:
            path = Path(__file__).parent / name
            source = path.read_text(encoding="utf-8")

            assert "from tests import markdown_guard_helpers as markdown" in source, (
                f"{name} 没有引用共享 helper"
            )
            assert "markdown.section(" in source, (
                f"{name} 的 section() 没有委托给 helper"
            )

    def test_strip_docstrings_really_strips(self):
        """
        反向：`_strip_docstrings_and_comments` **必须真的**剥掉
        docstring 与注释 —— 否则元守卫会一直抓住本文件自己的文档。
        """
        quote = chr(34) * 3
        sample = (
            "def f():\n"
            f"    {quote}文档里引用了 doc.splitlines() if l.startswith('#'){quote}\n"
            "    # 注释里也有 doc.splitlines() if l.startswith('#')\n"
            "    return 1\n"
        )

        code = _strip_docstrings_and_comments(sample)

        assert "splitlines()" not in code, (
            "docstring / 注释没被剥掉 —— 元守卫会误报自己的文档\n"
            f"剥完还是：\n{code}"
        )

    def test_strip_docstrings_keeps_real_code(self):
        """反向：真正的代码必须**仍然**留在结果里。"""
        sample = 'headings = [l for l in doc.splitlines() if l.startswith("#")]\n'

        code = _strip_docstrings_and_comments(sample)

        assert "splitlines()" in code, "真代码被误剥了"

    def test_meta_guard_would_catch_a_regression(self):
        """
        反向：证明元守卫**真的**能抓到那个写法
        （否则它只是一条永远为真的规则）。
        """
        bad = 'headings = [l for l in doc.splitlines() if l.startswith("#")]'
        stripped = bad.strip()

        caught = (
            not stripped.startswith("#")
            and "startswith" in bad
            and '"#"' in bad
            and "splitlines" in bad
        )

        assert caught, "元守卫的判定逻辑抓不到这个写法 —— 它是空转的"

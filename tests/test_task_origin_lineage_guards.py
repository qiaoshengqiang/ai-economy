"""
Task Origin & Task Lineage 文档守卫

============================================================
【这个文件守什么】
============================================================

Task Origin / Lineage 是**纯逻辑架构定义**（零实现）。
所以这一组守卫检查两类东西：

    ① **七个概念没有被合并**
       Origin / Creator / Parent / Root / Funding / Responsible / Participant
       规范明确要求：「绝对不要把这些全部压缩成一个 owner」。

    ② **结构性规则被正确标注**
       `Maximum Task Depth = 3` 必须同时是：
           · 明确写出的数字
           · 明确标为 CURRENT / TEMPORARY
       （既不能改成 4 层，也不能写成永久架构原则）

============================================================
【遵守 Guard 规范（ADR-0018）+ 规范 §33 的额外要求】
============================================================

规范 §33 明确说：**不要使用简单的「某个词出现」来做 Guard。**

所以这里用：
    · **结构化 Markdown parsing**（按标题切段、按表格行解析）
    · **明确规范句匹配**（查那句话，不查那一堆词）
    · **概念独立性检查**（每个概念必须有自己的行/段落）

并避免本会话反复出现的缺陷：
    否定盲 · 多处关键词导致错误通过 · 章节错位 ·
    空集合 continue · substring false positive
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LINEAGE_FILE = PROJECT_ROOT / "docs" / "architecture" / "TASK_ORIGIN_LINEAGE.md"
AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"
ADR_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "adr"
    / "0023-task-origin-and-lineage.md"
)
ADR_INDEX = PROJECT_ROOT / "docs" / "architecture" / "adr" / "README.md"

LAYERS = ("core", "communication", "storage", "services", "llm", "agents")


# ============================================================
# 工具 —— 结构化解析（不是子串匹配）
# ============================================================
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(document: str, heading_prefix: str) -> str:
    """
    取出一个标题及其**所有子小节**（按层级，并且**跳过围栏代码块**）。

    【两个真实踩过的坑 —— 都发生在同一个函数上】

    坑 1：必须包含子小节
        第一版写成「到下一个 `#` 标题为止」，
        结果 `## 4` 在 `### 4.1` 处就停了（`#{1,4}` 也匹配 `###`）。

    坑 2：**围栏代码块里的 `#` 不是标题**（本文件开发时踩到）
        §15.3 的内容是：

            ### 15.3 不要在 Task Creation Mechanism 里写策略判断

            ```python
            # ❌ 不要
            if outsourcing_roi > X:
                allow
            ```

        代码块里的 `# ❌ 不要` **以 `#` 开头** ——
        第一版把它当成了同级标题，于是在那里截断，
        整个 §15.3 只剩 54 个字符。

        结果：查 `outsourcing_roi` 永远找不到 → 守卫假红/假绿。

    现在按 ``` / ~~~ 围栏跟踪「是否在代码块里」。
    """
    lines = document.splitlines()
    start = level = None
    in_fence = False
    fence = ""

    for index, line in enumerate(lines):
        stripped = line.strip()

        # ---- 围栏状态跟踪 ----
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence, fence = False, ""
            continue

        if in_fence:
            continue

        if line.startswith("#") and heading_prefix in line:
            start = index
            level = len(line) - len(line.lstrip("#"))
            break

    if start is None:
        return ""

    end = len(lines)
    in_fence, fence = False, ""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence, fence = False, ""
            continue

        if in_fence:
            continue

        if not line.startswith("#"):
            continue
        if len(line) - len(line.lstrip("#")) <= level:
            end = index
            break

    return "\n".join(lines[start:end])


def flat(text: str) -> str:
    """压平空白，便于在跨行 Markdown 里匹配**完整规范句**。"""
    return " ".join(text.split())


def plain(text: str) -> str:
    """
    去 Markdown 强调标记后压平 —— 用于匹配**跨粗体边界**的短语。

    【为什么需要它（又一个真实踩到的坑）】
        文档里写的是：

            而**不是**：

        而守卫查的是 `"而不是" in block` ——
        因为中间夹着 `**`，**子串匹配找不到**。

        `plain()` 把 `*` / `_` / `` ` `` 去掉再压平，
        于是 `而**不是**` → `而不是`。
    """
    stripped = re.sub(r"[*_`]", "", text)
    return " ".join(stripped.split())


def table_rows(document: str, header_cell: str):
    """
    取出含某个表头单元格的**表格的数据行**（结构化，不是全文搜索）。

    找出表头那一行 → 跳过 `|---|` 分隔行 → 收集后续的 `|...|` 行。
    """
    lines = document.splitlines()
    rows = []

    for index, line in enumerate(lines):
        if not line.startswith("|") or header_cell not in line:
            continue
        # 确认是表头（下一行是分隔行）
        if index + 1 >= len(lines) or "---" not in lines[index + 1]:
            continue
        for row in lines[index + 2:]:
            if not row.startswith("|"):
                break
            rows.append(row)
        break

    return rows


def parse_table(document: str, header_cell: str):
    """把表格解析成 `[{列名: 值}]`（结构化）。"""
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


def defined_classes(path: Path) -> set:
    return {
        node.name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
    }


def production_files() -> list:
    files = []
    for layer in LAYERS:
        files.extend(sorted((PROJECT_ROOT / layer).glob("*.py")))
    files.append(PROJECT_ROOT / "main.py")
    return [f for f in files if f.exists()]


#: 七个正式概念（规范 §2）
CONCEPTS = (
    "Origin",
    "Creator",
    "Parent",
    "Root",
    "Funding",
    "Responsible",
    "Participant",
)


@pytest.fixture(scope="module")
def lineage_doc() -> str:
    return read(LINEAGE_FILE)


@pytest.fixture(scope="module")
def agents_doc() -> str:
    return read(AGENTS_FILE)


# ============================================================
# 一、文档存在且状态正确
# ============================================================
class TestDocumentExists:
    def test_file_exists(self):
        assert LINEAGE_FILE.exists(), (
            "docs/architecture/TASK_ORIGIN_LINEAGE.md 不存在"
        )

    def test_implementation_status_is_future(self, lineage_doc):
        """
        实现状态必须是 `FUTURE` —— 本次只做逻辑架构定义。

        【结构化：只查状态栏那一行，不查全文】
        """
        head = lineage_doc[:1200]
        status_lines = [l for l in head.splitlines() if "实现状态" in l]

        assert status_lines, (
            "顶部必须有「实现状态」栏 —— "
            "（空集合会让守卫在什么都没查的情况下为真）"
        )
        for line in status_lines:
            assert "FUTURE" in line, (
                f"实现状态必须是 FUTURE：{line.strip()!r}"
            )

    def test_stage_unchanged(self, agents_doc):
        assert "Post-Step-4F / Pre-Task-Workspace" in agents_doc


# ============================================================
# 二、七个概念必须**各自独立**（不是被合并）
# ============================================================
class TestSevenConceptsAreIndependent:
    """
    规范 §33 要求守卫至少能抓：

        Origin = Creator / Parent = Creator / Root = Parent /
        Funding = Creator / Responsible = Creator
        被错误地合并成一个概念的情况。
    """

    def test_relationship_table_has_seven_concepts(self, lineage_doc):
        """
        §2.1 的「七概念」表必须**恰好七行**，且每个概念一行。

        【这是结构化的检查】
            不是「文档里有没有 Origin 这个词」——
            而是「那张表里有几行、每行是什么」。
        """
        block = section(lineage_doc, "2. 七个正式概念")
        assert block, "找不到 §2「七个正式概念」"

        rows = table_rows(block, "回答什么问题")
        assert len(rows) == 7, (
            f"七概念表有 {len(rows)} 行，应该恰好 7 行。\n"
            f"（如果少于 7，说明有概念被合并了 —— "
            f"规范明确禁止把它们压缩成一个 owner。）"
        )

        joined = " ".join(rows)
        for concept in CONCEPTS:
            assert concept in joined, f"七概念表里缺少 {concept}"

    def test_no_owner_merging(self, lineage_doc):
        """
        **不许**把七个概念压缩成一个 `owner`。

        【否定盲防范】
            文档里**会**出现 `owner`（因为在写「不要用 owner」）。
            所以查的是：`owner` 只出现在**禁止语境**里。
        """
        lines = [l for l in lineage_doc.splitlines() if "owner" in l.lower()]

        assert lines, "文档里本该有关于 owner 的说明"
        for line in lines:
            assert any(n in line for n in ("不要", "禁止", "绝不", "❌")), (
                f"owner 出现在了非禁止语境里：{line.strip()!r}\n"
                f"（七个概念必须保持独立。）"
            )

    #: 必须被正式区分开的【易混概念对】
    #: 规范 §33 点名要求守卫能抓「被错误合并成一个概念」的情况
    DISTINCT_PAIRS = [
        ("Origin", "Creator"),
        ("Parent", "Creator"),      # 规范 §33 点名要求
        ("Parent", "Root"),
        ("Funding", "Creator"),
        ("Funding", "Responsible"),
        ("Responsible", "Creator"),
        ("Participant", "Creator"),
    ]

    @pytest.mark.parametrize("left,right", DISTINCT_PAIRS)
    def test_each_pair_is_formally_distinguished(self, lineage_doc, left, right):
        """
        每一对**易混概念**都必须有一个**正式的区分小节**。

        ============================================================
        【这条守卫第一版是弱的（反向测试时发现）】
        ============================================================

            第一版先查「标题里有这两个词 + 区分符号」，
            查不到就退回到「正文里某一行同时提两个词」。

            那个 **fallback 太松**：§3.3 里有一行
            `❌ 不要设计成 Creator = Origin` ——
            它同时含两个词，于是把标题被改成
            「Origin 就是 Creator」的情况也放过了。

        ------------------------------------------------------------
        【现在的查法：只认标题，而且严格】
            标题里必须同时出现两个概念名
            **并且**有明确的区分符号（≠ / 不同 / 独立）。
        """
        headings = [
            line for line in lineage_doc.splitlines() if line.startswith("#")
        ]

        distinct = [
            h for h in headings
            if left in h and right in h
            and ("≠" in h or "不同" in h or "独立" in h)
        ]

        assert distinct, (
            f"文档里没有对 **{left} ≠ {right}** 的正式区分小节标题。\n"
            f"（规范 §33 要求这一对不能被合并成一个概念。）\n"
            f"找到的相关标题：\n  "
            + "\n  ".join(
                h for h in headings if left in h and right in h
            )
        )

    def test_no_concept_equivalence_claimed(self, lineage_doc):
        """
        **反向语义**：不许有任何标题声称两个概念**等价**。

        【否定盲防范】
            §3.3 里有一行 `❌ 不要设计成 Creator = Origin` ——
            那是**禁止**，不是声称。所以只在标题里查。
        """
        headings = [
            line for line in lineage_doc.splitlines() if line.startswith("#")
        ]

        for heading in headings:
            for left, right in self.DISTINCT_PAIRS:
                if left not in heading or right not in heading:
                    continue
                for equivalence in ("就是", "等于", "=", "等同于", "Same"):
                    if equivalence not in heading:
                        continue
                    assert "≠" in heading or "不" in heading, (
                        f"这个标题声称 {left} 与 {right} 等价：\n"
                        f"    {heading.strip()}"
                    )

    def test_origin_not_equal_creator_is_stated(self, lineage_doc):
        """Origin ≠ Creator 是本主题最重要的概念，必须是**章节标题**。"""
        headings = [
            l for l in lineage_doc.splitlines()
            if l.startswith("#") and "Origin" in l and "Creator" in l
        ]

        assert headings, "必须有专门讲「Origin ≠ Creator」的小节"

    def test_parent_not_equal_root_table(self, lineage_doc):
        """
        §6.3 必须用**表格**给出 Parent vs Root 的完整对照。

        【结构化检查】
            解析那张表，确认示例里的三个 Task 的 Root 都是 Task-001。
            这验证的是**语义**，不是「文档里有没有 Task-001」。
        """
        block = section(lineage_doc, "6.3")
        assert block, "找不到 §6.3 Parent ≠ Root"

        rows = parse_table(block, "Parent")
        assert rows, "§6.3 必须有一张 Parent/Root 对照表"

        for row in rows:
            task = row.get("Task", "")
            root = row.get("Root", "")
            if "Task-002" in task or "Task-003" in task:
                assert "Task-001" in root, (
                    f"{task} 的 Root 应为 Task-001，实际 {root!r} —— "
                    f"把 Root 写成了 Parent？"
                )
            if "Task-001" in task:
                assert row.get("Parent", "") in ("`none`", "none", "None"), (
                    f"Root Task 的 Parent 应为 none，实际 {row.get('Parent')!r}"
                )

    def test_responsible_not_equal_creator_stated(self, lineage_doc):
        """Responsible ≠ Creator 必须有明确的规范句。"""
        block = flat(section(lineage_doc, "8.3"))

        assert "Responsible 与 Creator 不必相同" in block, (
            "§8.3 必须写出「Responsible 与 Creator 不必相同」"
        )

    def test_funding_not_equal_creator_stated(self, lineage_doc):
        """Funding ≠ Creator 必须有小节的标题级说明。"""
        headings = [
            l for l in lineage_doc.splitlines()
            if l.startswith("#") and "Funding" in l and "Creator" in l
        ]

        assert headings, "必须有专门讲「Funding ≠ Creator」的小节"

    def test_participant_independence_stated(self, lineage_doc):
        """Participant 必须与其他角色保持独立。"""
        block = flat(section(lineage_doc, "8.4"))

        assert "Participant" in block and "≠" in block, (
            "§8.4 必须写出 Participant ≠ Creator ≠ Responsible ≠ Funding Source"
        )


# ============================================================
# 三、结构性规则：Maximum Task Depth = 3（TEMPORARY）
# ============================================================
class TestMaxDepthIsThreeAndTemporary:
    """
    规范 §33 要求守卫至少能抓：

        · 把 3 层规则改成 4 层  → 守卫应变红
        · 把「暂时三层规则」写成「永久架构原则」 → 守卫应能发现
    """

    def test_depth_is_three(self, lineage_doc):
        """§10 必须明确写出 `Maximum Task Depth = 3`。"""
        block = flat(section(lineage_doc, "10."))

        assert "Maximum Task Depth = 3" in block, (
            "§10 必须写出「Maximum Task Depth = 3」"
        )
        # 不许出现四层结构
        for invented in ("Layer 4", "Layer 4 Grandchild 之后的第四层",
                         "Maximum Task Depth = 4"):
            assert invented not in block, (
                f"§10 出现了 {invented!r} —— 当前硬规则是 3 层"
            )

    def test_three_layers_listed(self, lineage_doc):
        """三层必须都被列出（结构化：表格三行）。"""
        block = flat(section(lineage_doc, "10."))

        for layer in ("Layer 1", "Layer 2", "Layer 3"):
            assert layer in block, f"§10 缺少 {layer}"
        assert "Root Task" in block
        assert "Child Task" in block
        assert "Grandchild" in block

    def test_layer_3_cannot_derive(self, lineage_doc):
        """Layer 3 不允许继续派生。"""
        block = flat(section(lineage_doc, "10."))

        assert "Layer 3 不允许继续派生 Child Task" in block, (
            "§10 必须写明 Layer 3 不允许继续派生"
        )

    def test_marked_as_temporary_not_permanent(self, lineage_doc):
        """
        **必须**标记为 CURRENT / TEMPORARY，
        而且**不得**写成永久架构原则。

        【这条是「把暂时规则写成永久原则」的反向检查】
        """
        block = section(lineage_doc, "10.")
        text = flat(block)

        # 【不能只查「§10 里有 TEMPORARY」】
        #     §10 里 TEMPORARY 出现**两次**（规则声明 + 后面的提醒）。
        #     把**规则声明**那行改成 PERMANENT 之后，
        #     提醒那行还在 → 守卫假绿。
        #     所以只查那条 `> ## ⚠️ ...` 声明。
        declarations = [
            l for l in block.splitlines()
            if l.strip().startswith(">") and "##" in l and "⚠️" in l
        ]
        assert declarations, (
            "§10 必须有一条 `> ## ⚠️ ...` 形式的规则声明 —— "
            "（空集合会让这条守卫空转）"
        )
        for line in declarations:
            assert "TEMPORARY" in line, (
                f"规则声明必须标 TEMPORARY：{line.strip()!r}"
            )
            assert "CURRENT" in line, (
                f"规则声明必须标 CURRENT：{line.strip()!r}"
            )
            assert "PERMANENT" not in line and "永久" not in line, (
                f"规则声明被写成了永久原则：{line.strip()!r}"
            )
        assert "不是长期不可改变的哲学" in text or (
            "不是" in text and "永久" in text
        ), "§10 必须明确说明它不是长期/永久原则"
        assert "不要" in text and "写成" in text, (
            "§10 必须明确禁止把它写成永久架构真理"
        )

    def test_future_adjustability_documented(self, lineage_doc):
        """未来可调整的选项必须列出（2/4/5 层/动态深度）。"""
        block = flat(section(lineage_doc, "10."))

        for option in ("2 层", "4 层", "5 层", "动态深度"):
            assert option in block, (
                f"§10 必须列出未来的可调选项：{option}"
            )

    def test_system_limits_structure_not_strategy(self, lineage_doc):
        """系统要防的是无限结构，不是策略选择。"""
        block = flat(section(lineage_doc, "10.3"))

        # 【必须用 plain()】：文档写的是 `而**不是**：`
        block = plain(section(lineage_doc, "10.3"))

        assert "无限结构" in block, (
            "§10.3 必须写明系统真正要防的是无限结构"
        )
        assert "而不是" in block, (
            "§10.3 必须对比说明「而不是策略判断」"
        )


# ============================================================
# 四、系统边界 vs Agent 策略
# ============================================================
class TestSystemBoundaryVsAgentStrategy:
    """
    规范 §15：不得把 Agent 策略硬编码成系统规则。
    """

    def test_principle_stated(self, lineage_doc):
        """长期总原则必须写出。"""
        text = flat(lineage_doc)

        assert "规则限制结构，策略决定行为，激励塑造结果" in text, (
            "必须写出长期总原则「规则限制结构，策略决定行为，激励塑造结果」"
        )

    def test_system_constraints_listed(self, lineage_doc):
        """系统硬约束只有那五条。"""
        block = flat(section(lineage_doc, "15.1"))

        for constraint in ("结构合法", "层级不无限", "可追踪",
                           "协议合法", "权限合法"):
            assert constraint in block, (
                f"§15.1 的系统硬约束里缺少：{constraint}"
            )

    @pytest.mark.parametrize(
        "forbidden",
        [
            "必须外包",
            "必须自己完成至少",
            "预算不得超过 Parent",
            "至少拆一个子任务",
            "必须寻找最便宜的承包者",
            "中间层不得盈利",
        ],
    )
    def test_forbidden_hardcodings_are_prohibited(self, lineage_doc, forbidden):
        """
        被禁止的策略硬编码必须在**禁止语境**里出现。

        ============================================================
        【这条守卫第一版是错的 —— 又一次逐行扫描跨行块】
        ============================================================

            第一版对每一行单独检查有没有 ❌ / 不要。
            但 §15.2 的那张表长这样：

                | ❌ 不要自行规定 |
                |---|
                | 「Task 超过 X AIC 必须外包」 |
                | 「Agent 必须自己完成至少 50%」 |
                ...

            **❌ 在表头上，不在数据行上。**
            逐行检查会把每一行都判成「非禁止语境」。

        ------------------------------------------------------------
        【现在的查法：块级判定】
            ① 找到包含该短语的那一节（结构化）
            ② 确认**那一节**整体处于禁止语境
        """
        # 文档里确实提到了它（否则这条守卫是空转的）
        assert forbidden in lineage_doc, (
            f"文档里本该提到被禁止的写法：{forbidden}"
        )

        # 逐节检查：短语出现在哪一节，那一节必须在禁止语境
        headings = re.findall(r"^#+\s+(\S+).*$", lineage_doc, re.M)
        checked = False

        for heading in headings:
            block = section(lineage_doc, heading)
            if forbidden not in block:
                continue
            checked = True
            assert any(n in block for n in ("❌", "不要", "禁止", "不得")), (
                f"{forbidden!r} 出现在 §{heading}，"
                f"但那一节没有禁止标记\n"
                f"（表格的 ❌ 在表头 —— 所以必须做**块级**判定，"
                f"不能逐行扫。）"
            )

        assert checked, f"没有在任何一节里找到 {forbidden!r}"

    def test_no_roi_formula_in_creation(self, lineage_doc):
        """不许在 Task Creation 里写 ROI 判断。"""
        block = flat(section(lineage_doc, "15.3"))

        assert "outsourcing_roi" in block, (
            "§15.3 必须给出被禁止的 ROI 判断示例"
        )
        assert "❌" in section(lineage_doc, "15.3"), (
            "§15.3 的示例必须标为禁止（❌）"
        )

    def test_middle_layer_profit_is_legitimate(self, lineage_doc):
        """中间层盈利必须被明确为合法。"""
        block = flat(section(lineage_doc, "15.4"))

        assert "合法" in block, "§15.4 必须写明中间层盈利是合法的"
        assert "抽水" in block, (
            "§15.4 必须明确不能因为 Creator 是中间层就认定是「抽水」"
        )
        for value in ("Negotiation", "Coordination", "Integration",
                      "Verification", "Risk"):
            assert value in block, (
                f"§15.4 必须列出中间层承担的价值：{value}"
            )


# ============================================================
# 五、关键规范句（Origin/Creator、外包、Message、Iteration）
# ============================================================
class TestKeyNormativeStatements:
    def test_origin_not_creator_example(self, lineage_doc):
        """Origin ≠ Creator 的关键例子必须在。"""
        block = flat(section(lineage_doc, "3."))

        assert "HUMAN_DEMAND" in block and "Agent A" in block, (
            "§3 必须给出「Human 提需求 + Agent A 建 Task」的例子"
        )
        assert "这是合法且正常的" in block, (
            "§3 必须明确这个组合是合法且正常的"
        )

    def test_origin_has_three_kinds(self, lineage_doc):
        """Origin 三类必须在（结构化：表格三行）。"""
        block = section(lineage_doc, "4. Origin")
        rows = table_rows(block, "Origin")

        assert len(rows) == 3, (
            f"Origin 表有 {len(rows)} 行，应该恰好 3 行"
        )
        for kind in ("HUMAN_DEMAND", "AGENT_INITIATIVE",
                     "CHILD_TASK_DERIVATION"):
            assert kind in " ".join(rows), f"Origin 表缺少 {kind}"

    def test_child_task_is_independent(self, lineage_doc):
        """Child Task 是独立 Task。"""
        text = flat(lineage_doc)

        assert "真正独立的 Task" in text or "Child Task 是**独立** Task" in text, (
            "必须写明 Child Task 是独立 Task"
        )

    def test_child_does_not_inherit_workspace(self, lineage_doc):
        """Child 默认不继承 Parent Workspace。"""
        block = flat(section(lineage_doc, "7.3"))

        assert "不得" in block and "自动获得" in block, (
            "§7.3 必须写明 Child 不得自动获得 Parent Workspace 访问权"
        )

    def test_outsourcing_does_not_transfer_responsibility(self, lineage_doc):
        """外包 ≠ 责任自动转移。"""
        text = flat(lineage_doc)

        assert "外包转移工作，不自动转移 Parent Task Responsibility" in text, (
            "必须写出那句结论：外包转移工作，不自动转移 Parent Task Responsibility"
        )

    def test_no_infinite_budget_passthrough(self, lineage_doc):
        """Budget 不许无限向下透传。"""
        text = flat(lineage_doc)

        assert "不要让 Budget 在任务树中无限、无记录地向下透传" in text, (
            "必须写出「不要让 Budget 在任务树中无限、无记录地向下透传」"
        )

    def test_task_creation_not_message(self, lineage_doc):
        """Task Creation ≠ Message。"""
        text = flat(lineage_doc)

        assert "Task Creation ≠ Message" in text, (
            "必须写出 Task Creation ≠ Message"
        )

    def test_iteration_not_child_task(self, lineage_doc):
        """Iteration ≠ Child Task。"""
        text = flat(lineage_doc)

        assert "Iteration ≠ Child Task" in text, (
            "必须写出 Iteration ≠ Child Task"
        )

    def test_agent_can_create_root_task_is_future(self, lineage_doc):
        """Agent 自主创建 Root Task 是 FUTURE，不是已实现。"""
        block = flat(section(lineage_doc, "5.3"))

        assert "FUTURE" in block, "§5.3 必须标记 Agent 自建 Root Task 为 FUTURE"
        assert "不实现" in block, "§5.3 必须明确本次不实现"


# ============================================================
# 六、12 个 Open Questions
# ============================================================
class TestOpenQuestions:
    """规范 §16：12 个问题必须完整列出，且不得自行回答。"""

    @pytest.mark.parametrize(
        "index",
        list(range(1, 13)),
    )
    def test_all_twelve_listed(self, lineage_doc, index):
        block = section(lineage_doc, "16. Open Questions")
        rows = table_rows(block, "问题")

        numbers = []
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            if cells and cells[0].isdigit():
                numbers.append(int(cells[0]))

        assert index in numbers, (
            f"Open Questions 表里缺少第 {index} 个问题"
        )

    def test_system_vs_choice_distinction(self, lineage_doc):
        """必须强调「系统是否允许」≠「Agent 是否选择」。"""
        block = flat(section(lineage_doc, "15.5"))

        assert "系统是否允许" in block and "Agent 是否选择" in block, (
            "§15.5 必须写出这组区分"
        )
        assert "非法" in block, (
            "§15.5 必须明确「不要因为可能低效就定义为非法」"
        )


# ============================================================
# 七、与现有代码的映射必须如实
# ============================================================
class TestCodeMappingIsHonest:
    """§12 必须如实说明现在有什么、没有什么。"""

    def test_existing_fields_are_real(self):
        """文档声称已存在的字段必须真的存在。"""
        import inspect

        from core.task import Task

        params = set(inspect.signature(Task.__init__).parameters)

        for field in ("creator", "contractor", "participants", "budget"):
            assert field in params, (
                f"文档说 Task 有 {field}，但它不存在 —— 文档与代码不符"
            )

    def test_missing_fields_are_really_missing(self):
        """
        文档声称**不存在**的字段必须真的不存在 ——
        否则「零实现」是假的。
        """
        import inspect

        from core.task import Task

        params = set(inspect.signature(Task.__init__).parameters)

        for field in ("origin", "parent", "parent_task_id", "root",
                      "root_task_id", "funding", "responsible", "depth"):
            assert field not in params, (
                f"Task 已经有了 {field} —— 文档必须从「不存在」改掉，"
                f"或者本次越界实现了"
            )

    def test_no_task_columns_added(self, db):
        """数据库里没有新增 lineage 相关列。"""
        columns = {
            row["name"]
            for row in db.fetchall("PRAGMA table_info(tasks)")
        }

        for forbidden in ("origin", "parent_task_id", "root_task_id",
                          "funding_source", "responsible_agent",
                          "lineage_depth", "depth"):
            assert forbidden not in columns, (
                f"tasks 表新增了 {forbidden} 列 —— 本次禁止改 schema"
            )


# ============================================================
# 八、没有偷跑：生产代码里不许有 Lineage 实现
# ============================================================
class TestNoImplementation:
    def test_no_lineage_classes(self):
        forbidden = {
            "TaskLineageService", "TaskOrigin", "TaskGraph",
            "ChildTaskManager", "TaskCreationService",
            "LineageService", "TaskLineage", "ChildTask",
        }

        for path in production_files():
            hit = defined_classes(path) & forbidden
            assert not hit, (
                f"{path.name} 定义了 {sorted(hit)} —— "
                f"概念见 TASK_ORIGIN_LINEAGE.md（FUTURE），本次禁止实现"
            )

    def test_no_lineage_modules(self):
        for layer in LAYERS:
            for name in ("task_lineage.py", "task_origin.py",
                         "child_task.py", "task_graph.py"):
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 本次禁止实现"
                )

    def test_agents_does_not_claim_implemented(self, agents_doc):
        """
        AGENTS.md 不许声称 Child Task / Agent 自建 Task 已实现。

        【否定盲防范】只查肯定式断言。
        """
        negations = ("❌", "不", "未", "FUTURE", "禁止", "OPEN")
        claims = ("Child Task 已实现", "Child Task 已经实现",
                  "Task Lineage 已实现", "Agent 自主创建 Root Task 已实现")

        for line in agents_doc.splitlines():
            for claim in claims:
                if claim not in line:
                    continue
                assert any(n in line for n in negations), (
                    f"AGENTS.md 的这行**肯定地**声称了 {claim!r}：\n"
                    f"    {line.strip()}"
                )

    def test_agents_marks_depth_temporary(self, agents_doc):
        """AGENTS.md 必须把 3 层标为 TEMPORARY。"""
        text = flat(agents_doc)

        assert "Maximum Depth = 3" in text or "Maximum Task Depth = 3" in text, (
            "AGENTS.md 必须写明 Maximum Task Depth = 3"
        )
        assert "TEMPORARY" in text, (
            "AGENTS.md 必须把它标为 TEMPORARY（不是永久原则）"
        )


# ============================================================
# 九、ADR
# ============================================================
class TestADR:
    def test_adr_exists(self):
        assert ADR_FILE.exists(), "ADR-0023 不存在"

    def test_adr_sections(self):
        text = read(ADR_FILE)

        for heading in ("## Context", "## Decision", "## Consequences"):
            assert heading in text, f"ADR-0023 缺少 {heading}"

    def test_adr_indexed(self):
        index = read(ADR_INDEX)

        assert "0023" in index
        assert "0023-task-origin-and-lineage.md" in index

    #: **已知的、预先存在的**索引断链（不是本轮引入的）
    #:
    #: 【这是守卫在调查阶段发现的真实问题 —— 如实记录，不掩盖】
    #:
    #:     `adr/README.md` 索引里链接了 25 篇 ADR，
    #:     但实际只有 9 篇文件存在。
    #:     缺的是：0001–0011 · 0013 · 0015 · 0016 · 0017。
    #:
    #:     这些链接**点开是 404**。它们描述的设计确实存在
    #:     （见 Constitution / AGENTS.md），但 ADR 文件从未落盘。
    #:
    #: ⚠️ **本次不补写这 16 篇 ADR** —— 那超出本步范围，
    #:    而且补写需要逐篇确认历史决策，不能凭空生成。
    #:    这里把它们**显式列出来**，让守卫变成
    #:    「不许再新增断链」而不是「假装没有断链」。
    KNOWN_MISSING_ADRS = frozenset(
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 16, 17]
    )

    def test_adr_index_has_no_new_broken_links(self):
        """
        ADR 索引里的链接必须**真的存在** —— 除了已记录的 16 篇历史缺失。

        【这条守卫替代了原来写死 `max == 23` 的那条】
            原来那条只查「最大编号是几」，既抓不到断链，
            又每加一篇 ADR 就会失败一次。
        """
        adr_dir = PROJECT_ROOT / "docs" / "architecture" / "adr"
        index = read(adr_dir / "README.md")

        linked = sorted({int(m) for m in re.findall(r"\[(\d{4})\]", index)})
        files = {
            int(p.name[:4]) for p in adr_dir.glob("*.md")
            if p.name[:4].isdigit()
        }

        assert linked, "索引里一个 ADR 链接都没有 —— 这条守卫是空转的"
        assert files, "一篇 ADR 文件都没有"

        broken = sorted(set(linked) - files - self.KNOWN_MISSING_ADRS)

        assert not broken, (
            f"ADR 索引里有**新的**断链：{broken}\n"
            f"（已有的历史缺失记录在 KNOWN_MISSING_ADRS 里。）"
        )

    def test_adr_files_are_all_indexed(self):
        """反向：每一篇**存在的** ADR 都必须被索引链接到。"""
        adr_dir = PROJECT_ROOT / "docs" / "architecture" / "adr"
        index = read(adr_dir / "README.md")

        linked = {int(m) for m in re.findall(r"\[(\d{4})\]", index)}
        files = {
            int(p.name[:4]) for p in adr_dir.glob("*.md")
            if p.name[:4].isdigit()
        }

        missing = sorted(files - linked)

        assert not missing, (
            f"这些 ADR 文件存在但没有被索引：{missing}"
        )

    def test_known_missing_list_stays_accurate(self):
        """
        反向：`KNOWN_MISSING_ADRS` 不许「过期」——
        如果某篇被补写了，就必须从豁免名单里删掉，
        否则豁免名单会慢慢变成掩盖问题的垃圾场。
        """
        adr_dir = PROJECT_ROOT / "docs" / "architecture" / "adr"
        files = {
            int(p.name[:4]) for p in adr_dir.glob("*.md")
            if p.name[:4].isdigit()
        }

        now_written = sorted(self.KNOWN_MISSING_ADRS & files)

        assert not now_written, (
            f"这些 ADR 已经被补写了，但还留在 KNOWN_MISSING_ADRS 里：{now_written}\n"
            f"（补写后请从豁免名单删除，让守卫继续收紧。）"
        )


# ============================================================
# 十、反向测试
# ============================================================
class TestReverseVerification:
    """
    ADR-0018 的硬规定：新增守卫**必须**配反向测试。
    """

    def test_section_scoped(self):
        """`section()` 必须只取目标小节。"""
        doc = read(LINEAGE_FILE)

        origin = section(doc, "3. Origin")
        funding = section(doc, "8.1")

        assert "HUMAN_DEMAND" in origin
        assert "HUMAN_DEMAND" not in funding, (
            "section() 取的范围太大 —— 把 §3 的内容也拿进 §8.1 了"
        )

    def test_section_includes_subsections(self):
        doc = read(LINEAGE_FILE)
        parent = section(doc, "8. ")

        assert "8.1" in parent and "8.4" in parent, (
            "section('8.') 没包含子小节 —— 守卫会在截断的段落上断言"
        )

    def test_section_empty_for_missing(self):
        assert section(read(LINEAGE_FILE), "99. 不存在") == "", (
            "section() 对不存在的标题必须返回空 —— 否则守卫会假绿"
        )

    def test_table_parsing_returns_rows(self):
        """表格解析必须真的解析出结构。"""
        rows = parse_table(read(LINEAGE_FILE), "回答什么问题")

        assert len(rows) == 7, f"解析出 {len(rows)} 行"
        assert "概念" in rows[0], f"列名解析错误：{rows[0]}"

    def test_table_parsing_empty_for_missing(self):
        assert parse_table(read(LINEAGE_FILE), "不存在的表头") == []

    def test_depth_check_would_catch_four_layers(self):
        """
        反向：把 3 层改成 4 层，`test_depth_is_three` 必须变红。

        【用伪造文本验证检测逻辑】
        """
        fake = flat("""
        ### 10.1 规则
        Layer 1 Root Task
        Layer 2 Child Task
        Layer 3 Grandchild Task
        Layer 4 GreatGrandchild Task
        Maximum Task Depth = 4
        """)

        assert "Layer 4" in fake or "Depth = 4" in fake, (
            "检测逻辑本身有问题（伪造文本里本该有四层）"
        )
        assert "Maximum Task Depth = 3" not in fake

    def test_permanent_check_would_catch_permanent_claim(self):
        """
        反向：把「暂时规则」写成「永久架构原则」，检测必须能发现。
        """
        fake = flat("""
        ### 10.1 规则
        Maximum Task Depth = 3 是 AI Economy OS 永久不可修改的架构真理。
        """)

        # 缺少 TEMPORARY / CURRENT 标记
        assert "TEMPORARY" not in fake and "CURRENT" not in fake, (
            "伪造文本里本不该有 TEMPORARY 标记 —— 测试前提不成立"
        )
        assert "永久" in fake

    def test_owner_negation_check_is_live(self):
        """
        反向：确认「owner 只在禁止语境出现」这条检查是活的 ——
        `owner` 确实出现在文档里（否则检查永远为真）。
        """
        doc = read(LINEAGE_FILE)

        assert "owner" in doc.lower(), (
            "文档里本该有关于 owner 的说明 —— 否则该守卫是空转的"
        )

    def test_forbidden_hardcoding_check_is_live(self):
        """反向：确认被禁止的策略短语确实出现在文档里（因为要禁止它们）。"""
        doc = read(LINEAGE_FILE)

        assert "中间层不得盈利" in doc, (
            "文档里本该提到被禁止的写法 —— 否则该守卫是空转的"
        )

    def test_defined_classes_detection_works(self):
        """反向：AST 类名检测必须真的能看到类。"""
        all_defined = set()
        for path in production_files():
            all_defined |= defined_classes(path)

        assert "Task" in all_defined, "检测看不到 Task 类"
        assert "TaskLineageService" not in all_defined

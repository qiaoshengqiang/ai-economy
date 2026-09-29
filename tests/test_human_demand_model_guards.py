"""
Human Demand 模型守卫（Guard A–F）

============================================================
【这个文件守什么】
============================================================

本轮修正确立：

    **Human Demand ≠ Task**
    **Human ≠ Task Creator**

    Human Demand → Agent Interpretation → Agent creates Root Task
    Agent Initiative → Agent creates Root Task

风险是**文档回退**：以后有人（人或 AI）重新把
「Human 创建 Root Task」写回架构正文。

这个文件把那件事变成本会话里**会立刻变红**的错误。

============================================================
【规范要求的 Guard A–F】
============================================================

    Guard A  不允许架构正文把 Human = Task Creator 作为**正式模型**
    Guard B  必须**允许**（不误杀）：Human provides Demand /
             creates Demand / provides Feedback / participates / Force Unlock
    Guard C  必须允许 `Human Demand → Agent → Root Task`
    Guard D  必须允许 `Agent Initiative → Root Task`
    Guard E  必须理解**否定语义与历史说明**
             （「Human is NOT the Task Creator」不能被误判为旧模型）
    Guard F  **反向测试**：把旧模型写回文档 → Guard 必须失败

============================================================
【为什么不能用 substring-only】
============================================================

本项目在这一类检查上已经踩过 6+ 次假绿/误报：

    · 否定盲      —— 把「≠ Task Workspace 已实现」当成「已实现」
    · 跨行块      —— ❌ 在表头，数据行没有
    · 代码块      —— `# ❌ 不要` 被当成 Markdown 标题
    · 多处出现    —— 目标那句改了，别处还有
    · 逐行扫描    —— 跨行的禁止清单被误判成肯定式

所以本文件的判定原则是：

    **一个旧模型写法只有在「肯定式、非历史、非示例」时才违规。**
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ARCH_DIR = PROJECT_ROOT / "docs" / "architecture"
AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"

#: 架构正文（要检查旧模型的文档）
ARCH_FILES = sorted(ARCH_DIR.rglob("*.md")) + [AGENTS_FILE]

#: 规范点名的「旧模型」写法（Guard A）
OLD_MODEL_PATTERNS = (
    "Human → Root Task",
    "Human ↓ Root Task",
    "Human-created Task",
    "Human creates Root Task",
    "Human create Root Task",
    "Human is Root Task Creator",
    "Human Root Task Creator",
    "Human Task Creator",
    "Human submits Task",
    "Root Task comes directly from Human",
    "Human directly creates Root Task",
    "Human creates Task",
)

#: 必须**允许**的写法（Guard B / C / D）—— 不能被误杀
ALLOWED_PATTERNS = (
    "Human Demand",
    "Agent Interpretation",
    "Agent creates Root Task",
    "AGENT_INITIATIVE",
    "Demand Provider",
    "Feedback Provider",
    "Optional Collaborator",
    "Governance Authority",
    "Human Force Unlock",
    "Authorized Human Modification",
    "Human provides",
)

#: 说明「这是旧模型 / 已被修正 / 否定」的标记（Guard E）
#: 出现在同一行或同一个小节里 → 那是在**讲历史或否定**，不是声称
HISTORY_OR_NEGATION = (
    # ---- 否定 ----
    "≠", "不是", "NOT", "not the",
    # ---- 禁止 / 不应 ----
    #   【"不要" 是漏掉的（反向测试时发现）】
    #   测试里有 `不要写成「Human submits Task」。` ——
    #   它含 "不要"，但第一版的标记表里没有这个词，
    #   于是被误判成「肯定式旧模型」。
    "❌", "不要", "禁止", "不得", "不允许", "不应",
    # ---- 历史 / 修正 ----
    "修正", "旧模型", "已被", "不再", "撤销", "废除", "反对",
    "错误", "曾经", "历史上", "以前", "旧设计", "被精确化",
)


# ============================================================
# 工具
# ============================================================
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """去掉围栏代码块的内容（代码块里的 `#` 不是标题，也不是正文声明）。"""
    out, in_fence, fence = [], False, ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence, fence = False, ""
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def section(document: str, heading_prefix: str) -> str:
    """
    取出一个标题及其子小节，**跳过围栏代码块**。

    （与 test_task_origin_lineage_guards.py 里修好的版本一致 ——
      代码块里的 `#` 不是标题。）
    """
    lines = document.splitlines()
    start = level = None
    in_fence, fence = False, ""

    for index, line in enumerate(lines):
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


def enclosing_section(document: str, index: int) -> str:
    """取出 `index` 所在的那个小节（用于判断上下文）。"""
    before = document[:index]
    headings = [
        (m.start(), m.group(0))
        for m in re.finditer(r"^#+\s+.*$", before, re.M)
    ]
    if not headings:
        return before
    return section(document, headings[-1][1].lstrip("#").strip())


def is_affirmative(document: str, index: int, match_text: str) -> bool:
    """
    判断第 `index` 处出现的 `match_text` 是不是**肯定式声称**。

    【判定规则】（Guard E）
        只要出现处的**同一行**或**所在小节**里有
        历史/否定标记，就不算肯定式声称。
    """
    line_start = document.rfind("\n", 0, index) + 1
    line_end = document.find("\n", index)
    line = document[line_start: line_end if line_end != -1 else len(document)]

    if any(marker in line for marker in HISTORY_OR_NEGATION):
        return False

    context = enclosing_section(document, index)
    if any(marker in context for marker in HISTORY_OR_NEGATION):
        return False

    return True


def find_old_model(document: str):
    """返回文档里所有**肯定式**旧模型写法：`[(模式, 行号, 行内容)]`。"""
    hits = []
    for pattern in OLD_MODEL_PATTERNS:
        for match in re.finditer(re.escape(pattern), document):
            if not is_affirmative(document, match.start(), pattern):
                continue
            line_no = document[: match.start()].count("\n") + 1
            line = document.splitlines()[line_no - 1].strip()
            hits.append((pattern, line_no, line))
    return hits


# ============================================================
# Guard A：架构正文不允许把 Human = Task Creator 当正式模型
# ============================================================
class TestGuardA_NoHumanAsTaskCreator:
    """
    **Guard A**

    架构正文（去掉代码块）里不允许出现**肯定式**的旧模型写法。

    ⚠️ 代码块里的图示**也要查** —— 因为架构图正是旧模型最容易残留的地方。
       所以这里用**全文**（不去代码块），但用 `is_affirmative` 排除历史/否定。
    """

    @pytest.mark.parametrize(
        "path", ARCH_FILES, ids=[p.name for p in ARCH_FILES]
    )
    def test_no_affirmative_old_model(self, path):
        document = read(path)
        hits = find_old_model(document)

        assert not hits, (
            f"{path.relative_to(PROJECT_ROOT)} 里出现了**肯定式**的旧模型写法：\n"
            + "\n".join(
                f"    行 {line_no}: {line!r}  ← 模式 {pattern!r}"
                for pattern, line_no, line in hits
            )
            + "\n\n（正确的模型是：Human Demand → Agent Interpretation "
            "→ Agent creates Root Task。）"
        )

    def test_old_model_patterns_are_detectable(self):
        """
        反向：确认 `find_old_model` **真的**能发现旧模型。

        如果这条不成立，上面那条守卫就是空转的。
        """
        fake = """
        ### 5.2 Root Task 的来源
        第一版的现实默认：
        Human → Root Task
        """
        hits = find_old_model(fake)

        assert hits, "检测逻辑找不到伪造的旧模型 —— 守卫是假的"
        assert hits[0][0] == "Human → Root Task"

    def test_simple_substring_would_be_wrong(self):
        """
        反向：证明**简单 substring** 是错的 ——
        它会把这里所有「历史/否定」的写法都误判成旧模型。

        如果 `find_old_model` 退化成 substring，这条会失败。
        """
        history_lines = [
            "❌ 旧模型：Human → Root Task（**已被 ADR-0024 修正**）",
            "> 本轮修正掉了「Human creates Root Task」这个说法。",
            "Human is NOT the Task Creator.",
            "以前写的是 Human-created Task，现在不再这样写。",
        ]

        for line in history_lines:
            doc = f"### 某节\n{line}\n"
            assert not find_old_model(doc), (
                f"这一行被误判成旧模型了：{line!r}\n"
                f"（它是在讲历史或否定，不是声称。）"
            )


# ============================================================
# Guard B：必须允许 Human 的四个角色与治理操作
# ============================================================
class TestGuardB_HumanRolesAreAllowed:
    """
    **Guard B**

    这些写法**必须**存在且不能被误杀：
        Human provides Demand · creates Demand · provides Feedback ·
        participates · Force Unlock
    """

    @pytest.fixture(scope="class")
    def lineage(self) -> str:
        return read(ARCH_DIR / "TASK_ORIGIN_LINEAGE.md")

    def test_human_demand_is_documented(self, lineage):
        assert "Human Demand" in lineage, (
            "必须写明 Human Demand 这个概念"
        )

    def test_four_human_roles_are_documented(self, lineage):
        """Human 的四个角色必须都写出（结构化：表格四行）。"""
        block = section(lineage, "2.5 Human 的四个角色")
        assert block, "找不到 §2.5 Human 的四个角色"

        rows = [
            line for line in block.splitlines()
            if line.startswith("|") and "---" not in line
        ]
        # 表头 1 行 + 4 个角色
        assert len(rows) >= 5, (
            f"Human 角色表只有 {len(rows)-1} 行，应该至少 4 个角色"
        )
        for role in ("Demand Provider", "Feedback Provider",
                     "Optional Collaborator", "Governance Authority"):
            assert role in block, f"§2.5 缺少角色：{role}"

    def test_feedback_does_not_modify_task_core(self, lineage):
        """Feedback ≠ 直接修改 Task Core。"""
        block = " ".join(section(lineage, "2.5").split())

        assert "Feedback" in block
        assert "不等于直接修改 Task Core" in block or (
            "不等于" in block and "Task Core" in block
        ), "§2.5 必须写明 Feedback 不等于直接修改 Task Core"

    def test_governance_is_not_task_creation(self, lineage):
        """Governance ≠ 普通 Task Creation / Execution。"""
        block = " ".join(section(lineage, "2.5").split())

        assert "Governance 不是普通 Task Creation" in block or (
            "属于治理层" in block
        ), "§2.5 必须写明 Governance 不属于普通 Task Creation/Execution"

    def test_allowed_patterns_not_flagged(self):
        """
        反向（Guard B 的核心）：**允许清单里的写法一个都不能被标记**。
        """
        fake = "\n".join(
            f"### 某节\n{pattern} 是允许的。\n" for pattern in ALLOWED_PATTERNS
        )
        hits = find_old_model(fake)

        assert not hits, (
            f"允许的写法被误判成旧模型：{hits}"
        )


# ============================================================
# Guard C / D：两条新链路必须存在且被允许
# ============================================================
class TestGuardCD_TwoCreationPaths:
    """
    **Guard C**：`Human Demand → Agent → Root Task` 必须被允许
    **Guard D**：`Agent Initiative → Root Task` 必须被允许
    """

    @pytest.fixture(scope="class")
    def lineage(self) -> str:
        return read(ARCH_DIR / "TASK_ORIGIN_LINEAGE.md")

    def test_guard_c_human_demand_path(self, lineage):
        """Guard C：Human Demand → Agent Interpretation → Agent creates Root Task。"""
        block = " ".join(section(lineage, "2.4").split())

        assert "Human Demand" in block, "§2.4 必须写出 Human Demand"
        assert "Agent Interpretation" in block, (
            "§2.4 必须写出 Agent Interpretation 这一步"
        )
        assert "Agent creates Root Task" in block, (
            "§2.4 必须写出 Agent creates Root Task"
        )

    def test_guard_c_path_is_not_flagged(self):
        """Guard C 的链路不能被守卫误判。"""
        fake = """
        ### 正确链路
        Human Demand
              ↓
        Agent Interpretation / Structuring / Decision
              ↓
        Agent creates Root Task
        """
        assert not find_old_model(fake), "正确链路被误判成旧模型了"

    def test_guard_d_agent_initiative_path(self, lineage):
        """Guard D：Agent Initiative → Root Task 必须被允许。"""
        block = " ".join(section(lineage, "2.7").split())

        assert "AGENT_INITIATIVE" in block, "§2.7 必须写出 AGENT_INITIATIVE"
        assert "自行创建 Root Task" in block or "创建 Root Task" in block, (
            "§2.7 必须写明 Agent 可以自行创建 Root Task"
        )

    def test_two_origins_are_parallel(self, lineage):
        """两种来源必须**并列**，不是「默认 vs 例外」。"""
        block = " ".join(section(lineage, "2.7").split())

        assert "HUMAN_DEMAND" in block, "§2.7 必须列出 HUMAN_DEMAND"
        assert "不应该天然绑定 Human" in block, (
            "§2.7 必须写明 Root Task 不天然绑定 Human"
        )

    def test_demand_source_not_equal_creator(self, lineage):
        """Demand Source ≠ Creator 必须显式区分。"""
        block = " ".join(section(lineage, "2.4").split())

        assert "Demand Source" in block, "§2.4 必须引入 Demand Source"
        assert "两者不能合并" in block or "不能合并" in block, (
            "§2.4 必须写明 Demand Source 与 Creator 不能合并"
        )


# ============================================================
# Guard E：理解否定语义与历史说明
# ============================================================
class TestGuardE_UnderstandsNegationAndHistory:
    """
    **Guard E**

    「Human is NOT the Task Creator」这类句子**不能被误判**。
    同时还必须能区分：历史设计 / 当前设计 / Future / Rejected / Example。
    """

    @pytest.mark.parametrize(
        "line",
        [
            "Human is NOT the Task Creator.",
            "❌ 旧模型：Human creates Root Task",
            "本轮修正掉了「Human-created Task」这个说法。",
            "> 以前写的是 Human → Root Task，已被 ADR-0024 修正。",
            "| 旧设计 | Human is Root Task Creator | **已被修正** |",
            "不要写成「Human submits Task」。",
            "历史上曾经把 Human 当成 Task Creator。",
        ],
    )
    def test_negation_and_history_are_not_flagged(self, line):
        """否定 / 历史 / 禁止语境下的旧模型写法**不算违规**。"""
        doc = f"### 某节\n{line}\n"

        assert not find_old_model(doc), (
            f"这一行被误判成旧模型：{line!r}\n"
            f"（它含否定或历史标记，Guard E 要求放过。）"
        )

    def test_affirmative_without_marker_is_flagged(self):
        """反向：**没有**任何标记的肯定式写法**必须**被标记。"""
        doc = "### 5.2 Root Task 的来源\n第一版的现实默认：\nHuman → Root Task\n"

        hits = find_old_model(doc)
        assert hits, "肯定式的旧模型没有被标记 —— Guard A 失效了"

    def test_section_level_context_is_used(self):
        """
        小节级上下文也算 —— 标题里写了「旧模型」，
        小节内所有旧写法都应被放过。
        """
        doc = """
        ### 5.1 旧模型（已被修正）
        第一版曾经这样写：

        Human → Root Task

        Human creates Task
        """
        assert not find_old_model(doc), (
            "小节标题已声明这是旧模型，但内容仍被标记"
        )


# ============================================================
# Guard F + 其他：反向测试与结构完整性
# ============================================================
class TestGuardF_ReverseTests:
    """**Guard F** —— 把旧模型写回 → 必须失败；恢复正确模型 → 必须通过。"""

    def test_guard_f_old_model_written_back_fails(self):
        """
        Guard F（核心）：故意把旧模型写回架构正文，Guard 必须变红。
        """
        correct = """
        ### 5.2 通常来自 Human Demand

        Human（Demand Provider）
          ↓
        Demand
          ↓
        Agent（Interpretation / Structuring / Decision）
          ↓
        Root Task            ← Creator = Agent
        """
        assert not find_old_model(correct), "正确模型不该被标记"

        regressed = correct.replace(
            "Root Task            ← Creator = Agent",
            "Root Task            ← Creator = Human",
        ).replace(
            "Human（Demand Provider）",
            "Human",
        ).replace(
            "Demand\n          ↓\n        Agent（Interpretation / Structuring / Decision）\n          ↓\n",
            "",
        )
        regressed += "\n\nHuman → Root Task\n"

        hits = find_old_model(regressed)
        assert hits, (
            "把旧模型写回去之后 Guard 没有变红 —— Guard F 失效"
        )

    def test_guard_f_restore_passes_again(self):
        """恢复正确模型后，Guard 必须重新通过。"""
        correct = read(ARCH_DIR / "TASK_ORIGIN_LINEAGE.md")
        assert not find_old_model(correct), (
            "当前文档里还有肯定式的旧模型"
        )

    def test_code_block_headings_do_not_truncate_sections(self):
        """
        反向：`section()` 必须跳过围栏代码块里的 `#`。

        （另一个守卫文件里踩过的坑：§15.3 的 ```python 块里
          有 `# ❌ 不要`，被当成了标题，整节被截断。）

        ⚠️ **必须 dedent** —— 第一版把文档写在缩进的 docstring 里，
        于是 `        ### 1.1 某节` 并不以 `#` 开头，
        `section()` 根本找不到标题，返回空串。
        那会让这条测试**永远失败**，而不是验证围栏逻辑。
        """
        doc = textwrap.dedent("""
            ### 1.1 某节

            ```python
            # ❌ 不要
            x = 1
            ```

            这一行必须还在。
            ### 1.2 下一节
            ### 1.3 别的
        """).strip()
        block = section(doc, "1.1")

        assert "这一行必须还在" in block, (
            "section() 在代码块里的 `#` 处截断了"
        )
        assert "1.2 下一节" not in block, (
            "section() 没有在下一个标题处停下"
        )

    def test_section_scoping(self):
        """`section()` 必须只取目标小节。"""
        doc = read(ARCH_DIR / "TASK_ORIGIN_LINEAGE.md")

        a = section(doc, "2.4")
        b = section(doc, "2.7")

        assert "Agent Interpretation" in a
        assert "AGENT_INITIATIVE" in b
        assert "AGENT_INITIATIVE" not in a, (
            "section() 范围太大 —— 把 §2.7 也拿进 §2.4 了"
        )


# ============================================================
# 其余：ADR 记录 + 不变的部分
# ============================================================
class TestADRTrail:
    """规范要求：**不要静默覆盖**旧 ADR，要保留演进痕迹。"""

    def test_adr_0024_exists(self):
        assert (ARCH_DIR / "adr" / "0024-human-demand-not-task-creator.md").exists(), (
            "ADR-0024 不存在"
        )

    def test_adr_0023_is_annotated_not_silently_overwritten(self):
        """
        ADR-0023 必须**保留原文**并标注「已被精确化」——
        不能把旧决策内容悄悄删掉。
        """
        text = read(ARCH_DIR / "adr" / "0023-task-origin-and-lineage.md")

        assert "ADR-0024" in text, (
            "ADR-0023 里没有指向 ADR-0024 的修正标注"
        )
        assert "精确化" in text or "修正" in text, (
            "ADR-0023 必须标注它的表述已被精确化"
        )
        # 历史内容仍在（没有被静默覆盖）
        assert "Child Task 是**独立** Task" in text, (
            "ADR-0023 的原有决策内容被删掉了 —— 应该保留并标注"
        )

    def test_adr_index_updated(self):
        index = read(ARCH_DIR / "adr" / "README.md")

        assert "0024" in index, "ADR 索引没有 0024"
        assert "0024-human-demand-not-task-creator.md" in index

    def test_adr_numbering_continuous(self):
        numbers = sorted(
            int(p.name[:4])
            for p in (ARCH_DIR / "adr").glob("*.md")
            if p.name[:4].isdigit()
        )
        assert max(numbers) == 24, f"最大 ADR 编号是 {max(numbers)}，应为 24"


class TestUnchangedModelsPreserved:
    """规范要求保持不变的部分，必须还在。"""

    @pytest.fixture(scope="class")
    def lineage(self) -> str:
        return read(ARCH_DIR / "TASK_ORIGIN_LINEAGE.md")

    def test_child_task_is_still_independent(self, lineage):
        assert "真正独立的 Task" in lineage or "Child Task 是**独立** Task" in lineage

    def test_task_creation_not_message(self, lineage):
        assert "Task Creation ≠ Message" in lineage

    def test_iteration_not_child_task(self, lineage):
        assert "Iteration ≠ Child Task" in lineage

    def test_max_depth_still_temporary(self, lineage):
        assert "TEMPORARY" in lineage, "最大三层必须仍标 TEMPORARY"

    def test_human_not_task_manager(self, lineage):
        """新增原则：Human 不应成为 Task Manager。"""
        block = " ".join(section(lineage, "2.6").split())

        assert "不负责持续拆解" in block or "Human 不应该成为 Task Manager" in block
        for forbidden in ("Human Task Manager", "Human Task Coordinator",
                          "Human Central Scheduler"):
            assert forbidden in block, (
                f"§2.6 必须明确禁止：{forbidden}"
            )

    def test_os_limits_structure_agent_decides_strategy(self, lineage):
        """OS 限制结构 / Agent 决定策略 的划分必须还在。"""
        block = " ".join(section(lineage, "2.8").split())

        assert "OS 负责限制结构" in block or "OS 限制结构" in block
        assert "Agent 决定策略" in block


class TestNoProductionImplementation:
    """本轮不实现 Demand / Origin / Lineage。"""

    def test_no_demand_or_lineage_modules(self):
        for layer in ("core", "services", "storage", "communication"):
            for name in ("demand.py", "task_lineage.py", "task_origin.py",
                         "demand_service.py"):
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 本轮禁止实现"
                )

    def test_no_demand_tables(self, db):
        names = " ".join(db.table_names())

        for forbidden in ("demands", "demand_records", "task_lineage",
                          "task_origins"):
            assert forbidden not in names.split(), (
                f"数据库里出现了 {forbidden} 表 —— 本轮禁止实现"
            )

    def test_no_new_task_columns(self, db):
        columns = {
            row["name"]
            for row in db.fetchall("PRAGMA table_info(tasks)")
        }

        for forbidden in ("origin", "demand_source", "parent_task_id",
                          "root_task_id", "funding_source",
                          "responsible_agent"):
            assert forbidden not in columns, (
                f"tasks 表新增了 {forbidden} 列 —— 本轮禁止改 schema"
            )

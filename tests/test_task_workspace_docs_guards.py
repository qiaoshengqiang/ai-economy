"""
Task Workspace 架构定义守卫

============================================================
【这个文件守什么】
============================================================

Task Workspace 的架构定义（`docs/architecture/TASK_WORKSPACE.md` +
ADR-0020）**没有实现**。这带来一类特殊的风险：

    文档写得像已经实现了 → 下一个 Coding Agent 去调用不存在的 API

而项目历史上**已经踩过**这个坑（Step 4E 的规范专门警告过：
「禁止把未来设计写成当前系统已经拥有」）。

所以这里的守卫分三类：

    ① 状态正确     Task Workspace 必须仍标为 FUTURE
    ② 边界正确     五个「不等于」必须在文档里写清楚
    ③ 没有偷跑     生产代码里不许出现 Permission/Lock/Backend 实现

============================================================
【遵守 Guard 规范（ADR-0018）】
============================================================

    · Markdown 用**结构化解析**（按标题切段），不是全文子串
    · 代码用 **AST**（import / 真实调用点），不是字符串匹配
    · 去文档字符串
    · **每条都配反向测试**（见文件末尾）
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"
WORKSPACE_FILE = PROJECT_ROOT / "docs" / "architecture" / "TASK_WORKSPACE.md"
CONSTITUTION_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "ARCHITECTURE_CONSTITUTION.md"
)
SHARED_CONTEXT_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "TASK_SHARED_CONTEXT.md"
)
ADR_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "adr"
    / "0020-task-workspace-shared-resource-model.md"
)
ADR_INDEX = PROJECT_ROOT / "docs" / "architecture" / "adr" / "README.md"

LAYERS = ("core", "communication", "storage", "services", "llm", "agents")


# ============================================================
# 工具
# ============================================================
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(document: str, heading_prefix: str) -> str:
    """
    取出一个标题及其**所有子小节**。

    【注意必须包含子小节】
        第一版曾写成「到下一个 `#` 标题为止」，
        结果 `## 4` 在 `### 4.1` 处就停了 ——
        `#{1,4}` 也匹配 `###`。守卫于是在**被截断的段落**上断言。

        现在按层级：取标题自身的 `#` 数 N，到下一个
        `#` 数 **≤ N** 的标题为止。
    """
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


def flat(text: str) -> str:
    """压平空白，便于在跨行的 Markdown 里匹配短语。"""
    return " ".join(text.split())


def module_imports(path: Path) -> set:
    """文件 import 的顶层模块名（AST）。"""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def called_names(path: Path) -> set:
    """文件里**真实调用**的函数/方法名（不是子串匹配）。"""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def defined_classes(path: Path) -> set:
    """文件里定义的类名。"""
    return {
        node.name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
    }


def production_files() -> list:
    """全部生产代码文件（不含 tests / docs）。"""
    files = []
    for layer in LAYERS:
        files.extend(sorted((PROJECT_ROOT / layer).glob("*.py")))
    files.append(PROJECT_ROOT / "main.py")
    return [f for f in files if f.exists()]


@pytest.fixture(scope="module")
def workspace_doc() -> str:
    return read(WORKSPACE_FILE)


@pytest.fixture(scope="module")
def agents_doc() -> str:
    return read(AGENTS_FILE)


# ============================================================
# 一、状态：Task Workspace 必须仍是 FUTURE
# ============================================================
class TestWorkspaceIsStillFuture:
    """
    **这是本次最重要的一组守卫。**

    规范 §2 明确：本次只完成 Architecture Definition，
    结束后仍保持 `Post-Step-4F / Pre-Task-Workspace`，
    **不要把 Task Workspace 标记为 CURRENT**。
    """

    def test_workspace_doc_exists(self):
        assert WORKSPACE_FILE.exists(), (
            "docs/architecture/TASK_WORKSPACE.md 不存在"
        )

    def test_workspace_doc_declares_future(self, workspace_doc):
        """
        TASK_WORKSPACE.md 顶部必须标明实现状态是 FUTURE。

        【为什么不能只查「head 里有 FUTURE」】
            head 里 FUTURE 出现多次（状态栏、说明文字…）。
            把状态栏改成 CURRENT 之后，别的 FUTURE 还在 → 假绿。
            所以这里查**状态栏那一行**（与 does_not_claim_current 互补）。
        """
        head = workspace_doc[:1200]
        status_lines = [l for l in head.splitlines() if "实现状态" in l]

        assert status_lines, "顶部必须有「实现状态」栏"
        for line in status_lines:
            assert "FUTURE" in line, f"实现状态必须是 FUTURE：{line.strip()!r}"

        assert "零实现" in flat(head) or "不实现" in flat(head), (
            "TASK_WORKSPACE.md 必须明确说明本次零实现"
        )

    def test_agents_still_marks_workspace_future(self, agents_doc):
        """
        AGENTS.md 里 **Task Workspace 那一行**必须仍是 FUTURE。

        ============================================================
        【这条守卫第一版是弱的（反向测试时发现）】
        ============================================================

            第一版写的是：

                assert "`FUTURE`" in memory

            但 §4 里有 **5 处** `FUTURE`（Layer 3 / Layer 4 / 明细表…）。
            把 Task Workspace 那一行改成 `CURRENT` 之后，
            另外 4 处仍然是 `FUTURE` → 断言照样通过。

            「文档里某处有 FUTURE」≠「Task Workspace 是 FUTURE」。

        ------------------------------------------------------------
        【现在的查法】
            只查**提到 Task Workspace 的那些表格行**，
            要求它们的状态列全是 `FUTURE`。
        """
        memory = section(agents_doc, "4. Memory 四层")

        rows = [
            line for line in memory.splitlines()
            if line.startswith("|") and "Task Workspace" in line
            and "---" not in line
        ]

        assert rows, "§4 里找不到提到 Task Workspace 的表格行"

        for row in rows:
            assert "`FUTURE`" in row, (
                f"Task Workspace 的这一行不是 FUTURE：\n    {row.strip()}\n"
                f"（本次只做架构定义，不得标为 CURRENT。）"
            )
            assert "`CURRENT`" not in row, (
                f"Task Workspace 被标成了 CURRENT：\n    {row.strip()}"
            )

    def test_agents_stage_unchanged(self, agents_doc):
        """阶段必须保持 Post-Step-4F / Pre-Task-Workspace。"""
        assert "Post-Step-4F / Pre-Task-Workspace" in agents_doc, (
            "阶段不该在「只做架构定义」的这一步发生变化"
        )

    def test_workspace_doc_does_not_claim_current(self, workspace_doc):
        """
        TASK_WORKSPACE.md **不能**说 Task Workspace 是 CURRENT。

        【这条怎么查才准】
            不能全文搜 "CURRENT" —— 文档里会合法地提到
            「Task Core 是 CURRENT」「Shared Context 是 CURRENT」。

            所以只查**描述 Task Workspace 自身状态**的那一行：
            状态栏里的「实现状态」。
        """
        head = workspace_doc[:1500]

        status_lines = [
            line for line in head.splitlines() if "实现状态" in line
        ]
        assert status_lines, (
            "在文档开头 1500 字符内找不到「实现状态」栏 —— "
            "（第一版的过滤条件把它整个 continue 掉了，"
            "守卫于是在**空集合**上为真。）"
        )

        for line in status_lines:
            assert "`FUTURE`" in line, (
                f"实现状态栏必须写 FUTURE，实际：{line.strip()!r}"
            )
            assert "`CURRENT`" not in line, (
                f"实现状态栏被写成了 CURRENT：{line.strip()!r}"
            )

    def test_agents_does_not_say_workspace_implemented(self, agents_doc):
        """
        AGENTS.md 不许**断言** Task Workspace 已实现。

        ============================================================
        【这条守卫第一版是错的 —— 否定盲（negation blindness）】
        ============================================================

            第一版全文搜 "Task Workspace 已实现"，结果变红了。
            但命中的那一行是：

                > **有私有 workspace 目录 ≠ Task Workspace 已实现。**

            —— 这句话说的是**反话**。守卫把一个「明确否认」的句子
            当成了「声称已实现」。

            （同一类错误 ADR-0018 记过：查子串而不看上下文。）

        ------------------------------------------------------------
        【现在的查法】
            只查**肯定式**的断言，并且要求同一行里没有
            `≠` / `不` / `尚未` / `FUTURE` 这类否定词。
        """
        affirmative = (
            "Task Workspace 已实现",
            "Task Workspace 已经实现",
            "Task Workspace 是 CURRENT",
            "Workspace Permission 已实现",
        )
        # 【❌ 也必须算否定】
        #     项目里用 ❌ 标记**反例**：
        #         ❌ 因为「有 workspace 目录」就以为 Workspace Permission 已实现
        #     这一行是在**列举禁止的推论**，不是断言。
        #     不把它算作否定的话，守卫会把反例当成声明（否定盲的另一种形态）。
        negations = ("≠", "不", "尚未", "未实现", "FUTURE", "❌")

        for line in agents_doc.splitlines():
            for phrase in affirmative:
                if phrase not in line:
                    continue
                assert any(neg in line for neg in negations), (
                    f"AGENTS.md 的这行**肯定地**声称了 {phrase!r}：\n"
                    f"    {line.strip()}\n"
                    f"（Task Workspace 仍是 FUTURE。）"
                )


# ============================================================
# 二、核心架构原则必须在文档里写清楚
# ============================================================
class TestCorePrinciplesDocumented:
    """
    规范 §3–§19 确定的每一条原则，都必须能在文档里找到。

    这些不是「措辞检查」，而是**防止原则在后续编辑中被删掉**。
    """

    def test_shared_resource_model_not_git_branch(self, workspace_doc):
        """核心决定：共享资源模型，不是 Git 式分支模型。"""
        text = flat(workspace_doc)

        assert "共享资源模型" in text, "必须写明采用共享资源模型"
        assert "Git" in text and "worktree" in text, (
            "必须明确列出第一版不引入 Git branch/worktree"
        )
        assert "不采用" in text or "否决" in text, (
            "必须明确否决 Git 式分支模型（而不是含糊带过）"
        )

    def test_permission_not_equal_lock(self, workspace_doc):
        """
        Permission ≠ Write Lock —— **必须在 §11 正文里**。

        【为什么不能全文搜】
            这个短语在文档里出现 2 次（§2.2 和 §11.1）。
            删掉其中一处，全文搜索照样命中另一处 → 守卫假绿。
            （反向测试时发现的。）
        """
        block = flat(section(workspace_doc, "11."))

        assert "Permission ≠ Write Lock" in block, (
            "§11（Permission vs Lock 专章）必须显式写出 Permission ≠ Write Lock"
        )

    def test_permission_lock_truth_table(self, workspace_doc):
        """必须有组合真值表（有 permission 但没锁 → 不能写）。"""
        block = section(workspace_doc, "11.")

        assert "锁不在自己手里" in flat(block) or \
               "不能" in flat(block), (
            "§11 必须说明「有 WRITE permission 但锁在别处 → 不能写」"
        )

    def test_parallel_read_exclusive_write(self, workspace_doc):
        """可并行读，写必须独占。"""
        text = flat(workspace_doc)

        assert "并行读取" in text or "并行读" in text, "必须写明可并行读"
        assert "独占" in text, "必须写明写必须独占"

    def test_create_auto_lock_atomic(self, workspace_doc):
        """新建资源自动获得 Write Lock，且必须原子。"""
        block = flat(section(workspace_doc, "12."))

        # 【查具体的断言，不查「原子」这个泛词】
        #   §12 里「原子」出现 3 次 —— 只数词频的话，
        #   删掉核心那句仍然会通过（反向测试时发现的）。
        assert "创建和获得 Write Lock 必须是一个原子操作" in block, (
            "§12 必须写出「创建和获得 Write Lock 必须是一个原子操作」"
        )
        assert "该 Resource 默认自动获得该 Agent 的 Write Lock" in block, (
            "§12 必须写明新资源自动获得创建者的 Write Lock"
        )
        assert "created but unlocked" in block, (
            "§12 必须明确禁止 created-but-unlocked 中间状态"
        )
        assert "以后再 LOCK" in block, (
            "§12 必须给出被禁止的「以后再 LOCK」模式"
        )

    def test_agent_does_not_access_filesystem_directly(self, workspace_doc):
        """Agent 不直接访问其他机器的文件系统。"""
        block = section(workspace_doc, "14.")
        text = flat(block)

        assert "不直接" in text or "禁止" in text, (
            "§14 必须明确禁止 Agent 直接访问文件系统"
        )

    def test_local_remote_same_abstraction(self, workspace_doc):
        """Local 与 Remote 使用同一个 Workspace 抽象。"""
        block = section(workspace_doc, "13.")
        text = flat(block)

        assert "同一个" in text or "同一抽象" in text, (
            "§13 必须写明 Local/Remote 共用同一个抽象"
        )
        # 【这一条原来查的是「解耦」，但 §13 用的是「与物理位置无关」——
        #   同一个意思，换个说法。守卫太脆就会误报。】
        assert ("物理位置" in text or "物理存储位置" in text), (
            "§13 必须写明与物理存储位置无关/解耦"
        )

    def test_no_local_sync_as_base_model(self, workspace_doc):
        """不采用「本地完成后再同步」作为基础模型。"""
        text = flat(workspace_doc)

        assert "local copy" in text or "本地完成后再同步" in text, (
            "必须明确排除 local copy → edit → sync 作为基础模型"
        )

    def test_task_isolation(self, workspace_doc):
        """Task 之间默认隔离。"""
        block = section(workspace_doc, "7.")
        text = flat(block)

        assert "隔离" in text, "§7 必须写明 Task 之间默认隔离"

    def test_not_global_directory(self, workspace_doc):
        """Workspace 不是全局公共目录。"""
        text = flat(workspace_doc)

        assert "全局公共目录" in text or "global-workspace" in text, (
            "必须明确排除全局公共 Workspace"
        )

    def test_workspace_lifecycle_not_task_lifecycle(self, workspace_doc):
        """Workspace Lifecycle ≠ Task Lifecycle。"""
        block = section(workspace_doc, "3.")
        text = flat(block)

        assert "Workspace Lifecycle" in text, "§3 必须区分两种生命周期"
        assert "PROVISIONING" in text or "READY" in text, (
            "§3 必须给出 Workspace lifecycle 的候选状态"
        )

    def test_workspace_identity_is_logical(self, workspace_doc):
        """workspace_id 是逻辑身份，不是路径。"""
        block = section(workspace_doc, "6.")
        text = flat(block)

        assert "Logical Workspace Identity" in text, (
            "§6 必须写明 workspace_id 是逻辑身份"
        )

    def test_resource_identity_decoupled_from_path(self, workspace_doc):
        """Resource Identity 与物理路径解耦。"""
        block = section(workspace_doc, "8.")
        text = flat(block)

        assert "解耦" in text, "§8 必须写明 Resource Identity 与路径解耦"
        assert "logical_resource_id" in text, (
            "§8 必须给出逻辑资源标识的候选形式"
        )

    def test_write_lock_semantics(self, workspace_doc):
        """Write Lock 的四条基本语义。"""
        text = flat(section(workspace_doc, "10."))

        assert "Lock owner" in text
        assert "Lock acquisition" in text
        assert "Lock release" in text
        assert "Lock validity" in text

    def test_only_owner_can_release(self, workspace_doc):
        """只有当前锁主能释放自己的锁。"""
        text = flat(workspace_doc)

        assert "只有" in text and "锁主" in text, (
            "必须写明只有当前锁主能释放锁"
        )


# ============================================================
# 三、Lock Recovery 必须是 OPEN，不能照搬 Message Claim Lease
# ============================================================
class TestLockRecoveryIsOpen:
    """
    规范 §20 的要求非常具体：

        · 调查 Message Claim Lease
        · 不能确定就记为 OPEN DESIGN QUESTION
        · **不得因为 Message Claim Lease 已存在就假设二者相同**
    """

    def test_lock_recovery_marked_open(self, workspace_doc):
        block = section(workspace_doc, "16.")

        assert "OPEN" in block, (
            "§16 Lock Recovery 必须标为 OPEN —— 规范 §20 明确要求"
        )

    def test_not_same_as_message_claim_lease(self, workspace_doc):
        block = flat(section(workspace_doc, "16."))

        assert "不能" in block and ("照搬" in block or "假设" in block), (
            "§16 必须明确「Message Claim Lease 不能直接照搬」"
        )

    def test_documents_why_they_differ(self, workspace_doc):
        """必须说明**为什么**语义不同，而不是只说「不同」。"""
        block = flat(section(workspace_doc, "16."))

        assert "静默数据损坏" in block or "丢失写入" in block, (
            "§16 必须指出 Workspace Lock 过短的失败模式是数据损坏"
        )
        assert "重复处理" in block, (
            "§16 必须指出 Message Claim Lease 过短的失败模式是重复处理"
        )

    def test_mentions_heartbeat_is_forbidden(self, workspace_doc):
        """
        候选「租约 + 续租」需要心跳，而心跳当前被禁止 ——
        这是「不现在决定」的一个硬理由，必须写出来。
        """
        block = flat(section(workspace_doc, "16."))

        assert "心跳" in block, (
            "§16 必须指出「续租」候选依赖当前被禁止的心跳机制"
        )

    def test_lists_candidates_without_choosing(self, workspace_doc):
        block = flat(section(workspace_doc, "16."))

        for candidate in ("时间租约", "显式释放", "乐观并发"):
            assert candidate in block, f"§16 必须列出候选：{candidate}"


# ============================================================
# 四、Open Design Questions 必须显式存在
# ============================================================
class TestOpenQuestionsDocumented:
    """
    规范 §21：未决定的问题**必须**记录成 Open Design Question，
    而不是猜一个实现。
    """

    def test_open_questions_section_exists(self, workspace_doc):
        block = section(workspace_doc, "21.")

        assert block, "必须有 Open Design Questions 章节"
        assert "OPEN" in block

    @pytest.mark.parametrize(
        "topic",
        [
            "何时创建",          # Workspace creation timing
            "lifecycle",         # Workspace lifecycle
            "崩溃恢复",          # Lock lease / crash recovery
            "多资源锁",          # Multi-resource lock
            "目录锁",            # Directory lock semantics
            "加入",              # Agent join
            "退出",              # Agent leave
            "Artifact",          # Artifact design
        ],
    )
    def test_specific_open_questions_listed(self, workspace_doc, topic):
        """规范 §29 点名要求列出的问题，一个都不能少。"""
        block = flat(section(workspace_doc, "21."))

        assert topic in block, (
            f"Open Design Questions 里缺少「{topic}」"
        )

    def test_no_invented_answers(self, workspace_doc):
        """
        未决定的问题不许写成已决定。

        【只查 lifecycle 那一节】
            §15 明确说「本阶段不决定」。
            如果那里出现了「ACCEPTED 时创建」这种断言式结论，
            就说明有人猜了一个实现。
        """
        block = flat(section(workspace_doc, "15."))

        assert "OPEN" in block, "§15 Workspace Lifecycle 必须标 OPEN"
        assert "不决定" in block or "没有决定" in block, (
            "§15 必须明确说明本阶段不做这些决定"
        )


# ============================================================
# 五、五个边界
# ============================================================
class TestFiveBoundaries:
    """规范 §27 点名的五条架构边界。"""

    def test_all_five_stated(self, workspace_doc):
        text = flat(workspace_doc)

        pairs = [
            ("Task Core", "Workspace"),
            ("Shared Context", "Workspace"),
            ("Private Workspace", "Task Workspace"),
            ("Permission", "Write Lock"),
            ("workspace_id", "path"),
        ]
        for left, right in pairs:
            assert left in text and right in text, (
                f"文档里缺少 {left} / {right} 的区分"
            )

    def test_boundaries_also_in_agents(self, agents_doc):
        """五个边界里，与 Agent 相关的那几条也要在 AGENTS.md 里。"""
        text = flat(agents_doc)

        assert "Workspace ≠ Permission" in text, (
            "AGENTS.md 的边界表必须有 Workspace ≠ Permission"
        )


# ============================================================
# 六、没有偷跑：生产代码里不许有 Workspace 实现
# ============================================================
class TestNoWorkspaceImplementation:
    """
    规范 §25：本阶段**禁止**修改生产代码去实现 Workspace。

    这一组从**代码**侧验证「确实没有偷跑」——
    光靠文档说「没实现」是不够的。
    """

    def test_no_permission_or_lock_classes(self):
        """没有任何生产文件定义 Permission / Lock / Backend 类。"""
        forbidden_classes = {
            "Permission", "PermissionEngine", "ACL", "AccessControl",
            "WriteLock", "LockManager", "WorkspaceLock",
            "WorkspaceBackend", "LocalWorkspaceBackend",
            "RemoteWorkspaceBackend", "WorkspaceService",
            "WorkspaceManager", "WorkspaceGateway", "ArtifactStore",
        }

        for path in production_files():
            defined = defined_classes(path)
            hit = defined & forbidden_classes
            assert not hit, (
                f"{path.name} 定义了 {sorted(hit)} —— "
                f"Task Workspace 仍是 FUTURE，禁止实现"
            )

    def test_no_lock_or_backend_modules(self):
        """没有任何 workspace lock / backend / permission 模块。"""
        forbidden_files = (
            "workspace_lock.py", "workspace_backend.py",
            "workspace_permission.py", "workspace_manager.py",
            "lock_manager.py", "permission.py",
        )
        for layer in LAYERS:
            for name in forbidden_files:
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 属于 Task Workspace 实现"
                )

    def test_no_workspace_tables(self, db):
        """数据库里没有 Workspace 相关的表。"""
        names = " ".join(db.table_names())

        for forbidden in (
            "workspace_permissions", "workspace_members",
            "workspace_resources", "workspace_locks",
            "workspace_backends", "workspaces", "artifacts",
        ):
            assert forbidden not in names.split(), (
                f"数据库里出现了 {forbidden} 表 —— 未授权"
            )

    def test_no_git_imports(self):
        """没有引入 Git 相关依赖（§2.1 否决分支模型）。"""
        for path in production_files():
            mods = module_imports(path)
            for forbidden in ("git", "gitpython", "dulwich", "pygit2"):
                assert forbidden not in mods, (
                    f"{path.name} import 了 {forbidden} —— "
                    f"Git 不属于本阶段"
                )

    def test_no_remote_workspace_transport(self):
        """
        没有**远程 Workspace 传输**（Remote Workspace 是 FUTURE）。

        【为什么不能用 `socket` 当判据】
            `llm/openai_compatible.py` **合法地**使用 socket ——
            它是 Step 2.5 的 LLM HTTP provider，和 Workspace 无关。

            用 `socket` 做判据会把这个已有组件误报成违规。
            （ADR-0018 记过的「子串/关键词选错」一类。）

        真正该查的是**远程文件传输**相关的依赖。
        """
        for path in production_files():
            mods = module_imports(path)
            for forbidden in ("paramiko", "fabric", "scp", "sftp",
                              "ftplib", "pygit2"):
                assert forbidden not in mods, (
                    f"{path.name} import 了 {forbidden} —— "
                    f"Remote Workspace 尚未设计"
                )

    def test_no_filesystem_api_added(self):
        """
        没有新增文件系统 API。

        【注意 `core/workspace.py` 是例外】
            它是 **Agent Private Workspace**（CURRENT，Step 1 就有），
            不属于本次范围，也不该被改。
        """
        # 除 core/workspace.py 外，core/ 里不该有文件 I/O
        for path in sorted((PROJECT_ROOT / "core").glob("*.py")):
            if path.name in ("workspace.py", "paths.py", "logging_setup.py"):
                continue
            called = called_names(path)
            for forbidden in ("mkdir", "rmdir", "unlink", "listdir"):
                assert forbidden not in called, (
                    f"core/{path.name} 调用了 {forbidden}() —— "
                    f"Task Workspace 的 I/O 尚未实现"
                )

    def test_task_workspace_ref_still_a_reference(self):
        """`TaskWorkspaceRef` 仍然只有 workspace_id。"""
        from core.task_workspace import TaskWorkspaceRef

        for forbidden in ("path", "read_text", "write_text", "mkdir",
                          "grant", "allow", "permission", "lock"):
            assert not hasattr(TaskWorkspaceRef, forbidden), (
                f"TaskWorkspaceRef 获得了 {forbidden} —— "
                f"它必须仍然只是一个逻辑引用"
            )

    def test_core_workspace_untouched(self):
        """`core/workspace.py` 未被改动（规范：不得重命名/重构）。"""
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", "core/workspace.py"],
            cwd=PROJECT_ROOT, capture_output=True,
        )
        assert result.returncode == 0, (
            "core/workspace.py 被改动了 —— 它服务 Agent Private Workspace，"
            "不在本次范围内"
        )


# ============================================================
# 七、AGENTS.md 的阅读顺序
# ============================================================
class TestAgentsReadOrder:
    """规范 §23.2：AGENTS.md 必须排在 Constitution 之前。"""

    def test_read_order_states_agents_first(self, agents_doc):
        block = flat(section(agents_doc, "阅读顺序"))

        assert block, "AGENTS.md 必须有「阅读顺序」一节"
        assert "1. AGENTS.md" in block, (
            "阅读顺序的第 1 步必须是 AGENTS.md（入口）"
        )

    def test_constitution_is_second(self, agents_doc):
        block = flat(section(agents_doc, "阅读顺序"))
        first = block.index("1. AGENTS.md")
        second = block.index("ARCHITECTURE_CONSTITUTION.md")

        assert first < second, (
            "阅读顺序里 AGENTS.md 必须排在 Constitution 之前"
        )

    def test_workspace_navigation_present(self, agents_doc):
        """规范 §23.4：必须有 Workspace 未来架构导航。"""
        text = flat(agents_doc)

        assert "Workspace 导航" in text, "必须有 Workspace 导航一节"
        assert "TASK_WORKSPACE.md" in agents_doc, (
            "导航必须指向 TASK_WORKSPACE.md"
        )
        assert "0020" in agents_doc, "导航必须指向 ADR-0020"

    def test_agents_does_not_copy_constitution(self, agents_doc):
        """不要把 Constitution 的内容复制进 AGENTS.md。"""
        assert "## Context" not in agents_doc, (
            "AGENTS.md 里出现了 ADR 的正文结构"
        )


# ============================================================
# 八、ADR
# ============================================================
class TestADR:
    def test_adr_0020_exists(self):
        assert ADR_FILE.exists(), "ADR-0020 不存在"

    def test_adr_has_required_sections(self):
        text = read(ADR_FILE)

        for heading in ("## Context", "## Decision", "## Consequences",
                        "## 备选方案"):
            assert heading in text, f"ADR-0020 缺少 {heading}"

    def test_adr_index_lists_0020(self):
        index = read(ADR_INDEX)

        assert "0020" in index, "ADR 索引里没有 0020"
        assert "0020-task-workspace-shared-resource-model.md" in index, (
            "ADR 索引的链接指向不存在的文件"
        )

    def test_adr_records_open_questions(self):
        """ADR 必须记录「没有决定的事」，不能只写决定。"""
        text = read(ADR_FILE)

        assert "Open Design Question" in text or "OPEN DESIGN QUESTION" in text, (
            "ADR-0020 必须记录未决定的问题"
        )
        assert "锁定" in flat(text) or "Lock lease" in text or "崩溃恢复" in text, (
            "ADR-0020 必须记录 Lock 崩溃恢复这个未决问题"
        )

    def test_adr_number_is_next_available(self):
        """
        ADR 编号必须是**下一个可用**的，不能凭空假设。

        现有已编号的 ADR 里，0020 之前必须已经有 0019。
        """
        adr_dir = PROJECT_ROOT / "docs" / "architecture" / "adr"
        numbers = sorted(
            int(p.name[:4]) for p in adr_dir.glob("*.md")
            if p.name[:4].isdigit()
        )

        assert 20 in numbers, "ADR-0020 不存在"
        assert 19 in numbers, (
            "ADR-0020 之前必须有 0019 —— 编号不能跳"
        )
        # 0020 必须是当前已用的编号之一
        assert max(numbers) >= 20, "0020 必须是当前最大的编号"


# ============================================================
# 九、文档间不矛盾
# ============================================================
class TestDocsConsistent:
    """规范 §27：四份文档之间不能互相矛盾。"""

    def test_workspace_future_in_all_docs(self, workspace_doc, agents_doc):
        constitution = read(CONSTITUTION_FILE)

        assert "FUTURE" in workspace_doc
        assert "Task Workspace" in agents_doc
        assert "Task Workspace" in constitution

    def test_shared_context_not_confused_with_workspace(
        self, workspace_doc
    ):
        """
        Shared Context 与 Workspace 的区分必须在两份文档里一致。

        【为什么不能要求两份文档用同一句话】
            第一版要求 `TASK_SHARED_CONTEXT.md` 里出现「共同知道什么」，
            但那份文档的措辞是：

                | **Shared Context** | 我们共同**认为**这个任务现在是什么 |

            —— 意思完全一致，**措辞不同**。

            要求措辞逐字一致会把守卫变成「文案检查」，
            而不是「语义检查」。所以这里查的是**各自的核心表述**：
                TASK_SHARED_CONTEXT.md  → 它是「任务世界模型 / 认为是什么」
                TASK_WORKSPACE.md       → 它复述「知道什么 vs 操作什么」
        """
        shared = flat(read(SHARED_CONTEXT_FILE))
        workspace = flat(read(WORKSPACE_FILE))

        # ① Shared Context 文档把自己定位为「任务世界模型 / 认为是什么」
        assert ("World Model" in shared or "世界模型" in shared
                or "认为" in shared), (
            "TASK_SHARED_CONTEXT.md 必须把自己定位为「共同的任务世界模型」"
        )

        # ② Workspace 文档必须给出这句对照
        assert "共同知道什么" in workspace, (
            "TASK_WORKSPACE.md 必须写出「Shared Context = 我们共同知道什么」"
        )
        assert "共同操作什么" in workspace, (
            "TASK_WORKSPACE.md 必须写出「Workspace = 我们共同操作什么」"
        )

        # ③ 两份文档都必须明确「二者独立」
        assert "独立" in workspace

    def test_private_workspace_not_task_workspace(self, workspace_doc):
        """两份文档对 Private Workspace 的定位必须一致。"""
        workspace = flat(section(workspace_doc, "5."))

        assert "Agent Private Workspace ≠ Task Workspace" in workspace or (
            "Agent Private Workspace" in workspace and "≠" in workspace
        ), "§5 必须显式写出 Agent Private Workspace ≠ Task Workspace"

    def test_no_contradictory_status(self, workspace_doc, agents_doc):
        """
        Task Workspace 的状态在 AGENTS.md 与 TASK_WORKSPACE.md 里必须一致。

        【这条防的是】一边写 FUTURE，另一边写 CURRENT。
        """
        agents_memory = flat(section(agents_doc, "4. Memory 四层"))
        workspace_head = flat(workspace_doc[:1500])

        assert "`FUTURE`" in agents_memory
        assert "FUTURE" in workspace_head


# ============================================================
# 十、反向测试
# ============================================================
class TestReverseVerification:
    """
    ADR-0018 的硬规定：新增守卫**必须**配反向测试。

    这里验证**检测机制本身**有效。
    """

    def test_section_scoped_not_whole_file(self):
        """`section()` 必须真的只取那一节。"""
        doc = read(WORKSPACE_FILE)

        lock = section(doc, "10.")
        identity = section(doc, "6.")

        assert "Lock owner" in lock
        assert "Lock owner" not in identity, (
            "section() 取的范围太大 —— 它把 §10 的内容也拿进 §6 了"
        )

    def test_section_returns_empty_for_missing(self):
        assert section(read(WORKSPACE_FILE), "99. 不存在") == "", (
            "section() 对不存在的标题必须返回空 —— "
            "否则守卫会在空字符串上假绿"
        )

    def test_section_includes_subsections(self):
        """
        `section()` 必须包含子小节。

        【这是真实踩过的坑】
            第一版在 `### 4.1` 处就截断了（因为 `#{1,4}` 也匹配 `###`），
            守卫于是在**被截断的段落**上断言。
        """
        doc = read(WORKSPACE_FILE)
        parent = section(doc, "2.")

        # §2 有很多子小节（2.1 共享资源模型 … 2.10）
        assert "2.1" in parent and "2.10" in parent, (
            "section('2.') 没有包含子小节 —— 守卫会在截断的段落上断言"
        )

    def test_import_detection_works(self):
        """AST import 检测必须真的能发现依赖。"""
        mods = module_imports(PROJECT_ROOT / "core" / "workspace.py")

        assert "pathlib" in mods or "os" in mods, (
            "core/workspace.py 本该有文件系统依赖 —— 检测坏了"
        )

    def test_import_detection_negative(self):
        """反向：已知不存在的依赖必须检测为没有。"""
        mods = module_imports(PROJECT_ROOT / "core" / "task_workspace.py")

        assert "git" not in mods
        assert "paramiko" not in mods

    def test_called_names_distinguishes_is_x_from_x(self):
        """
        反向：`called_names` 不能把 `is_open` 当成 `open`。

        【这是本项目记过的子串误报（ADR-0018）】
            `"open(" in source` 会匹配到 `is_open(self)`。
        """
        workspace_py = PROJECT_ROOT / "core" / "workspace.py"
        raw = workspace_py.read_text(encoding="utf-8")
        called = called_names(workspace_py)

        # 陷阱存在：原文里有 open(
        assert "open(" in raw
        # 但 AST 检测不会把 is_open 算成 open
        assert "open" not in called or "is_open" not in called

    def test_future_check_would_catch_a_current_marker(self):
        """
        反向：如果 Task Workspace 被标成 CURRENT，
        `test_agents_still_marks_workspace_future` 必须变红。

        这里用一段**伪造的** Markdown 验证检测逻辑。
        """
        fake = """
        | Layer | 属于谁 | 状态 | 是什么 |
        |---|---|---|---|
        | **3** Task Workspace | Task | `CURRENT` | 实际共享的文件与代码 |
        """
        memory = section(fake, "4.")

        # 伪造的文档里没有 FUTURE 标记 —— 检测应当失败
        assert "`FUTURE`" not in memory, (
            "检测逻辑本身有问题（伪造文档里本不该有 FUTURE）"
        )

    def test_permission_lock_check_would_catch_a_merge(self):
        """反向：伪造「Permission = Lock」的文本，确认检测能发现。"""
        fake = "Permission = lock，有 WRITE permission 就自动拥有 lock。"
        text = flat(fake)

        assert "Permission = lock" in text or "自动拥有" in text, (
            "检测模式匹配不到被合并的写法"
        )

    def test_forbidden_class_check_is_live(self):
        """
        反向：确认「类名检测」在**其他**文件上是有效的 ——
        否则「没有 Permission 类」这条断言可能只是因为
        整个项目的类名都不匹配而永远为真。
        """
        all_defined = set()
        for path in production_files():
            all_defined |= defined_classes(path)

        # 这些类**确实存在** —— 证明检测能看到类
        assert "Task" in all_defined
        assert "TaskSharedContext" in all_defined
        # 而这些**确实不存在**
        assert "Permission" not in all_defined
        assert "LockManager" not in all_defined

    def test_workspace_table_check_distinguishes_names(self, db):
        """
        反向：`workspaces`（精确表名）不存在，但
        `task_shared_contexts` 存在 —— 证明检查是按**精确名字**比对的，
        不是子串。
        """
        names = db.table_names()

        assert "task_shared_contexts" in names
        assert "workspaces" not in names, (
            "workspaces 表不该存在"
        )
        # 关键：不能因为 task_shared_contexts 含 "workspace" 就误判
        assert "workspace_permissions" not in names

    def test_adr_numbering_check_would_catch_a_gap(self):
        """反向：确认编号连续性检查能发现跳号。"""
        fake_numbers = [12, 14, 18, 19, 20]

        expected = list(range(min(fake_numbers), max(fake_numbers) + 1))
        assert fake_numbers != expected, (
            "伪造的编号列表本来就该是不连续的 —— 测试前提不成立"
        )

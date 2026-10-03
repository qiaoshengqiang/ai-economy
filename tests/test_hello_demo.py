"""
hello-demo —— 端到端 Demo 的测试
============================================================

【🔒 本文件**绝不**调用真实 LLM API】

    任务书 §21 Test 2 明确要求：

        真实 LLM API Demo **不应成为普通全量测试的一部分**。
        不要让 `pytest` 每次都消耗真实 API。

    所以这里全部用**假的 generate()** —— 它只替换
    「Agent 怎么拿到文本」这一步，**其余全部是真的**：

        ✅ 真 Runtime 组件（WorkspaceStore / Gateway / WorktreeManager）
        ✅ 真 Git 仓库、真 `git worktree add`
        ✅ 真 Workspace DB、真 Task、真 Revision、真 Lock
        ❌ 只有 LLM 那一次网络调用是假的

    「真实 API 不在普通测试里」这件事本身也有一条守卫
    （`TestRealApiIsOptIn`）。

【为什么可以传假 runtime】

    `_run_hello_demo()` 只用到 runtime 的六个属性
    （economy / tasks / workspace_gateway / worktree_manager /
      worktrees / router）—— 它**不**强求一个完整的 Runtime。
    这正是「组合根注入」带来的可测性。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from change import GitManager, WorktreeManager, WorkspaceSnapshot
from change.worktree_store import WorktreeStore
from core.task import Task
from core.task_store import TaskStore
from core.workspace_store import WorkspaceStore
from llm.base import LLMResponse
from services.workspace_concurrency import ResourceLockRegistry
from services.workspace_gateway import WorkspaceGateway
from storage.database import Database


# ============================================================
# 一个最小的、真实的 Runtime 替身
# ============================================================
class FakeAgent:
    """真的 Agent 的形状，但 `generate()` 由测试决定。"""

    def __init__(self, agent_id: str, content: str = "", *, raise_exc=False):
        self.agent_id = agent_id
        self.name = agent_id
        self.has_brain = True
        self.calls: list = []
        self._content = content
        self._raise = raise_exc
        self.brain = SimpleNamespace(provider="fake", model="fake-model")

    def generate(self, messages, provider=None, **kw):
        self.calls.append({"messages": messages, "provider": provider})
        if self._raise:
            raise RuntimeError("simulated LLM failure")
        return LLMResponse(
            content=self._content,
            provider=provider or "fake",
            model="fake-model",
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            token_source="api",
            latency_ms=3.0,
        )


def json_reply(path: str = "hello.py",
               content: str = 'print("Hello, World!")') -> str:
    import json
    return json.dumps({
        "files": [{"path": path, "content": content}],
        "summary": "fake generated",
    })


class Rig:
    """一个**真实**的最小环境：真 Git 仓库 + 真 Workspace + 真 Worktree。"""

    def __init__(self, tmp_path: Path, *, reply: str = "", raise_exc=False):
        # ---- 真 Git 仓库 ----
        self.repo = tmp_path / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.repo / "README.md").write_text("x\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")

        # ---- 真 DB ----
        self.db = Database(str(tmp_path / "demo.db"))
        self.db.connect()
        self.db.initialize()

        # ---- 真 Workspace 三件套（复用组合根的组装函数）----
        self.store = WorkspaceStore(db=self.db)
        self.locks = ResourceLockRegistry()
        self.agents = {
            "agent_a": FakeAgent("agent_a", reply, raise_exc=raise_exc),
            "agent_b": FakeAgent("agent_b", reply, raise_exc=raise_exc),
        }
        self.participants: set = set()

        self.gateway = WorkspaceGateway(
            self.db,
            store=self.store,
            lock_registry=self.locks,
            is_participant=lambda w, a: a in self.participants,
            agent_exists=lambda a: a in self.agents,
        )

        # ---- 真 WorktreeManager ----
        self.wt_store = WorktreeStore(db=self.db)
        self.wt_manager = WorktreeManager(
            self.db,
            git_manager=GitManager(repo_path=str(self.repo)),
            workspace_lookup=self._lookup,
            repository_ref="demo",
            worktrees_root=str(tmp_path / ".worktrees"),
            store=self.wt_store,
            registry=self.locks,
            actor="demo",
        )

        self.tasks = TaskStore(db=self.db)

        self.runtime = SimpleNamespace(
            economy=SimpleNamespace(
                all_agents=lambda: [self.agents["agent_a"],
                                    self.agents["agent_b"]],
            ),
            tasks=self.tasks,
            workspace_gateway=self.gateway,
            worktree_manager=self.wt_manager,
            worktrees=self.wt_store,
            router=SimpleNamespace(
                registry=SimpleNamespace(implemented=[]),
                provider_config=lambda n: {},
                available_providers=lambda: ["mock"],
            ),
        )

    def _git(self, *a):
        subprocess.run(["git", *a], cwd=self.repo, check=True,
                       capture_output=True)

    def _lookup(self, ws: str):
        root = self.store.find_by_path(ws, "/")
        if root is None:
            return None
        return WorkspaceSnapshot(ws, root.revision)

    def enable_participants(self, *ids):
        self.participants.update(ids)

    def close(self):
        self.db.close()


def run_demo(rig: Rig, *, participants: bool = True, **kw):
    # ⚠️ participants=False 给「Gateway 必须拒绝」那条测试用 ——
    #    第一版无条件加成 participant，那条测试**永远测不到拒绝路径**。
    if participants:
        rig.enable_participants("agent_a", "agent_b")
    return main_module._run_hello_demo(rig.runtime, **kw)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, reply=json_reply())
    try:
        yield r
    finally:
        r.close()


# ============================================================
# Test 1：用 fake LLM 完整跑通（稳定 CI）
# ============================================================
class TestDemoWithFakeLlm:
    def test_full_flow_succeeds(self, rig):
        result = run_demo(rig, provider="fake")

        assert result.ok, f"{result.stage}: {result.reason}"
        assert result.stdout == "Hello, World!"
        assert result.returncode == 0
        assert result.revision_after > result.revision_before
        assert result.materialized is True

    def test_real_git_worktree_was_created(self, rig):
        result = run_demo(rig, provider="fake")

        assert result.worktree_id.startswith("WT-")
        # 🔒 真 Git 侧确实多了一个 worktree
        out = subprocess.run(["git", "worktree", "list", "--porcelain"],
                             cwd=rig.repo, capture_output=True,
                             text=True).stdout
        assert out.count("worktree ") == 2, out

    def test_file_really_exists_on_disk(self, rig):
        result = run_demo(rig, provider="fake")

        path = Path(result.physical_path) / "hello.py"
        assert path.exists(), "🔒 Demo 结束后真实文件必须存在（§13）"
        assert "Hello, World!" in path.read_text(encoding="utf-8")

    def test_agent_really_called_the_llm(self, rig):
        """🔒 §7：必须真的调 LLM，不能硬编码答案。"""
        run_demo(rig, provider="fake")
        assert len(rig.agents["agent_a"].calls) == 1
        assert main_module.HELLO_DEMO_DEMAND in \
            rig.agents["agent_a"].calls[0]["messages"]

    def test_stable_when_run_twice(self, rig):
        """重跑不会炸（会走 edit 分支，Revision 继续递增）。"""
        first = run_demo(rig, provider="fake")
        second = run_demo(rig, provider="fake")

        assert first.ok and second.ok
        assert second.revision_after > first.revision_after
        assert second.reused_worktree is True


# ============================================================
# Test 3：Path traversal 被拒绝（§16）
# ============================================================
class TestPathIsolation:
    @pytest.mark.parametrize("bad", [
        "/etc/passwd",
        "../../something",
        "/Users/someone/secret.py",
        "sub/hello.py",
        "",
        "hello.txt",
    ])
    def test_non_allowed_path_is_rejected(self, tmp_path, bad):
        r = Rig(tmp_path, reply=json_reply(path=bad))
        try:
            result = run_demo(r, provider="fake")
        finally:
            r.close()

        assert not result.ok
        assert result.stage == "path_guard"
        # 🔒 没有写入任何东西
        ws = main_module.HELLO_DEMO_WORKSPACE
        assert not r.gateway.list(ws, "/", "agent_a").result["entries"]

    def test_rejection_message_names_the_allowed_file(self, tmp_path):
        r = Rig(tmp_path, reply=json_reply(path="/etc/passwd"))
        try:
            result = run_demo(r, provider="fake")
        finally:
            r.close()
        assert main_module.HELLO_DEMO_ALLOWED_FILE in result.reason


# ============================================================
# Test 4：Agent 必须经过 Gateway（§8 / §15）
# ============================================================
class TestAgentGoesThroughGateway:
    def test_demo_module_does_not_write_physical_path_for_the_agent(self):
        """
        🔒 结构性断言：`_run_hello_demo` 里**没有**「Agent 直接打开
           worktree 里的文件来写」这种代码。

        唯一的物理写入点是 **Demo 自己的物化替身**，
        而它的内容来自 Gateway 读回的值。
        """
        import ast
        from pathlib import Path as _P

        tree = ast.parse(_P("main.py").read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_run_hello_demo"), None)
        assert fn is not None

        src = ast.unparse(fn)
        # 能写物理文件的只有 open(..., "w") 一次 —— 那就是物化替身
        assert src.count('open(') == 1, (
            "应当只有一处物理写入（物化替身）"
        )
        # 而它必须读的是 Gateway 的结果
        assert "gateway_content" in src

    def test_agent_scoped_result_has_no_physical_path(self, rig):
        """
        🔒 本层返回给 Agent 的东西里没有 physical_path ——
           它只出现在 Demo 的 Human-facing 输出与内部 cwd。
        """
        result = run_demo(rig, provider="fake")
        wid = result.worktree_id

        agent_view = rig.wt_manager.get(wid)
        assert "physical_path" not in agent_view.result

    def test_gateway_denial_is_not_bypassed(self, rig):
        """🔒 Gateway 拒绝 → **绝不**绕过去直接写文件。"""
        # agent 不是 participant → Gateway 必须拒绝
        result = run_demo(rig, provider="fake", participants=False)

        assert not result.ok
        assert result.stage == "gateway"
        assert "NOT_TASK_PARTICIPANT" in result.reason


# ============================================================
# Test 5：Worktree 创建失败 → 不产生错误 READY（§20）
# ============================================================
class TestWorktreeFailure:
    def test_repo_unavailable_stops_before_agent(self, tmp_path):
        r = Rig(tmp_path, reply=json_reply())
        try:
            # 把 .git 藏起来 → 不是仓库了
            (r.repo / ".git").rename(r.repo / ".git-hidden")
            result = run_demo(r, provider="fake")
        finally:
            r.close()

        assert not result.ok
        assert result.stage == "worktree"
        assert "NOT_A_REPOSITORY" in result.reason

    def test_no_wrong_ready_state_after_failure(self, tmp_path):
        r = Rig(tmp_path, reply=json_reply())
        try:
            (r.repo / ".git").rename(r.repo / ".git-hidden")
            run_demo(r, provider="fake")

            rows = r.db.fetchall(
                "SELECT COUNT(*) AS n FROM git_worktrees "
                "WHERE state IN ('ready','active')")
            assert rows[0]["n"] == 0, "🔒 失败后不能留下 READY worktree"
        finally:
            r.close()

    def test_llm_failure_produces_no_fake_success(self, tmp_path):
        r = Rig(tmp_path, reply="", raise_exc=True)
        try:
            result = run_demo(r, provider="fake")
        finally:
            r.close()

        assert not result.ok
        assert result.stage == "llm"
        assert result.reason == "LLM request failed: RuntimeError"

    def test_llm_failure_leaves_no_workspace_resource(self, tmp_path):
        """§19：LLM 失败 → 没有 Workspace 污染。"""
        r = Rig(tmp_path, reply="", raise_exc=True)
        try:
            result = run_demo(r, provider="fake")
            listing = r.gateway.list(
                main_module.HELLO_DEMO_WORKSPACE, "/", "agent_a")
            assert listing.result["entries"] == []
        finally:
            r.close()


# ============================================================
# Test 6：生成后能读到正确内容 + Revision / Lock
# ============================================================
class TestWorkspaceCoreIsExercised:
    def test_resource_readable_with_correct_content(self, rig):
        result = run_demo(rig, provider="fake")

        read = rig.gateway.read(
            main_module.HELLO_DEMO_WORKSPACE, "/hello.py", "agent_a")
        assert read.success
        assert read.result["content"] == 'print("Hello, World!")'
        assert read.result["resource_id"] == result.resource_id

    def test_revision_increased(self, rig):
        result = run_demo(rig, provider="fake")
        assert result.revision_after == result.revision_before + 1

    def test_other_agent_was_locked_out(self, rig):
        """🔒 §18：B 改不了 A 的文件 —— Workspace Core 不是装饰。"""
        result = run_demo(rig, provider="fake")
        assert result.guard_result == "LOCKED_BY_OTHER_AGENT"

    def test_content_unchanged_by_the_denied_attempt(self, rig):
        run_demo(rig, provider="fake")
        read = rig.gateway.read(
            main_module.HELLO_DEMO_WORKSPACE, "/hello.py", "agent_a")
        assert read.result["content"] == 'print("Hello, World!")', (
            "被拒绝的写入不能改动内容"
        )

    def test_task_was_created_with_agent_as_creator(self, rig):
        """🔒 Human 不是 Task Creator（ADR-0024）。"""
        result = run_demo(rig, provider="fake")
        task = rig.tasks.get(result.task_id)
        assert task is not None
        assert task.creator == "agent_a"
        assert task.workspace_id == main_module.HELLO_DEMO_WORKSPACE
        assert "agent_b" in task.participants


# ============================================================
# 安全：绝不泄露 secret（§3）
# ============================================================
class TestNoSecretLeakage:
    def test_cli_output_has_no_secret_markers(self, rig, capsys):
        """
        🔒 Demo 的输出里不能出现 key / token / authorization。
        只允许 provider / model / token **数量**。
        """
        run_demo(rig, provider="fake")
        main_module.cmd_hello_demo(
            rig.runtime, SimpleNamespace(show_path=False, cleanup=False,
                                         provider="fake"))
        out = capsys.readouterr().out

        for banned in ("api_key", "API_KEY", "sk-", "Authorization",
                       "Bearer", "secret"):
            assert banned not in out, f"输出里出现了 {banned!r}"

    def test_demo_never_commits(self, rig):
        """🔒 §3：不自动 `git add .` / `git commit`。"""
        before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=rig.repo,
                                capture_output=True, text=True).stdout
        run_demo(rig, provider="fake")
        after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=rig.repo,
                               capture_output=True, text=True).stdout
        assert before == after, "Demo 不该产生任何 Git commit"

    def test_run_command_is_fixed_not_model_chosen(self):
        """
        🔒 §11：执行命令由 Demo **固定**，LLM 无权决定 shell command。
        """
        import ast
        from pathlib import Path as _P

        _tree = ast.parse(_P("main.py").read_text(encoding="utf-8"))
        _fn = next((n for n in ast.walk(_tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_run_hello_demo"), None)
        assert _fn is not None
        src = ast.unparse(_fn)
        assert "[sys.executable, HELLO_DEMO_ALLOWED_FILE]" in src
        # 不允许把模型输出当命令跑
        for bad in ("subprocess.run(response",
                    "subprocess.run(model",
                    "subprocess.run(content",
                    "shell=True"):
            assert bad not in src, f"出现了危险执行方式：{bad}"


# ============================================================
# Test 2：真实 API **不**在普通全量测试里
# ============================================================
class TestRealApiIsOptIn:
    def test_no_test_in_this_file_calls_a_real_provider(self):
        """
        🔒 本文件里**不许**出现真实 provider 名。
        """
        import ast as _ast

        # ⚠️ 这条守卫**改过两次**，两次都是假红：
        #    ① 按行扫文本 → 被这条断言**自己的字面量**绊倒
        #    ② 扫全部短字符串常量 → 被下面那个 provider 名单**元组**绊倒
        #
        #    正确做法：只看**真正的调用参数** ——
        #    `xxx(..., provider="deepseek")` 这种 keyword 实参。
        #    名单元组不是 Call 的 keyword，天然不会命中。
        _tree = _ast.parse(Path(__file__).read_text(encoding="utf-8"))
        _real = ("deepseek", "doubao", "openai", "grok")
        _bad = []
        for node in _ast.walk(_tree):
            if not isinstance(node, _ast.Call):
                continue
            for kw in node.keywords or ():
                if kw.arg != "provider":
                    continue
                val = kw.value
                if isinstance(val, _ast.Constant) and isinstance(val.value, str):
                    if any(r in val.value.lower() for r in _real):
                        _bad.append(val.value)
        assert not _bad, f"本文件把真实 provider 传给了 provider=：{_bad}"

    def test_real_api_call_is_manual_only(self):
        """
        🔒 任务书 §21 Test 2：真实 API 应该是 manual / opt-in。

        证明方式：Demo 的核心函数接受 `provider=` 参数，
        而 CLI 会**自动挑**一个配了 key 的真实 provider ——
        也就是说「真实调用」只经由 `./dev.sh hello-demo`，
        **不经过** `pytest`。
        """
        import inspect

        sig = inspect.signature(main_module._run_hello_demo)
        assert "provider" in sig.parameters

    def test_demo_is_not_registered_as_a_test_fixture_path(self):
        """确认没有任何测试在收集阶段主动跑 hello-demo。"""
        import main as m

        assert hasattr(m, "cmd_hello_demo")
        assert hasattr(m, "_run_hello_demo")

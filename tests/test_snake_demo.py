"""
snake-demo —— Task Demo V2 的**定向**测试
============================================================

【🔒 本文件绝不调用真实 LLM】

    与 hello-demo 同样的规则（任务书 §12）：
    普通测试用 fake `generate()`，真实 API 只经 `./dev.sh snake-demo`。

【⚠️ 本文件是**定向**测试，不是架构守卫】

    任务书 §25 明确要求：不要再次增加几十个架构守卫。
    所以这里只覆盖 10 件事，全部围绕这个 Demo 本身。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from change import GitManager, WorktreeManager, WorkspaceSnapshot
from change.materializer import MaterializationError, WorktreeMaterializer
from change.worktree_store import WorktreeStore
from core.task_context_store import TaskContextStore
from core.task_store import TaskStore
from core.workspace_store import WorkspaceStore
from llm.base import LLMResponse
from services.task_alignment_service import TaskAlignmentService
from services.workspace_concurrency import ResourceLockRegistry
from services.workspace_gateway import WorkspaceGateway
from storage.database import Database


def project(**over) -> dict:
    """一份**最小但合规**的多文件项目（fake LLM 用）。"""
    files = [
        {"path": "index.html",
         "content": '<html><body><canvas id="c"></canvas>'
                    '<script src="config.js"></script>'
                    '<script src="game.js"></script></body></html>'},
        {"path": "style.css", "content": "body{margin:0}"},
        {"path": "config.js",
         "content": "const GAME_CONFIG = {\n"
                    "  boardWidth: 20,\n  boardHeight: 20,\n"
                    "  speed: 120,\n  initialLength: 3,\n"
                    "  wrapWalls: false\n};\n"},
        {"path": "game.js",
         "content": "function tick(){ setInterval(step, GAME_CONFIG.speed); }\n"
                    "const w = GAME_CONFIG.boardWidth;\n"},
    ]
    for path, content in over.items():
        files = [f for f in files if f["path"] != path]
        files.append({"path": path, "content": content})
    return {"files": files, "summary": "fake", "configFile": "config.js"}


class FakeAgent:
    def __init__(self, replies: list):
        self.agent_id = "agent_a"
        self.name = "agent_a"
        self.has_brain = True
        self._replies = list(replies)
        self.calls: list = []
        self.brain = SimpleNamespace(provider="fake", model="fake")

    def generate(self, messages, provider=None, **kw):
        self.calls.append(messages)
        text = self._replies.pop(0) if self._replies else ""
        return LLMResponse(content=text, provider=provider or "fake",
                           model="fake", input_tokens=1, output_tokens=1,
                           token_source="api", latency_ms=1.0)


class Rig:
    def __init__(self, tmp_path: Path, replies: list):
        self.repo = tmp_path / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@e.com")
        self._git("config", "user.name", "T")
        (self.repo / "README.md").write_text("x\n")
        self._git("add", "."); self._git("commit", "-q", "-m", "init")

        self.db = Database(str(tmp_path / "snake.db"))
        self.db.connect(); self.db.initialize()

        self.agent = FakeAgent(replies)
        self.store = WorkspaceStore(db=self.db)
        self.locks = ResourceLockRegistry()
        self.gateway = WorkspaceGateway(
            self.db, store=self.store, lock_registry=self.locks,
            is_participant=lambda w, a: a == "agent_a",
            agent_exists=lambda a: a == "agent_a")

        self.wt_store = WorktreeStore(db=self.db)
        self.wt_manager = WorktreeManager(
            self.db, git_manager=GitManager(repo_path=str(self.repo)),
            workspace_lookup=self._lookup, repository_ref="demo",
            worktrees_root=str(tmp_path / ".worktrees"),
            store=self.wt_store, registry=self.locks, actor="demo")

        self.tasks = TaskStore(db=self.db)
        # 🔴 `_run_snake_demo` 现在走 `runtime.create_task` —— Task 和它的
        #    Shared Context **一起诞生**。本 rig 是 Runtime 的**替身**，
        #    所以必须把这两个也接上。
        #
        #    为什么不能省：少了容器，Agent 之后**连验收人都指派不了**
        #    （`assign_acceptance` 报 `no_shared_context`）——
        #    demo 会看起来正常，实际环境缺一块。
        self.alignment = TaskAlignmentService(
            tasks=self.tasks,
            contexts=TaskContextStore(db=self.db),
        )

        self.runtime = SimpleNamespace(
            economy=SimpleNamespace(all_agents=lambda: [self.agent]),
            tasks=self.tasks,
            alignment=self.alignment,
            create_task=self._create_task,
            workspace_gateway=self.gateway,
            worktree_manager=self.wt_manager, worktrees=self.wt_store,
            router=SimpleNamespace(
                registry=SimpleNamespace(implemented=[]),
                provider_config=lambda n: {}, available_providers=[]),
        )

    def _create_task(self, task, *, by=None):
        """与 `Runtime.create_task` **同语义**（本 rig 是它的替身）。"""
        saved = self.tasks.save(task)
        if self.alignment.current(task.task_id) is None:
            self.alignment.create_context(task.task_id,
                                          by=by or task.creator)
        return saved

    def _git(self, *a):
        subprocess.run(["git", *a], cwd=self.repo, check=True,
                       capture_output=True)

    def _lookup(self, ws):
        root = self.store.find_by_path(ws, "/")
        return WorkspaceSnapshot(ws, root.revision) if root else None

    def close(self):
        self.db.close()


def run(rig, **kw):
    return main_module._run_snake_demo(rig.runtime, provider="fake", **kw)


def text(rig) -> str:
    return rig.agent._replies[0] if rig.agent._replies else ""


# ============================================================
# 1  mock LLM 能生成多文件结果
# ============================================================
class TestMultiFileGeneration:
    def test_fake_llm_produces_a_working_project(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
        finally:
            r.close()
        assert res.ok, f"{res.stage}: {res.reason}"
        assert len(res.files) == 4

    def test_real_files_exist_in_the_worktree(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
            for name in ("index.html", "style.css", "config.js", "game.js"):
                assert (Path(res.physical_path) / name).exists(), name
        finally:
            r.close()


# ============================================================
# 2  文件数量验证（§7）
# ============================================================
class TestFileCountValidation:
    def test_html_only_is_rejected(self, tmp_path):
        only_html = {"files": [{"path": "index.html", "content": "<html/>"}],
                     "configFile": ""}
        r = Rig(tmp_path, [json.dumps(only_html)])
        try:
            res = run(r)
        finally:
            r.close()
        assert not res.ok
        assert res.stage == "multi_file"
        assert "multi-file" in res.reason

    def test_one_js_is_not_enough(self, tmp_path):
        # 真的只留 1 个 js（config.js）→ 必须被多文件验收挡住
        p = project()
        p["files"] = [f for f in p["files"] if f["path"] != "game.js"]
        r = Rig(tmp_path, [json.dumps(p)])
        try:
            res = run(r)
        finally:
            r.close()
        assert not res.ok and res.stage == "multi_file"

    def test_counts_are_reported(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
        finally:
            r.close()
        assert res.counts["html"] >= 1
        assert res.counts["css"] >= 1
        assert res.counts["js"] >= 2


# ============================================================
# 3  path 安全
# ============================================================
class TestPathSafety:
    @pytest.mark.parametrize("bad", ["/etc/passwd", "../../x.js", "a/../../b.js"])
    def test_traversal_is_rejected(self, tmp_path, bad):
        p = project()
        p["files"].append({"path": bad, "content": "x"})
        r = Rig(tmp_path, [json.dumps(p)])
        try:
            res = run(r)
        finally:
            r.close()
        assert not res.ok
        assert res.stage == "path_guard"

    def test_materializer_rejects_traversal(self):
        m = WorktreeMaterializer(read_resource=lambda w, p: "x",
                                 worktree_path_for=lambda wid: "/tmp")
        with pytest.raises(MaterializationError):
            m.materialize("WS", "WT", ["../escape.js"])

    def test_materializer_rejects_absolute(self):
        m = WorktreeMaterializer(read_resource=lambda w, p: "x",
                                 worktree_path_for=lambda wid: "/tmp")
        with pytest.raises(MaterializationError):
            m.materialize("WS", "WT", ["/etc/passwd"])


# ============================================================
# 4  配置文件存在
# ============================================================
class TestConfigFileExists:
    def test_missing_config_file_is_rejected(self, tmp_path):
        p = project()
        p["files"] = [f for f in p["files"] if f["path"] != "config.js"]
        # 补一个别的 js，好让它**走到 config 检查**而不是先被多文件挡住
        p["files"].append({"path": "ui.js", "content": "const x = 1;"})
        p["configFile"] = ""
        r = Rig(tmp_path, [json.dumps(p)])
        try:
            res = run(r)
        finally:
            r.close()
        assert not res.ok and res.stage == "config"

    def test_config_file_is_reported(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
        finally:
            r.close()
        assert res.config_file == "config.js"


# ============================================================
# 5  配置字段存在 + 真的被引用（§8）
# ============================================================
class TestConfigFields:
    def test_all_five_concepts_are_found(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
        finally:
            r.close()
        assert len(res.config_fields) == 5

    def test_config_not_used_by_logic_is_rejected(self, tmp_path):
        """🔒 「看起来有配置，实际游戏不用配置」的假配置必须被拒。"""
        p = project()
        p["files"] = [f for f in p["files"] if f["path"] != "game.js"]
        p["files"].append({"path": "game.js",
                           "content": "setInterval(tick, 120);"})
        r = Rig(tmp_path, [json.dumps(p)])
        try:
            res = run(r)
        finally:
            r.close()
        assert not res.ok and res.stage == "config"

    def test_missing_wrap_option_triggers_bounded_repair(self, tmp_path):
        """
        🔒 实测动机：DeepSeek 连续两次漏掉「是否允许穿墙」。
           这里验证「缺概念 → 触发修复 → 修复成功则通过」。
        """
        broken = project()
        broken["files"] = [f for f in broken["files"]
                           if f["path"] != "config.js"]
        broken["files"].append({
            "path": "config.js",
            "content": "const GAME_CONFIG = {boardWidth:20, boardHeight:20,"
                       " speed:120, initialLength:3};\n"})
        r = Rig(tmp_path, [json.dumps(broken), json.dumps(project())])
        try:
            res = run(r)
        finally:
            r.close()
        assert res.ok, f"{res.stage}: {res.reason}"
        assert len(r.agent.calls) == 2, "应当恰好修复一次"


# ============================================================
# 6  Gateway 写入
# ============================================================
class TestGatewayWrite:
    def test_resources_are_readable_through_the_gateway(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
            read = r.gateway.read(res.workspace_id, "/config.js", "agent_a")
            assert read.success
            assert "GAME_CONFIG" in read.result["content"]
        finally:
            r.close()

    def test_agent_gets_no_physical_path(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
            view = r.wt_manager.get(res.worktree_id).result
            assert "physical_path" not in view
        finally:
            r.close()


# ============================================================
# 7  Materialization
# ============================================================
class TestMaterialization:
    def test_materializes_only_the_listed_paths(self, tmp_path):
        """🔒 §18：**不扫描** —— 只写调用方列的。"""
        written = []

        def read(ws, p):
            written.append(p)
            return "content"

        root = tmp_path / "wt"
        root.mkdir()
        m = WorktreeMaterializer(read_resource=read,
                                 worktree_path_for=lambda wid: str(root))
        report = m.materialize("WS", "WT", ["a.js", "b.css"])

        assert report.written == ["a.js", "b.css"]
        assert written == ["a.js", "b.css"], "不该去读别的路径"
        assert (root / "a.js").exists()

    def test_materializer_does_not_touch_workspace_state(self, tmp_path):
        """🔒 §17③：不产生 Workspace Revision 的伪变化。"""
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
            before = r.gateway.read(res.workspace_id, "/config.js",
                                    "agent_a").result["revision"]
            m = WorktreeMaterializer(
                read_resource=lambda w, p: "x",
                worktree_path_for=lambda wid: res.physical_path)
            m.materialize(res.workspace_id, res.worktree_id, ["config.js"])
            after = r.gateway.read(res.workspace_id, "/config.js",
                                   "agent_a").result["revision"]
            assert before == after
        finally:
            r.close()

    def test_nested_paths_materialize(self, tmp_path):
        root = tmp_path / "wt"; root.mkdir()
        m = WorktreeMaterializer(read_resource=lambda w, p: "x",
                                 worktree_path_for=lambda wid: str(root))
        report = m.materialize("WS", "WT", ["js/game.js"])
        assert (root / "js" / "game.js").exists()
        assert report.written == ["js/game.js"]


# ============================================================
# 8  目录结构
# ============================================================
class TestDirectoryStructure:
    def test_flat_project(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
            names = sorted(p.name for p in Path(res.physical_path).glob("*")
                           if p.is_file())
            assert {"index.html", "style.css", "config.js",
                    "game.js"} <= set(names)
        finally:
            r.close()

    def test_nested_project_layout(self, tmp_path):
        p = project()
        p["files"] = [
            {"path": "index.html",
             "content": '<script src="js/config.js"></script>'
                        '<script src="js/game.js"></script>'},
            {"path": "css/style.css", "content": "body{}"},
            {"path": "js/config.js",
             "content": "const GAME_CONFIG = {boardWidth:20,boardHeight:20,"
                        "speed:120,initialLength:3,wrapWalls:false};"},
            {"path": "js/game.js", "content": "GAME_CONFIG.speed;"},
        ]
        p["configFile"] = "js/config.js"
        r = Rig(tmp_path, [json.dumps(p)])
        try:
            res = run(r)
            assert res.ok, f"{res.stage}: {res.reason}"
            assert (Path(res.physical_path) / "js" / "game.js").exists()
        finally:
            r.close()


# ============================================================
# 9  Browser server command 固定（§19）
# ============================================================
class TestBrowserServerCommand:
    def test_command_is_fixed(self):
        import ast
        src = Path("main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_snake_start_server"), None)
        assert fn is not None
        body = ast.unparse(fn)

        # ast.unparse 用单引号，所以只比内容不比引号风格
        assert "http.server" in body
        assert "127.0.0.1" in body
        assert "shell=True" not in body
        # 🔒 不接受任何来自 LLM 的命令
        assert "response" not in body and "content" not in body

    def test_llm_cannot_choose_the_command(self):
        import ast
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_run_snake_demo"), None)
        body = ast.unparse(fn)
        for bad in ("subprocess.run(response", "subprocess.run(content",
                    "subprocess.run(model"):
            assert bad not in body


# ============================================================
# 10 不泄露 secret
# ============================================================
class TestNoSecretLeakage:
    def test_cli_output_has_no_secret(self, tmp_path, capsys):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            main_module.cmd_snake_demo(
                r.runtime,
                SimpleNamespace(show_path=False, cleanup=False, stop=False,
                                port=8765, provider="fake"))
            out = capsys.readouterr().out
        finally:
            r.close()

        for banned in ("api_key", "API_KEY", "sk-", "Bearer",
                       "Authorization", "secret"):
            assert banned not in out

    def test_single_agent_is_used(self, tmp_path):
        """🔒 §13：单 Agent —— 不发协商、不引入 B/C。"""
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            res = run(r)
        finally:
            r.close()
        assert res.agent_id == "agent_a"
        assert len(r.agent.calls) >= 1

    def test_no_git_commit_is_made(self, tmp_path):
        r = Rig(tmp_path, [json.dumps(project())])
        try:
            before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r.repo,
                                    capture_output=True, text=True).stdout
            run(r)
            after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r.repo,
                                   capture_output=True, text=True).stdout
            assert before == after
        finally:
            r.close()

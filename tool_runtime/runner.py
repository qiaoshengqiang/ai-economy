"""
Tool Runner —— 受控的代码执行与测试
=========================================

【它做什么】

    在**独立的 run 目录**里执行公共工具库中声明过的工具：

        python / pytest / node / playwright

【它不是什么】

    ❌ 不是通用 shell
    ❌ 不是 Agent 任意下载/安装器
    ❌ 不是 OS 级安全沙箱（V1 的信任模型见设计文档）

【硬约束】

    ① 只执行 manifest 里的 command；Agent 只能追加参数，不能换可执行文件
    ② `shell=False`；命令是列表
    ③ cwd 固定在本次 run 的 work 目录
    ④ 每次 run 独立 HOME / TMPDIR；清空 LLM/API secret
    ⑤ 超时 + 输出上限
    ⑥ 工具库只读；安装/更新不由 Agent 触发
"""

from __future__ import annotations

import functools
import http.server
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tool_runtime.library import ToolLibrary, ToolLibraryError, ToolSpec
from tool_runtime.workspace import (
    RunWorkspace, materialize_private_workspace, materialize_task_workspace,
)

#: 单次输出最多回给 Agent 多少字符（前面 8k + 后面 4k）。
MAX_OUTPUT_CHARS = 12_000
_OUT_HEAD = 8_000
_OUT_TAIL = 4_000

#: Playwright 的「项目配置」搜索：子目录里的 config 不能被根 bootstrap 遮蔽。
PLAYWRIGHT_CONFIG_NAMES = (
    "playwright.config.js", "playwright.config.ts",
    "playwright.config.mjs", "playwright.config.cjs",
)

#: Playwright 冒烟门禁：在跑全量套件前先花几秒确认页面真的启动。
SMOKE_CONFIG_NAME = "playwright.config.js"
SMOKE_SPEC_NAME = "smoke.spec.js"
SMOKE_ENTRY_CANDIDATES = (
    "site/index.html", "index.html", "public/index.html",
    "dist/index.html", "app/index.html", "src/index.html",
)
SMOKE_TIMEOUT_SECONDS = 45

#: 图片扫描：只把「新产生的、可读的」图片交给上层。
MAX_IMAGE_BYTES = 4_000_000
MAX_IMAGES = 4
IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_IMAGE_SKIP_DIRS = {
    "node_modules", ".git", ".smoke", "smoke",
    "playwright-report", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

#: 冒烟门禁用**独立**的最小 config：不启动项目自己的 webServer，
#: 只验证「页面能不能打开、body 不是空的、页面没有 JS 崩溃」。
SMOKE_CONFIG_SOURCE = """\
module.exports = {
  testDir: '.',
  testMatch: 'smoke.spec.js',
  timeout: 35000,
  workers: 1,
  reporter: [['line']],
  use: { channel: 'chrome', headless: true },
};
"""

SMOKE_SPEC_SOURCE = """\
const { test, expect } = require('@playwright/test');

test('smoke gate: page boots', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(String((e && e.message) || e)));

  const response = await page.goto(process.env.SMOKE_URL, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });
  expect(response, '页面没有响应').not.toBeNull();
  expect(response.status(), 'HTTP 状态').toBeLessThan(400);

  const bodyLength = await page.evaluate(
    () => ((document.body && document.body.innerHTML) || '').trim().length,
  );
  expect(bodyLength, 'body 为空（HTML 可能被截断）').toBeGreaterThan(20);

  if (await page.$('#app')) {
    await expect(page.locator('#app canvas, #app > *').first())
      .toBeVisible({ timeout: 10000 });
  }
  if (await page.$('#ui-root')) {
    await expect(page.locator('#ui-root > *').first())
      .toBeVisible({ timeout: 10000 });
  }
  expect(errors, '页面抛出了 JS 错误').toEqual([]);
});
"""


class ToolRunError(Exception):
    """执行前置条件失败（不是进程本身失败）。"""


@dataclass(frozen=True)
class ToolRunResult:
    """一次工具执行的完整结果。物理路径不进入 Agent 输出。"""

    ran: bool
    ok: bool
    tool: str
    run_id: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: float = 0.0
    code: str = ""
    message: str = ""
    materialized_files: int = 0
    output_truncated: bool = False
    #: Playwright 实际使用的项目配置（空 = 用 run 根目录的默认配置）。
    config_used: str = ""
    #: 冒烟门禁的真实结果（enabled=false 表示跳过，附 reason）。
    smoke: Dict[str, Any] = field(default_factory=dict)
    #: 本次执行新产生的图片（只含相对路径 + 元数据，不含 base64）。
    images: List[Dict[str, Any]] = field(default_factory=list)

    def to_agent_dict(self) -> Dict[str, Any]:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "tool": self.tool,
            "run_id": self.run_id,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "code": self.code,
            "message": self.message,
            "materialized_files": self.materialized_files,
            "output_truncated": self.output_truncated,
            "config_used": self.config_used,
            "smoke": dict(self.smoke or {}),
            "images": list(self.images or []),
        }


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    head = text[:_OUT_HEAD]
    tail = text[-_OUT_TAIL:]
    return (
        f"{head}\n\n...（输出被截断：共 {len(text)} 字符）...\n\n{tail}",
        True,
    )


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    """冒烟门禁用的静态服务器：不把访问日志混进测试输出。"""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class ToolRunner:
    """按公共工具清单执行命令。"""

    def __init__(self, library: ToolLibrary, *, run_root: Path,
                 max_timeout_seconds: int = 300):
        self.library = library
        self.run_root = Path(run_root).resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.max_timeout_seconds = int(max_timeout_seconds)

    # ================================================================
    # public
    # ================================================================
    def catalog(self) -> List[Dict[str, Any]]:
        return self.library.public_catalog()

    def guide(self, tool: str = "") -> Dict[str, Any]:
        """返回工具清单 / 某个工具的完整用法文档。

        文档来源固定为 `resources/public/tools/<tool>.md`；
        工具名先经过 ToolLibrary 校验，避免把任意路径当文档读。
        """
        docs_dir = (
            Path(self.library.project_root)
            / "resources" / "public" / "tools"
        )
        name = str(tool or "").strip()
        if not name:
            path = docs_dir / "README.md"
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = ""
            return {
                "ok": True,
                "tool": "",
                "content": content,
                "tools": self.library.public_catalog(),
            }
        try:
            spec = self.library.get(name)
        except Exception as exc:                              # noqa: BLE001
            return {
                "ok": False,
                "code": "unknown_tool",
                "message": str(exc),
                "tools": self.library.public_catalog(),
            }
        path = docs_dir / f"{spec.name}.md"
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = f"# {spec.name}\n\n{spec.description}\n"
        return {
            "ok": True,
            "tool": spec.name,
            "content": content,
            "tools": self.library.public_catalog(),
        }

    def run_from_task(self, gateway: Any, workspace_id: str, agent_id: str,
                      *, tool: str, args: Optional[List[str]] = None,
                      source_path: str = "",
                      timeout_seconds: Optional[int] = None) -> ToolRunResult:
        run = RunWorkspace.create(self.run_root)
        try:
            stats = materialize_task_workspace(
                gateway, workspace_id, agent_id, source_path,
                target=run.work)
        except Exception as exc:                              # noqa: BLE001
            return self._pre_run_failure(
                tool, run.run_id, f"materialize_failed: {exc}")
        return self._execute(tool, args, run, stats["files"],
                             timeout_seconds)

    def run_from_private(self, workspace: Any, *,
                         tool: str, args: Optional[List[str]] = None,
                         source_path: str = "",
                         timeout_seconds: Optional[int] = None) -> ToolRunResult:
        run = RunWorkspace.create(self.run_root)
        try:
            stats = materialize_private_workspace(
                workspace, source_path, target=run.work)
        except Exception as exc:                              # noqa: BLE001
            return self._pre_run_failure(
                tool, run.run_id, f"materialize_failed: {exc}")
        return self._execute(tool, args, run, stats["files"],
                             timeout_seconds)

    # ================================================================
    # internal
    # ================================================================
    def _pre_run_failure(self, tool: str, run_id: str,
                         message: str) -> ToolRunResult:
        return ToolRunResult(
            ran=False, ok=False, tool=str(tool), run_id=run_id,
            code="pre_run_failed", message=message)

    def _execute(self, tool_name: str, args: Optional[List[str]],
                 run: RunWorkspace, materialized: int,
                 timeout_seconds: Optional[int]) -> ToolRunResult:
        try:
            spec = self.library.get(tool_name)
        except ToolLibraryError as exc:
            return self._pre_run_failure(tool_name, run.run_id, str(exc))

        clean_args = self._clean_args(args)
        if clean_args is None:
            return self._pre_run_failure(
                tool_name, run.run_id,
                "args 必须是最多 100 项的字符串列表，"
                "每项长度 ≤ 2000 字符；长脚本请先写文件再运行")

        timeout = self._timeout(timeout_seconds)
        if timeout is None:
            return self._pre_run_failure(
                tool_name, run.run_id,
                f"timeout_seconds 必须是 1..{self.max_timeout_seconds} 的整数")

        try:
            self._bootstrap(spec, run.work)
        except Exception as exc:                              # noqa: BLE001
            return self._pre_run_failure(
                tool_name, run.run_id, f"bootstrap_failed: {exc}")

        # Playwright 的 manifest 已经包含 `test` 子命令；
        # 兼容 Agent 仍然写 args=["test", ...] 的形状。
        if spec.name == "playwright" and clean_args:
            if clean_args[0] == "test":
                clean_args = clean_args[1:]

        #: `--no-smoke` 是本 Runtime 的开关，不属于 Playwright CLI。
        smoke_disabled = False
        if spec.name == "playwright":
            smoke_disabled = "--no-smoke" in clean_args
            clean_args = [a for a in clean_args if a != "--no-smoke"]

        config_used = ""
        smoke: Dict[str, Any] = {}
        if spec.name == "playwright":
            clean_args, config_used = self._playwright_config_args(
                run.work, clean_args)
            smoke = self._playwright_smoke(
                run, spec, disabled=smoke_disabled)
            if smoke.get("enabled") and not smoke.get("ok"):
                return ToolRunResult(
                    ran=True, ok=False, tool=spec.name, run_id=run.run_id,
                    exit_code=smoke.get("exit_code"),
                    stdout=str(smoke.get("stdout") or ""),
                    stderr=str(smoke.get("stderr") or ""),
                    timed_out=bool(smoke.get("timed_out")),
                    duration_ms=float(smoke.get("duration_ms") or 0.0),
                    code="smoke_failed",
                    message=(f"smoke gate failed: entry={smoke.get('entry')}; "
                             "全量套件未执行"),
                    materialized_files=int(materialized),
                    config_used=config_used, smoke=smoke)

        images_before = self._image_snapshot(run.work)
        command = [*spec.command, *clean_args]
        env = self._env(run, spec)
        suite_server = None
        suite_url = ""
        try:
            # 🔒 smoke gate 的静态服务器在 smoke 结束后就关闭了；
            #    正式 suite 需要自己的服务器。把入口页 URL 通过
            #    BASE_URL 暴露给 spec，避免 Agent 猜端口。
            if spec.name == "playwright":
                entry = self._smoke_entry(run.work)
                if entry and entry.startswith(("http://", "https://")):
                    env["BASE_URL"] = entry
                elif entry:
                    suite_server, suite_url = self._serve_work(
                        run.work, entry)
                    env["BASE_URL"] = suite_url
            started = time.perf_counter()
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(run.work),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                return self._pre_run_failure(
                    tool_name, run.run_id, f"tool_not_found: {exc}")
            except Exception as exc:                          # noqa: BLE001
                return self._pre_run_failure(
                    tool_name, run.run_id, f"spawn_failed: {exc}")

            timed_out = False
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
            duration_ms = round((time.perf_counter() - started) * 1000, 1)

            stdout = stdout or ""
            stderr = stderr or ""
            stdout, cut_out = _clip(stdout)
            stderr, cut_err = _clip(stderr)
            exit_code = proc.returncode
            ok = (not timed_out) and exit_code == 0
            code = "" if ok else ("timeout" if timed_out
                                  else "tool_exit_nonzero")
            message = ""
            if (spec.name == "playwright" and not ok
                    and "No tests found" in stdout):
                code = "no_tests"
                message = (
                    "Playwright 没有找到测试文件。当前 testMatch 是 "
                    "`**/*.spec.js`；测试必须使用 `@playwright/test` 的 "
                    "`test(...)`。可调用 tool_help(tool='playwright') "
                    "查看完整写法。")
            return ToolRunResult(
                ran=True, ok=ok, tool=spec.name, run_id=run.run_id,
                exit_code=exit_code, stdout=stdout, stderr=stderr,
                timed_out=timed_out, duration_ms=duration_ms,
                code=code, message=message,
                materialized_files=int(materialized),
                output_truncated=cut_out or cut_err,
                config_used=config_used, smoke=smoke,
                images=self._new_images(run.work, images_before))
        finally:
            if suite_server is not None:
                try:
                    suite_server.shutdown()
                    suite_server.server_close()
                except Exception:                             # noqa: BLE001
                    pass

    # ------------------------------------------------------------
    # Playwright：项目配置发现 + 冒烟门禁
    # ------------------------------------------------------------
    @staticmethod
    def _playwright_config_args(work: Path,
                                args: List[str]) -> tuple[List[str], str]:
        """
        如果 run 目录里有**子目录**的 playwright config，自动把它传给 CLI。

        🔒 解决的问题：Tool Runtime 会在 run 根目录 bootstrap 一个
           `playwright.config.js`，Playwright 默认只读根配置；
           Agent 写在 `qa/playwright.config.js` 的 baseURL / webServer
           就会被静默忽略。这里显式把项目配置交给 Playwright。

        🔒 不覆盖 Agent 显式写的 `--config`；没有子目录配置时保持原样。
        """
        for arg in args:
            if (arg in ("--config", "-c")
                    or arg.startswith("--config=")
                    or arg.startswith("-c=")):
                return list(args), ""

        found: List[Path] = []
        for root, dirs, files in os.walk(work):
            dirs[:] = [d for d in dirs if d not in _IMAGE_SKIP_DIRS]
            root_path = Path(root)
            if root_path == work:
                # 根配置是 Runtime 的 bootstrap；没有子目录配置时就是默认。
                continue
            for name in files:
                if name in PLAYWRIGHT_CONFIG_NAMES:
                    found.append(root_path / name)
        if not found:
            return list(args), ""

        found.sort(key=lambda p: p.relative_to(work).as_posix())
        qa_configs = [
            p for p in found
            if p.relative_to(work).as_posix().startswith("qa/")
        ]
        if qa_configs:
            chosen = qa_configs[0]
        elif len(found) == 1:
            chosen = found[0]
        else:
            # 多个候选时**不替 Agent 猜**；它仍然可以显式传 --config。
            return list(args), ""

        rel = chosen.relative_to(work).as_posix()
        return ["--config", rel, *args], rel

    def _playwright_smoke(self, run: RunWorkspace, spec: ToolSpec,
                          *, disabled: bool = False) -> Dict[str, Any]:
        """
        跑一个 ~5 秒的冒烟门禁；失败时**不执行全量套件**。

        🔒 门禁只回答「页面是否启动」：
           - HTTP 状态 < 400；
           - body 不是空/截断的 HTML；
           - #app / #ui-root 存在时有可见子节点；
           - 没有 pageerror。
           它**不**判断视觉质量（那是读图/人工验收的事）。
        """
        if disabled:
            return {"enabled": False, "reason": "disabled_by_args"}
        flag = os.environ.get("PLAYWRIGHT_SMOKE", "").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return {"enabled": False, "reason": "disabled_by_env"}

        entry = self._smoke_entry(run.work)
        if not entry:
            return {"enabled": False, "reason": "no_entry"}

        smoke_dir = run.root / "smoke"
        smoke_dir.mkdir(parents=True, exist_ok=True)
        (smoke_dir / SMOKE_CONFIG_NAME).write_text(
            SMOKE_CONFIG_SOURCE, encoding="utf-8")
        (smoke_dir / SMOKE_SPEC_NAME).write_text(
            SMOKE_SPEC_SOURCE, encoding="utf-8")

        server = None
        try:
            url = entry
            if not entry.startswith(("http://", "https://")):
                server, url = self._serve_work(run.work, entry)
            env = self._env(run, spec)
            env["SMOKE_URL"] = url
            command = [
                *spec.command,
                "--config", str(smoke_dir / SMOKE_CONFIG_NAME),
                str(smoke_dir / SMOKE_SPEC_NAME),
            ]
            started = time.perf_counter()
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(run.work),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    start_new_session=True,
                )
            except Exception as exc:                          # noqa: BLE001
                return {
                    "enabled": True, "ran": False, "ok": False,
                    "entry": entry, "duration_ms": 0.0,
                    "message": f"smoke_spawn_failed: {exc}",
                }

            timed_out = False
            try:
                stdout, stderr = proc.communicate(
                    timeout=SMOKE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            stdout, cut_out = _clip(stdout or "")
            stderr, cut_err = _clip(stderr or "")
            ok = (not timed_out) and proc.returncode == 0
            return {
                "enabled": True, "ran": True, "ok": ok,
                "entry": entry, "exit_code": proc.returncode,
                "timed_out": timed_out, "duration_ms": duration_ms,
                "stdout": stdout, "stderr": stderr,
                "output_truncated": cut_out or cut_err,
            }
        except Exception as exc:                              # noqa: BLE001
            return {
                "enabled": True, "ran": False, "ok": False,
                "entry": entry, "duration_ms": 0.0,
                "message": f"smoke_error: {exc}",
            }
        finally:
            if server is not None:
                try:
                    server.shutdown()
                    server.server_close()
                except Exception:                             # noqa: BLE001
                    pass

    @staticmethod
    def _smoke_entry(work: Path) -> str:
        configured = os.environ.get("PLAYWRIGHT_SMOKE_ENTRY", "").strip()
        if configured:
            if configured.startswith(("http://", "https://")):
                return configured
            rel = configured.lstrip("/")
            return rel if (work / rel).is_file() else ""
        for candidate in SMOKE_ENTRY_CANDIDATES:
            if (work / candidate).is_file():
                return candidate
        return ""

    @staticmethod
    def _serve_work(work: Path, entry: str) -> tuple[Any, str]:
        handler = functools.partial(_QuietStaticHandler, directory=str(work))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/{str(entry).lstrip('/')}"
        return httpd, url

    # ------------------------------------------------------------
    # 图片扫描
    # ------------------------------------------------------------
    def _image_snapshot(self, work: Path) -> Dict[str, tuple]:
        snapshot: Dict[str, tuple] = {}
        for path in self._image_files(work):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path)] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _new_images(self, work: Path,
                    before: Dict[str, tuple]) -> List[Dict[str, Any]]:
        fresh: List[Path] = []
        for path in self._image_files(work):
            try:
                stat = path.stat()
            except OSError:
                continue
            if before.get(str(path)) == (stat.st_mtime_ns, stat.st_size):
                continue
            fresh.append(path)
        def _mtime(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        fresh.sort(key=lambda p: (
            0 if "screenshot" in p.name.lower() else 1,
            -_mtime(p),
            str(p),
        ))
        out: List[Dict[str, Any]] = []
        for path in fresh[:MAX_IMAGES]:
            try:
                stat = path.stat()
                rel = path.relative_to(work).as_posix()
            except (OSError, ValueError):
                continue
            out.append({
                "path": rel,
                "mime": IMAGE_SUFFIXES.get(path.suffix.lower(), ""),
                "bytes": stat.st_size,
            })
        return out

    @staticmethod
    def _image_files(work: Path) -> List[Path]:
        files: List[Path] = []
        for root, dirs, names in os.walk(work):
            dirs[:] = [d for d in dirs if d not in _IMAGE_SKIP_DIRS]
            for name in names:
                path = Path(root) / name
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                try:
                    if path.stat().st_size > MAX_IMAGE_BYTES:
                        continue
                except OSError:
                    continue
                files.append(path)
        return files

    @staticmethod
    def _clean_args(args: Optional[List[str]]) -> Optional[List[str]]:
        if args is None:
            return []
        if not isinstance(args, (list, tuple)) or len(args) > 100:
            return None
        out: List[str] = []
        for value in args:
            if not isinstance(value, str):
                return None
            if "\x00" in value or len(value) > 2000:
                return None
            out.append(value)
        return out

    def _timeout(self, value: Optional[int]) -> Optional[int]:
        if value is None:
            return 60
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 1 or value > self.max_timeout_seconds:
            return None
        return value

    def _bootstrap(self, spec: ToolSpec, work: Path) -> None:
        """把公共工具声明的配置模板写进 run 目录（不覆盖 Agent 文件）。"""
        for relative, content in spec.bootstrap_files.items():
            target = (work / relative).resolve()
            if target != work and work not in target.parents:
                raise ToolRunError(f"bootstrap 路径跑出 run 目录：{relative}")
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _env(self, run: RunWorkspace, spec: ToolSpec) -> Dict[str, str]:
        """
        最小环境变量集。

        🔒 不继承 `.env` / LLM API Key / 宿主机凭据。
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "HOME": str(run.home),
            "TMPDIR": str(run.tmp),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(run.work),
            "RUN_DIR": str(run.work),
            "CI": "1",
        }
        env.update({str(k): str(v) for k, v in spec.env.items()})
        return env

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        """超时后杀掉整个进程组，避免子进程残留。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:                                     # noqa: BLE001
            try:
                proc.kill()
            except Exception:                                 # noqa: BLE001
                pass

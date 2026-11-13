"""
AIEOS UI Server —— 人类与 Front Agent 的唯一入口
==================================================

    Human
      ↕ 浏览器
    AIEOS UI（本文件）
      ↕
    AIEOSController
      ↕
    Front / Solo Agent / Task / Workspace

🔒 只用标准库 `http.server`，不引入新依赖。
🔒 绑定 127.0.0.1；不暴露到公网。
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Handler(BaseHTTPRequestHandler):
    controller = None
    web_search_token = ""
    server_version = "AIEOSUI/1.0"

    # ------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        return (
            (path / ".git").exists()
            or ((path / "HEAD").is_file() and (path / "objects").is_dir())
        )

    def _list_dirs(self, raw: str) -> dict:
        if raw:
            if not Path(raw).is_absolute():
                raise ValueError("目录浏览需要绝对路径")
            base = Path(raw).expanduser().resolve()
        else:
            base = Path.home().resolve()
        if not base.exists():
            raise ValueError("目录不存在")
        if not base.is_dir():
            raise ValueError("不是目录")
        entries = []
        for child in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
            try:
                if not child.is_dir() or child.name == ".git":
                    continue
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_git_repo": self._is_git_repo(child),
            })
        return {
            "ok": True,
            "path": str(base),
            "parent": str(base.parent) if base.parent != base else "",
            "home": str(Path.home().resolve()),
            "is_git_repo": self._is_git_repo(base),
            "entries": entries,
        }

    # ------------------------------------------------------------
    # GET
    # ------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in ("/", "/index.html"):
            self._send_file(STATIC / "index.html")
            return
        if path in ("/task", "/task.html"):
            self._send_file(STATIC / "task.html")
            return
        if path.startswith("/static/"):
            relative = path[len("/static/"):]
            target = (STATIC / relative).resolve()
            if STATIC.resolve() not in target.parents and target != STATIC.resolve():
                self.send_error(403)
                return
            self._send_file(target)
            return
        if path == "/api/state":
            self._send_json(self.controller.state())
            return
        if path == "/api/settings":
            self._send_json(self.controller.settings_snapshot())
            return
        if path == "/api/commit/repo":
            query = parse_qs(parsed.query)
            requested = str((query.get("path") or [""])[0] or "").strip()
            self._send_json(self.controller.commit_repo_info(requested))
            return
        if path == "/api/fs/dirs":
            query = parse_qs(parsed.query)
            raw = str((query.get("path") or [""])[0] or "").strip()
            try:
                self._send_json(self._list_dirs(raw))
            except (OSError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)}, status=400)
            return
        if path.startswith("/api/task/"):
            task_id = path[len("/api/task/"):].strip()
            if not task_id or not re.fullmatch(r"[A-Za-z0-9_.:-]+", task_id):
                self.send_error(400)
                return
            try:
                self._send_json(self.controller.task_detail(task_id))
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=404)
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        self.send_error(404)

    # ------------------------------------------------------------
    # POST
    # ------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        payload = self._read_json()
        try:
            if path in ("/internal/web-search", "/internal/web-fetch"):
                supplied = str(
                    self.headers.get("X-AIEOS-Search-Token") or "")
                if not self.web_search_token or not hmac.compare_digest(
                        supplied, self.web_search_token):
                    self._send_json(
                        {"ok": False, "error": "forbidden"}, status=403)
                    return
                agent_id = str(
                    payload.get("agent_id") or self.controller.front_agent)
                if path == "/internal/web-search":
                    result = self.controller.web_search(
                        agent_id,
                        str(payload.get("query") or ""),
                        count=int(payload.get("count") or 5),
                        need_content=bool(payload.get("need_content", False)),
                        ori_question=str(payload.get("ori_question") or ""),
                    )
                else:
                    result = self.controller.web_fetch(
                        agent_id, str(payload.get("url") or ""))
                self._send_json({"ok": True, **result})
                return
            if path == "/api/submit":
                text = str(payload.get("text") or "").strip()
                commit_repo = str(
                    payload.get("commit_repo") or "").strip()
                job_id = self.controller.submit(
                    text, commit_repo=commit_repo)
                self._send_json({"ok": True, "job_id": job_id})
                return
            if path == "/api/retry":
                job_id = str(payload.get("job_id") or "").strip()
                new_job_id = self.controller.retry(job_id)
                self._send_json({"ok": True, "job_id": new_job_id})
                return
            if path == "/api/answer":
                task_id = str(payload.get("task_id") or "").strip()
                answer = str(payload.get("answer") or "").strip()
                option_id = str(payload.get("option_id") or "").strip()
                job_id = self.controller.answer_question(
                    task_id, answer, option_id=option_id)
                self._send_json({"ok": True, "job_id": job_id})
                return
            if path == "/api/settings":
                settings = self.controller.update_settings(
                    front_agent=payload.get("front_agent"),
                    max_parallel_teams=payload.get("max_parallel_teams"),
                    max_solo_turns=payload.get("max_solo_turns"),
                    pi_work_agents=payload.get("pi_work_agents"),
                    disabled_agents=payload.get("disabled_agents"),
                    prompt_log_enabled=payload.get("prompt_log_enabled"),
                    agent_thinking=payload.get("agent_thinking"),
                    pi_timeout_enabled=payload.get("pi_timeout_enabled"),
                    pi_timeout_seconds=payload.get("pi_timeout_seconds"),
                    work_mode=payload.get("work_mode"),
                    front_mode=payload.get("front_mode"),
                    commit_mode=payload.get("commit_mode"),
                    commit_output_repo=payload.get("commit_output_repo"),
                    commit_base_ref=payload.get("commit_base_ref"),
                    commit_auto_merge=payload.get("commit_auto_merge"),
                    commit_max_file_bytes=payload.get(
                        "commit_max_file_bytes"),
                    commit_max_total_bytes=payload.get(
                        "commit_max_total_bytes"),
                )
                self._send_json({"ok": True, "settings": settings})
                return
            if path == "/api/settings/system":
                data = payload.get("data")
                if not isinstance(data, dict):
                    data = {}
                result = self.controller.apply_system_settings(
                    str(payload.get("action") or ""), data)
                self._send_json({"ok": True, "system": result})
                return
            if path == "/api/settings/roles":
                data = payload.get("data")
                if not isinstance(data, dict):
                    data = {}
                result = self.controller.apply_worker_roles(
                    str(payload.get("action") or ""), data)
                self._send_json({"ok": True, "roles": result})
                return
        except Exception as exc:                              # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        # 保留在 stderr，但不写 HTTP 访问日志到前端。
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), fmt % args))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="aieos-ui",
        description="AIEOS 人类交互界面（Front Agent + Team）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None,
                    help="Runtime 数据库；默认 runs/ui/aieos.db")
    ap.add_argument("--export-dir", default=None,
                    help="Front 导出产物的宿主机目录；默认 runs/exports")
    ap.add_argument("--front-agent", default="agent_a")
    ap.add_argument("--max-teams", type=int, default=3)
    ap.add_argument("--worker-turns", type=int, default=8)
    ap.add_argument("--integration-turns", type=int, default=8)
    ap.add_argument("--solo-turns", type=int, default=24)
    ap.add_argument("--cognitive-budget", type=int, default=65536)
    ap.add_argument(
        "--pi-agents", default="agent_b,agent_c,agent_d,agent_e",
        help="逗号分隔：这些 Agent 的 Solo Task 使用 Pi Work 引擎")
    ap.add_argument(
        "--pi-cli",
        default=str(ROOT / "experiments" / "pi_compare" / "pi_node"
                    / "node_modules" / ".bin" / "pi"),
        help="Pi CLI 路径")
    ap.add_argument("--pi-provider", default="",
                    help="覆盖 Pi provider；空 = 用 Agent Brain 的 provider")
    ap.add_argument("--pi-model", default="",
                    help="覆盖 Pi model；空 = 用 Agent Brain 的 model")
    ap.add_argument("--pi-agent-dir", default=None,
                    help="PI_CODING_AGENT_DIR（Pi 配置目录）")
    ap.add_argument("--pi-timeout", type=float, default=7200.0)
    ap.add_argument("--task-root", default=None,
                    help="真实 Task 目录根；默认 <repo>/tasks")
    ap.add_argument(
        "--commit-mode", default="manual",
        choices=["manual", "limited", "auto"],
        help="输出仓库提交策略；默认 manual（每次人类确认）")
    ap.add_argument(
        "--output-repo", default="",
        help="独立输出仓库路径；空 = 不启用提交")
    ap.add_argument(
        "--commit-base-ref", default="main",
        help="输出仓库基线分支；默认 main")
    ap.add_argument(
        "--commit-auto-merge", action="store_true",
        help="auto 模式下允许 fast-forward 合并到基线分支")
    # 保留旧参数兼容

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    from main import build_runtime
    from core.logging_setup import setup_logging
    from core.paths import (
        ENV_FILE, GLOBAL_CONFIG_FILE, GLOBAL_LLM_CONFIG_FILE,
        GLOBAL_WORKER_ROLES_FILE,
    )
    from ui.rounds import TaskRoundLog
    from work.controller import AIEOSController
    from work.settings_admin import SystemConfigAdmin

    setup_logging(level=os.environ.get("AI_ECONOMY_LOG_LEVEL", "INFO"))

    # Pi 的 web_search CLI 桥；provider key 不进入 Pi。
    search_token = secrets.token_urlsafe(32)
    Handler.web_search_token = search_token
    search_url = f"http://127.0.0.1:{int(args.port)}"
    os.environ["AIEOS_WEB_SEARCH_URL"] = search_url
    os.environ["AIEOS_WEB_SEARCH_TOKEN"] = search_token
    os.environ["AIEOS_WEB_SEARCH_CMD"] = (
        f"{sys.executable} {ROOT / 'scripts' / 'aieos_web_search.py'}")
    os.environ["AIEOS_WEB_SEARCH_BASE_URL"] = "https://api.deepseek.com"

    if args.db:
        db_path = Path(args.db).expanduser().resolve()
    else:
        db_path = (ROOT / "runs" / "ui" / "aieos.db").resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    runtime = build_runtime(db_path=str(db_path))

    # 每天的首次启动清理 `data/tool_runs/run_*`（一次性沙箱副本）。
    # 🔒 同一天最多一次；失败不阻止启动。
    try:
        cleanup = runtime.tool_runner.cleanup_runs_once_per_day()
        print(f"Tool run 清理：{cleanup}", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"Tool run 清理失败（已忽略）：{exc}", flush=True)

    system_admin = SystemConfigAdmin(
        config_path=GLOBAL_CONFIG_FILE,
        llm_path=GLOBAL_LLM_CONFIG_FILE,
        env_path=ENV_FILE,
        backup_dir=db_path.parent / "config_backups",
    )
    controller = AIEOSController(
        runtime,
        front_agent=str(args.front_agent),
        max_parallel_teams=max(1, int(args.max_teams)),
        max_worker_turns=max(1, int(args.worker_turns)),
        max_integration_turns=max(1, int(args.integration_turns)),
        max_solo_turns=max(1, int(args.solo_turns)),
        cognitive_budget=max(1, int(args.cognitive_budget)),
        pi_work_agents=[
            item.strip() for item in str(args.pi_agents or "").split(",")
            if item.strip()
        ],
        pi_cli_path=args.pi_cli,
        pi_provider=args.pi_provider,
        pi_model=args.pi_model,
        pi_agent_dir=(
            Path(args.pi_agent_dir).expanduser().resolve()
            if args.pi_agent_dir else None
        ),
        pi_timeout_seconds=max(1.0, float(args.pi_timeout)),
        commit_mode=args.commit_mode,
        commit_output_repo=args.output_repo,
        commit_base_ref=args.commit_base_ref,
        commit_auto_merge=bool(args.commit_auto_merge),
        task_root=(
            Path(args.task_root).expanduser().resolve()
            if args.task_root else (ROOT / "tasks").resolve()
        ),
        # 当前阶段只跑 Solo；Team 路径保留在代码里但不由 UI 触发。
        solo_only=True,
        settings_path=db_path.parent / "ui_settings.json",
        log_dir=db_path.parent / "jobs",
        round_log=TaskRoundLog(db_path.parent / "rounds"),
        export_root=(
            Path(args.export_dir).expanduser().resolve()
            if args.export_dir else (ROOT / "runs" / "exports").resolve()
        ),
        worker_roles_path=GLOBAL_WORKER_ROLES_FILE,
        worker_roles_backup_dir=db_path.parent / "config_backups",
        prompt_log_path=db_path.parent / "prompts",
        system_settings=system_admin,
    )
    Handler.controller = controller
    httpd = ThreadingHTTPServer((args.host, int(args.port)), Handler)
    print(
        f"AIEOS UI 已启动：http://{args.host}:{args.port}/\n"
        f"Front Agent：{controller.front_agent}\n"
        f"DB：{db_path}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭 AIEOS UI…", flush=True)
    finally:
        controller.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

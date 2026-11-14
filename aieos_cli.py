"""AIEOS Launcher CLI（Phase 1）。

只依赖标准库。职责：
  · 启动 / 停止 / 重启 / 状态 / 日志 / doctor
  · 管理进程级启动参数（runs/ui/launcher.json）
  · 复用 start_aieos.sh 的 PID / 端口 / 健康检查

不管理 UI 运行设置（front_agent / pi_agents / commit_mode 等）；
它们仍以 runs/ui/ui_settings.json 为权威，由设置页修改。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DEFAULT_START_SCRIPT = ROOT / "start_aieos.sh"
DEFAULT_RUN_DIR = ROOT / "runs" / "ui"

DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8000,
    "db": "",
    "log": "runs/ui/server.log",
    "pid_file": "runs/ui/aieos.pid",
    "pi_cli": "experiments/pi_compare/pi_node/node_modules/.bin/pi",
    "pi_provider": "",
    "pi_model": "",
    "pi_agent_dir": "",
    "task_root": "",
    "export_dir": "",
}

ENV_KEYS = {
    "host": "AIEOS_HOST",
    "port": "AIEOS_PORT",
    "db": "AIEOS_DB",
    "log": "AIEOS_LOG",
    "pid_file": "AIEOS_PID_FILE",
    "pi_cli": "AIEOS_PI_CLI",
    "pi_provider": "AIEOS_PI_PROVIDER",
    "pi_model": "AIEOS_PI_MODEL",
    "pi_agent_dir": "AIEOS_PI_AGENT_DIR",
    "task_root": "AIEOS_TASK_ROOT",
    "export_dir": "AIEOS_EXPORT_DIR",
}

PATH_KEYS = {
    "db", "log", "pid_file", "pi_cli",
    "pi_agent_dir", "task_root", "export_dir",
}
INT_KEYS = {"port"}


class LauncherError(RuntimeError):
    """用户可以修正的启动器错误。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce(key: str, value: Any) -> Any:
    if key in INT_KEYS:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise LauncherError(f"{key} 必须是整数：{value!r}") from exc
        return number
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Launcher:
    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        config_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
        start_script: Optional[Path] = None,
    ) -> None:
        self.root = Path(root or ROOT).expanduser().resolve()
        run_dir = self.root / "runs" / "ui"
        self.config_path = Path(config_path or run_dir / "launcher.json")
        self.state_path = Path(state_path or run_dir / "launcher-state.json")
        self.ui_settings_path = run_dir / "ui_settings.json"
        self.start_script = Path(start_script or self.root / "start_aieos.sh")

    # ------------------------------------------------------------
    # config
    # ------------------------------------------------------------
    def _read_json(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LauncherError(f"读取 {path} 失败：{exc}") from exc
        if not isinstance(data, dict):
            raise LauncherError(f"{path} 必须是 JSON 对象")
        return data

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def file_config(self) -> Dict[str, Any]:
        raw = self._read_json(self.config_path)
        config = dict(DEFAULTS)
        for key in DEFAULTS:
            if key in raw:
                config[key] = _coerce(key, raw[key])
        return config

    def merge_config(
        self,
        overrides: Optional[Dict[str, Any]] = None,
        *,
        use_env: bool = True,
    ) -> Dict[str, Any]:
        """CLI 参数 > 环境变量 > launcher.json > 默认值。"""
        config = self.file_config()
        if use_env:
            for key, env_name in ENV_KEYS.items():
                raw = os.environ.get(env_name)
                if raw is not None:
                    config[key] = _coerce(key, raw)
        for key, value in (overrides or {}).items():
            if key in DEFAULTS and value is not None:
                config[key] = _coerce(key, value)
        self.validate_config(config)
        return config

    def validate_config(self, config: Dict[str, Any]) -> None:
        host = str(config.get("host") or "").strip()
        if not host:
            raise LauncherError("host 不能为空")
        port = int(config.get("port") or 0)
        if port < 1 or port > 65535:
            raise LauncherError("port 必须在 1–65535 之间")
        for key in ("log", "pid_file"):
            if not str(config.get(key) or "").strip():
                raise LauncherError(f"{key} 不能为空")
        if not str(config.get("pi_cli") or "").strip():
            raise LauncherError("pi_cli 不能为空")
        for key in ("db", "pi_agent_dir", "task_root", "export_dir"):
            value = config.get(key)
            if value is not None and not isinstance(value, str):
                raise LauncherError(f"{key} 必须是字符串")

    def save_config(self, config: Dict[str, Any]) -> None:
        self.validate_config(config)
        payload = {key: config.get(key, DEFAULTS[key]) for key in DEFAULTS}
        self._write_json(self.config_path, payload)

    def set_value(self, key: str, value: Any) -> Any:
        if key not in DEFAULTS:
            raise LauncherError(
                f"未知启动参数：{key}；可用：{', '.join(DEFAULTS)}")
        coerced = _coerce(key, value)
        raw = self._read_json(self.config_path)
        raw[key] = coerced
        config = dict(DEFAULTS)
        config.update({k: _coerce(k, v) for k, v in raw.items() if k in DEFAULTS})
        self.validate_config(config)
        self.save_config(config)
        return coerced

    def unset_value(self, key: str) -> None:
        if key not in DEFAULTS:
            raise LauncherError(
                f"未知启动参数：{key}；可用：{', '.join(DEFAULTS)}")
        raw = self._read_json(self.config_path)
        raw.pop(key, None)
        self._write_json(self.config_path, raw)

    def reset_config(self) -> None:
        if self.config_path.exists():
            self.config_path.unlink()

    # ------------------------------------------------------------
    # env / process
    # ------------------------------------------------------------
    def environ(self, config: Dict[str, Any]) -> Dict[str, str]:
        env = os.environ.copy()
        for key, env_name in ENV_KEYS.items():
            env[env_name] = str(config.get(key, DEFAULTS[key]))
        return env

    def _run_start_script(
        self, action: str, config: Dict[str, Any],
    ) -> int:
        if not self.start_script.exists():
            raise LauncherError(f"找不到启动脚本：{self.start_script}")
        try:
            result = subprocess.run(
                [str(self.start_script), action],
                cwd=str(self.root),
                env=self.environ(config),
            )
        except OSError as exc:
            raise LauncherError(f"执行启动脚本失败：{exc}") from exc
        return int(result.returncode)

    def display_url(self, config: Dict[str, Any]) -> str:
        host = str(config.get("host") or "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{int(config.get('port') or 0)}"

    def record_state(self, config: Dict[str, Any]) -> None:
        pid = 0
        try:
            pid = int(
                (self.root / str(config.get("pid_file") or "")).read_text(
                    encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid <= 0:
            return
        self._write_json(self.state_path, {
            "pid": pid,
            "host": str(config.get("host") or ""),
            "port": int(config.get("port") or 0),
            "url": self.display_url(config),
            "log": str(self.root / str(config.get("log") or "")),
            "pid_file": str(
                self.root / str(config.get("pid_file") or "")),
            "started_at": _now_iso(),
        })

    def read_state(self) -> Dict[str, Any]:
        return self._read_json(self.state_path)

    def clear_state(self) -> None:
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _health(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(
                url.rstrip("/") + "/api/health", timeout=1.5) as response:
                return 200 <= int(response.status) < 300
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def status(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = config or self.merge_config()
        state = self.read_state()
        pid = int(state.get("pid") or 0)
        if pid and not self._pid_alive(pid):
            self.clear_state()
            state = {}
            pid = 0

        url = str(state.get("url") or "")
        if not pid:
            try:
                pid = int(
                    (self.root / str(config.get("pid_file") or "")).read_text(
                        encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = 0
            if not pid or not self._pid_alive(pid):
                pid = 0
            url = self.display_url(config)

        running = bool(pid and self._pid_alive(pid))
        if not url:
            url = self.display_url(config)
        log_path = str(
            state.get("log")
            or (self.root / str(config.get("log") or "")))
        return {
            "running": running,
            "pid": pid,
            "url": url,
            "health": self._health(url) if running else False,
            "log": log_path,
            "config": str(self.config_path),
        }

    # ------------------------------------------------------------
    # commands
    # ------------------------------------------------------------
    def start(
        self, config: Dict[str, Any], *, save: bool = False,
    ) -> int:
        if save:
            self.save_config(config)
        current = self.status(config)
        if current["running"]:
            print(f"AIEOS 已经在运行（PID {current['pid']}）")
            print(f"  {current['url']}")
            print("如需应用新的启动参数，请使用 restart。")
            return 0
        code = self._run_start_script("start", config)
        if code == 0:
            self.record_state(config)
        else:
            self.clear_state()
        return code

    def stop(self, config: Optional[Dict[str, Any]] = None) -> int:
        config = config or self.merge_config()
        state = self.read_state()
        if state.get("port"):
            config = dict(config)
            config["port"] = int(state["port"])
            config["host"] = str(state.get("host") or config["host"])
        code = self._run_start_script("stop", config)
        if code == 0:
            self.clear_state()
        return code

    def restart(
        self, config: Dict[str, Any], *, save: bool = False,
    ) -> int:
        if save:
            self.save_config(config)
        code = self._run_start_script("restart", config)
        if code == 0:
            self.record_state(config)
        else:
            self.clear_state()
        return code

    def logs(self, lines: int = 100, follow: bool = False) -> int:
        config = self.merge_config()
        state = self.read_state()
        raw_path = state.get("log") or (self.root / str(config.get("log") or ""))
        path = Path(str(raw_path))
        if not path.exists():
            print(f"日志文件不存在：{path}", file=sys.stderr)
            return 1
        lines = max(1, int(lines))
        if follow:
            print(f"==> {path} <==", flush=True)
            try:
                return int(subprocess.run(
                    ["tail", "-n", str(lines), "-f", str(path)]).returncode)
            except KeyboardInterrupt:
                return 0
        try:
            content = path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(f"读取日志失败：{exc}", file=sys.stderr)
            return 1
        print("\n".join(content[-lines:]))
        return 0

    def doctor(self) -> int:
        checks: List[Tuple[bool, str, bool]] = []
        # (ok, message, blocking)

        venv_python = self.root / ".venv" / "bin" / "python"
        checks.append((
            venv_python.exists() and os.access(venv_python, os.X_OK),
            f"虚拟环境 Python：{venv_python}",
            True,
        ))
        env_file = self.root / ".env"
        checks.append((env_file.exists(), f".env：{env_file}", False))

        try:
            config = self.merge_config()
            checks.append((True, f"launcher 配置：{self.config_path}", False))
        except LauncherError as exc:
            print(f"❌ launcher 配置无效：{exc}", file=sys.stderr)
            return 1

        pi_cli = Path(str(config["pi_cli"]))
        if not pi_cli.is_absolute():
            pi_cli = self.root / pi_cli
        checks.append((
            pi_cli.exists() and os.access(pi_cli, os.X_OK),
            f"Pi CLI：{pi_cli}",
            True,
        ))

        try:
            if self.ui_settings_path.exists():
                self._read_json(self.ui_settings_path)
            checks.append((
                True, f"UI 设置：{self.ui_settings_path}", False))
        except LauncherError as exc:
            checks.append((False, f"UI 设置无效：{exc}", True))

        for key in ("log", "pid_file"):
            path = self.root / str(config[key])
            parent = path.parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
                writable = os.access(parent, os.W_OK)
            except OSError:
                writable = False
            checks.append((
                writable, f"{key} 目录可写：{parent}", True))

        status = self.status(config)
        if status["running"]:
            checks.append((
                status["health"],
                f"运行实例：PID {status['pid']} · {status['url']}",
                True,
            ))
        else:
            host = str(config.get("host") or "127.0.0.1")
            if host in ("0.0.0.0", "::", ""):
                host = "127.0.0.1"
            try:
                with socket.create_connection(
                    (host, int(config["port"])), timeout=0.5):
                    port_free = False
            except OSError:
                port_free = True
            checks.append((
                port_free,
                f"端口 {config['port']} 可用",
                False,
            ))

        blocking = False
        for ok, message, is_blocking in checks:
            icon = "✅" if ok else ("❌" if is_blocking else "⚠️ ")
            print(f"{icon} {message}")
            if not ok and is_blocking:
                blocking = True
        return 1 if blocking else 0

    def open_browser(self) -> int:
        status = self.status()
        if not status["running"]:
            print("AIEOS 当前没有运行；先执行 ./aieos start", file=sys.stderr)
            return 1
        url = str(status["url"])
        print(url)
        webbrowser.open(url)
        return 0

    def config_summary(self) -> None:
        config = self.merge_config()
        print("Launcher 参数（进程级，重启生效）")
        for key in DEFAULTS:
            print(f"  {key:<14} {config.get(key, '')}")
        print()
        print("UI 运行设置（ui_settings.json 优先，由设置页管理）")
        try:
            ui = self._read_json(self.ui_settings_path)
        except LauncherError as exc:
            print(f"  （读取失败：{exc}）")
            return
        if not ui:
            print("  （文件不存在；使用 UI 默认值）")
            return
        for key in (
            "front_agent", "pi_work_agents", "work_mode", "front_mode",
            "commit_mode", "commit_output_repo",
        ):
            if key in ui:
                value = ui[key]
                if isinstance(value, list):
                    value = ",".join(str(item) for item in value)
                print(f"  {key:<18} {value}")


def _launcher_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--db")
    parser.add_argument("--log")
    parser.add_argument("--pid-file")
    parser.add_argument("--pi-cli")
    parser.add_argument("--pi-provider")
    parser.add_argument("--pi-model")
    parser.add_argument("--pi-agent-dir")
    parser.add_argument("--task-root")
    parser.add_argument("--export-dir")
    parser.add_argument(
        "--save", action="store_true",
        help="把本次参数写入 launcher.json")


def _overrides(args: argparse.Namespace) -> Dict[str, Any]:
    mapping = {
        "host": "host",
        "port": "port",
        "db": "db",
        "log": "log",
        "pid_file": "pid_file",
        "pi_cli": "pi_cli",
        "pi_provider": "pi_provider",
        "pi_model": "pi_model",
        "pi_agent_dir": "pi_agent_dir",
        "task_root": "task_root",
        "export_dir": "export_dir",
    }
    return {
        key: getattr(args, arg)
        for arg, key in mapping.items()
        if getattr(args, arg, None) is not None
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aieos",
        description="AIEOS Launcher：启动 / 停止 / 重启 / 参数 / 日志",
    )
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="启动 AIEOS")
    _launcher_options(start)
    restart = sub.add_parser("restart", help="重启 AIEOS")
    _launcher_options(restart)
    sub.add_parser("stop", help="停止 AIEOS")

    status = sub.add_parser("status", help="查看状态")
    status.add_argument("--json", action="store_true")

    logs = sub.add_parser("logs", help="查看日志")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--lines", type=int, default=100)

    sub.add_parser("open", help="打开 AIEOS 界面")
    sub.add_parser("doctor", help="检查启动环境与配置")

    config = sub.add_parser("config", help="管理启动参数")
    config_sub = config.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="显示参数")
    get = config_sub.add_parser("get", help="读取一个参数")
    get.add_argument("key")
    set_ = config_sub.add_parser("set", help="写入一个参数")
    set_.add_argument("key")
    set_.add_argument("value")
    unset = config_sub.add_parser("unset", help="恢复一个参数的默认值")
    unset.add_argument("key")
    config_sub.add_parser("reset", help="删除 launcher.json")
    return parser


def _menu(launcher: Launcher) -> int:
    print("AIEOS Launcher")
    print("  1) 启动")
    print("  2) 停止")
    print("  3) 重启")
    print("  4) 状态")
    print("  5) 修改启动参数")
    print("  6) 查看日志")
    print("  7) 打开界面")
    print("  d) doctor")
    print("  q) 退出")
    while True:
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            if choice in ("q", "quit", "exit"):
                return 0
            if choice == "1":
                launcher.start(launcher.merge_config())
            elif choice == "2":
                launcher.stop()
            elif choice == "3":
                launcher.restart(launcher.merge_config())
            elif choice == "4":
                _print_status(launcher.status())
            elif choice == "5":
                launcher.config_summary()
                key = input("参数名（留空取消）> ").strip()
                if not key:
                    continue
                value = input("新值 > ").strip()
                launcher.set_value(key, value)
                print(f"✅ 已保存 {key}；下次 start/restart 生效")
            elif choice == "6":
                launcher.logs()
            elif choice == "7":
                launcher.open_browser()
            elif choice == "d":
                launcher.doctor()
            else:
                print("未知选项")
        except LauncherError as exc:
            print(f"❌ {exc}", file=sys.stderr)


def _print_status(status: Dict[str, Any]) -> None:
    if status["running"]:
        print("AIEOS 正在运行")
        print(f"  URL:    {status['url']}")
        print(f"  PID:    {status['pid']}")
        print(f"  健康:   {'OK' if status['health'] else '未通过'}")
        print(f"  日志:   {status['log']}")
    else:
        print("AIEOS 当前没有运行。")


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    launcher = Launcher()
    try:
        if not args.command:
            return _menu(launcher)
        if args.command == "start":
            config = launcher.merge_config(_overrides(args))
            return launcher.start(config, save=bool(args.save))
        if args.command == "restart":
            config = launcher.merge_config(_overrides(args))
            return launcher.restart(config, save=bool(args.save))
        if args.command == "stop":
            return launcher.stop()
        if args.command == "status":
            status = launcher.status()
            if args.json:
                print(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                _print_status(status)
            return 0 if status["running"] else 1
        if args.command == "logs":
            return launcher.logs(lines=args.lines, follow=args.follow)
        if args.command == "open":
            return launcher.open_browser()
        if args.command == "doctor":
            return launcher.doctor()
        if args.command == "config":
            if args.config_command in (None, "show"):
                launcher.config_summary()
                return 0
            if args.config_command == "get":
                config = launcher.merge_config()
                if args.key not in DEFAULTS:
                    raise LauncherError(f"未知启动参数：{args.key}")
                print(config[args.key])
                return 0
            if args.config_command == "set":
                value = launcher.set_value(args.key, args.value)
                print(f"✅ 已保存 {args.key}={value}；下次 start/restart 生效")
                return 0
            if args.config_command == "unset":
                launcher.unset_value(args.key)
                print(f"✅ 已恢复 {args.key} 的默认值")
                return 0
            if args.config_command == "reset":
                try:
                    answer = input(
                        "删除 launcher.json 并恢复全部默认值？[y/N] ")
                except EOFError:
                    print("已取消")
                    return 0
                if answer.strip().lower() not in ("y", "yes"):
                    print("已取消")
                    return 0
                launcher.reset_config()
                print("✅ 已恢复默认值")
                return 0
    except LauncherError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

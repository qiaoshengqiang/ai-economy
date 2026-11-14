"""AIEOS Launcher CLI（Phase 1）单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aieos_cli import Launcher, LauncherError


def _launcher(tmp_path: Path) -> Launcher:
    return Launcher(root=tmp_path)


def test_defaults_and_saved_config(tmp_path, monkeypatch):
    launcher = _launcher(tmp_path)
    assert launcher.merge_config(use_env=False)["port"] == 8000

    monkeypatch.setenv("AIEOS_PORT", "9000")
    assert launcher.merge_config()["port"] == 9000

    launcher.set_value("port", 8001)
    assert launcher.file_config()["port"] == 8001
    # 环境变量仍然高于文件
    assert launcher.merge_config()["port"] == 9000
    # 命令行参数最高
    assert launcher.merge_config({"port": 8002})["port"] == 8002


def test_validation_rejects_bad_port(tmp_path):
    launcher = _launcher(tmp_path)
    with pytest.raises(LauncherError):
        launcher.set_value("port", 0)
    with pytest.raises(LauncherError):
        launcher.set_value("port", 70000)
    with pytest.raises(LauncherError):
        launcher.set_value("unknown", "x")


def test_unset_and_reset(tmp_path):
    launcher = _launcher(tmp_path)
    launcher.set_value("pi_provider", "deepseek")
    assert launcher.file_config()["pi_provider"] == "deepseek"

    launcher.unset_value("pi_provider")
    assert launcher.file_config()["pi_provider"] == ""

    launcher.reset_config()
    assert not launcher.config_path.exists()


def test_status_is_not_running_without_pid(tmp_path):
    launcher = _launcher(tmp_path)
    status = launcher.status(launcher.merge_config(use_env=False))
    assert status["running"] is False
    assert status["url"] == "http://127.0.0.1:8000"


def test_record_state_uses_pid_file(tmp_path):
    launcher = _launcher(tmp_path)
    config = launcher.merge_config(use_env=False)
    pid_file = tmp_path / config["pid_file"]
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("12345\n", encoding="utf-8")

    launcher.record_state(config)
    state = json.loads(launcher.state_path.read_text(encoding="utf-8"))
    assert state["pid"] == 12345
    assert state["url"] == "http://127.0.0.1:8000"

    launcher.clear_state()
    assert not launcher.state_path.exists()


def test_wildcard_host_uses_loopback_url(tmp_path):
    launcher = _launcher(tmp_path)
    config = launcher.merge_config({"host": "0.0.0.0"}, use_env=False)
    assert launcher.display_url(config) == "http://127.0.0.1:8000"


def test_environ_resolves_relative_paths(tmp_path):
    launcher = _launcher(tmp_path)
    config = launcher.merge_config(use_env=False)
    env = launcher.environ(config)
    assert env["AIEOS_PI_CLI"] == str(
        tmp_path / "experiments/pi_compare/pi_node/node_modules/.bin/pi")
    assert env["AIEOS_LOG"] == str(tmp_path / "runs/ui/server.log")
    assert env["AIEOS_PID_FILE"] == str(tmp_path / "runs/ui/aieos.pid")


def test_default_paths_are_portable():
    from aieos_cli import DEFAULTS

    for key in ("pi_cli", "log", "pid_file"):
        assert not Path(DEFAULTS[key]).is_absolute()

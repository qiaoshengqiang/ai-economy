"""
AI Economy OS - 路径工具
============================================================

整个项目所有「文件在哪里」的问题，都只在这里回答一次。

为什么要单独有这个文件？
    Agent 的工作空间隔离是本项目的核心原则之一。
    如果每个模块都自己拼 `../agents/agent_a/...` 这种路径，
    迟早会出现 Agent A 摸到 Agent B 的文件这种事。
    所以路径计算集中在一处，方便审计和以后加权限校验。
"""

from __future__ import annotations

from pathlib import Path

# ------------------------------------------------------------
# 项目根目录
# 本文件位于 <项目根>/core/paths.py ，所以上溯两级就是根目录。
# ------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

CONFIG_DIR: Path = PROJECT_ROOT / "config"
AGENTS_DIR: Path = PROJECT_ROOT / "agents"
DATA_DIR: Path = PROJECT_ROOT / "data"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
TESTS_DIR: Path = PROJECT_ROOT / "tests"

GLOBAL_CONFIG_FILE: Path = CONFIG_DIR / "config.yaml"
#: GUI「系统设置」管理的 LLM 注册表（providers / models / credentials）。
#: 它是这三段的唯一权威；config.yaml 不再保留副本。
GLOBAL_LLM_CONFIG_FILE: Path = CONFIG_DIR / "llm.yaml"
#: GUI「常规设置」管理的 Worker Roles。
GLOBAL_WORKER_ROLES_FILE: Path = CONFIG_DIR / "worker_roles.yaml"
ENV_FILE: Path = PROJECT_ROOT / ".env"


def agent_dir(agent_id: str) -> Path:
    """返回某个 Agent 的私有根目录：<项目根>/agents/<agent_id>/"""
    return AGENTS_DIR / agent_id


def agent_config_file(agent_id: str) -> Path:
    """返回某个 Agent 的私有配置文件。"""
    return agent_dir(agent_id) / "config.yaml"


def agent_workspace_dir(agent_id: str) -> Path:
    """返回某个 Agent 的工作空间目录（它干活产出的文件放这里）。"""
    return agent_dir(agent_id) / "workspace"


def agent_memory_dir(agent_id: str) -> Path:
    """返回某个 Agent 的记忆目录（它私有的经历与知识）。"""
    return agent_dir(agent_id) / "memory"


def ensure_runtime_dirs() -> None:
    """
    确保运行时需要的目录存在。

    data/ 和 logs/ 不进 git（数据库和日志是运行产物），
    所以在新克隆的机器上可能不存在，启动时补一下。
    """
    for directory in (DATA_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

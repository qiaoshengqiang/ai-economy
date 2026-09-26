"""
Test: 启动流程（Step 1 的验收测试）
============================================================

这个文件直接验证「第一阶段成功标准」：

    python3 main.py
        ↓
    创建 Agent A / Agent B
        ↓
    读取各自配置
        ↓
    创建各自钱包
        ↓
    读取余额（各等于配置里的 initial_balance）
        ↓
    System status: READY

如果这个文件的测试全过，说明 Step 1 达标了。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.ledger import format_amount

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def configured_initial_balance() -> float:
    """
    从真实配置里读出 Agent 的初始余额。

    为什么不直接写 100？
        因为 initial_balance 是 config/config.yaml 里的可调参数。
        测试写死数字的话，你一改配置就会假报警。
    """
    from core.config import load_agent_config

    return load_agent_config("agent_a").initial_balance


# ============================================================
# 启动逻辑
# ============================================================
def test_build_economy_creates_two_agents(tmp_path):
    """
    直接调用 main.build_economy()，验证两个 Agent 被正确创建。
    """
    from main import build_economy

    db_file = tmp_path / "test_economy.db"
    economy = build_economy(db_path=str(db_file))

    try:
        agents = economy.all_agents()
        assert len(agents) == 2

        balances = {a.agent_id: a.balance for a in agents}
        expected = configured_initial_balance()
        assert balances == {"agent_a": expected, "agent_b": expected}

        assert economy.is_ready() is True
        assert db_file.exists()
    finally:
        economy.close()


def test_database_tables_are_created(tmp_path):
    from main import build_economy

    db_file = tmp_path / "test_tables.db"
    economy = build_economy(db_path=str(db_file))

    try:
        tables = economy.db.table_names()
        for expected in ("agents", "wallets", "ledger_transactions", "llm_calls"):
            assert expected in tables, f"缺少表：{expected}"
    finally:
        economy.close()


def test_restart_does_not_duplicate_money(tmp_path):
    """
    【重要】程序重启不能重新发钱。

    跑两次 build_economy，余额必须还是初始值，
    而不是变成 200。这是账本持久化是否正确的关键验证。
    """
    from main import build_economy

    db_file = tmp_path / "test_restart.db"

    economy = build_economy(db_path=str(db_file))
    first = {a.agent_id: a.balance for a in economy.all_agents()}
    economy.close()

    economy2 = build_economy(db_path=str(db_file))
    second = {a.agent_id: a.balance for a in economy2.all_agents()}

    expected = configured_initial_balance()
    assert first == second == {"agent_a": expected, "agent_b": expected}

    # 账本里应该只有 2 笔铸币
    assert economy2.ledger.count() == 2
    economy2.close()


# ============================================================
# 真实命令行运行
# ============================================================
def test_main_runs_as_script(tmp_path):
    """
    真的执行 `python main.py`，检查输出。

    这是最贴近用户实际体验的测试。
    """
    db_file = tmp_path / "cli_test.db"

    result = subprocess.run(
        [sys.executable, "main.py", "--db", str(db_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"main.py 运行失败\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    output = result.stdout
    assert "AI Economy OS" in output
    assert "Agent A" in output
    assert "Agent B" in output
    assert f"{format_amount(configured_initial_balance())} AI" in output
    assert "READY" in output


def test_main_quiet_mode(tmp_path):
    """--quiet 模式输出应该简洁且可解析。"""
    db_file = tmp_path / "quiet_test.db"

    result = subprocess.run(
        [sys.executable, "main.py", "--db", str(db_file), "--quiet"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0
    lines = [line for line in result.stdout.strip().splitlines() if line]

    expected = configured_initial_balance()
    assert f"Agent A: {format_amount(expected)} AIC" in lines
    assert f"Agent B: {format_amount(expected)} AIC" in lines


def test_main_reports_not_ready_when_only_one_agent(tmp_path):
    """
    如果系统只剩一个 Agent，状态必须是 NOT READY。
    """
    from core.economy import Economy
    from core.config import load_global_config

    from storage.database import create_database

    db_file = tmp_path / "one_agent.db"
    db = create_database(str(db_file))
    economy = Economy(db=db, config=load_global_config())
    economy.create_agent("agent_a")

    assert economy.is_ready() is False
    economy.close()

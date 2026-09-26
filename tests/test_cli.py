"""
Test: 命令行界面
============================================================

验证 main.py 的命令行命令真的能用。

为什么要测命令行？
    因为它是你实际操作的入口。
    如果测试只覆盖了 Python API，而命令行坏了，
    你还是用不了这个系统 —— 测试就没起到该起的作用。

验证目标：
    1. 裸命令 `main.py` 仍然可用（向后兼容，Step 1 的验收标准）
    2. transfer 命令能转钱并打印结果
    3. 经济规则拒绝时，退出码是 2（而不是崩溃的 1）
    4. balances / ledger / audit 命令能跑
    5. --db 写在子命令前面或后面都能生效
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.ledger import format_amount

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 退出码约定（写在这里方便以后统一改）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REJECTED = 2


def run_cli(*args, db_path=None, timeout=60):
    """
    运行 main.py 命令行的辅助函数。

    统一注入 --db，保证测试不碰真实的 data/economy.db。
    """
    command = [sys.executable, "main.py"]
    if db_path is not None:
        command += ["--db", str(db_path)]
    command += list(args)

    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def balance0() -> float:
    """
    从真实配置里读出的初始余额。

    为什么不在断言里直接写 100？
        因为 initial_balance 是 config/config.yaml 里的可调参数。
        如果测试写死 100，你把它改成 200 做实验时测试就会假报警。
        所以这里读取配置，断言里用 f-string 拼出【期望的数字】。
    """
    from core.config import load_agent_config

    return load_agent_config("agent_a").initial_balance


@pytest.fixture(scope="module")
def amount(balance0) -> float:
    """
    测试用的转账金额 = 初始余额的 1/4。

    为什么不写死 20？
        因为如果你把 initial_balance 调成 7（想看看 Agent 破产），
        写死的 20 就超过余额了，测试会因为「余额不足」而失败 ——
        但那是你的配置改对了，测试不该报错。
        用余额的比例来定金额，任何正数余额都够用。
    """
    return balance0 / 4


@pytest.fixture(scope="module")
def too_much(balance0) -> float:
    """
    一个「肯定超过余额」的金额，用来测试余额不足被拒绝。

    用余额的 1000 倍加 1，保证无论你怎么调初始余额都必然超出。
    """
    return balance0 * 1000 + 1


# ============================================================
# 向后兼容：裸命令仍然是启动横幅
# ============================================================
def test_bare_command_still_prints_banner(tmp_path, balance0):
    """
    Step 1 的验收标准不能被破坏：
    `python main.py` 必须还是启动横幅。
    """
    result = run_cli(db_path=tmp_path / "bare.db")

    assert result.returncode == EXIT_OK
    assert "AI Economy OS" in result.stdout
    assert "Agent A" in result.stdout
    assert "Agent B" in result.stdout
    assert f"{format_amount(balance0)} AI" in result.stdout
    assert "READY" in result.stdout


def test_quiet_flag_still_works(tmp_path, balance0):
    result = run_cli("--quiet", db_path=tmp_path / "quiet.db")

    assert result.returncode == EXIT_OK
    assert f"Agent A: {format_amount(balance0)} AI" in result.stdout
    assert f"Agent B: {format_amount(balance0)} AI" in result.stdout


# ============================================================
# transfer
# ============================================================
def test_cli_transfer_moves_money(tmp_path, balance0, amount):
    db_file = tmp_path / "transfer.db"

    result = run_cli(
        "transfer", "agent_a", "agent_b", format_amount(amount), db_path=db_file
    )

    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "转账成功" in result.stdout
    # 期望的余额变化用配置值算出来，而不是写死
    assert f"{format_amount(balance0)} -> {format_amount(balance0 - amount)}" in result.stdout
    assert f"{format_amount(balance0)} -> {format_amount(balance0 + amount)}" in result.stdout


def test_cli_transfer_persists_across_invocations(tmp_path, balance0, amount):
    """
    转账之后重新启动，余额必须还是改过的。

    这条测试验证「经济状态真的落到了数据库」，而不是只活在内存里。
    """
    db_file = tmp_path / "persist.db"

    run_cli("transfer", "agent_a", "agent_b", format_amount(amount), db_path=db_file)
    result = run_cli("balances", db_path=db_file)

    assert result.returncode == EXIT_OK
    # balances 输出是表格，直接断言期望的数字出现在输出里。
    # 期望值由配置算出来，不写死。
    assert format_amount(balance0 - amount) in result.stdout
    assert format_amount(balance0 + amount) in result.stdout


def test_cli_transfer_accepts_shorthand(tmp_path, amount):
    """简写 a / b 应该能用。"""
    result = run_cli(
        "transfer", "a", "b", format_amount(amount), db_path=tmp_path / "short.db"
    )

    assert result.returncode == EXIT_OK
    assert "转账成功" in result.stdout


def test_cli_transfer_accepts_name(tmp_path, amount):
    """也可以用显示名 "Agent A"。"""
    result = run_cli(
        "transfer", "Agent A", "Agent B", format_amount(amount),
        db_path=tmp_path / "name.db",
    )

    assert result.returncode == EXIT_OK
    assert "转账成功" in result.stdout


def test_cli_transfer_with_reason_and_task(tmp_path, amount):
    result = run_cli(
        "transfer", "agent_a", "agent_b", format_amount(amount),
        "--reason", "task_payment",
        "--task-id", "task_001",
        db_path=tmp_path / "reason.db",
    )

    assert result.returncode == EXIT_OK
    assert "task_payment" in result.stdout
    assert "task_001" in result.stdout


# ============================================================
# 被拒绝的经济操作
# ============================================================
def test_cli_rejects_overspend_with_exit_code_2(tmp_path, too_much):
    """
    余额不足时：
        - 退出码必须是 2（「被经济规则拒绝」，不是程序崩溃）
        - 输出必须是友好的中文提示，不能是 Python 堆栈
    """
    result = run_cli(
        "transfer", "agent_a", "agent_b", format_amount(too_much),
        db_path=tmp_path / "over.db",
    )

    assert result.returncode == EXIT_REJECTED
    assert "操作被经济系统拒绝" in result.stdout
    assert "余额不足" in result.stdout
    # 不能把 traceback 甩给用户
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_rejects_self_transfer(tmp_path):
    result = run_cli(
        "transfer", "agent_a", "agent_a", "10", db_path=tmp_path / "self.db"
    )

    assert result.returncode == EXIT_REJECTED
    assert "不能给自己转账" in result.stdout


def test_cli_rejects_negative_amount(tmp_path):
    result = run_cli(
        "transfer", "agent_a", "agent_b", "-50", db_path=tmp_path / "neg.db"
    )

    assert result.returncode == EXIT_REJECTED
    assert "必须大于 0" in result.stdout


def test_cli_rejects_unknown_agent(tmp_path):
    result = run_cli(
        "transfer", "agent_zzz", "agent_b", "10", db_path=tmp_path / "unk.db"
    )

    assert result.returncode == EXIT_REJECTED
    assert "找不到 Agent" in result.stdout


def test_rejected_cli_transfer_changes_nothing(tmp_path, balance0, too_much):
    """
    被拒绝之后，余额必须完全没变。这是最重要的安全性质。
    """
    db_file = tmp_path / "nochange.db"

    run_cli("transfer", "agent_a", "agent_b", format_amount(too_much), db_path=db_file)
    result = run_cli("balances", db_path=db_file)

    assert f"{format_amount(balance0)} AI" in result.stdout
    assert format_amount(balance0 * 2) in result.stdout   # 合计没变


# ============================================================
# 其它命令
# ============================================================
def test_cli_balances(tmp_path):
    result = run_cli("balances", db_path=tmp_path / "bal.db")

    assert result.returncode == EXIT_OK
    assert "Agent A" in result.stdout
    assert "agent_a" in result.stdout
    assert "ACTIVE" in result.stdout


def test_cli_ledger_shows_genesis(tmp_path):
    result = run_cli("ledger", db_path=tmp_path / "led.db")

    assert result.returncode == EXIT_OK
    assert "GENESIS_MINT" in result.stdout
    assert "agent_a" in result.stdout


def test_cli_ledger_filters_by_agent(tmp_path, amount):
    db_file = tmp_path / "ledfilter.db"

    run_cli("transfer", "agent_a", "agent_b", format_amount(amount), db_path=db_file)
    result = run_cli("ledger", "--agent", "a", db_path=db_file)

    assert result.returncode == EXIT_OK
    assert "TRANSFER" in result.stdout
    assert "Agent A" in result.stdout


def test_cli_audit_passes(tmp_path):
    result = run_cli("audit", db_path=tmp_path / "audit.db")

    assert result.returncode == EXIT_OK
    assert "OK" in result.stdout
    assert "差额:          0" in result.stdout or "差额: 0" in result.stdout


def test_cli_audit_still_passes_after_transfers(tmp_path, balance0, amount):
    db_file = tmp_path / "audit2.db"

    run_cli("transfer", "agent_a", "agent_b", format_amount(amount), db_path=db_file)
    run_cli(
        "transfer", "agent_b", "agent_a", format_amount(amount / 2), db_path=db_file
    )
    result = run_cli("audit", db_path=db_file)

    assert result.returncode == EXIT_OK
    assert "OK" in result.stdout
    # 转账不改变货币总量，所以还是两倍的初始余额
    assert format_amount(balance0 * 2) in result.stdout


# ============================================================
# 参数位置
# ============================================================
def test_db_flag_before_subcommand(tmp_path):
    result = run_cli("balances", db_path=tmp_path / "before.db")
    assert result.returncode == EXIT_OK


def test_db_flag_after_subcommand(tmp_path):
    """
    --db 写在子命令后面也要生效。

    这依赖 argparse 的 default=SUPPRESS 技巧，
    所以要专门测一下，防止以后有人改坏了。
    """
    db_file = tmp_path / "after.db"

    result = subprocess.run(
        [sys.executable, "main.py", "balances", "--db", str(db_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == EXIT_OK
    assert db_file.exists(), "--db 写在子命令后面时没有被识别"


# ============================================================
# Step 2：LLM 相关命令
# ============================================================
class TestProviderCommands:
    """
    这些测试保护你实际会敲的命令。

    功能测试（tests/test_llm_provider.py）证明 LLM 层是对的，
    但如果你敲的命令跑不起来，那些正确性你也用不上。
    """

    def test_providers_lists_mock(self, tmp_path):
        result = run_cli("providers", db_path=tmp_path / "prov.db")

        assert result.returncode == EXIT_OK
        assert "mock" in result.stdout
        assert "mock-model-v1" in result.stdout

    def test_providers_shows_unimplemented_ones(self, tmp_path):
        """未实现的厂商要显示「需要新建哪个文件」，而不是藏起来。"""
        result = run_cli("providers", db_path=tmp_path / "prov2.db")

        assert "deepseek" in result.stdout
        assert "llm/deepseek.py" in result.stdout
        assert "doubao" in result.stdout
        assert "grok" in result.stdout

    def test_providers_shows_each_agent_brain(self, tmp_path):
        result = run_cli("providers", db_path=tmp_path / "prov3.db")

        assert "agent_a" in result.stdout
        assert "agent_b" in result.stdout
        assert "provider=mock" in result.stdout

    def test_ask_returns_mock_response(self, tmp_path):
        result = run_cli("ask", "Hello, Agent A.", db_path=tmp_path / "ask.db")

        assert result.returncode == EXIT_OK, result.stdout + result.stderr
        assert "Mock response generated for Agent A." in result.stdout

    def test_ask_shows_token_usage(self, tmp_path):
        result = run_cli("ask", "你好", db_path=tmp_path / "ask2.db")

        assert "Token usage" in result.stdout
        assert "Input:" in result.stdout
        assert "Output:" in result.stdout
        assert "Total:" in result.stdout

    def test_ask_marks_simulated(self, tmp_path):
        result = run_cli("ask", "你好", db_path=tmp_path / "ask3.db")
        assert "Simulated" in result.stdout

    def test_ask_agent_b(self, tmp_path):
        result = run_cli(
            "ask", "--agent", "b", "hi", db_path=tmp_path / "askb.db"
        )

        assert result.returncode == EXIT_OK
        assert "Agent B" in result.stdout

    def test_ask_agent_shorthand(self, tmp_path):
        result = run_cli("ask", "--agent", "a", "hi", db_path=tmp_path / "askc.db")

        assert result.returncode == EXIT_OK
        assert "Agent A" in result.stdout

    def test_ask_records_to_llm_calls(self, tmp_path):
        """
        问一句之后，llm_calls 表里必须有一条记录。

        这是「LLM 调用是经营成本」的原料 ——
        如果调用没被记下来，以后就没法算钱。
        """
        import sqlite3

        db_file = tmp_path / "ask4.db"
        run_cli("ask", "记录我", db_path=db_file)

        connection = sqlite3.connect(db_file)
        try:
            rows = connection.execute(
                "SELECT agent_id, provider, model, total_tokens, simulated "
                "FROM llm_calls"
            ).fetchall()
        finally:
            connection.close()

        assert len(rows) == 1
        agent_id, provider, model, total_tokens, simulated = rows[0]
        assert agent_id == "agent_a"
        assert provider == "mock"
        assert model == "mock-model-v1"
        assert total_tokens > 0
        assert simulated == 1

    def test_demo_runs_full_chain(self, tmp_path):
        result = run_cli("demo", db_path=tmp_path / "demo.db")

        assert result.returncode == EXIT_OK, result.stdout + result.stderr
        # 链路的每一环都要显示出来
        assert "Agent A" in result.stdout
        assert "Brain" in result.stdout
        assert "Router" in result.stdout
        assert "MockProvider" in result.stdout
        assert "LLMResponse" in result.stdout
        assert "READY" in result.stdout

    def test_demo_shows_conversation(self, tmp_path):
        result = run_cli("demo", db_path=tmp_path / "demo2.db")

        assert "User:" in result.stdout
        assert "Mock response generated for Agent A." in result.stdout

    def test_demo_does_not_charge_the_wallet(self, tmp_path):
        """
        【本阶段的边界】调用模型暂时不扣钱。

        只记录 token 用量，不连接经济系统 ——
        那要等 Step 6 的 Cost Calculator。
        """
        db_file = tmp_path / "demo3.db"
        result = run_cli("demo", db_path=db_file)

        assert "未扣费" in result.stdout
        # 账本审计仍然平衡，说明经济层没被动过
        assert "账本审计: OK" in result.stdout

    def test_demo_with_custom_prompt(self, tmp_path):
        result = run_cli(
            "demo", "--prompt", "自定义问题", db_path=tmp_path / "demo4.db"
        )

        assert result.returncode == EXIT_OK
        assert "自定义问题" in result.stdout

    def test_demo_agent_b(self, tmp_path):
        result = run_cli(
            "demo", "--agent", "b", db_path=tmp_path / "demo5.db"
        )

        assert result.returncode == EXIT_OK
        assert "Agent B" in result.stdout

    def test_repeated_ask_accumulates_records(self, tmp_path):
        db_file = tmp_path / "multi.db"

        run_cli("ask", "第一次", db_path=db_file)
        result = run_cli("ask", "第二次", db_path=db_file)

        # 第二次的输出里应该显示累计 2 次
        assert "累计: 2 次调用" in result.stdout

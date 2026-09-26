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
    assert f"{format_amount(balance0)} AIC" in result.stdout
    assert "READY" in result.stdout


def test_quiet_flag_still_works(tmp_path, balance0):
    result = run_cli("--quiet", db_path=tmp_path / "quiet.db")

    assert result.returncode == EXIT_OK
    assert f"Agent A: {format_amount(balance0)} AIC" in result.stdout
    assert f"Agent B: {format_amount(balance0)} AIC" in result.stdout


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

    assert f"{format_amount(balance0)} AIC" in result.stdout
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

    def test_providers_shows_implemented_and_unimplemented(self, tmp_path):
        """已实现的要列出来；未实现的要显示「需要新建哪个文件」，而不是藏起来。"""
        result = run_cli("providers", db_path=tmp_path / "prov2.db")

        # deepseek 和 doubao 现在都实现了
        assert "deepseek" in result.stdout
        assert "doubao" in result.stdout
        # 未实现的仍然要给出明确的下一步
        assert "grok" in result.stdout
        assert "llm/grok.py" in result.stdout

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
        assert "Input tokens:" in result.stdout
        assert "Output tokens:" in result.stdout
        assert "Total tokens:" in result.stdout
        assert "Latency:" in result.stdout

    def test_ask_marks_simulated(self, tmp_path):
        result = run_cli("ask", "你好", db_path=tmp_path / "ask3.db")
        assert "Simulated:     True" in result.stdout
        # Step 2.5：还要说清 token 数据是从哪来的
        assert "Token source:  estimated" in result.stdout

    def test_ask_records_token_source(self, tmp_path):
        """llm_calls 里要记下 token 的来源，方便以后区分真假数据。"""
        import sqlite3

        db_file = tmp_path / "asksrc.db"
        run_cli("ask", "你好", db_path=db_file)

        connection = sqlite3.connect(db_file)
        try:
            row = connection.execute(
                "SELECT token_source, simulated FROM llm_calls"
            ).fetchone()
        finally:
            connection.close()

        assert row == ("estimated", 1)

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


# ============================================================
# Step 3.5：通信日志命令
# ============================================================
class TestCommunicationLogCommands:
    """
    光把日志写进数据库还不算「能定位问题」——
    还得能在出问题时真的查出来。
    """

    @staticmethod
    def _run_demo_then(db_file, *args):
        """先跑一次通信演示产生日志，再执行查询命令。"""
        run_cli("communication-demo", db_path=db_file)
        return run_cli(*args, db_path=db_file)

    def test_log_lists_recent_messages(self, tmp_path):
        result = self._run_demo_then(tmp_path / "log1.db", "communication-log")

        assert result.returncode == EXIT_OK
        assert "通信日志" in result.stdout
        assert "agent_a" in result.stdout
        assert "agent_b" in result.stdout
        assert "msg_" in result.stdout

    def test_log_empty_database_is_friendly(self, tmp_path):
        """一条记录都没有时不该报错，要给出有用的提示。"""
        result = run_cli("communication-log", db_path=tmp_path / "log_empty.db")

        assert result.returncode == EXIT_OK
        assert "还没有任何通信记录" in result.stdout

    def test_log_traces_lists_traces(self, tmp_path):
        result = self._run_demo_then(
            tmp_path / "log2.db", "communication-log", "--traces"
        )

        assert result.returncode == EXIT_OK
        assert "trc_" in result.stdout
        assert "链路" in result.stdout

    def test_log_trace_detail(self, tmp_path):
        db_file = tmp_path / "log3.db"
        run_cli("communication-demo", db_path=db_file)

        traces = run_cli("communication-log", "--traces", db_path=db_file)
        import re

        match = re.search(r"trc_[0-9a-f]+", traces.stdout)
        assert match, f"没能从输出里找到 trace_id：{traces.stdout}"

        result = run_cli(
            "communication-log", "--trace", match.group(0), db_path=db_file
        )

        assert result.returncode == EXIT_OK
        assert "消息 2 条" in result.stdout
        assert "状态汇总" in result.stdout

    def test_log_message_detail_shows_event_stream(self, tmp_path):
        db_file = tmp_path / "log4.db"
        run_cli("communication-demo", db_path=db_file)

        listing = run_cli("communication-log", db_path=db_file)
        import re

        match = re.search(r"msg_[0-9a-f]+", listing.stdout)
        assert match

        result = run_cli(
            "communication-log", "--message", match.group(0), db_path=db_file
        )

        assert result.returncode == EXIT_OK
        for status in ("CREATED", "SENT", "DELIVERED", "RECEIVED",
                       "PROCESSING", "PROCESSED"):
            assert status in result.stdout, f"事件流里缺少 {status}"

    def test_log_message_detail_shows_all_timestamps(self, tmp_path):
        db_file = tmp_path / "log5.db"
        run_cli("communication-demo", db_path=db_file)

        listing = run_cli("communication-log", db_path=db_file)
        import re

        message_id = re.search(r"msg_[0-9a-f]+", listing.stdout).group(0)

        result = run_cli(
            "communication-log", "--message", message_id, db_path=db_file
        )

        for field in ("created_at", "sent_at", "received_at", "processed_at"):
            assert field in result.stdout

    def test_log_unknown_message_returns_2(self, tmp_path):
        result = run_cli(
            "communication-log", "--message", "msg_不存在",
            db_path=tmp_path / "log6.db",
        )

        assert result.returncode == EXIT_REJECTED
        assert "找不到消息" in result.stdout

    def test_log_filter_by_agent(self, tmp_path):
        db_file = tmp_path / "log7.db"
        run_cli("communication-demo", db_path=db_file)

        result = run_cli(
            "communication-log", "--agent", "agent_a", db_path=db_file
        )

        assert result.returncode == EXIT_OK
        assert "msg_" in result.stdout

    def test_log_filter_by_status(self, tmp_path):
        db_file = tmp_path / "log8.db"
        run_cli("communication-demo", db_path=db_file)

        processed = run_cli(
            "communication-log", "--status", "PROCESSED", db_path=db_file
        )
        failed = run_cli(
            "communication-log", "--status", "FAILED", db_path=db_file
        )

        assert "PROCESSED" in processed.stdout
        # 标题里要写出筛选条件，否则分不清「查不到」和「被筛掉了」
        assert "status=PROCESSED" in processed.stdout
        assert "status=FAILED" in failed.stdout

    def test_log_empty_filter_result_is_explained(self, tmp_path):
        """
        筛选后没结果时，必须说清楚「是筛掉了」而不是「日志是空的」。

        这两种情况对排查问题的人来说意义完全不同：
            前者说明系统正常（本来就没有失败的）
            后者说明日志坏了
        """
        db_file = tmp_path / "log8b.db"
        run_cli("communication-demo", db_path=db_file)

        result = run_cli(
            "communication-log", "--status", "FAILED", db_path=db_file
        )

        assert result.returncode == EXIT_OK
        assert "没有符合条件的消息" in result.stdout
        assert "库里有" in result.stdout
        # 不能误导成「根本没有记录」
        assert "还没有任何通信记录" not in result.stdout

    def test_log_truly_empty_database_says_so(self, tmp_path):
        """确实是空库时，给一条「去看演示」的提示。"""
        result = run_cli("communication-log", db_path=tmp_path / "log8c.db")

        assert "还没有任何通信记录" in result.stdout
        assert "communication-demo" in result.stdout

    def test_log_conversation(self, tmp_path):
        db_file = tmp_path / "log9.db"
        run_cli("communication-demo", db_path=db_file)

        # 先拿到一个 conversation_id
        listing = run_cli("communication-log", db_path=db_file)
        import re

        message_id = re.search(r"msg_[0-9a-f]+", listing.stdout).group(0)
        detail = run_cli(
            "communication-log", "--message", message_id, db_path=db_file
        )
        conv = re.search(r"conv_[0-9a-f]+", detail.stdout)
        assert conv, "消息详情里应该有 conversation_id"

        result = run_cli(
            "communication-log", "--conversation", conv.group(0), db_path=db_file
        )

        assert result.returncode == EXIT_OK
        assert "对话" in result.stdout

    def test_demo_reports_journal(self, tmp_path):
        """演示本身应该显示日志已经落库，并给出查询方式。"""
        result = run_cli("communication-demo", db_path=tmp_path / "log10.db")

        assert result.returncode == EXIT_OK
        assert "通信日志" in result.stdout
        assert "trace_id" in result.stdout
        assert "communication-log" in result.stdout

    def test_log_never_prints_api_keys(self, tmp_path):
        """
        【安全】日志命令的输出里不能出现任何 Key 样式的字符串。
        """
        import re

        db_file = tmp_path / "log11.db"
        run_cli("communication-demo", db_path=db_file)
        result = run_cli("communication-log", db_path=db_file)

        assert not re.search(r"sk-[A-Za-z0-9]{20,}", result.stdout)
        assert not re.search(r"ark-[A-Za-z0-9]{20,}", result.stdout)


# ============================================================
# 位置参数形式的 ID 查询
# ============================================================
class TestPositionalIdLookup:
    """
    用户会自然地敲 `communication-log trc_xxx`。

    最初只实现了 `--trace trc_xxx`，结果这种写法直接报
    「unrecognized arguments」—— 而且只打了一大段 usage，
    没告诉他正确的写法。这一组测试锁住修好之后的行为。
    """

    @staticmethod
    def _demo(db_file):
        run_cli("communication-demo", db_path=db_file)

    @classmethod
    def _message_id(cls, db_file):
        """从默认列表里取一个 message_id。"""
        import re

        listing = run_cli("communication-log", db_path=db_file)
        match = re.search(r"msg_[0-9a-f]+", listing.stdout)
        return match.group(0) if match else None

    @classmethod
    def _trace_id(cls, db_file):
        """
        从 --traces 里取一个 trace_id。

        【注意】默认列表【不显示】trace_id —— 它列的是消息。
        要找 trace 必须用 --traces（或从某条消息详情的 trace_id 字段拿）。
        这一点正是最初让人困惑的地方，所以测试要照着真实行为写。
        """
        import re

        listing = run_cli("communication-log", "--traces", db_path=db_file)
        match = re.search(r"trc_[0-9a-f]+", listing.stdout)
        return match.group(0) if match else None

    @classmethod
    def _conversation_id(cls, db_file, message_id):
        import re

        detail = run_cli("communication-log", message_id, db_path=db_file)
        match = re.search(r"conv_[0-9a-f]+", detail.stdout)
        return match.group(0) if match else None

    def test_positional_trace_id(self, tmp_path):
        db_file = tmp_path / "pos1.db"
        self._demo(db_file)
        trace_id = self._trace_id(db_file)
        assert trace_id

        result = run_cli("communication-log", trace_id, db_path=db_file)

        assert result.returncode == EXIT_OK
        assert "链路" in result.stdout
        assert trace_id in result.stdout
        assert "状态汇总" in result.stdout

    def test_positional_message_id(self, tmp_path):
        db_file = tmp_path / "pos2.db"
        self._demo(db_file)
        message_id = self._message_id(db_file)
        assert message_id

        result = run_cli("communication-log", message_id, db_path=db_file)

        assert result.returncode == EXIT_OK
        assert "消息" in result.stdout
        assert message_id in result.stdout
        assert "CREATED" in result.stdout

    def test_positional_conversation_id(self, tmp_path):
        db_file = tmp_path / "pos3.db"
        self._demo(db_file)
        message_id = self._message_id(db_file)
        assert message_id

        detail = run_cli("communication-log", message_id, db_path=db_file)
        import re

        conv = re.search(r"conv_[0-9a-f]+", detail.stdout)
        assert conv

        result = run_cli("communication-log", conv.group(0), db_path=db_file)

        assert result.returncode == EXIT_OK
        assert "对话" in result.stdout

    def test_positional_and_flag_are_equivalent(self, tmp_path):
        """两种写法必须给出同样的结果。"""
        db_file = tmp_path / "pos4.db"
        self._demo(db_file)
        trace_id = self._trace_id(db_file)

        positional = run_cli("communication-log", trace_id, db_path=db_file)
        flagged = run_cli(
            "communication-log", "--trace", trace_id, db_path=db_file
        )

        assert positional.stdout == flagged.stdout

    def test_unknown_prefix_gives_guidance(self, tmp_path):
        """
        认不出的 ID 要给「怎么写才对」，而不是报一句 unrecognized。
        """
        result = run_cli(
            "communication-log", "随便写的ID", db_path=tmp_path / "pos5.db"
        )

        assert result.returncode == EXIT_REJECTED
        assert "认不出" in result.stdout
        assert "trc_" in result.stdout          # 告诉他前缀规则
        assert "communication-log --traces" in result.stdout

    def test_missing_id_gives_suggestions(self, tmp_path):
        """
        ID 抄错一个字母时的兜底：先在库里找一遍，
        找不到再说「不确定就去看有哪些」。
        """
        db_file = tmp_path / "pos6.db"
        self._demo(db_file)
        trace_id = self._trace_id(db_file)

        # 去掉前缀，让它认不出来
        wrong = trace_id.replace("trc_", "")

        result = run_cli("communication-log", wrong, db_path=db_file)

        assert result.returncode == EXIT_REJECTED
        assert "认不出" in result.stdout


class TestArgumentErrors:
    def test_unknown_flag_gives_hint(self, tmp_path):
        """
        参数写错时不能只甩一段 usage。

        argparse 默认行为是打印 usage 然后 exit(2)，
        用户得自己从里面找答案。现在会补一段「常见写法」。
        """
        result = run_cli(
            "communication-log", "--根本不存在", db_path=tmp_path / "arg1.db"
        )

        assert result.returncode == EXIT_REJECTED
        combined = result.stdout + result.stderr
        assert "参数写错了" in combined
        assert "./dev.sh" in combined

    def test_help_still_works(self, tmp_path):
        """--help 是正常退出（0），不该被当成错误。"""
        result = run_cli("communication-log", "--help")

        assert result.returncode == EXIT_OK
        assert "usage" in result.stdout.lower()

    def test_log_help_shows_positional_id(self):
        result = run_cli("communication-log", "--help")

        assert "ID" in result.stdout
        assert "trc_" in result.stdout


# ============================================================
# Step 4A：Task 命令
# ============================================================
class TestTaskCommands:
    def test_task_demo_runs(self, tmp_path):
        result = run_cli("task-demo", db_path=tmp_path / "task1.db")

        assert result.returncode == EXIT_OK
        assert "Task Core Demo" in result.stdout

    def test_task_demo_walks_the_lifecycle(self, tmp_path):
        result = run_cli("task-demo", db_path=tmp_path / "task2.db")

        for status in ("CREATED", "OFFERED", "NEGOTIATING", "ACCEPTED",
                       "IN_PROGRESS", "SUBMITTED", "VERIFIED", "COMPLETED"):
            assert status in result.stdout, f"演示里缺少状态 {status}"

    def test_task_demo_shows_budget_and_agreed_price_separately(self, tmp_path):
        """演示必须同时显示预算和成交价，且两个数不一样。"""
        result = run_cli("task-demo", db_path=tmp_path / "task3.db")

        assert "100 AIC" in result.stdout      # budget
        assert "25 AIC" in result.stdout       # agreed_price
        assert "预算" in result.stdout
        assert "成交" in result.stdout

    def test_task_demo_proves_no_money_moved(self, tmp_path):
        """演示里必须明确显示「一分钱没动」。"""
        result = run_cli("task-demo", db_path=tmp_path / "task4.db")

        assert "没有产生任何账目" in result.stdout
        assert "一分钱没动" in result.stdout

    def test_task_demo_proves_no_workspace_created(self, tmp_path):
        result = run_cli("task-demo", db_path=tmp_path / "task5.db")

        assert "没有创建 Workspace" in result.stdout

    def test_task_demo_shows_offer_chain(self, tmp_path):
        result = run_cli("task-demo", db_path=tmp_path / "task6.db")

        assert "TASK_OFFER" in result.stdout
        assert "COUNTER_OFFER" in result.stdout
        assert "ACCEPT" in result.stdout

    def test_task_log_empty_is_friendly(self, tmp_path):
        result = run_cli("task-log", db_path=tmp_path / "task7.db")

        assert result.returncode == EXIT_OK
        assert "还没有任何任务" in result.stdout
        assert "task-demo" in result.stdout

    def test_task_log_lists_tasks(self, tmp_path):
        db_file = tmp_path / "task8.db"
        run_cli("task-demo", db_path=db_file)

        result = run_cli("task-log", db_path=db_file)

        assert result.returncode == EXIT_OK
        assert "task_" in result.stdout
        assert "COMPLETED" in result.stdout
        assert "WS-001" in result.stdout

    def test_task_log_detail(self, tmp_path):
        import re

        db_file = tmp_path / "task9.db"
        run_cli("task-demo", db_path=db_file)
        listing = run_cli("task-log", db_path=db_file)
        task_id = re.search(r"task_[0-9a-f]+", listing.stdout).group(0)

        result = run_cli("task-log", task_id, db_path=db_file)

        assert result.returncode == EXIT_OK
        assert task_id in result.stdout
        assert "budget" in result.stdout
        assert "agreed_price" in result.stdout
        assert "报价历史" in result.stdout

    def test_task_log_detail_shows_workspace_is_not_accessible(self, tmp_path):
        """
        详情里必须明确显示 workspace 是不可访问的 ——
        这是 Step 4A 的核心承诺。
        """
        import re

        db_file = tmp_path / "task10.db"
        run_cli("task-demo", db_path=db_file)
        listing = run_cli("task-log", db_path=db_file)
        task_id = re.search(r"task_[0-9a-f]+", listing.stdout).group(0)

        result = run_cli("task-log", task_id, db_path=db_file)

        assert "'accessible': False" in result.stdout

    def test_task_log_unknown_task(self, tmp_path):
        result = run_cli(
            "task-log", "task_不存在", db_path=tmp_path / "task11.db"
        )

        assert result.returncode == EXIT_REJECTED
        assert "找不到任务" in result.stdout

    def test_task_log_filter_by_status(self, tmp_path):
        db_file = tmp_path / "task12.db"
        run_cli("task-demo", db_path=db_file)

        completed = run_cli(
            "task-log", "--status", "COMPLETED", db_path=db_file
        )
        created = run_cli("task-log", "--status", "CREATED", db_path=db_file)

        assert "COMPLETED" in completed.stdout
        assert "还没有任何任务" in created.stdout

    def test_task_demo_does_not_touch_wallet(self, tmp_path):
        """
        跑完演示后，余额必须还是初始值。
        """
        db_file = tmp_path / "task13.db"
        run_cli("task-demo", db_path=db_file)

        balances = run_cli("balances", db_path=db_file)

        # 演示里显示的「一分钱没动」是最直接的证据
        assert "一分钱没动" in run_cli(
            "task-demo", db_path=tmp_path / "task13b.db"
        ).stdout
        assert "100 AIC" in balances.stdout

    def test_task_demo_does_not_call_llm(self, tmp_path):
        """
        演示输出里不该出现任何模型调用记录。
        """
        result = run_cli("task-demo", db_path=tmp_path / "task14.db")

        assert "调用记录" not in result.stdout
        assert "没有调用任何模型" in result.stdout

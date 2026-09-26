"""
pytest 共享配置
============================================================

这个文件做两件关键的事：

1. 让测试能 import 到项目里的模块（core / agents / storage ...）
2. 提供一个干净的「内存数据库 + Economy」测试夹具

为什么用内存数据库？
    测试跑完不留痕迹，不会污染 data/economy.db。
    而且每次都是全新环境，不会出现「测试通过是因为上次的数据还在」
    这种假绿。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ------------------------------------------------------------
# 把项目根加进 import 路径
# ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config, load_global_config  # noqa: E402
from core.economy import Economy                     # noqa: E402
from core.ledger import Ledger                       # noqa: E402
from storage.database import create_memory_database  # noqa: E402


# ============================================================
# 与用户本机的 .env 隔离（自动应用）
# ============================================================
# 【为什么需要这个】
#
#     项目会在 load_global_config() 时读取 .env 并写入 os.environ。
#     这在运行时是想要的行为 —— 但在测试里是个陷阱：
#
#         你本机 .env 里写了 DEEPSEEK_MODEL=某个值，
#         于是「环境变量覆盖配置」这条规则被激活，
#         某些断言「配置值应该生效」的测试就会莫名其妙地失败。
#
#     更糟的是它【时快时慢】：.env 存在就失败，删掉就通过。
#     这种依赖用户本机状态的测试是假信号 ——
#     它测的不是代码对不对，而是你的 .env 长什么样。
#
# 【怎么隔离】
#
#     1. 把 core.config.ENV_FILE 指到一个不存在的路径
#        → load_env_file() 什么都不做
#     2. 清掉已经进入进程环境的相关变量
#
#     real_api 测试例外 —— 它们【就是要】用你的真实 .env。
PROVIDER_ENV_VARS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DOUBAO_API_KEY",
    "DOUBAO_BASE_URL",
    "DOUBAO_MODEL",
    "GROK_API_KEY",
    "GROK_BASE_URL",
    "GROK_MODEL",
    "AI_ECONOMY_DB_PATH",
    "AI_ECONOMY_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def isolate_from_user_dotenv(request, monkeypatch):
    """
    让单元测试不读用户本机的 .env。

    只对 real_api 测试放行 —— 那些测试的目的就是验证真实 API。
    """
    if request.node.get_closest_marker("real_api"):
        return

    # 指向一个不可能存在的文件，于是 load_env_file() 直接返回
    monkeypatch.setattr(
        "core.config.ENV_FILE",
        Path("/nonexistent/dsh-tests-have-no-dotenv"),
    )

    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ============================================================
# 基础夹具
# ============================================================
@pytest.fixture
def db():
    """一个干净的、只在内存里的数据库，建好所有表。"""
    database = create_memory_database()
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    """真实读取 config/config.yaml（但不受用户 .env 影响）。"""
    return load_global_config()


@pytest.fixture
def ledger(db) -> Ledger:
    """一个绑定在内存数据库上的空账本。"""
    return Ledger(db=db, currency="AI", amount_precision=4)


@pytest.fixture
def economy(config) -> Economy:
    """
    一个绑定在内存数据库上的 Economy。

    注意这里不复用 db 夹具 —— 因为 Economy 需要自己初始化数据库。
    """
    database = create_memory_database()
    eco = Economy(db=database, config=config)
    yield eco
    eco.close()


# ============================================================
# 测试里使用的固定初始余额
# ============================================================
# 【为什么要单独定一个常量，而不是直接读 config/config.yaml？】
#
# 因为配置是【用户随时可以改的运行参数】。
# 如果测试断言依赖配置文件里的数字，那么你只要把 initial_balance
# 从 100 改成 200 做实验，就会有一堆测试变红 ——
# 但代码其实完全没坏。那种「假警报」会让人不敢动配置，
# 也分不清「我真的改坏了」和「我只是调了个参数」。
#
# 所以测试自己指定明确的初始余额，与用户配置解耦。
# 至于「配置到底有没有生效」，由专门的测试
# （test_config_controls_initial_balance）单独验证。
TEST_INITIAL_BALANCE = 100.0


@pytest.fixture
def funded_agents(economy):
    """
    创建两个各带 TEST_INITIAL_BALANCE (100) AI 的 Agent。

    返回 (economy, agent_a, agent_b)。
    这是测试经济行为最常用的起点。

    注意 initial_balance 是显式传进去的，不依赖 config/config.yaml。
    """
    agent_a = economy.create_agent(
        "agent_a", initial_balance=TEST_INITIAL_BALANCE
    )
    agent_b = economy.create_agent(
        "agent_b", initial_balance=TEST_INITIAL_BALANCE
    )
    return economy, agent_a, agent_b


@pytest.fixture
def configured_initial_balance() -> float:
    """
    从【真实配置】里读出 Agent A 的初始余额。

    给那些必须跑真实配置的测试用（比如启动 main.py 的子进程测试），
    这样它们就不需要把数字硬编码进断言。
    """
    from core.config import load_agent_config

    return load_agent_config("agent_a").initial_balance

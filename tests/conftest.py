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
    """真实读取 config/config.yaml。"""
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


@pytest.fixture
def funded_agents(economy):
    """
    创建两个各带 100 AI 的 Agent，返回 (economy, agent_a, agent_b)。

    这是测试经济行为最常用的起点。
    """
    agent_a = economy.create_agent("agent_a")
    agent_b = economy.create_agent("agent_b")
    return economy, agent_a, agent_b

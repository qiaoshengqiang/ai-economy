"""
数据库访问层
============================================================

职责：只管「怎么连数据库、怎么建表、怎么跑 SQL」。
    不掺和任何经济规则（那是 core/ledger.py 和 core/wallet.py 的事）。

设计要点：
  1. 用标准库 sqlite3，不用 ORM。
  2. 使用 Row factory，让查询结果可以像字典一样用 row["agent_id"]。
  3. 开启外键约束（SQLite 默认是关的，必须手动开）。
  4. 支持「内存数据库」，这样 pytest 可以跑得飞快且互不干扰。
  5. 提供事务上下文管理器 transaction()，保证「要么全成功，要么全回滚」。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from core.paths import PROJECT_ROOT
from storage.models import COLUMN_MIGRATIONS, SCHEMA_STATEMENTS


class Database:
    """
    一个 SQLite 数据库连接的包装。

    典型用法：
        db = Database("data/economy.db")
        db.initialize()
        with db.transaction() as conn:
            conn.execute("INSERT ...")
    """

    def __init__(self, path: Union[str, Path] = ":memory:"):
        self.path = str(path)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------
    @property
    def is_memory(self) -> bool:
        return self.path == ":memory:"

    def connect(self) -> sqlite3.Connection:
        """
        建立连接（若已连接则复用）。

        为什么 check_same_thread=False？
            v0.1 是单线程的，但以后 Agent 可能用线程/异步并行干活。
            现在先设好，避免以后踩坑。
        """
        if self._conn is not None:
            return self._conn

        if not self.is_memory:
            # 确保父目录存在，否则 sqlite3.connect 会报 "unable to open database file"
            db_path = Path(self.path)
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
                self.path = str(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.path, check_same_thread=False)

        # 让查询结果支持 row["column_name"] 这种访问方式
        conn.row_factory = sqlite3.Row

        # SQLite 默认关闭外键约束，必须每个连接手动打开
        conn.execute("PRAGMA foreign_keys = ON")

        # WAL 模式：读写并发更好，且断电更不容易损坏数据。
        # 内存数据库不支持 WAL，所以跳过。
        if not self.is_memory:
            conn.execute("PRAGMA journal_mode = WAL")

        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------
    def initialize(self) -> None:
        """
        创建所有表，并补齐老库缺失的列。可以重复调用。

        顺序很重要：
            1. 先 CREATE TABLE IF NOT EXISTS（新库在这里就建全了）
            2. 再跑增量迁移（老库在这里补上新加的列）
        """
        conn = self.connect()
        with conn:  # with conn 会自动 commit / rollback
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)

        # 补列必须单独走一步：ALTER TABLE 不能在
        # CREATE TABLE 的同一批里混着写。
        self.migrate()

    # ------------------------------------------------------------
    # 增量迁移
    # ------------------------------------------------------------
    def columns_of(self, table: str) -> list:
        """列出某个表当前的列名。表不存在返回空列表。"""
        rows = self.fetchall(f"PRAGMA table_info({table})")
        return [row["name"] for row in rows]

    def migrate(self) -> list:
        """
        给已存在的表补上新增的列。

        为什么需要这个？
            CREATE TABLE IF NOT EXISTS 对【已经存在】的表不起作用。
            项目已经跑过、data/economy.db 已经建好之后，
            光改 DDL 是加不上新列的 —— 你的库会一直缺那一列，
            然后在写入时报 "no such column"。

        这个方法是幂等的：
            列已经存在就跳过，不会重复添加，也不会报错。
            所以每次启动都可以放心调用。

        返回：本次真正补上的列，格式 ["llm_calls.latency_ms", ...]
        """
        applied: list = []

        for table, columns in COLUMN_MIGRATIONS.items():
            existing = set(self.columns_of(table))
            if not existing:
                # 表都还不存在，说明 CREATE TABLE 那一步没跑到，
                # 这种情况不该静默跳过
                continue

            for column_name, column_def in columns:
                if column_name in existing:
                    continue

                # 注意：ALTER TABLE 必须走独立事务，
                # 不能和上面的建表语句混在同一个 with conn 里。
                with self.transaction() as conn:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}"
                    )
                applied.append(f"{table}.{column_name}")

        return applied

    # ------------------------------------------------------------
    # 事务
    # ------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """
        事务上下文。

        为什么这个很重要？
            转账必须「扣款 + 入账」同时成功或同时失败。
            如果扣完款程序崩了，钱就凭空消失了。
            用 BEGIN ... COMMIT/ROLLBACK 保证原子性。

        用法：
            with db.transaction() as conn:
                conn.execute(...)
                conn.execute(...)
            # 到这里自动 COMMIT；中途抛异常则自动 ROLLBACK
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------
    # 便捷查询
    # ------------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        conn = self.connect()
        with conn:
            return conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list:
        return self.connect().execute(sql, params).fetchall()

    def table_names(self) -> list:
        """列出当前库里所有表名（测试用）。"""
        rows = self.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return [row["name"] for row in rows]

    def __repr__(self) -> str:
        return f"Database(path={self.path!r}, connected={self._conn is not None})"


# ============================================================
# 工厂函数
# ============================================================
def create_database(db_path: Optional[Union[str, Path]] = None) -> Database:
    """
    按配置创建数据库。

    db_path 为 None 时读取全局配置里的 storage.database_path。
    """
    if db_path is None:
        from core.config import load_global_config

        config = load_global_config()
        db_path = config.get("storage.database_path", "data/economy.db")

    db = Database(db_path)
    db.connect()
    db.initialize()
    return db


def create_memory_database() -> Database:
    """
    创建一个只在内存里的数据库，专门给测试用。

    好处：pytest 每次跑都是干净的环境，不会污染 data/economy.db，
        也不会出现「测试通过了但其实是上次的数据」这种假绿。
    """
    db = Database(":memory:")
    db.connect()
    db.initialize()
    return db

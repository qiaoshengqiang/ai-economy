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

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from core.logging_setup import get_logger
from core.paths import PROJECT_ROOT
logger = get_logger(__name__)

from storage.models import (
    COLUMN_MIGRATIONS,
    DATA_MIGRATIONS,
    POST_MIGRATION_INDEXES,
    SCHEMA_STATEMENTS,
)


#: 默认的 SQLite 忙等超时（毫秒）。
#:
#: 【为什么必须显式设置，而不是"Python 默认就有"】
#:     `sqlite3.connect(timeout=5.0)` 确实会把 busy_timeout 设成 5000，
#:     所以过去**碰巧**是 5 秒。但那是 Python 的默认值，
#:     不是我们的决定 —— 它没有写在任何地方，也没法配置。
#:
#:     多进程写同一个库时，这个值决定了「另一个进程拿着写锁时，
#:     我怎么等」。等不到就抛 database is locked。
#:     这是个可靠性参数，应该被明确声明。
DEFAULT_BUSY_TIMEOUT_MS = 5000


class Database:
    """
    一个 SQLite 数据库连接的包装。

    典型用法：
        db = Database("data/economy.db")
        db.initialize()
        with db.transaction() as conn:
            conn.execute("INSERT ...")
    """

    def __init__(
        self,
        path: Union[str, Path] = ":memory:",
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ):
        self.path = str(path)
        self._conn: Optional[sqlite3.Connection] = None
        # 事务嵌套深度（见 transaction() 的说明）
        self._tx_depth = 0

        # 这个连接是**哪个进程**创建的。
        #
        # 【为什么需要一个 PID】
        #     看 connect() 里的说明 —— fork 之后子进程会继承
        #     父进程的连接，而那是不能用的。
        self._owner_pid: Optional[int] = None

        if busy_timeout_ms < 0:
            raise ValueError(
                f"busy_timeout_ms 不能为负，收到 {busy_timeout_ms}"
            )
        self.busy_timeout_ms = int(busy_timeout_ms)

    # ------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------
    @property
    def is_memory(self) -> bool:
        return self.path == ":memory:"

    @property
    def owner_pid(self) -> Optional[int]:
        """
        持有当前连接的进程 PID（None 表示还没连接）。

        给测试用 —— 让「每个进程一个连接」这件事可以被**断言**，
        而不是只能靠读代码相信。
        """
        return self._owner_pid

    def pragmas(self) -> dict:
        """
        当前连接的关键 PRAGMA（给测试 / 演示 / 排查用）。

        没有连接时返回 {}。
        """
        if self._conn is None:
            return {}

        def value(name):
            row = self._conn.execute(f"PRAGMA {name}").fetchone()
            return row[0] if row else None

        return {
            "journal_mode": value("journal_mode"),
            "busy_timeout": value("busy_timeout"),
            "foreign_keys": value("foreign_keys"),
            "synchronous": value("synchronous"),
        }

    def connect(self) -> sqlite3.Connection:
        """
        建立连接（**按进程**复用）。

        ============================================================
        【Step 4D.1 之后这里加了两件事 —— Workspace Phase 0 修的】
        ============================================================

        【一、连接缓存必须是 PID 感知的】

            原来是这样：

                if self._conn is not None:
                    return self._conn

            单进程没问题。但 `fork()` 之后：

                父进程建了连接 → fork → 子进程**继承** self._conn

            子进程拿到的那个连接是**父进程创建的** ——
            SQLite 明确说明跨 fork 复用连接是不安全的
            （共享的页缓存、WAL 索引、锁状态会不一致）。

            现在缓存里记了 owner_pid：

                PID 相同 → 复用（正常路径，快）
                PID 不同 → 丢弃继承来的连接，**重新建一个**

            这样「每个进程一个连接」是结构性保证，
            而不是靠调用方记得别把 Database 对象传给子进程。

        【二、busy_timeout 显式设置】

            过去它**碰巧**是 5000 —— 因为 sqlite3.connect(timeout=5.0)
            的默认值。但那是 Python 的默认，不是我们的决定。

            多进程写同一个库时，这个值决定「另一个进程拿着写锁时我等多久」。
            现在它被显式声明、可配置、有文档。

            同时把 connect 的 timeout 也设成一致的值 ——
            否则「Python 层等 5 秒」和「SQLite 层 busy_timeout」
            可能不一致，出问题时很难判断是哪一层在等。

        【check_same_thread=False 是什么】

            v0.1 是单线程的，但以后 Agent 可能用线程/异步并行干活。
            设成 False 让连接可以在别的线程里用。

            ⚠️ 注意：这只是**允许**跨线程使用，不等于线程安全。
            多个线程共享同一个 Database 时，连接本身和事务深度
            都不是线程安全的 —— 见报告里的技术债。
        """
        pid = os.getpid()

        if self._conn is not None:
            if self._owner_pid == pid:
                return self._conn

            # ---- fork 之后：继承来的连接不能用 ----
            #
            # 【为什么只丢弃、不 close()】
            #     子进程里 close 一个「父进程开启、可能有未提交事务」的连接，
            #     会触发一次 rollback —— 而那是父进程的事务。
            #     虽然 fd 已经各自独立，但为了绝对不干扰父进程，
            #     这里只丢引用，让它在子进程退出时自然回收。
            #
            #     （子进程通常是短命的；真正的长命进程应该自己建
            #       Database 对象，而不是继承父进程的。）
            logger.warning(
                "进程 %s 继承了一个由进程 %s 建立的数据库连接，"
                "已丢弃并重新建立（SQLite 连接不能跨 fork 复用）",
                pid, self._owner_pid,
            )
            self._conn = None
            self._tx_depth = 0

        if not self.is_memory:
            # 确保父目录存在，否则 sqlite3.connect 会报 "unable to open database file"
            db_path = Path(self.path)
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
                self.path = str(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            # 和下面的 PRAGMA 保持一致 —— 免得两层超时不一样
            timeout=self.busy_timeout_ms / 1000.0,
        )

        # 让查询结果支持 row["column_name"] 这种访问方式
        conn.row_factory = sqlite3.Row

        # SQLite 默认关闭外键约束，必须每个连接手动打开
        conn.execute("PRAGMA foreign_keys = ON")

        # 写竞争时等待而不是立刻失败（显式声明，不依赖 Python 默认）
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")

        # WAL 模式：读写并发更好，且断电更不容易损坏数据。
        #
        # 【为什么多进程下它很关键】
        #     回滚日志模式下，写者会阻塞读者；
        #     WAL 模式下读写可以真正并行 —— 这正是多进程场景需要的。
        #
        # 【Workspace Phase 0：这里改成了「先看再设」】
        #
        #     原来是无条件执行：
        #
        #         PRAGMA journal_mode = WAL
        #
        #     但这条 PRAGMA **需要独占访问**。多进程同时 connect() 时，
        #     它们会互相撞成：
        #
        #         sqlite3.OperationalError: database is locked
        #
        #     （这是真的测出来的 —— 两个进程同时连同一个库，
        #       其中一半直接在 connect() 就炸了。busy_timeout 也救不了，
        #       因为切换 journal mode 要的是排他锁。）
        #
        #     现在先读一次当前模式：已经是 WAL 就什么都不做。
        #     只有真的需要切换时才执行 —— 而那通常只发生在
        #     第一个进程建库的时候。
        #
        #     即使切换时撞上锁也不该让连接失败：那说明**别的进程
        #     正在切**，切完之后就是 WAL 了。
        if not self.is_memory:
            try:
                current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(current_mode).lower() != "wal":
                    conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as exc:
                logger.debug(
                    "切换 journal_mode 时撞上锁（大概率是别的进程正在切）：%s",
                    exc,
                )

        self._conn = conn
        self._owner_pid = pid
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._owner_pid = None
        self._tx_depth = 0

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
            3. 最后建「依赖新列的索引」

        第 3 步不能提前：索引建立在列之上，而那一列可能是第 2 步
        才加上去的。提前建会在老库上报 "no such column"。
        """
        conn = self.connect()
        with conn:  # with conn 会自动 commit / rollback
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)

        # 补列必须单独走一步：ALTER TABLE 不能在
        # CREATE TABLE 的同一批里混着写。
        self.migrate()

        # 补完列才能给新列建索引
        with conn:
            for statement in POST_MIGRATION_INDEXES:
                conn.execute(statement)

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

        # 补完列，再修数据。
        # 顺序不能反 —— 数据回填依赖列已经存在。
        applied.extend(self._backfill())

        return applied

    def _backfill(self) -> list:
        """
        跑数据回填语句（幂等，可以每次启动都跑）。

        为什么需要这一步？
            给表加列时，老数据只能拿到一个统一的 DEFAULT 值。
            但真实情况往往更复杂 —— 比如「Step 2 时期的记录
            其实都是估算值，不该被标成来源不明」。
            这时就需要加完列之后再补一次数据。

        返回：本次真正改动了行的回填说明。
        """
        applied: list = []

        for description, sql in DATA_MIGRATIONS:
            with self.transaction() as conn:
                cursor = conn.execute(sql)
                changed = cursor.rowcount

            if changed:
                applied.append(f"{description}（{changed} 行）")

        return applied

    # ------------------------------------------------------------
    # 事务
    # ------------------------------------------------------------
    @contextmanager
    def transaction(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        """
        事务上下文。**可重入**（嵌套时自动加入外层事务）。

        为什么这个很重要？
            转账必须「扣款 + 入账」同时成功或同时失败。
            如果扣完款程序崩了，钱就凭空消失了。
            用 BEGIN ... COMMIT/ROLLBACK 保证原子性。

        用法：
            with db.transaction() as conn:
                conn.execute(...)
                conn.execute(...)
            # 到这里自动 COMMIT；中途抛异常则自动 ROLLBACK

        ============================================================
        【为什么必须可重入 —— Step 4D.1 补上的一个真实缺口】
        ============================================================

            SQLite 不允许嵌套 BEGIN：

                with db.transaction():
                    with db.transaction():     # ❌ OperationalError:
                        ...                    #    cannot start a transaction
                                           #    within a transaction

            而这个项目的每一层都在自己开事务：

                TaskStore.save()              with db.transaction()
                TaskStore.record_execution()  with db.transaction()

            于是「把两件事放进同一个事务」在 4D 是**做不到的** ——
            不是因为没人想写，而是因为写了会直接报错。

            结果是 TaskExecutionService 只能这样：

                record_execution(...)   # 事务 1：写事件
                save(task)              # 事务 2：改状态

            两个事务之间崩溃 → 半成功状态：
                ✅ 事件写了，❌ 状态没改
            或者反过来。

            【现在的行为】

                深度 0 → BEGIN，并在退出时 COMMIT / ROLLBACK
                深度 >0 → 直接复用外层连接，不 BEGIN、不 COMMIT

                内层抛异常 → 异常继续向上传播 →
                最外层看到异常 → ROLLBACK 整个事务

            所以「要么都成功，要么都不发生」对嵌套块同样成立。

        【参数 write】

            write=True（默认） → BEGIN IMMEDIATE（拿写锁，多进程安全）
            write=False        → BEGIN（延迟读事务，可并发读）

            默认 True 是因为这个项目里 transaction() **绝大多数**用于写：
            TaskStore 的每个写方法、recorder 的每个写方法都自己开事务。
            把默认设成 True 意味着「忘了写 write=False」的代价只是
            「读也串行化了」—— 慢一点，但**不会错**。
            反过来（默认 False）的代价是并发下随机 `database is locked`。

            只读的调用点见 TaskStore.executions_for / find_execution_by_message
            / count_executions —— 它们显式传 write=False。

        【为什么用简单嵌套而不是 SAVEPOINT】

            SAVEPOINT 能让内层独立回滚而外层继续提交。
            但本项目的语义是「这两件事必须一起成功」——
            内层失败时外层**不该**继续提交。

            所以简单嵌套（内层失败 → 整个事务失败）正是想要的语义，
            而且更简单。需要独立回滚语义时再加 SAVEPOINT 不迟。
        """
        conn = self.connect()
        depth = self._tx_depth

        if depth == 0:
            # ============================================================
            # 【Workspace Phase 0：写事务改用 BEGIN IMMEDIATE】
            # ============================================================
            #
            # 原来一律用 `BEGIN`（= BEGIN DEFERRED）。它的行为是：
            # 事务开始时**只拿读锁**，等到第一次写才尝试升级成写锁。
            #
            # 单进程下没问题。两个进程并发时会出现经典的
            # **升级死锁**：
            #
            #     A: BEGIN → SELECT（拿 SHARED）→ UPDATE（想升 RESERVED）
            #     B: BEGIN → SELECT（拿 SHARED）→ UPDATE（想升 RESERVED）
            #
            #     两边都持着 SHARED，都在等对方释放才能升级 —— 死锁。
            #
            # SQLite 检测到这种死锁后会**立刻**返回 SQLITE_BUSY，
            # **不理会 busy_timeout**（等下去只会永远等）。
            # 于是调用方拿到的是：
            #
            #     OperationalError: database is locked
            #
            # 而不是我们期待的 `ConcurrentTaskModification` ——
            # 并发保护在真实多进程下就**失效**了。
            #
            # `BEGIN IMMEDIATE` 在事务开始时就拿写锁，
            # 所以不存在「先读后升级」这一步，也就没有死锁：
            #
            #     两个进程同时 BEGIN IMMEDIATE
            #     → 一个立刻拿到写锁
            #     → 另一个等 busy_timeout
            #     → 等到之后发现 version 已经变了
            #     → 正确地抛 ConcurrentTaskModification
            #
            # 【代价】
            #     写事务之间**完全串行**。这是 SQLite 的模型决定的
            #     （单写者），不是我们引入的限制。
            #
            # 【只读事务呢】
            #     传 write=False 走原来的 `BEGIN`（延迟读事务）——
            #     它们不升级，不会有死锁，而且可以真正并发读。
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")

        self._tx_depth = depth + 1
        try:
            yield conn
        except Exception:
            self._tx_depth = depth
            if depth == 0:
                # 最外层：整个事务回滚
                conn.rollback()
            # 内层失败时不在这里回滚 —— 异常继续向上传播，
            # 由最外层统一回滚。这样不会出现「内层回滚了、
            # 外层以为还活着、继续写」的错乱状态。
            raise
        else:
            self._tx_depth = depth
            if depth == 0:
                conn.commit()

    @property
    def transaction_depth(self) -> int:
        """
        当前事务嵌套深度（0 表示不在事务里）。

        主要用于测试和守卫 —— 让「这两件事在同一个事务里」
        可以被**断言**，而不是只能靠读代码相信。
        """
        return self._tx_depth

    def in_transaction(self) -> bool:
        """当前是否处于事务中。"""
        return self._tx_depth > 0

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

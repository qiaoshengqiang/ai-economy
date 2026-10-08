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
import threading
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


class _ThreadSession:
    """
    一个线程**独占**的数据库连接，以及它**自己的**事务深度。

    ============================================================
    【为什么连接必须和深度绑在一起 —— 而不是只把深度做成 thread-local】
    ============================================================

        技术债 #2 的原始描述是「`_tx_depth` 不是 thread-local」。
        但只把深度改成 thread-local **并不够**：

            如果两个线程共用**同一个连接**，那么

                Thread A: BEGIN IMMEDIATE   → 连接上开了一个事务
                Thread B: （深度 0，于是）BEGIN IMMEDIATE
                          → ❌ "cannot start a transaction within a transaction"

            而且更糟的是：

                Thread A: BEGIN → 写入一些行
                Thread B: BEGIN 失败 / 或写入
                Thread A: ROLLBACK  → **把 Thread B 的未提交写入一起回滚**

            也就是说，「独立的 transaction scope」在共享连接上
            **根本无法成立** —— 无论深度记在哪里。

        所以正确的划分单位是 **(连接, 深度)** 而不是单独的深度：

            一个线程 → 一个 session → 一个连接 + 一个 depth

        这样：
            · 深度天然隔离（每个 session 有自己的 depth）
            · 事务天然隔离（每个连接有自己的 BEGIN / COMMIT / ROLLBACK）
            · **不需要任何全局锁** —— 线程之间根本不在同一个事务上下文里
            · 嵌套事务语义不变（同一个 session 内 depth 递增）

    【进程呢】

        session 里也记 `pid`。fork 之后子进程继承来的 session
        带着父进程的 pid，会被丢弃并重建 ——
        与原来「每个进程一个连接」的行为一致（SQLite 连接不能跨 fork 复用）。
    """

    __slots__ = ("conn", "depth", "pid")

    def __init__(self, conn: sqlite3.Connection, pid: int) -> None:
        self.conn = conn
        self.depth = 0
        self.pid = pid


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

        # ============================================================
        # 线程 / 进程模型（技术债 #2 的基础设施层修复）
        # ============================================================
        #
        # 【旧模型】
        #
        #     self._conn      = 一个连接，所有线程共用
        #     self._tx_depth  = 一个整数，所有线程共用
        #
        #   两个都是**实例状态** —— 多线程下会被交错修改：
        #   深度算错 → 该 BEGIN 的时候不 BEGIN、该 COMMIT 的时候不 COMMIT。
        #   本阶段因此产生过两个 flaky 测试。
        #
        # 【新模型】
        #
        #     threading.local() → 每个线程自己的 _ThreadSession
        #     session = (连接 + 事务深度 + pid)
        #
        #   文件库：每个线程开**自己的连接**到同一个文件 ——
        #          事务真正互相独立，而且**不需要全局锁**。
        #
        #   内存库：见下面的 `_shared_session` —— SQLite 的 `:memory:`
        #          在结构上就是「一个连接一个库」。
        self._local = threading.local()

        # 已打开的 session（close() 要能关掉所有线程的）
        self._sessions: list = []
        # 🔒 只保护上面这个**注册表**，不保护事务 ——
        #    它不是在给并发"上锁"，只是记账。
        self._sessions_lock = threading.Lock()

        # 内存库专用的那**一个** session。
        # 见 `_session()` 的说明：`:memory:` 无法在多个连接间共享。
        self._shared_session: Optional[_ThreadSession] = None

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

    # ------------------------------------------------------------
    # Session 管理（技术债 #2 的修复核心）
    # ------------------------------------------------------------
    def _peek_session(self) -> Optional[_ThreadSession]:
        """
        看当前线程**已有**的 session，**不会建立新连接**。

        给 `transaction_depth` / `in_transaction` / `owner_pid` /
        `pragmas` 用 —— 这几个方法只是**观察**，不该有副作用
        （否则 `db.in_transaction()` 会顺手把库连上）。
        """
        if self.is_memory:
            return self._shared_session
        return getattr(self._local, "session", None)

    def _session(self) -> _ThreadSession:
        """
        拿当前线程（或当前进程）的 session，没有就建一个。

        ============================================================
        【⚠️ 内存库为什么只能有一个连接 —— 这是**测出来的**，不是猜的】
        ============================================================

            最直觉的做法是「所有库都每线程一个连接」。
            对文件库成立；对 `:memory:` **不成立**：

                SQLite 的 `:memory:` 是**每个连接一个独立的库**。
                线程 B 新开一个连接 → 那是一个**空空的新库**，
                看不到线程 A 建的表。

            于是去查解决办法，标准答案是 shared-cache URI：

                file:memdb_xxx?mode=memory&cache=shared

            我实测了它，**在并发写下不成立**：

                20 线程 × 5 次 BEGIN IMMEDIATE + INSERT
                → 17 个线程抛 `database table is locked`
                → 最终只写进 25/101 行

                原因：shared-cache 模式用的是**表级锁**，
                冲突报的是 `SQLITE_LOCKED` ——
                而 `SQLITE_LOCKED` **不受 busy_timeout 影响**，
                等多久都不会好。

            所以内存库**结构上**只能有一个连接 + 一个共享深度。
                · 这是 SQLite 的性质，不是我们偷懒
                · 生产用的是**文件库**（`data/economy.db`）→ 拿到完整修复
                · 测试里的约束由 meta-guard 保证
                  （并发测试必须用文件库 —— 那是**测试环境**约束，
                   不再承担「防止 Database 本身出错」的职责）

            换句话说：**文件库的并发正确性由本文件保证；
            内存库的单线程约束由测试守卫保证。** 两件事分开。
        """
        pid = os.getpid()

        # ---- 内存库：全进程唯一那个 session ----
        if self.is_memory:
            session = self._shared_session
            if session is not None and session.pid == pid:
                return session
            if session is not None and session.pid != pid:
                self._warn_inherited(session.pid, pid)
            session = self._open_session(pid)
            self._shared_session = session
            return session

        # ---- 文件库：每个线程自己的 session ----
        session = getattr(self._local, "session", None)
        if session is not None and session.pid == pid:
            return session

        if session is not None and session.pid != pid:
            self._warn_inherited(session.pid, pid)

        session = self._open_session(pid)
        self._local.session = session
        return session

    @staticmethod
    def _warn_inherited(old_pid: int, new_pid: int) -> None:
        logger.warning(
            "进程 %s 继承了一个由进程 %s 建立的数据库连接，"
            "已丢弃并重新建立（SQLite 连接不能跨 fork 复用）",
            new_pid, old_pid,
        )

    def _open_session(self, pid: int) -> _ThreadSession:
        """
        真正建连接的地方（原来 `connect()` 里那段）。

        每个 session 都要重新设 PRAGMA ——
        因为 `foreign_keys` 和 `busy_timeout` 是**连接级**的
        （`journal_mode` 是库级的，所以那边才要"先看再设"）。
        """
        if not self.is_memory:
            # 确保父目录存在，否则 sqlite3.connect 会报
            # "unable to open database file"
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
        #     原来是无条件执行 `PRAGMA journal_mode = WAL`。
        #     但这条 PRAGMA **需要独占访问**，多进程同时 connect() 时
        #     会互相撞成 "database is locked"
        #     （busy_timeout 也救不了 —— 切换 journal mode 要的是排他锁）。
        #
        #     现在先读一次当前模式：已经是 WAL 就什么都不做。
        #     只有真的需要切换时才执行 —— 那通常只发生在第一个进程建库时。
        #
        #     ⚠️ 对**文件库的第二个线程**来说，库已经是 WAL 了，
        #        所以这里只读一次、不写 —— 不会和别的线程抢排他锁。
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

        session = _ThreadSession(conn, pid)
        with self._sessions_lock:
            self._sessions.append(session)
        return session

    @property
    def owner_pid(self) -> Optional[int]:
        """
        当前线程那个连接是**哪个进程**建的（None 表示还没连接）。

        给测试用 —— 让「每个进程一个连接」这件事可以被**断言**，
        而不是只能靠读代码相信。
        """
        session = self._peek_session()
        return session.pid if session is not None else None

    @property
    def open_connection_count(self) -> int:
        """
        当前**本进程**还开着几个连接。

        给「每线程一个连接」这件事一个可断言的口子 ——
        也让测试能验证 `close()` 真的把所有线程的连接都关了。
        """
        pid = os.getpid()
        with self._sessions_lock:
            return sum(1 for s in self._sessions if s.pid == pid)

    def pragmas(self) -> dict:
        """
        当前连接的关键 PRAGMA（给测试 / 演示 / 排查用）。

        没有连接时返回 {}。
        """
        session = self._peek_session()
        if session is None:
            return {}

        def value(name):
            row = session.conn.execute(f"PRAGMA {name}").fetchone()
            return row[0] if row else None

        return {
            "journal_mode": value("journal_mode"),
            "busy_timeout": value("busy_timeout"),
            "foreign_keys": value("foreign_keys"),
            "synchronous": value("synchronous"),
        }

    def connect(self) -> sqlite3.Connection:
        """
        拿到**当前线程**的连接（没有就建一个）。

        ============================================================
        【技术债 #2 的修复：从「一个连接」到「每线程一个连接」】
        ============================================================

            【旧模型】

                self._conn     = 一个连接，全进程共用
                self._tx_depth = 一个整数，全进程共用

                两者都是**实例状态**。多线程下：
                    · 深度算错 → 该 BEGIN 不 BEGIN、该 COMMIT 不 COMMIT
                    · 更根本的是：**共用一个连接 = 共用一个事务**
                      → Thread A 的 ROLLBACK 会把 Thread B 的
                        未提交写入一起回滚（半成功状态）

            【新模型】

                每个线程一个 `_ThreadSession`（连接 + 深度 + pid）
                → 事务真正互相独立
                → **不需要任何全局锁**（线程不在同一个事务上下文里）

            【为什么不能只把深度改成 thread-local】

                那样只会把「静默交错」换成
                `cannot start a transaction within a transaction` ——
                因为连接还是共用的。**连接的粒度必须和深度一致。**

        ============================================================
        【PID 感知（Workspace Phase 0 加的，保留）】
        ============================================================

            fork() 之后子进程会**继承**父进程的连接，而 SQLite 明确说明
            跨 fork 复用连接不安全（共享的页缓存、WAL 索引、锁状态会不一致）。

            session 里记了 owner pid：PID 相同 → 复用；PID 不同 → 重建。

            【为什么只丢弃、不 close()】
                子进程里 close 一个「父进程开启、可能有未提交事务」的连接，
                会触发一次 rollback —— 而那是**父进程的**事务。
                所以只丢引用，让它在子进程退出时自然回收。

        ============================================================
        【busy_timeout 显式设置（保留）】

            过去它**碰巧**是 5000（`sqlite3.connect(timeout=5.0)` 的默认值）。
            那是 Python 的默认，不是我们的决定。现在它被显式声明、
            可配置、有文档，并且 connect 的 timeout 与之一致。

        ============================================================
        【check_same_thread=False（保留）】
            每个线程会建**自己的**连接，所以连接通常是
            「在哪个线程建、在哪个线程用」。
            保留 False 是为了兼容「连接在别的线程里被 close」
            这类边缘情况（例如父线程 close 整个 Database）。
        """
        return self._session().conn

    def close(self) -> None:
        """
        关掉**所有线程**的连接。

        ⚠️ fork 继承来的 session **只丢引用、不 close** ——
           那可能是父进程正在用的事务，关掉会触发它的 rollback。
           （与 `connect()` 里「只丢弃不 close」是同一条理由。）
        """
        pid = os.getpid()
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []

        for session in sessions:
            if session.pid != pid:
                continue
            try:
                session.conn.close()
            except Exception:            # pragma: no cover - 防御
                logger.debug("关闭数据库连接时出错（忽略）", exc_info=True)

        self._shared_session = None
        # 丢掉本线程的 session 缓存（其余线程的天然失效，因为注册表清空了）
        self._local = threading.local()

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
        # 🔒 session 而不是 self —— 连接与深度是**同一粒度**的
        session = self._session()
        conn = session.conn
        depth = session.depth

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

        session.depth = depth + 1
        try:
            yield conn
        except Exception:
            session.depth = depth
            if depth == 0:
                # 最外层：整个事务回滚
                conn.rollback()
            # 内层失败时不在这里回滚 —— 异常继续向上传播，
            # 由最外层统一回滚。这样不会出现「内层回滚了、
            # 外层以为还活着、继续写」的错乱状态。
            raise
        else:
            session.depth = depth
            if depth == 0:
                conn.commit()

    @property
    def transaction_depth(self) -> int:
        """
        当前事务嵌套深度（0 表示不在事务里）。

        主要用于测试和守卫 —— 让「这两件事在同一个事务里」
        可以被**断言**，而不是只能靠读代码相信。
        """
        session = self._peek_session()
        return session.depth if session is not None else 0

    def in_transaction(self) -> bool:
        """
        当前**本线程**是否处于事务中。

        用 `_peek_session()` 而不是 `_session()` ——
        只是观察，不该顺手把数据库连上。
        """
        session = self._peek_session()
        return session is not None and session.depth > 0

    # ------------------------------------------------------------
    # 便捷查询
    # ------------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """
        单条写语句的便捷入口。**事务感知。**

        ============================================================
        【Step 4F 修掉的一个真实原子性缺口】
        ============================================================

            原来写的是：

                conn = self.connect()
                with conn:                    # ← sqlite3 的事务上下文
                    return conn.execute(sql, params)

            `with conn:` 在退出时**无条件 commit**。
            于是只要有人在显式事务里调 `db.execute()`：

                with db.transaction():
                    db.execute("UPDATE a ...")     # ← 这里就提交了！
                    db.execute("UPDATE b ...")     # ← 之后失败
                    # 回滚 → 但 a 已经提交，回不去了

            → **半成功状态**。而且它不报错，只是让数据悄悄不对。

            这个缺口一直存在，只是之前没人踩到：
            `TaskStore` 在事务里总是用 `with db.transaction() as conn`
            拿到的那个 conn，从不调 `db.execute()`。
            Step 4F 的 `decide_change` 第一次踩上了 ——
            表现为「change 是 ACCEPTED，但对应的 revision 没提交」。

        ------------------------------------------------------------
        【现在的行为】
        ------------------------------------------------------------

            事务深度 0  → 和以前完全一样：执行 + 提交
            事务深度 >0 → 只执行，**不提交** ——
                          边界由外层事务拥有

            所以：
                · 事务外的调用方：行为不变
                · 事务内的调用方：终于真的是原子的

        想显式控制事务边界，用 `transaction()`，别依赖 `execute()` 提交。
        """
        conn = self.connect()

        if self.in_transaction():
            # 已经在显式事务里 —— 提交权归外层
            return conn.execute(sql, params)

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
        session = self._peek_session()
        return (
            f"Database(path={self.path!r}, "
            f"connected={session is not None}, "
            f"connections={self.open_connection_count})"
        )


# ============================================================
# 工厂函数
# ============================================================
def create_database(db_path: Optional[Union[str, Path]] = None) -> Database:
    """
    按配置创建数据库。

    db_path 为 None 时读取全局配置里的 runtime.database_path。
    """
    if db_path is None:
        from core.config import load_global_config

        config = load_global_config()
        # 🔒 canonical 位置是 `runtime.database_path`。
        #    `storage.database_path` 已删除 —— 那曾经是同一事实的第二份定义。
        db_path = config.get("runtime.database_path", "data/economy.db")

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

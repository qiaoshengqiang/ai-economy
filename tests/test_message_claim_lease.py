"""
Test: Message Claim Lease / 超时回收（Workspace Phase 0 / 任务 B）
============================================================

【这个文件验证什么】

    Step 4D.1 给了我们 claim_id / claimed_at —— 但那是**永久**认领。
    如果 worker 被 kill -9，这个 claim 就永远卡在那里：
    消息永远显示 BUSY，再也没人能处理它。

    Workspace Phase 0 把它变成**短期租约**：

        UNCLAIMED ──claim──→ CLAIMED ──success──→ PROCESSED
                                │
                                ├── failure ──→ UNCLAIMED
                                └── lease 过期 → UNCLAIMED

【最重要的一条边界】

    **租约只管「谁现在有权处理」，不管「处理成功没有」。**

        租约过期  → 别人可以接手       ✅
        租约过期  → processed_once 不变 ✅
        租约过期  → failure_count 不变  ✅
        租约过期  → 消息不删除          ✅

    把这两件事混起来（比如「超时就算失败」或者「超时就把消息重置」），
    就等于让一次 worker 崩溃抹掉真实业务状态。
"""

from __future__ import annotations

import multiprocessing as mp

import pytest

from core.identity import utc_now, utc_now_iso
from storage.database import Database

from communication import Message, MessageType
from communication.dedup import ClaimStatus, MessageClaim
from communication.recorder import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    CommunicationRecorder,
)
from tests import multiprocess_helpers as helpers


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def recorder(db) -> CommunicationRecorder:
    """租约 60 秒（测试用固定值，不依赖默认）。"""
    return CommunicationRecorder(db, claim_lease_seconds=60)


def make_message(message_id="msg_1"):
    return Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.SUBMIT,
        payload={"task_id": "T-1"},
        message_id=message_id,
    )


def at(offset_seconds: float) -> str:
    """
    「现在 + offset 秒」的 ISO 时间戳。

    【注意：每次调用都会重新读时钟】
        所以 `at(60) - at(0)` **不等于** 精确的 60 秒 ——
        两次调用之间真实时间在流逝（哪怕只有几微秒）。

        测「边界的精确行为」时不能用它，要用下面的 iso_at(base, offset)。
        测「30 秒 vs 60 秒」这种大间隔时无所谓。
    """
    from datetime import timedelta

    return (utc_now() + timedelta(seconds=offset_seconds)).isoformat()


def iso_at(base, offset_seconds: float) -> str:
    """
    基于**固定的**基准时间算偏移。

    边界测试必须用它 —— 否则「恰好等于租约时长」这件事
    会被两次读时钟之间的漂移破坏掉。
    """
    from datetime import timedelta

    return (base + timedelta(seconds=offset_seconds)).isoformat()


# ============================================================
# 一、租约的基本行为（规范里的 Test 1–4）
# ============================================================
class TestLeaseBasics:
    def test_normal_claim(self, recorder):
        """Test 1：A 认领成功。"""
        claim = recorder.claim_message(make_message(), "A", now=at(0))

        assert claim.status == ClaimStatus.CLAIMED
        assert claim.owner == "A"
        assert claim.reclaimed is False
        assert claim.recovered is False

    def test_second_worker_cannot_claim_while_leased(self, recorder):
        """Test 1 / 2：租约未过期，B 抢不到。"""
        recorder.claim_message(make_message(), "A", now=at(0))

        claim = recorder.claim_message(make_message(), "B", now=at(30))

        assert claim.status == ClaimStatus.BUSY
        assert claim.owner == "A"

    def test_expired_lease_can_be_reclaimed(self, recorder):
        """Test 3：租约过期，B 可以接手。"""
        recorder.claim_message(make_message(), "A", now=at(0))

        claim = recorder.claim_message(make_message(), "B", now=at(90))

        assert claim.status == ClaimStatus.CLAIMED
        assert claim.reclaimed is True
        assert claim.recovered is True
        assert claim.previous_owner == "A"

    def test_reclaim_updates_owner_and_timestamp(self, recorder):
        """Test 4：接手之后 claim_id 和 claimed_at 都更新。"""
        # 【用固定的基准时间】—— at(90) 调两次会得到两个不同的微秒值
        base = utc_now()
        first_claim_at = iso_at(base, 0)
        second_claim_at = iso_at(base, 90)

        recorder.claim_message(make_message(), "A", now=first_claim_at)
        recorder.claim_message(make_message(), "B", now=second_claim_at)

        state = recorder.processing_state("msg_1")

        assert state["claimed_by"] == "B"
        assert state["claimed_at"] == second_claim_at
        assert state["claimed_at"] > first_claim_at

    def test_exactly_at_boundary(self, recorder):
        """
        恰好等于租约时长时的行为（边界）。

        【为什么必须用固定基准】
            at(0) 和 at(60) 各读一次时钟 ——
            两者之间真实流逝的那几微秒会让差值变成 60 秒多一点，
            于是「恰好等于」这个边界根本测不到。
        """
        base = utc_now()
        recorder.claim_message(make_message(), "A", now=iso_at(base, 0))

        # cutoff = now - 60 = base；A 的 claimed_at 也是 base
        # 条件是 claimed_at < cutoff → base < base 为假 → **还没过期**
        claim = recorder.claim_message(make_message(), "B", now=iso_at(base, 60))
        assert claim.status == ClaimStatus.BUSY, "恰好到期时还不该被抢走"

        # 再多一点点就过期了
        claim = recorder.claim_message(
            make_message(), "B", now=iso_at(base, 60.5)
        )
        assert claim.status == ClaimStatus.CLAIMED

    def test_just_before_boundary(self, recorder):
        base = utc_now()
        recorder.claim_message(make_message(), "A", now=iso_at(base, 0))

        claim = recorder.claim_message(
            make_message(), "B", now=iso_at(base, 59.999)
        )
        assert claim.status == ClaimStatus.BUSY

    def test_reclaim_detail_explains_what_happened(self, recorder):
        recorder.claim_message(make_message(), "A", now=at(0))
        claim = recorder.claim_message(make_message(), "B", now=at(90))

        assert "A" in claim.detail
        assert "租约" in claim.detail or "过期" in claim.detail

    def test_lease_seconds_override(self, recorder):
        """单次调用可以覆盖租约时长。"""
        recorder.claim_message(make_message(), "A", now=at(0), lease_seconds=10)

        assert recorder.claim_message(
            make_message(), "B", now=at(5), lease_seconds=10
        ).status == ClaimStatus.BUSY
        assert recorder.claim_message(
            make_message(), "B", now=at(11), lease_seconds=10
        ).status == ClaimStatus.CLAIMED

    def test_invalid_lease_rejected(self, recorder):
        with pytest.raises(ValueError):
            recorder.claim_message(make_message(), "A", lease_seconds=0)
        with pytest.raises(ValueError):
            recorder.claim_message(make_message(), "A", lease_seconds=-5)


# ============================================================
# 二、租约 ≠ 处理结果（本文件最关键的边界）
# ============================================================
class TestLeaseDoesNotChangeOutcome:
    def test_lease_expiry_does_not_mark_processed(self, recorder):
        """
        【租约过期绝不等于处理完成】

            否则一次 worker 崩溃就会让未处理的消息看起来处理过了。
        """
        recorder.claim_message(make_message(), "A", now=at(0))
        recorder.claim_message(make_message(), "B", now=at(9999))

        assert recorder.was_processed("msg_1") is False
        assert recorder.processing_state("msg_1")["processed"] is False

    def test_lease_expiry_does_not_touch_failure_count(self, recorder):
        """
        【worker 崩溃 ≠ 业务处理失败】

            租约过期只是「占用失效」，不是「处理失败」。
            随意 +1 会让这个计数失去意义。
        """
        recorder.claim_message(make_message(), "A", now=at(0))
        recorder.mark_failed(make_message(), "A", "E", "业务失败")
        assert recorder.processing_state("msg_1")["failure_count"] == 1

        # 再认领一次然后让租约过期
        recorder.claim_message(make_message(), "B", now=at(10))
        recorder.claim_message(make_message(), "C", now=at(9999))

        assert recorder.processing_state("msg_1")["failure_count"] == 1, (
            "租约过期不该增加 failure_count"
        )

    def test_lease_expiry_does_not_delete_message(self, recorder):
        """消息永远不删 —— 超时只是让占用失效。"""
        recorder.claim_message(make_message(), "A", now=at(0))
        recorder.claim_message(make_message(), "B", now=at(9999))

        assert recorder.processing_state("msg_1") is not None
        assert recorder.count_messages() == 1

    def test_processed_once_is_permanent(self, recorder):
        """
        【Test 5】处理成功之后，**即使租约早已过期**也永久不可认领。
        """
        recorder.claim_message(make_message(), "A", now=at(0))
        recorder.mark_processed(make_message(), "A")

        claim = recorder.claim_message(make_message(), "B", now=at(10_000_000))

        assert claim.status == ClaimStatus.DUPLICATE
        assert claim.status != ClaimStatus.CLAIMED
        assert recorder.was_processed("msg_1") is True

    def test_processed_survives_lease_cycle(self, recorder):
        """认领 → 过期 → 再认领 → 成功 —— 最终事实是「成功」。"""
        recorder.claim_message(make_message(), "A", now=at(0))
        recorder.claim_message(make_message(), "B", now=at(90))
        recorder.mark_processed(make_message(), "B")

        assert recorder.was_processed("msg_1") is True
        assert recorder.processing_state("msg_1")["claimed_by"] is None

    def test_failure_releases_claim_immediately(self, recorder):
        """失败是立刻释放认领（不用等租约）—— 和租约过期是两条路。"""
        recorder.claim_message(make_message(), "A", now=at(0))
        recorder.mark_failed(make_message(), "A", "E", "boom")

        # 不用等租约，马上就能重新认领
        claim = recorder.claim_message(make_message(), "B", now=at(1))

        assert claim.claimed is True
        assert claim.reclaimed is False, "这不是「接手过期」，是「认领被释放了」"


# ============================================================
# 三、reclaim 的原子性
# ============================================================
class TestReclaimIsAtomic:
    def test_reclaim_is_one_conditional_update(self):
        """
        【不能 SELECT 判断过期 → 再 UPDATE】

            两个进程可能同时判断「过期了」，然后都去 UPDATE。

            必须是**一条**带条件的 UPDATE，让数据库裁决。
        """
        import ast
        from pathlib import Path

        source = Path("communication/recorder.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        # 找到 claim_message 方法
        claim_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "claim_message":
                claim_fn = node
        assert claim_fn is not None

        # 统计方法体里的 UPDATE 语句数
        body = ast.unparse(claim_fn)
        assert body.count("UPDATE communication_messages") == 1, (
            "claim_message 应该只有一条 UPDATE —— "
            "多条意味着「先查再改」的窗口"
        )

    def test_lease_condition_is_in_the_where_clause(self):
        """租约过期判断必须在 WHERE 里，而不是在 Python 里。"""
        import ast
        from pathlib import Path

        source = Path("communication/recorder.py").read_text(encoding="utf-8")
        body = " ".join(ast.unparse(ast.parse(source)).split())

        assert "claimed_at < ?" in body, (
            "过期判断必须在 SQL 的 WHERE 里（claimed_at < ?）—— "
            "放在 Python 里就会有 TOCTOU 窗口"
        )

    def test_concurrent_reclaim_only_one_wins(self, db):
        """
        两个「接手者」同时抢一个过期的认领 —— 只能有一个成功。

        （用两个 recorder 模拟两个 worker，时间点相同。）
        """
        first = CommunicationRecorder(db, claim_lease_seconds=60)
        second = CommunicationRecorder(db, claim_lease_seconds=60)

        first.claim_message(make_message(), "A", now=at(0))

        # 两个人都认为租约过期了，同时动手
        c1 = first.claim_message(make_message(), "B", now=at(90))
        c2 = second.claim_message(make_message(), "C", now=at(90))

        assert c1.claimed is True
        assert c2.claimed is False, "只有一个人能接手"
        assert c2.status == ClaimStatus.BUSY
        assert second.processing_state("msg_1")["claimed_by"] == "B"


# ============================================================
# 四、真实进程崩溃后的恢复（Test 6）
# ============================================================
class TestCrashRecovery:
    """
    【为什么必须用真进程】

        「崩溃」的意思是这个进程**没有机会执行任何清理代码**：

            · 没有 except 会跑
            · 没有 finally 会跑
            · 没有 close() 会跑

        在同一个进程里模拟不了这一点 —— 你总能把清理代码放别处。
        真进程直接 return 退出（不释放认领、不关连接）才是最接近的。
    """

    def test_crashed_worker_leaves_claim(self, db_file_for_crash):
        """
        崩溃的 worker 会留下一个 claim —— 这是**预期**的，
        因为我们刻意不给它任何清理机会。
        """
        results = self._crash(db_file_for_crash, lease_seconds=60)

        assert results[0]["ok"] is True

        db = Database(db_file_for_crash)
        state = CommunicationRecorder(db).processing_state("msg_crash")
        assert state["claimed_by"] == "crashed-worker"
        assert state["processed"] is False
        db.close()

    def test_cannot_claim_before_lease_expiry(self, db_file_for_crash):
        """崩溃之后、租约到期之前 —— 别人抢不到（防止重复处理）。"""
        self._crash(db_file_for_crash, lease_seconds=3600)

        db = Database(db_file_for_crash)
        recorder = CommunicationRecorder(db, claim_lease_seconds=3600)
        claim = recorder.claim_message(make_message("msg_crash"), "rescuer")
        db.close()

        assert claim.status == ClaimStatus.BUSY
        assert claim.owner == "crashed-worker"

    def test_can_claim_after_lease_expiry(self, db_file_for_crash):
        """
        【这就是租约存在的理由】

            崩溃之后、租约到期之后 —— 另一个人可以接手，
            而且**不需要崩溃的进程做任何配合**。
        """
        self._crash(db_file_for_crash, lease_seconds=60)

        db = Database(db_file_for_crash)
        recorder = CommunicationRecorder(db, claim_lease_seconds=60)

        # 把「现在」设成崩溃之后很久
        claim = recorder.claim_message(
            make_message("msg_crash"), "rescuer", now=at(3600)
        )
        state = recorder.processing_state("msg_crash")
        db.close()

        assert claim.claimed is True
        assert claim.reclaimed is True
        assert claim.previous_owner == "crashed-worker"
        assert state["claimed_by"] == "rescuer"

    def test_recovered_message_can_be_processed(self, db_file_for_crash):
        """接手之后能真正处理完 —— 完整闭环。"""
        self._crash(db_file_for_crash, lease_seconds=60)

        db = Database(db_file_for_crash)
        recorder = CommunicationRecorder(db, claim_lease_seconds=60)
        claim = recorder.claim_message(
            make_message("msg_crash"), "rescuer", now=at(3600)
        )
        assert claim.claimed
        recorder.mark_processed(make_message("msg_crash"), "rescuer")

        assert recorder.was_processed("msg_crash") is True
        state = recorder.processing_state("msg_crash")
        db.close()

        assert state["claimed_by"] is None
        assert state["failure_count"] == 0, "崩溃不该被算成业务失败"

    def test_crash_does_not_create_duplicate_rows(self, db_file_for_crash):
        self._crash(db_file_for_crash, lease_seconds=60)
        self._crash(db_file_for_crash, lease_seconds=60, owner="second-crash")

        db = Database(db_file_for_crash)
        count = db.fetchone(
            "SELECT COUNT(*) AS n FROM communication_messages "
            "WHERE message_id = 'msg_crash'"
        )["n"]
        db.close()

        assert count == 1, "同一条消息永远只有一行"

    # ------------------------------------------------------------
    @staticmethod
    def _crash(db_path, lease_seconds, owner="crashed-worker"):
        ctx = mp.get_context("spawn")
        out = ctx.Queue()
        process = ctx.Process(
            target=helpers.crash_after_claim_worker,
            args=(str(db_path), "msg_crash", owner, out, lease_seconds),
        )
        process.start()
        process.join(timeout=30)
        assert not process.is_alive()

        results = []
        while not out.empty():
            results.append(out.get())
        out.close()
        return results


@pytest.fixture
def db_file_for_crash(tmp_path):
    """给崩溃测试用的独立数据库文件。"""
    path = tmp_path / "crash.db"
    db = Database(str(path))
    db.initialize()
    db.close()
    return str(path)


# ============================================================
# 五、配置
# ============================================================
class TestLeaseConfiguration:
    def test_default_is_documented_constant(self):
        assert DEFAULT_CLAIM_LEASE_SECONDS > 0
        assert isinstance(DEFAULT_CLAIM_LEASE_SECONDS, int)

    def test_recorder_uses_default(self, db):
        recorder = CommunicationRecorder(db)
        assert recorder.claim_lease_seconds == DEFAULT_CLAIM_LEASE_SECONDS

    def test_recorder_accepts_override(self, db):
        recorder = CommunicationRecorder(db, claim_lease_seconds=7)
        assert recorder.claim_lease_seconds == 7

    def test_invalid_lease_rejected_at_construction(self, db):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                CommunicationRecorder(db, claim_lease_seconds=bad)

    def test_config_file_has_the_setting(self):
        """config.yaml 里必须真的有这个键，而且被读到。"""
        from core.config import load_global_config

        config = load_global_config()
        value = config.get("system.reliability.message_claim_lease_seconds")

        assert value is not None, (
            "config.yaml 里缺少 system.reliability.message_claim_lease_seconds"
        )
        assert int(value) > 0

    def test_composition_root_reads_the_config(self, tmp_path):
        """
        组合根**真的读了**配置 —— 不是碰巧用了默认值。
        """
        from main import build_runtime

        runtime = build_runtime(db_path=tmp_path / "cfg.db")
        try:
            from core.config import load_global_config

            expected = int(
                load_global_config().get(
                    "system.reliability.message_claim_lease_seconds"
                )
            )
            assert runtime.journal.claim_lease_seconds == expected
        finally:
            runtime.close()

    def test_lease_is_not_hardcoded_in_handlers(self):
        """
        租约时长不许写死在 Handler / 服务里 ——
        它是持久化层的策略，由组合根按配置注入。
        """
        import ast
        from pathlib import Path

        for path in sorted(Path("services").glob("*.py")):
            code = ast.unparse(ast.parse(path.read_text(encoding="utf-8")))
        # 【⚠️ 必须用**词边界**匹配，不能查子串】
        #
        #   真实踩到的坑：Workspace Core 引入 `release()` 之后，
        #   `services/workspace_gateway.py` 与
        #   `services/workspace_concurrency.py` 里含有 "release" ——
        #   它**包含** "lease" 这个子串，于是这两条守卫**假红**。
        #
        #   这属于本项目反复记录的那一类错误：
        #       **子串匹配误报**（`agent_directory` 含 `agent_d`、
        #       `seen_tracker` 含 `ack`，现在 `release` 含 `lease`）。
        #
        #   `\blease\b` 只匹配**独立的** lease/LEASE 标识符，
        #   不会命中 release / leased_to_other 之类。**

            import re as _re
            assert not _re.search(r"\blease\b", code), (
                f"services/{path.name} 里出现了 lease —— "
                f"租约判定属于持久化层"
            )


# ============================================================
# 六、MessageClaim 结果值
# ============================================================
class TestMessageClaimValue:
    def test_recovered_predicate(self):
        normal = MessageClaim(status=ClaimStatus.CLAIMED, message_id="m")
        recovered = MessageClaim(status=ClaimStatus.CLAIMED, message_id="m",
                                 reclaimed=True, previous_owner="A")

        assert normal.recovered is False
        assert recovered.recovered is True

    def test_busy_is_not_recovered(self):
        busy = MessageClaim(status=ClaimStatus.BUSY, message_id="m")
        assert busy.recovered is False

    def test_serialization_includes_lease_fields(self):
        claim = MessageClaim(status=ClaimStatus.CLAIMED, message_id="m",
                             reclaimed=True, previous_owner="A")
        data = claim.to_dict()

        assert data["reclaimed"] is True
        assert data["previous_owner"] == "A"

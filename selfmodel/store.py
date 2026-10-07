"""
Self Model V1 —— 持久化与检索
============================================================

【持久化（§十一）】

    Self Model 必须是**真正的持久状态**。
    Agent 重启后仍然存在 —— 不能只活在 LLM context / Python object /
    runtime session 里。

    本模块复用项目已有的 `storage.database.Database`，
    **不重新发明数据库**。

【🔴 检索是「选择」，不是「门控」（§十 / §十二）】

    `retrieve()` 做的是：
        从大量 Self Model 状态中，按 scope / domain / situation /
        kind / confidence 选出**与当前情境相关**的那部分。

    它**不做**：
        ❌ 判断该不该用某条 Strategy
        ❌ 按阈值筛掉「不够好」的策略
        ❌ 给出「最佳策略」

    §十 明确禁止：
        if applicability > 0.7: use_strategy()
        if expected_benefit > cost * 1.5: apply()

    本模块提供的 `min_confidence` 只是一个**检索过滤条件**
    （「我这次只想看高确信度的」），
    **不是** Runtime 替 Agent 做的取舍 —— 谁调用、传什么值，
    是调用方（Agent 的认知过程）的决定。

    检索结果 `SelfModelView` **没有** `best_strategy()` /
    `should_use()` —— 见 models.py 里那个类的 docstring。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso
from core.logging_setup import get_logger
from selfmodel.models import (
    KIND_CLAIM, STATUS_ACTIVE, STATUS_RETIRED, VALID_KINDS, VALID_STATUSES,
    Evidence, Scope, SelfClaim, SelfModelView, Strategy,
    _clamp_confidence, _new_id, dump_evidence, load_evidence,
)

logger = get_logger(__name__)

TABLE_SELF_CLAIMS = "self_model_claims"
TABLE_SELF_STRATEGIES = "self_model_strategies"

#: 🔒 建表语句由本模块**自带**，不放进 storage/models.py ——
#:    这样对既有存储层是**零侵入**（CREATE TABLE IF NOT EXISTS 对新表有效）。
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SELF_CLAIMS} (
    claim_id     TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    content      TEXT NOT NULL,
    world        TEXT,
    domain       TEXT,
    situation    TEXT,
    confidence   REAL NOT NULL DEFAULT 0.5,
    evidence     TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_smc_agent
    ON {TABLE_SELF_CLAIMS}(agent_id, status);

CREATE TABLE IF NOT EXISTS {TABLE_SELF_STRATEGIES} (
    strategy_id  TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    domain       TEXT,
    situation    TEXT,
    rationale    TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 0.5,
    evidence     TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sms_agent
    ON {TABLE_SELF_STRATEGIES}(agent_id, status);
"""


class SelfModelError(Exception):
    """Self Model 层面的错误。"""


def _row_to_claim(row: Any) -> SelfClaim:
    return SelfClaim(
        claim_id=row["claim_id"], agent_id=row["agent_id"],
        kind=row["kind"], content=row["content"],
        scope=Scope(world=row["world"], domain=row["domain"],
                    situation=row["situation"]),
        confidence=float(row["confidence"] or 0.0),
        evidence=load_evidence(row["evidence"]),
        status=row["status"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _row_to_strategy(row: Any) -> Strategy:
    return Strategy(
        strategy_id=row["strategy_id"], agent_id=row["agent_id"],
        name=row["name"], domain=row["domain"], situation=row["situation"],
        rationale=row["rationale"] or "",
        confidence=float(row["confidence"] or 0.0),
        evidence=load_evidence(row["evidence"]),
        status=row["status"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class SelfModelStore:
    """
    Self Model 的持久化与检索。**只存取，不判断。**

    🔒 它**没有** `decide()` / `should_use()` / `best_strategy()`。
       那是 Agent 的事（§十 / §十七：不要 CentralDecisionEngine）。
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """幂等建表。对新表来说 CREATE TABLE IF NOT EXISTS 就够了。"""
        with self.db.transaction() as conn:
            conn.executescript(_SCHEMA)

    # ================================================================
    # Claims
    # ================================================================
    def add_claim(
        self,
        agent_id: str,
        content: str,
        *,
        kind: str = KIND_CLAIM,
        scope: Optional[Scope] = None,
        confidence: float = 0.5,
        evidence: Optional[List[Evidence]] = None,
        claim_id: Optional[str] = None,
    ) -> SelfClaim:
        """写入一条 Self Claim。"""
        if not agent_id:
            raise SelfModelError("agent_id 不能为空")
        if not content or not str(content).strip():
            raise SelfModelError("content 不能为空")
        if kind not in VALID_KINDS:
            raise SelfModelError(
                f"未知 kind {kind!r}；合法取值：{sorted(VALID_KINDS)}")

        scope = scope or Scope()
        now = utc_now_iso()
        claim = SelfClaim(
            claim_id=claim_id or _new_id("claim"),
            agent_id=agent_id, kind=kind, content=str(content).strip(),
            scope=scope, confidence=_clamp_confidence(confidence),
            evidence=list(evidence or []), status=STATUS_ACTIVE,
            created_at=now, updated_at=now,
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_SELF_CLAIMS} "
                "(claim_id, agent_id, kind, content, world, domain, "
                " situation, confidence, evidence, status, "
                " created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim.claim_id, claim.agent_id, claim.kind, claim.content,
                 scope.world, scope.domain, scope.situation,
                 claim.confidence, dump_evidence(claim.evidence),
                 claim.status, now, now),
            )
        return claim

    def revise_claim(
        self,
        claim_id: str,
        *,
        content: Optional[str] = None,
        confidence: Optional[float] = None,
        scope: Optional[Scope] = None,
        evidence: Optional[List[Evidence]] = None,
    ) -> SelfClaim:
        """
        修正一条 claim。

        🔒 **修正**（不是覆盖）—— `updated_at` 会变，
           但 `created_at` 保留，这样「这条认知是怎么演化的」可追溯。
        """
        existing = self.get_claim(claim_id)
        if existing is None:
            raise SelfModelError(f"claim {claim_id!r} 不存在")
        new_scope = scope or existing.scope
        new = SelfClaim(
            claim_id=existing.claim_id, agent_id=existing.agent_id,
            kind=existing.kind,
            content=str(content).strip() if content else existing.content,
            scope=new_scope,
            confidence=(_clamp_confidence(confidence)
                        if confidence is not None else existing.confidence),
            evidence=(list(evidence) if evidence is not None
                      else existing.evidence),
            status=existing.status,
            created_at=existing.created_at, updated_at=utc_now_iso(),
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {TABLE_SELF_CLAIMS} SET content=?, world=?, domain=?,"
                " situation=?, confidence=?, evidence=?, updated_at=? "
                "WHERE claim_id=?",
                (new.content, new.scope.world, new.scope.domain,
                 new.scope.situation, new.confidence,
                 dump_evidence(new.evidence), new.updated_at, new.claim_id),
            )
        return new

    def retire_claim(self, claim_id: str) -> SelfClaim:
        """
        废弃一条 claim（**不删除**）。

        🔒 为什么不删除？
           provenance 是 Self Model 的价值所在 ——
           「我曾经这么以为，后来改了」本身就是重要认知。
        """
        return self._set_claim_status(claim_id, STATUS_RETIRED)

    def _set_claim_status(self, claim_id: str, status: str) -> SelfClaim:
        if status not in VALID_STATUSES:
            raise SelfModelError(f"未知 status {status!r}")
        existing = self.get_claim(claim_id)
        if existing is None:
            raise SelfModelError(f"claim {claim_id!r} 不存在")
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {TABLE_SELF_CLAIMS} SET status=?, updated_at=? "
                "WHERE claim_id=?", (status, now, claim_id))
        return self.get_claim(claim_id)          # type: ignore[return-value]

    def get_claim(self, claim_id: str) -> Optional[SelfClaim]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_SELF_CLAIMS} WHERE claim_id=?", (claim_id,))
        return _row_to_claim(row) if row else None

    # ================================================================
    # Strategies
    # ================================================================
    def add_strategy(
        self,
        agent_id: str,
        name: str,
        *,
        domain: Optional[str] = None,
        situation: Optional[str] = None,
        rationale: str = "",
        confidence: float = 0.5,
        evidence: Optional[List[Evidence]] = None,
        strategy_id: Optional[str] = None,
    ) -> Strategy:
        """创建一条 Strategy（§九 lifecycle: create）。"""
        if not agent_id:
            raise SelfModelError("agent_id 不能为空")
        if not name or not str(name).strip():
            raise SelfModelError("name 不能为空")

        now = utc_now_iso()
        s = Strategy(
            strategy_id=strategy_id or _new_id("strat"),
            agent_id=agent_id, name=str(name).strip(),
            domain=domain, situation=situation, rationale=rationale or "",
            confidence=_clamp_confidence(confidence),
            evidence=list(evidence or []), status=STATUS_ACTIVE,
            created_at=now, updated_at=now,
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_SELF_STRATEGIES} "
                "(strategy_id, agent_id, name, domain, situation, rationale,"
                " confidence, evidence, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (s.strategy_id, s.agent_id, s.name, s.domain, s.situation,
                 s.rationale, s.confidence, dump_evidence(s.evidence),
                 s.status, now, now),
            )
        return s

    def revise_strategy(
        self,
        strategy_id: str,
        *,
        name: Optional[str] = None,
        rationale: Optional[str] = None,
        confidence: Optional[float] = None,
        domain: Optional[str] = None,
        situation: Optional[str] = None,
        evidence: Optional[List[Evidence]] = None,
    ) -> Strategy:
        """修正一条 Strategy（§九: revise）。`created_at` 保留。"""
        existing = self.get_strategy(strategy_id)
        if existing is None:
            raise SelfModelError(f"strategy {strategy_id!r} 不存在")
        new = Strategy(
            strategy_id=existing.strategy_id, agent_id=existing.agent_id,
            name=str(name).strip() if name else existing.name,
            domain=domain if domain is not None else existing.domain,
            situation=(situation if situation is not None
                       else existing.situation),
            rationale=(rationale if rationale is not None
                       else existing.rationale),
            confidence=(_clamp_confidence(confidence)
                        if confidence is not None else existing.confidence),
            evidence=(list(evidence) if evidence is not None
                      else existing.evidence),
            status=existing.status,
            created_at=existing.created_at, updated_at=utc_now_iso(),
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {TABLE_SELF_STRATEGIES} SET name=?, domain=?,"
                " situation=?, rationale=?, confidence=?, evidence=?,"
                " updated_at=? WHERE strategy_id=?",
                (new.name, new.domain, new.situation, new.rationale,
                 new.confidence, dump_evidence(new.evidence),
                 new.updated_at, new.strategy_id),
            )
        return new

    def abandon_strategy(self, strategy_id: str) -> Strategy:
        """
        放弃一条 Strategy（§九: abandon）。

        🔒 **不删除** —— 「这个策略我试过，不行」是宝贵的认知。
        """
        if self.get_strategy(strategy_id) is None:
            raise SelfModelError(f"strategy {strategy_id!r} 不存在")
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {TABLE_SELF_STRATEGIES} SET status=?, updated_at=? "
                "WHERE strategy_id=?", (STATUS_RETIRED, now, strategy_id))
        return self.get_strategy(strategy_id)    # type: ignore[return-value]

    def get_strategy(self, strategy_id: str) -> Optional[Strategy]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_SELF_STRATEGIES} WHERE strategy_id=?",
            (strategy_id,))
        return _row_to_strategy(row) if row else None

    # ================================================================
    # 检索（§十二）
    # ================================================================
    def retrieve(
        self,
        agent_id: str,
        *,
        world: Optional[str] = None,
        domain: Optional[str] = None,
        situation: Optional[str] = None,
        kind: Optional[str] = None,
        min_confidence: Optional[float] = None,
        include_retired: bool = False,
        limit: int = 20,
        strategy_limit: Optional[int] = None,
    ) -> SelfModelView:
        """
        按当前情境检索相关的 Self Model 内容。

        🔒 **这是选择，不是门控。**
           它按「相关性」排序并截断，**不判断该不该用**。
           结果是一袋数据，交给 Agent 自己判断。

        排序键（仅用于决定**谁先被看到**）：
            ① scope 匹配度（限定了更多维度且匹配的排前）
            ② confidence 降序
            ③ updated_at 降序（近期修正过的排前）
        """
        ctx = Scope(world=world, domain=domain, situation=situation)

        claims = [
            c for c in self.list_claims(agent_id, kind=kind,
                                        include_retired=include_retired)
            if self._scope_ok(c.scope, ctx, world, domain, situation)
            and (min_confidence is None or c.confidence >= min_confidence)
        ]
        strategies = [
            s for s in self.list_strategies(agent_id,
                                            include_retired=include_retired)
            if self._strategy_ok(s, ctx, domain, situation)
            and (min_confidence is None or s.confidence >= min_confidence)
        ]

        # 排序：情境相关度 → scope 具体度 → confidence → 近期修正
        claims.sort(key=lambda c: (-c.scope.relevance(ctx),
                                   -c.scope.specificity(),
                                   -c.confidence,
                                   _neg_str(c.updated_at)))
        strategies.sort(key=lambda s: (-_strat_relevance(s, ctx),
                                       -_strat_specificity(s),
                                       -s.confidence,
                                       _neg_str(s.updated_at)))

        slimit = strategy_limit if strategy_limit is not None else limit
        return SelfModelView(claims=claims[:limit],
                             strategies=strategies[:slimit])

    @staticmethod
    def _scope_ok(claim_scope: Scope, ctx: Scope, world, domain,
                  situation) -> bool:
        """
        🔒 只按 **world / domain 硬边界**排除（§九 的「优先」不是「只」）。
           situation 不一致的降级为**排序靠后**，而不是被丢掉。
        """
        if not any(v is not None for v in (world, domain, situation)):
            return True                      # 未给情境 → 不筛
        return claim_scope.compatible(ctx)

    @staticmethod
    def _strategy_ok(s: Strategy, ctx: Scope, domain, situation) -> bool:
        if domain is not None and s.domain is not None:
            if str(s.domain).strip().lower() != str(domain).strip().lower():
                return False
        if situation is not None and s.situation is not None:
            if (str(s.situation).strip().lower()
                    != str(situation).strip().lower()):
                return False
        return True

    def list_claims(self, agent_id: str, *, kind: Optional[str] = None,
                    include_retired: bool = False) -> List[SelfClaim]:
        sql = f"SELECT * FROM {TABLE_SELF_CLAIMS} WHERE agent_id=?"
        params: List[Any] = [agent_id]
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if not include_retired:
            sql += " AND status=?"; params.append(STATUS_ACTIVE)
        return [_row_to_claim(r) for r in self.db.fetchall(sql, tuple(params))]

    def list_strategies(self, agent_id: str,
                        include_retired: bool = False) -> List[Strategy]:
        sql = f"SELECT * FROM {TABLE_SELF_STRATEGIES} WHERE agent_id=?"
        params: List[Any] = [agent_id]
        if not include_retired:
            sql += " AND status=?"; params.append(STATUS_ACTIVE)
        return [_row_to_strategy(r)
                for r in self.db.fetchall(sql, tuple(params))]

    def count(self, agent_id: Optional[str] = None) -> Dict[str, int]:
        if agent_id:
            c = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_SELF_CLAIMS} "
                "WHERE agent_id=?", (agent_id,))
            s = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_SELF_STRATEGIES} "
                "WHERE agent_id=?", (agent_id,))
        else:
            c = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_SELF_CLAIMS}")
            s = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_SELF_STRATEGIES}")
        return {"claims": int(c["n"] if c else 0),
                "strategies": int(s["n"] if s else 0)}


def _neg_str(value: Any) -> str:
    """把时间字符串反过来用于「降序」排序（避免依赖 SQLite 的时间类型）。"""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in str(value or ""))


def _strat_specificity(s: Strategy) -> int:
    return sum(1 for v in (s.domain, s.situation) if v)


def _strat_relevance(s: Strategy, ctx: Scope) -> int:
    """策略的情境相关度。**仅用于排序。**"""
    score = 0
    if s.domain is not None and ctx.domain is not None and \
            str(s.domain).strip().lower() == str(ctx.domain).strip().lower():
        score += 1
    if s.situation is not None and ctx.situation is not None and \
            str(s.situation).strip().lower() == \
            str(ctx.situation).strip().lower():
        score += 1
    return score

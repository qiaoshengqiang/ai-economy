"""
WorktreeManager —— Git worktree 的生命周期管理（Phase 1）
============================================================

【它负责什么，不负责什么】

    ✅ Git repository 的确认 · worktree 生命周期 · Git base reference
       physical materialization · intent log · reconciliation · 幂等

    ❌ **不负责** logical resource / revision / responsibility / lock
       —— 那些是 Workspace Core 的（已冻结）。

【🔒 边界：注入而不是 import】

    它**不 import** `core.workspace_store`。
    「Workspace 存在吗、revision 是多少」是通过注入的
    `workspace_lookup` 回调问的 ——
    与 `WorkspaceGateway` 用注入的 `is_participant` 是同一种手法。

    ```
    Runtime（组合根）
        ├── WorkspaceStore ──► workspace_lookup 回调
        ├── GitManager
        └── WorktreeManager(db, git_manager, workspace_lookup, ...)
    ```

【🔒 三条不可违反的约束】

    ① **physical_path 绝不进 Agent-facing 接口**
       所有 public 方法的返回都过 `_public()`。

    ② **同一 Workspace 最多一个 live worktree**
       由 DB 的**部分唯一索引**保证 ——
       **不是** `if not exists: create`（那个非原子，任务书 §17 明确禁止）。

    ③ **Reconciliation 不改 Workspace Responsibility**（任务书 §13）
       `reconcile()` 只返回**结论**，不碰 lock / controller /
       revision / participant。

【🔒 明确不做的事（任务书 §20 / §21 / §22）】

    ❌ Test Runner / 临时测试 worktree
    ❌ Patch / git diff 持久化 / applicability / STALE
    ❌ Execution Lease / heartbeart / retry

    ⚠️ 尤其**不写** `lease expired → delete worktree` ——
       那会把「执行机会」和「工作区生命周期」混为一谈。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from change.git import GitCommandError, GitManager
from change.worktree import (
    LIVE_STATES,
    IntentKind,
    IntentState,
    InformationalState,
    WorktreeErrorCode,
    WorktreeEventType,
    WorktreeRecord,
    WorktreeState,
    WorkspaceSnapshot,
    new_worktree_id,
)
from change.worktree_store import (
    DuplicateLiveWorktree,
    DuplicateOperation,
    WorktreeStore,
)
from core.logging_setup import get_logger
from core.workspace_core.operation import OperationResult, new_operation_id

logger = get_logger(__name__)

#: 「这个 Workspace 现在长什么样」—— 由组合根注入。
#: ⚠️ 返回 `None` 表示 Workspace 不存在。
WorkspaceLookup = Callable[[str], Optional[WorkspaceSnapshot]]

#: Worktree 目录名 —— 物理布局只在**内部**有意义。
DEFAULT_WORKTREES_DIRNAME = ".worktrees"


@dataclass(frozen=True)
class ReconcileReport:
    """`reconcile()` 的结论。**只报告，不修改责任。**"""

    worktree_id: Optional[str]
    workspace_id: Optional[str]
    finding: InformationalState
    action: str
    detail: Optional[dict[str, Any]] = None


class WorktreeManager:
    """Git worktree 的生命周期管理。"""

    def __init__(
        self,
        db,
        *,
        git_manager: GitManager,
        workspace_lookup: WorkspaceLookup,
        repository_ref: str,
        worktrees_root: Optional[str] = None,
        store: Optional[WorktreeStore] = None,
        registry=None,
        actor: str = "system",
    ) -> None:
        self.db = db
        self.git = git_manager
        self.store = store or WorktreeStore(db)
        self.repository_ref = repository_ref
        self.actor = actor

        if not callable(workspace_lookup):
            raise ValueError(
                "workspace_lookup 是 WorktreeManager 的必需依赖（fail-closed）。"
                "它回答「这个 workspace 存在吗、revision 是多少」—— "
                "没有它就无法验证 Workspace 边界，也无法记录 provenance。"
            )
        self._lookup: WorkspaceLookup = workspace_lookup

        # 🔒 物理根目录：可用显式传入（测试用），否则从 repo 推导。
        #
        #    ⚠️ 它只在**内部**使用。public 方法永远不返回它。
        root = worktrees_root or self._derive_worktrees_root()
        self._worktrees_root = str(Path(root).resolve())

        # ⚠️ **不是全局锁** —— 按 Workspace 分键（见 `_workspace_guard`）。
        self._registry = registry

    # ==========================================================
    # 内部
    # ==========================================================
    def _derive_worktrees_root(self) -> str:
        repo = self.git.repository_root() or self.git.repo_path or os.getcwd()
        return os.path.join(repo, DEFAULT_WORKTREES_DIRNAME)

    def _path_for(self, workspace_id: str, worktree_id: str) -> str:
        """
        物理路径由 `workspace_id` + `worktree_id` **确定性地**推导。

        🔒 确定性很重要：**重试必须落到同一个路径**，
           否则 reconciliation 无法判断「上次到底建到哪一步」。
        """
        return os.path.join(self._worktrees_root, workspace_id, worktree_id)

    @contextmanager
    def _workspace_guard(self, workspace_id: str):
        """
        按 Workspace 分键的**进程内**互斥。

        ⚠️ 这**不是**全局锁（任务书 §27 明确禁止 GLOBAL_WORKTREE_LOCK）：

            · 不同 Workspace → **不同的 key** → 完全并行
            · 而且它只是**减少无用功**，
              真正的并发正确性由 DB 的部分唯一索引保证

        换句话说：即使这把锁完全不存在，正确性也不变 ——
        因为「两个 live worktree」会被数据库拒绝。
        """
        if self._registry is None:
            yield
            return
        with self._registry.hold(f"WT:{self.repository_ref}", workspace_id):
            yield

    def _emit(self, conn, workspace_id, event_type, worktree_id, detail=None):
        """写事件。🔒 detail 里**绝不**放 physical_path。"""
        safe = dict(detail or {})
        safe.pop("physical_path", None)
        return self.store.add_event(
            conn,
            workspace_id=workspace_id,
            event_type=event_type.value if hasattr(event_type, "value") else event_type,
            actor=self.actor,
            worktree_id=worktree_id,
            detail=safe or None,
        )

    @staticmethod
    def _public(record: WorktreeRecord) -> dict[str, Any]:
        """
        🔒 **Agent-facing 视图 —— 剥掉 physical_path。**

        只留：worktree_id / workspace_id / task_id / state /
              base_commit / workspace_revision / git_ref / 时间戳
        """
        return {
            "worktree_id": record.worktree_id,
            "workspace_id": record.workspace_id,
            "task_id": record.task_id,
            "state": record.state.value,
            "repository_ref": record.repository_ref,
            "base_commit": record.base_commit,
            "workspace_revision": record.workspace_revision,
            "git_ref": record.git_ref,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "last_verified_at": record.last_verified_at,
        }

    def _fail(self, op_id, code, message, **extra):
        return OperationResult.fail(
            operation_id=op_id, code=code, message=message, **extra)

    # ==========================================================
    # create（§8 的九步）
    # ==========================================================
    def create(
        self,
        workspace_id: str,
        *,
        operation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        base_ref: Optional[str] = None,
    ) -> OperationResult:
        """
        为这个 Workspace 创建（或复用）它的 live worktree。

        【🔒 顺序是有意的 —— 它决定了 crash consistency 的性质】

            ① 幂等快路：同一个 operation 已经做完 → 直接返回
            ② 进程内按-workspace 互斥（只减少无用功）
            ③ 事务：验 Workspace → 写 intent(PENDING)
                     → **插入 PROVISIONING 行**（原子地占住唯一槽位）
            ④ **事务外**：`git worktree add`（慢，且不可回滚）
            ⑤ 事务：确认 READY + 解决 intent + 发事件

        【为什么「插入 PROVISIONING 行」必须在 git add **之前**】

            因为**唯一索引要在 Git 副作用之前占住槽位**。
            如果先 git add 再插行，两个并发 create 会**都**建出
            Git worktree，然后其中一个插行失败 ——
            于是留下一个**孤儿 worktree**。

            先占槽位 ⇒ 第二个 create 在「插行」这一步就被数据库拒绝，
            **根本不会**执行 git add。

        【为什么 git add 在**事务外**】

            Git 操作不能被 SQLite 回滚。
            把它放进事务里只会制造「事务回滚了但 worktree 还在」的错觉。
            ADR-0036 的做法是：**intent → 副作用 → confirm**，
            崩在中间由 reconciliation 收拾。
        """
        op_id = operation_id or new_operation_id()
        idem = f"worktree:provision:{workspace_id}:{op_id}"

        # ---- ① 幂等快路 ----
        prior = self.store.find_intent_by_key(idem)
        if prior is not None:
            if prior.state is IntentState.COMPLETED and prior.worktree_id:
                rec = self.store.get(prior.worktree_id)
                if rec is not None:
                    logger.debug("幂等命中：%s 已存在", idem)
                    return OperationResult.ok(
                        operation_id=op_id,
                        result=self._public(rec),
                        references={"idempotent_replay": True},
                    )
            # 还没走完 → 不能盲目再来一次
            return self._fail(
                op_id, WorktreeErrorCode.RECONCILIATION_REQUIRED,
                "这个操作之前已经开始但尚未完成 —— 需要先 reconcile",
                existing_intent=prior.intent_id,
            )

        with self._workspace_guard(workspace_id):
            # ---- ③ 事务：验 Workspace + 占槽位 ----
            with self.db.transaction(write=True) as conn:
                snap = self._lookup(workspace_id)
                if snap is None or not snap.exists:
                    return self._fail(
                        op_id, WorktreeErrorCode.WORKSPACE_NOT_FOUND,
                        f"Workspace {workspace_id} 不存在",
                    )

                # §12 触发方式 2：操作边界遇到未解决 intent 就停下
                pending = self.store.unresolved_intent_for(workspace_id)
                if pending is not None:
                    return self._fail(
                        op_id, WorktreeErrorCode.RECONCILIATION_REQUIRED,
                        "这个 Workspace 有未解决的 intent —— 先 reconcile",
                        existing_intent=pending.intent_id,
                    )

                live = self.store.find_live(workspace_id)
                if live is not None:
                    # 幂等友好：已经 READY/ACTIVE 就当作成功返回
                    if live.state in (WorktreeState.READY, WorktreeState.ACTIVE):
                        return OperationResult.ok(
                            operation_id=op_id,
                            result=self._public(live),
                            references={"reused": True},
                        )
                    return self._fail(
                        op_id, WorktreeErrorCode.ALREADY_ACTIVE,
                        f"这个 Workspace 已经有 live worktree（{live.state.value}）",
                        worktree_id=live.worktree_id,
                    )

                # 🔒 Git 侧前置条件（在 DB 里检查，不产生副作用）
                if not self.git.is_repository():
                    return self._fail(
                        op_id, WorktreeErrorCode.NOT_A_REPOSITORY,
                        "配置的 Git 仓库不可用",
                    )

                target_ref = base_ref or "HEAD"
                base_commit = self.git.rev_parse(target_ref)
                if base_commit is None:
                    return self._fail(
                        op_id, WorktreeErrorCode.BASE_REF_NOT_FOUND,
                        f"解析不出基线 ref：{target_ref}",
                    )

                worktree_id = new_worktree_id()
                path = self._path_for(workspace_id, worktree_id)
                # ============================================================
                # 🔒 branch 是**每个 worktree 一个**，不是「每个 workspace 一个」
                # ============================================================
                #
                #   【第一版写错了什么】
                #
                #       第一版用 `aieco/{workspace_id}` —— 一个稳定的
                #       「任务分支」名。看起来更符合设计文档 §3.3 的
                #       「1 Task Workspace = 1 branch」。
                #
                #       但它和「归档 worktree 可以存在」（任务书 §3）
                #       **直接冲突**：
                #
                #           归档的 worktree 仍然 checked out 在
                #           `aieco/WS-001` 上
                #           → 新建 live worktree 时 Git 报
                #             "fatal: 'aieco/WS-001' is already
                #              checked out at ..."
                #
                #       Git **不允许**同一个 branch 被两个 worktree
                #       同时 checkout —— 这是 Git 的硬规则，不是我们的选择。
                #
                #   【修法：采用标准 Git worktree 实践】
                #
                #       branch 名由 `worktree_id` 派生 ⇒ **天然唯一** ⇒
                #       不同的 worktree 永远不抢同一个 branch。
                #
                #       代价：branch 数量 = worktree 数量（含归档的）。
                #       这与「20 Agent ≠ 20 worktree」（§18）一致 ——
                #       worktree 数由 Task/Workspace 决定，不由 Agent 决定。
                #
                #   ⚠️ 这是**对设计文档 §3.3 的一处细化**（不是推翻）：
                #       那里说的「1 branch」指的是「同一个 workspace 在
                #       任一时刻只有一个**活的**工作分支」。
                #       见 ADR-0037。
                branch = f"aieco/{workspace_id}/{worktree_id}"

                intent = self.store.create_intent(
                    conn,
                    workspace_id=workspace_id,
                    kind=IntentKind.PROVISION,
                    idempotency_key=idem,
                    operation_id=op_id,
                    worktree_id=worktree_id,
                    payload={"path": path, "branch": branch,
                             "base_commit": base_commit},
                )

                # 🔒 **占住唯一槽位** —— 第二个并发 create 会在这里被拒绝
                record = self.store.insert_worktree(
                    conn,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    repository_ref=self.repository_ref,
                    base_commit=base_commit,
                    workspace_revision=snap.revision,
                    git_ref=f"refs/heads/{branch}",
                    state=WorktreeState.PROVISIONING,
                    physical_path=path,
                    worktree_id=worktree_id,
                )

            # ---- ④ 事务外：真正的 Git 副作用 ----
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                create_branch = not self.git.branch_exists(branch)
                self.git.worktree_add(
                    path,
                    commit=base_commit,
                    branch=branch,
                    create_branch=create_branch,
                )
            except (GitCommandError, OSError) as exc:
                logger.warning("git worktree add 失败：%s", exc)
                with self.db.transaction(write=True) as conn:
                    self.store.update_state(
                        conn, worktree_id,
                        state=WorktreeState.PROVISION_FAILED,
                        detail={"error": type(exc).__name__},
                    )
                    self.store.resolve_intent(
                        conn, intent.intent_id,
                        state=IntentState.FAILED, error=str(exc)[:500],
                    )
                    self._emit(conn, workspace_id,
                               WorktreeEventType.WORKTREE_PROVISION_FAILED,
                               worktree_id,
                               {"error": type(exc).__name__})
                return self._fail(
                    op_id, WorktreeErrorCode.PROVISION_FAILED,
                    "创建 Git worktree 失败",
                    worktree_id=worktree_id,
                )

            # ---- ⑤ 事务：确认 ----
            with self.db.transaction(write=True) as conn:
                self.store.update_state(
                    conn, worktree_id,
                    state=WorktreeState.READY, verified=True,
                    physical_path=path,
                )
                self.store.resolve_intent(
                    conn, intent.intent_id,
                    state=IntentState.COMPLETED, worktree_id=worktree_id,
                )
                self._emit(conn, workspace_id,
                           WorktreeEventType.WORKTREE_CREATED, worktree_id,
                           {"base_commit": base_commit,
                            "workspace_revision": snap.revision})

            final = self.store.get(worktree_id)
            return OperationResult.ok(
                operation_id=op_id, result=self._public(final))

    # ==========================================================
    # 查询
    # ==========================================================
    def get(self, worktree_id: str) -> OperationResult:
        op_id = new_operation_id()
        rec = self.store.get(worktree_id)
        if rec is None:
            return self._fail(op_id, WorktreeErrorCode.WORKTREE_NOT_FOUND,
                              f"没有这个 worktree：{worktree_id}")
        return OperationResult.ok(operation_id=op_id, result=self._public(rec))

    def get_for_workspace(self, workspace_id: str) -> OperationResult:
        """
        这个 Workspace 的 **live** worktree（若没有则报 NOT_FOUND）。

        ⚠️ 不返回归档的 —— 那些要显式 `list()`。
        """
        op_id = new_operation_id()
        rec = self.store.find_live(workspace_id)
        if rec is None:
            return self._fail(op_id, WorktreeErrorCode.WORKTREE_NOT_FOUND,
                              f"{workspace_id} 没有 live worktree")
        return OperationResult.ok(operation_id=op_id, result=self._public(rec))

    def list(self, workspace_id: Optional[str] = None) -> OperationResult:
        op_id = new_operation_id()
        records = (self.store.list_for_workspace(workspace_id)
                   if workspace_id else self.store.list_all())
        return OperationResult.ok(
            operation_id=op_id,
            result={"worktrees": [self._public(r) for r in records]},
        )

    def status(self, worktree_id: str) -> OperationResult:
        """
        状态 = DB 记录 + **Git 侧的实时事实**。

        🔒 它**不修改任何东西** —— 只是报告。
           要修正不一致请调 `reconcile()`。
        """
        op_id = new_operation_id()
        rec = self.store.get(worktree_id)
        if rec is None:
            return self._fail(op_id, WorktreeErrorCode.WORKTREE_NOT_FOUND,
                              f"没有这个 worktree：{worktree_id}")

        git_present = bool(rec.physical_path) and self.git.worktree_is_usable(
            rec.physical_path)
        consistent = self._is_consistent(rec, git_present)

        return OperationResult.ok(
            operation_id=op_id,
            result={
                **self._public(rec),
                "git_present": git_present,
                "consistent": consistent,
            },
        )

    @staticmethod
    def _is_consistent(rec: WorktreeRecord, git_present: bool) -> bool:
        if rec.state in (WorktreeState.PROVISIONING,):
            # PROVISIONING 本来就允许「Git 侧还没出现」
            return True
        if rec.state in (WorktreeState.READY, WorktreeState.ACTIVE):
            return git_present
        return True

    # ==========================================================
    # freeze / archive（§24 —— V1 **不物理删除**）
    # ==========================================================
    def freeze(self, worktree_id: str, *, operation_id: Optional[str] = None,
               reason: str = "") -> OperationResult:
        """
        READY / ACTIVE → FROZEN。

        🔒 **冻结不是删除。** 物理目录、Git 记录、DB 记录全部保留。

        ⚠️ 任务书 §24：「不要因为有 `git worktree remove`
           就简单提供一个无条件 delete」。
           所以 V1 **只提供 freeze / archive，不提供物理删除**。
        """
        op_id = operation_id or new_operation_id()
        idem = f"worktree:freeze:{worktree_id}:{op_id}"

        return self._transition(
            worktree_id, op_id, idem,
            intent_kind=IntentKind.FREEZE,
            allowed=(WorktreeState.READY, WorktreeState.ACTIVE),
            target=WorktreeState.FROZEN,
            event=WorktreeEventType.WORKTREE_FROZEN,
            label="freeze",
            detail={"reason": reason} if reason else None,
        )

    def archive(self, worktree_id: str, *, operation_id: Optional[str] = None,
                reason: str = "") -> OperationResult:
        """
        FROZEN → ARCHIVED。

        【⚠️ 为什么不接受 READY / ACTIVE 直接 archive】

            生命周期是 `…→ FROZEN → ARCHIVED`。
            允许跳步会让「冻结」这个语义变得可有可无。

            要归档一个活着的 worktree，先 `freeze()` ——
            那是一次**显式的**操作，会被记进 intent 与 event。
        """
        op_id = operation_id or new_operation_id()
        idem = f"worktree:archive:{worktree_id}:{op_id}"

        return self._transition(
            worktree_id, op_id, idem,
            intent_kind=IntentKind.ARCHIVE,
            allowed=(WorktreeState.FROZEN,),
            target=WorktreeState.ARCHIVED,
            event=WorktreeEventType.WORKTREE_ARCHIVED,
            label="archive",
            detail={"reason": reason} if reason else None,
        )

    def _transition(
        self, worktree_id, op_id, idem, *, intent_kind, allowed,
        target, event, label, detail=None,
    ) -> OperationResult:
        """freeze / archive 共用的转换逻辑（含 intent 与幂等）。"""
        prior = self.store.find_intent_by_key(idem)
        if prior is not None and prior.state is IntentState.COMPLETED:
            rec = self.store.get(worktree_id)
            if rec is not None:
                return OperationResult.ok(
                    operation_id=op_id, result=self._public(rec),
                    references={"idempotent_replay": True},
                )

        rec = self.store.get(worktree_id)
        if rec is None:
            return self._fail(op_id, WorktreeErrorCode.WORKTREE_NOT_FOUND,
                              f"没有这个 worktree：{worktree_id}")

        with self._workspace_guard(rec.workspace_id):
            with self.db.transaction(write=True) as conn:
                fresh = self.store.get(worktree_id)
                if fresh.state not in allowed:
                    return self._fail(
                        op_id, WorktreeErrorCode.INVALID_STATE,
                        f"当前状态 {fresh.state.value} 不允许 {label}",
                        current_state=fresh.state.value,
                        allowed=[s.value for s in allowed],
                    )

                try:
                    intent = self.store.create_intent(
                        conn,
                        workspace_id=fresh.workspace_id,
                        kind=intent_kind,
                        idempotency_key=idem,
                        operation_id=op_id,
                        worktree_id=worktree_id,
                    )
                except DuplicateOperation as exc:
                    return self._fail(
                        op_id, WorktreeErrorCode.DUPLICATE_OPERATION,
                        f"这个操作正在处理中（{exc.key}）",
                    )

                self.store.update_state(conn, worktree_id, state=target)
                self.store.resolve_intent(
                    conn, intent.intent_id, state=IntentState.COMPLETED)
                self._emit(conn, fresh.workspace_id, event, worktree_id,
                           detail or {k: v for k, v in
                                      {"reason": None}.items() if v})

            return OperationResult.ok(
                operation_id=op_id,
                result=self._public(self.store.get(worktree_id)),
            )

    # ==========================================================
    # reconcile（§11 / §12 / §13）
    # ==========================================================
    def reconcile(self, worktree_id: str) -> OperationResult:
        """
        比较 **DB 状态** 与 **Git 事实**，修正 Change Pipeline 状态。

        🔒 **它只恢复 Change Pipeline 状态，不改变 Workspace Responsibility**
           （任务书 §13）。具体地，它**绝不**：

               ❌ 改 Workspace Lock / Controller / Revision / Participant
               ❌ 自动重新分配 Agent
               ❌ 释放 responsibility
               ❌ 覆盖 Git branch
               ❌ 删除「未知」的 worktree（只标记 ORPHANED）
        """
        op_id = new_operation_id()
        rec = self.store.get(worktree_id)
        if rec is None:
            return self._fail(op_id, WorktreeErrorCode.WORKTREE_NOT_FOUND,
                              f"没有这个 worktree：{worktree_id}")

        git_present = bool(rec.physical_path) and self.git.worktree_is_usable(
            rec.physical_path)
        report = self._reconcile_record(rec, git_present)

        return OperationResult.ok(
            operation_id=op_id,
            result={
                **self._public(self.store.get(worktree_id)),
                "finding": report.finding.value,
                "action": report.action,
            },
        )

    def _reconcile_record(self, rec: WorktreeRecord,
                          git_present: bool) -> ReconcileReport:
        """核心判定 —— §11 的三种情况。"""
        pending = self.store.unresolved_intent_for(rec.workspace_id)

        # ---- 情况 A：DB 说 PROVISIONING ----
        if rec.state is WorktreeState.PROVISIONING:
            if git_present:
                # Git 侧其实建好了，只是最后一步没落 ── 确认它
                with self.db.transaction(write=True) as conn:
                    self.store.update_state(
                        conn, rec.worktree_id,
                        state=WorktreeState.READY, verified=True)
                    if pending is not None:
                        self.store.resolve_intent(
                            conn, pending.intent_id,
                            state=IntentState.COMPLETED,
                            worktree_id=rec.worktree_id)
                    self._emit(conn, rec.workspace_id,
                               WorktreeEventType.WORKTREE_RECONCILED,
                               rec.worktree_id,
                               {"finding": "db_provisioning_git_exists"})
                return ReconcileReport(
                    rec.worktree_id, rec.workspace_id,
                    InformationalState.DB_PROVISIONING_GIT_EXISTS,
                    "confirmed READY",
                )

            # Git 侧没有 ── 副作用从未发生，安全地标记失败
            with self.db.transaction(write=True) as conn:
                self.store.update_state(
                    conn, rec.worktree_id,
                    state=WorktreeState.PROVISION_FAILED,
                    detail={"reconciled": "git never materialized"})
                if pending is not None:
                    self.store.resolve_intent(
                        conn, pending.intent_id, state=IntentState.FAILED,
                        error="reconcile: git worktree never materialized")
                self._emit(conn, rec.workspace_id,
                           WorktreeEventType.WORKTREE_PROVISION_FAILED,
                           rec.worktree_id,
                           {"finding": "db_provisioning_git_missing"})
            return ReconcileReport(
                rec.worktree_id, rec.workspace_id,
                InformationalState.UNRESOLVED_INTENT,
                "marked PROVISION_FAILED (side effect never happened)",
            )

        # ---- 情况 B：DB 说活着，Git 说没有 ----
        if rec.state in (WorktreeState.READY, WorktreeState.ACTIVE) and not git_present:
            with self.db.transaction(write=True) as conn:
                self.store.update_state(
                    conn, rec.worktree_id,
                    state=WorktreeState.INCONSISTENT,
                    detail={"reconciled": "git worktree missing"})
                self._emit(conn, rec.workspace_id,
                           WorktreeEventType.WORKTREE_INCONSISTENT,
                           rec.worktree_id,
                           {"finding": "db_ready_git_missing"})
            return ReconcileReport(
                rec.worktree_id, rec.workspace_id,
                InformationalState.DB_READY_GIT_MISSING,
                "marked INCONSISTENT",
            )

        # ---- 一致 ----
        with self.db.transaction(write=True) as conn:
            self.store.update_state(
                conn, rec.worktree_id, state=rec.state, verified=True)
            if pending is not None and rec.state not in LIVE_STATES:
                self.store.resolve_intent(
                    conn, pending.intent_id, state=IntentState.ABANDONED,
                    error="reconcile: worktree already terminal")
        return ReconcileReport(
            rec.worktree_id, rec.workspace_id,
            InformationalState.CONSISTENT, "no change",
        )

    def reconcile_all(self) -> OperationResult:
        """
        全量 reconciliation —— **§12 触发方式 1（Startup）**。

        做两件事：

            ① 对每条 DB 记录：`_reconcile_record`
            ② **扫描 Git 侧**：找出「在我们管理的根目录下、
               但 DB 里没有记录」的 worktree
               → 记为 **ORPHANED**（§11 情况 C）

        🔒 情况 C **绝不静默当成合法 worktree**，也**绝不删除它**。
           只记录 + 发事件，由人/治理决定。
        """
        op_id = new_operation_id()
        reports: list[dict] = []

        for rec in self.store.list_all():
            git_present = bool(rec.physical_path) and \
                self.git.worktree_is_usable(rec.physical_path)
            report = self._reconcile_record(rec, git_present)
            reports.append({
                "worktree_id": report.worktree_id,
                "workspace_id": report.workspace_id,
                "finding": report.finding.value,
                "action": report.action,
            })

        reports.extend(self._find_orphans())
        return OperationResult.ok(
            operation_id=op_id, result={"reports": reports})

    def _find_orphans(self) -> list[dict]:
        """
        §11 **情况 C**：Git 里有、DB 里没有。

        🔒 判定范围严格限制在 `self._worktrees_root` **之下** ——
           否则主仓库自己的 worktree（以及用户手动建的）
           会被误判成我们的孤儿。
        """
        found: list[dict] = []
        root = Path(self._worktrees_root)

        for info in self.git.list_worktrees():
            try:
                path = Path(info.path).resolve()
                path.relative_to(root)
            except (ValueError, OSError):
                continue                      # 不在我们的根目录下 —— 与我们无关

            if self.store.find_by_physical_path(str(path)) is not None:
                continue                      # 已知，不是孤儿

            workspace_id = self._workspace_from_path(path)
            if workspace_id is None:
                continue

            with self.db.transaction(write=True) as conn:
                # ⚠️ 插成 ORPHANED —— 它**不占** live 槽位
                #    （部分唯一索引只约束 provisioning/ready/active），
                #    所以这个 Workspace 之后仍可以正常 create。
                record = self.store.insert_worktree(
                    conn,
                    workspace_id=workspace_id,
                    repository_ref=self.repository_ref,
                    base_commit=info.head or "",
                    workspace_revision=0,     # 未知 —— 标 0 而不是编一个
                    git_ref=info.branch,
                    state=WorktreeState.ORPHANED,
                    physical_path=str(path),
                    detail={"discovered_by": "reconcile_all"},
                )
                self._emit(conn, workspace_id,
                           WorktreeEventType.WORKTREE_ORPHANED,
                           record.worktree_id,
                           {"finding": "db_none_git_exists"})

            found.append({
                "worktree_id": record.worktree_id,
                "workspace_id": workspace_id,
                "finding": InformationalState.DB_NONE_GIT_EXISTS.value,
                "action": "recorded ORPHANED (not deleted, not trusted)",
            })

        return found

    def _workspace_from_path(self, path: Path) -> Optional[str]:
        """
        从 `<root>/<workspace_id>/<worktree_id>` 反推 workspace_id。

        ⚠️ 推不出来就返回 None —— **不猜**。
        """
        try:
            rel = path.relative_to(Path(self._worktrees_root))
        except ValueError:
            return None
        parts = rel.parts
        return parts[0] if len(parts) >= 1 and parts[0] else None

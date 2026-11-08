"""
Workspace Gateway —— Agent 访问 Task Workspace 的**唯一受控入口**（Step 5/6/7）
============================================================

【🔒 Gateway 是什么、不是什么（任务书 §15）】

    是：  Agent 访问 Workspace 的**唯一受控入口**
    不是：IAM · Task Manager · Git Manager

【🔒 三条不可违反的约束】

    ① **Agent 永远看不到宿主机物理路径。**
       所有接口只接受 / 返回 `workspace_id` + 逻辑 `path` / `resource_id`。

    ② **不提供传统的 `lock(resource)` API**（任务书 §43）。
       Lock 是**工作责任**，不是 mutex ——
       不允许「我先把这个资源锁起来再说」这种无责任占用。
       Lock 只能通过 Create / Handoff / Release / Force Unlock 形成。

    ③ **不提供任何 Git 操作**（任务书 §42）。

【🔒 统一操作生命周期（任务书 §27）】

    Mutation:
        Resolve Resource → Validate Participant
        → Validate Responsibility / Lock → Validate Revision
        → Execute → Update State → Create Revision
        → Create Audit → Emit Event → Return OperationResult

    Read:  Validate Participant → Read → Return（**不产生 Event / 不记读 Audit**）

    Responsibility:  Validate → Change → Audit → Event → Return

【🔒 并发安全（任务书 §36–§41）】

    每一层都在起作用，缺一不可：

        资源级锁（进程内）  → 同资源竞争者先排队，不同资源并行
        SQLite 写事务       → 「检查 + 修改」原子
        revision 乐观锁     → Revision Race 确定地失败

    ⚠️ **底层瓶颈如实记录**：SQLite 单写者 ⇒ 不同 Workspace 的**写**
       在底层仍会排队。但这是**基础设施瓶颈**，
       **不会**让 W2 得到 `LOCKED_BY_OTHER_AGENT` ——
       Workspace 的锁语义仍然是资源级的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.workspace_core import (
    AuditAction,
    AuditResult,
    ErrorCode,
    ExplicitLock,
    LockKind,
    OperationResult,
    Resource,
    ResourceState,
    ResourceTree,
    ResourceType,
    WorkspaceEventType,
    WorkspacePathError,
    normalize_path,
    new_operation_id,
)
from core.workspace_core.resource import basename_of, join_path
from core.workspace_store import WorkspaceStore
from services.workspace_concurrency import ResourceLockRegistry

#: 判定「这个 Agent 是不是这个 Task Workspace 的 Participant」。
#: 由组合根注入（默认拒绝一切 —— 安全的默认值）。
ParticipantCheck = Callable[[str, str], bool]

#: 判定「这个 Agent 到底存不存在」（身份层的事，不是权限）。
#:
#: 🔒 **这是 `WorkspaceGateway` 的必需依赖（fail-closed）。**
#:
#: 【为什么不允许 None】
#
#     第一版把它做成可选的，`None` 表示「跳过存在性校验」——
#     那是 **fail-open**：
#
#         handoff(A → 一个完全不存在的 Agent) → 成功
#
#     于是资源责任挂在一个虚构的 holder 上，
#     而 Effective Lock 解析会忠实地返回那个名字。
#
#     这与 Workspace Core 已经确定的 responsibility 语义不一致：
#     **责任必须交给一个真实存在的主体。**
#
#     所以现在没提供它 → **Gateway 构造失败**，
#     而不是等到 runtime 某次 handoff 才发现。
AgentExistsCheck = Callable[[str], bool]


def _deny_all(workspace_id: str, agent_id: str) -> bool:  # pragma: no cover
    return False


@dataclass(frozen=True)
class WorkspaceLimits:
    """
    资源层的**结构上限**。由组合根从 `config.yaml` 的 `workspace:` 段注入。

    🔒 为什么由 **Gateway** 强制，而不是 `services/artifacts.py`：

        「路径有多深」「一个目录能挂几个孩子」是关于**资源树结构**的事实。
        资源树的权威是 `WorkspaceGateway`（它拥有 `WorkspaceStore` 与锁）。
        `services/artifacts.py` 只是便捷包装 —— 以前那条「最多 8 层」
        写在它里面，是**分层失误**：

            结构规则 与 便捷包装 混在一起 ⇒ 换个调用方就绕过了上限。

    🔒 上限**宽到不影响正常分工** —— 它防的是意外爆炸，不是限制协作。
    """

    #: 逻辑路径最多几层（`a/b/c.md` = 3 层；只算非空、非 `.` 的段）。
    max_path_depth: int = 8
    #: 一个目录最多几个**直接**子项（文件 + 子目录）。
    max_children_per_directory: int = 50

    @classmethod
    def from_mapping(cls, raw: Any) -> "WorkspaceLimits":
        """
        从配置的 `workspace:` 段构造。

        🔒 配置缺失 / 值不合法时**回落到这里声明的默认值** ——
           而不是回落到另一处写死的数字（那会变成两个来源）。

        ⚠️ 非正整数一律忽略（`0` / 负数 / 字符串都不接受）——
           上限为 0 会**静默禁掉一切创建**，那比不设上限更危险。
        """
        def _positive(key: str, default: int) -> int:
            try:
                got = int((raw or {}).get(key, default))
            except (TypeError, ValueError):
                return default
            return got if got > 0 else default

        return cls(
            max_path_depth=_positive("max_path_depth", 8),
            max_children_per_directory=_positive(
                "max_children_per_directory", 50),
        )


class WorkspaceGateway:
    """Workspace Core 的门面（唯一受控入口）。"""

    def __init__(
        self,
        db,
        *,
        store: Optional[WorkspaceStore] = None,
        lock_registry: Optional[ResourceLockRegistry] = None,
        is_participant: Optional[ParticipantCheck] = None,
        limits: Optional["WorkspaceLimits"] = None,
        agent_exists: AgentExistsCheck,
    ) -> None:
        """
        【🔒 两个依赖，两种 fail-closed 方式】

            `is_participant`  —— 可省略。省略时 = **拒绝一切**
                                 （安全的默认值：忘记注入不会放开访问）

            `agent_exists`    —— **必需**。省略时 Python 直接
                                 `TypeError`，Gateway **根本构造不出来**。

        【为什么两者不对称】

            `is_participant` 有一个语义正确的"最严格默认值"（谁也不许）。
            `agent_exists` 没有一个合理的默认值：

                · 默认 True  → 等于没校验（fail-open）❌
                · 默认 False → 任何 handoff 都会 UNKNOWN_AGENT，
                               错误信息会误导人（"Agent 不存在"
                               而不是"你没配身份校验"）❌

            所以让**构造期**就失败，把配置问题暴露在最容易发现的地方。
            这正是任务书要求的 fail-closed。
        """
        # 🔒 显式传 None 也不行 —— 关键字必需只挡住"没传"，
        #    挡不住 `agent_exists=None`。这里补一道运行时检查。
        if agent_exists is None:
            raise ValueError(
                "agent_exists 是 WorkspaceGateway 的必需依赖（fail-closed）。"
                "它是'这个 Agent 是否真实存在'的判定回调 —— "
                "没有它，handoff 可以指向一个虚构的接收方，"
                "资源责任就会挂在一个不存在的 holder 上。"
                "如果确实不需要身份校验，请显式传 `lambda _: True` "
                "（但那等于放弃 fail-closed，应当有明确理由）。"
            )
        if not callable(agent_exists):
            raise TypeError(
                f"agent_exists 必须是可调用对象，收到 {type(agent_exists).__name__}"
            )

        self.db = db
        self.store = store or WorkspaceStore(db)
        self.locks = lock_registry or ResourceLockRegistry()
        # 🔒 结构上限：由组合根从配置注入；不传就用 WorkspaceLimits 的默认。
        self.limits = limits or WorkspaceLimits()
        # 默认拒绝 —— 忘记注入时**不会**意外放开访问
        self._is_participant: ParticipantCheck = is_participant or _deny_all
        # 🔒 必需依赖，构造期已校验
        self._agent_exists: AgentExistsCheck = agent_exists

    # ==========================================================
    # 内部工具
    # ==========================================================
    def _participant(self, workspace_id: str, agent_id: str) -> bool:
        return bool(self._is_participant(workspace_id, agent_id))

    def _fail(
        self,
        op_id: str,
        code: ErrorCode,
        message: str,
        *,
        audit: Optional[tuple[AuditAction, str, Optional[str]]] = None,
        workspace_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
        **extra: Any,
    ) -> OperationResult:
        """
        统一的失败路径：**写 Audit、不写 Event**。

        🔒 **失败的 mutation 不产生成功的 Event**（任务书 §20 / §30）——
           它只留一条 Audit（`*_ATTEMPT` / FAILED）。
        """
        if audit and workspace_id:
            action, actor, resource_id = audit
            with self.db.transaction(write=True) as conn:
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=action.value,
                    actor=actor,
                    result=AuditResult.FAILED.value,
                    resource_id=resource_id,
                    error_code=code.value,
                    detail=detail,
                )
        return OperationResult.fail(
            operation_id=op_id, code=code, message=message, **extra
        )

    @staticmethod
    def _attempt(action: AuditAction) -> AuditAction:
        from core.workspace_core.events import attempt_action
        return attempt_action(action)

    def _bump_revision(
        self,
        conn,
        resource,
        *,
        change_type: str,
        actor: str,
        change_ref: str,
        state: Optional[ResourceState] = None,
    ):
        """
        **递增 revision 的唯一正确方式。**

        ============================================================
        【🔒 为什么必须包成这样 —— Hardening 抓到的真实不一致】
        ============================================================

            资源的 `revision` **列**与 `workspace_revisions` **表**
            是两个必须同步的东西。第一版有几处只改了列、没写表：

                · `rename` 递增父目录 revision     → 没有 revision 行
                · `move`   递增 source/dest 父目录 → 没有 revision 行
                · `delete` 目录时把整棵子树标成 DELETED
                          并 revision+1            → 没有 revision 行

            结果是 `revisions_for(resource)` 的历史出现**空洞**：

                列 revision = 5
                表里最大    = 2      ← 中间三次变化查不到

            这直接违反任务书 §20 的不变量
            「**revision 单调递增**」的精神 ——
            单调是对的，但**可追溯性断了**。

            修法：把「改列 + 写表」绑成一个动作，让调用方
            **没有机会**只做一半。
        """
        new_rev = resource.revision + 1
        # ⚠️ `state` 必须一起传 —— 否则「级联删除子树」会只改 revision、
        #    不改 state，子资源看起来还活着。
        #    （这是我把 delete 的子树循环改写成 _bump_revision 时
        #      **引入过的真实回归**，被不变量检查器抓出来。）
        self.store.update_resource(conn, resource, revision=new_rev, state=state)
        self.store.add_revision(
            conn,
            resource_id=resource.resource_id,
            workspace_id=resource.workspace_id,
            revision_number=new_rev,
            created_by=actor,
            change_type=change_type,
            change_ref=change_ref,
        )
        return new_rev

    # ==========================================================
    # 引导：创建 Workspace 根
    # ==========================================================
    def ensure_workspace(self, workspace_id: str, owner: str) -> OperationResult:
        """
        确保一个 Workspace 存在，并让 `owner` 成为它的**第一任 Controller**。

        【为什么这是唯一「凭空出现」责任的地方】

            🔒 任务书要求 **Control 必须有明确来源，不能凭空出现**。
               一个 Workspace 总要有第一个负责人 ——
               那个来源就是「创建这个 Task Workspace 的人」。
               此后所有责任都必须经由 Create 的继承
               或 Handoff / Release 的正式变化产生。

        【为什么自带一把显式锁是**不**冗余的】

            §11「创建新资源不产生 Explicit Lock」的前提是
            **创建者已经持有父目录的 Directory Lock**。
            Workspace 根**没有父目录**，所以它必须自带显式锁 ——
            否则整棵树将永远没有责任的源头。
        """
        op_id = new_operation_id()
        with self.locks.hold_namespace(workspace_id, "ROOT"):
            with self.db.transaction(write=True) as conn:
                existing = self.store.find_by_path(workspace_id, "/")
                if existing is not None:
                    return OperationResult.ok(
                        operation_id=op_id,
                        result={
                            "resource_id": existing.resource_id,
                            "revision": existing.revision,
                            "created": False,
                        },
                    )

                root = self.store.create_resource(
                    conn,
                    workspace_id=workspace_id,
                    parent=None,
                    name="",
                    resource_type=ResourceType.DIRECTORY,
                    path="/",
                    created_by=owner,
                )
                self.store.set_explicit_lock(
                    conn,
                    resource_id=root.resource_id,
                    holder=owner,
                    kind=LockKind.DIRECTORY,
                    acquired_by=owner,
                )
                self.store.add_revision(
                    conn,
                    resource_id=root.resource_id,
                    workspace_id=workspace_id,
                    revision_number=1,
                    created_by=owner,
                    change_type="CREATE",
                    change_ref=op_id,
                )
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.CREATE.value,
                    actor=owner,
                    result=AuditResult.SUCCESS.value,
                    resource_id=root.resource_id,
                    detail={"path": "/", "type": "directory", "bootstrap": True},
                )
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.RESOURCE_CREATED.value,
                    actor=owner,
                    resource_id=root.resource_id,
                    revision=1,
                    detail={"path": "/", "type": "directory"},
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": root.resource_id,
                        "revision": 1,
                        "created": True,
                    },
                )

    # ==========================================================
    # READ —— 不产生 Event，不记录读 Audit（任务书 §17 / §18）
    # ==========================================================
    def list(
        self, workspace_id: str, path: str, agent_id: str
    ) -> OperationResult:
        op_id = new_operation_id()

        if not self._participant(workspace_id, agent_id):
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.NOT_TASK_PARTICIPANT,
                message="不是这个 Task Workspace 的 Participant",
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target

        if target.is_file:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_RESOURCE_TYPE,
                message="list() 只能作用于目录",
            )

        tree = self.store.load_tree(workspace_id)
        entries = []
        for child in tree.children_of(target.resource_id):
            if child.is_deleted:
                continue
            entries.append(self._describe(child, tree))

        return OperationResult.ok(
            operation_id=op_id,
            result={
                "resource_id": target.resource_id,
                "path": target.path,
                "revision": target.revision,
                # 🔒 列目录时同时给出**这个目录自己**的责任/锁事实。
                #    `entries` 里每个子项也有；但 root 不在 entries 里，
                #    Agent 需要它来判断“我能不能在根下建 tests/”。
                "effective_controller": tree.effective_controller(target),
                "effective_lock_holder": tree.effective_lock(target),
                "entries": entries,
            },
        )

    def read(
        self, workspace_id: str, path: str, agent_id: str
    ) -> OperationResult:
        """
        🔒 `revision` **必须**返回 ——
           Agent 后续 `edit(base_revision=...)` 要用它（任务书 §18）。
        """
        op_id = new_operation_id()

        if not self._participant(workspace_id, agent_id):
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.NOT_TASK_PARTICIPANT,
                message="不是这个 Task Workspace 的 Participant",
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target

        return OperationResult.ok(
            operation_id=op_id,
            result={
                "resource_id": target.resource_id,
                "path": target.path,
                "content": target.content,
                "revision": target.revision,
                "type": target.resource_type.value,
            },
        )

    def _describe(self, resource: Resource, tree: ResourceTree) -> dict[str, Any]:
        """
        `list()` 里每个条目的描述。

        🔒 额外给出 `effective_controller` / `effective_lock_holder`
           —— 让 Agent 能快速知道「谁长期负责、现在谁在干」
           （任务书 §17 的推荐项）。
        """
        return {
            "resource_id": resource.resource_id,
            "name": resource.name,
            "type": resource.resource_type.value,
            "revision": resource.revision,
            "effective_controller": tree.effective_controller(resource),
            "effective_lock_holder": tree.effective_lock(resource),
        }

    def _resolve_path(
        self, workspace_id: str, path: str, op_id: str
    ) -> Resource | OperationResult:
        try:
            normalized = normalize_path(path)
        except WorkspacePathError as exc:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.OUTSIDE_WORKSPACE,
                message=str(exc),
            )

        resource = self.store.find_by_path(workspace_id, normalized)
        if resource is None:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.RESOURCE_NOT_FOUND,
                message=f"路径不存在：{normalized}",
            )
        return resource

    # ==========================================================
    # CREATE
    # ==========================================================
    def create(
        self,
        workspace_id: str,
        parent_path: str,
        name: str,
        resource_type: str,
        agent_id: str,
        *,
        content: Optional[str] = None,
    ) -> OperationResult:
        """
        前置：**Effective Directory Lock(parent) == Agent**。

        因为创建本质上是在改**父目录的 Namespace**（任务书 §19）。

        🔒 新资源 **Explicit Lock = None**，Effective Lock 靠继承父目录 ——
           创建者已经持有父目录责任，再记一条显式锁是冗余（§11）。
        """
        op_id = new_operation_id()
        try:
            rtype = ResourceType(resource_type)
        except ValueError:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"resource_type 必须是 directory / file，收到 {resource_type!r}",
            )

        if not self._participant(workspace_id, agent_id):
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.NOT_TASK_PARTICIPANT,
                message="不是这个 Task Workspace 的 Participant",
            )

        parent = self._resolve_path(workspace_id, parent_path, op_id)
        if isinstance(parent, OperationResult):
            return parent
        if parent.is_file:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_PARENT,
                message="父资源必须是目录",
            )

        try:
            child_path = join_path(parent.path, name)
        except WorkspacePathError as exc:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_ARGUMENT,
                message=str(exc),
            )

        # 🔴 结构上限①：路径深度。
        #
        #    以前这条写在 `services/artifacts.py` 的
        #    `validate_artifact_path()` 里（写死 8）—— 分层失误：
        #    结构权威是资源树，不是便捷包装层。
        #    现在由 Gateway 从配置强制，**artifact 层那段已删掉**。
        #
        #    ⚠️ 段数算法与旧实现一致（只算非空、非 `.` 的段），
        #       所以既有路径的判定结果**不变**。
        _seg = [s for s in child_path.split("/") if s not in ("", ".")]
        if len(_seg) > self.limits.max_path_depth:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_ARGUMENT,
                message=(f"路径层级过深（{len(_seg)} > "
                         f"{self.limits.max_path_depth}）：{child_path}"),
            )

        # 🔒 目录结构操作共用**父目录的命名空间锁**
        #
        # ⚠️ 锁的 key 用**加锁前**解析出的 resource_id —— 那是安全的，
        #    因为 resource_id 是**稳定身份**，不随 rename / move 变化。
        with self.locks.hold_namespace(workspace_id, parent.resource_id):
            with self.db.transaction(write=True) as conn:
                tree = self.store.load_tree(workspace_id)

                # ============================================================
                # 🔒 **必须在事务内重新读取 parent —— Hardening 抓到的真实 bug**
                #
                #   第一版直接用**加锁之前**解析出的 `parent` 对象。
                #   于是 12 个并发 create 全部拿着同一份陈旧快照：
                #
                #       每个线程都看到 parent.revision == 1
                #       → 每个都算出 new_rev = 2
                #       → 根目录出现 **12 条 revision_number 都是 2** 的行
                #
                #   命名空间锁确实把它们**串行**了 ——
                #   但串行的顺序对了，**读到的数据是旧的**。
                #   这正说明：序列化 ≠ 一致性。
                #
                #   资源级锁只能保证「不会同时进入」，
                #   「进入之后看到最新状态」必须靠**在事务内重读**。
                # ============================================================
                fresh_parent = tree.get(parent.resource_id)
                if fresh_parent is None or fresh_parent.is_deleted:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_NOT_FOUND,
                        "父目录不存在或已被删除",
                        audit=(self._attempt(AuditAction.CREATE), agent_id,
                               parent.resource_id),
                        workspace_id=workspace_id,
                    )
                parent = fresh_parent

                holder = tree.effective_lock(parent)
                if holder != agent_id:
                    return self._fail(
                        op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                        "创建资源需要父目录的 Directory Lock",
                        audit=(self._attempt(AuditAction.CREATE), agent_id,
                               parent.resource_id),
                        workspace_id=workspace_id,
                        detail={"parent": parent.path,
                                "parent_effective_lock": holder,
                                "actor": agent_id},
                        lock_holder=holder,
                    )

                # 🔴 结构上限②：一个目录的**直接**子项数。
                #
                #    🔒 必须在**事务内、锁检查之后**数 —— 锁只保证
                #       「不会同时进入」，「进入后看到最新状态」靠事务内重读
                #       （本项目在 create 里踩过「序列化 ≠ 一致性」的坑）。
                _n_kids = len(tree.children_of(parent.resource_id))
                if _n_kids >= self.limits.max_children_per_directory:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                        (f"父目录子项数已达上限（{_n_kids} >= "
                         f"{self.limits.max_children_per_directory}）："
                         f"{parent.path}"),
                        audit=(self._attempt(AuditAction.CREATE), agent_id,
                               parent.resource_id),
                        workspace_id=workspace_id,
                        detail={"parent": parent.path,
                                "children": _n_kids,
                                "limit":
                                    self.limits.max_children_per_directory},
                    )

                if self.store.find_by_path(workspace_id, child_path) is not None:
                    return self._fail(
                        op_id, ErrorCode.NAME_CONFLICT,
                        f"同名资源已存在：{child_path}",
                        audit=(self._attempt(AuditAction.CREATE), agent_id,
                               parent.resource_id),
                        workspace_id=workspace_id,
                        detail={"path": child_path},
                    )

                created = self.store.create_resource(
                    conn,
                    workspace_id=workspace_id,
                    parent=parent,
                    name=name,
                    resource_type=rtype,
                    path=child_path,
                    created_by=agent_id,
                    content=content if rtype is ResourceType.FILE else None,
                )

                # 父目录结构变了 → 父目录 revision 递增
                # （🔒 走 _bump_revision：改列与写表绑在一起）
                self._bump_revision(
                    conn, parent,
                    change_type="CREATE", actor=agent_id, change_ref=op_id,
                )
                self.store.add_revision(
                    conn,
                    resource_id=created.resource_id,
                    workspace_id=workspace_id,
                    revision_number=1,
                    created_by=agent_id,
                    change_type="CREATE",
                    change_ref=op_id,
                )
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.CREATE.value,
                    actor=agent_id,
                    result=AuditResult.SUCCESS.value,
                    resource_id=created.resource_id,
                    detail={"path": created.path,
                            "parent": parent.path,
                            "type": rtype.value},
                )
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.RESOURCE_CREATED.value,
                    actor=agent_id,
                    resource_id=created.resource_id,
                    revision=1,
                    detail={"path": created.path, "type": rtype.value},
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": created.resource_id,
                        "path": created.path,
                        "revision": 1,
                        "type": rtype.value,
                    },
                )

    # ==========================================================
    # EDIT
    # ==========================================================
    def edit(
        self,
        workspace_id: str,
        path: str,
        base_revision: int,
        content: str,
        agent_id: str,
    ) -> OperationResult:
        """
        前置（任务书 §20）：
            ① Participant
            ② Resource 是 File
            ③ **Effective File Lock == Agent**
            ④ **base_revision == current revision**
            ⑤ 未删除

        🔒 成功时 **File Lock 不变化** —— `EDIT END ≠ LOCK RELEASE`。
        """
        op_id = new_operation_id()

        if not self._participant(workspace_id, agent_id):
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.NOT_TASK_PARTICIPANT,
                message="不是这个 Task Workspace 的 Participant",
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target

        if target.is_directory:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_RESOURCE_TYPE,
                message="edit() 只能作用于文件",
            )

        # 🔒 资源级锁：同一 File 的竞争者在这里排队
        with self.locks.hold(workspace_id, target.resource_id):
            with self.db.transaction(write=True) as conn:
                tree = self.store.load_tree(workspace_id)
                fresh = tree.get(target.resource_id)
                if fresh is None or fresh.is_deleted:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_DELETED,
                        "资源已被删除",
                        audit=(self._attempt(AuditAction.EDIT), agent_id,
                               target.resource_id),
                        workspace_id=workspace_id,
                    )

                holder = tree.effective_lock(fresh)
                if holder != agent_id:
                    return self._fail(
                        op_id, ErrorCode.LOCKED_BY_OTHER_AGENT,
                        "这个文件当前由其他 Agent 负责",
                        audit=(self._attempt(AuditAction.EDIT), agent_id,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                        detail={"current_revision": fresh.revision,
                                "provided_revision": base_revision},
                        lock_holder=holder,
                        current_revision=fresh.revision,
                        provided_revision=base_revision,
                    )

                # 🔒 Revision 检查必须在**同一个事务**里（防 Revision Race）
                if base_revision != fresh.revision:
                    return self._fail(
                        op_id, ErrorCode.STALE_REVISION,
                        "基础版本已过期 —— 请重新 read() 后再改",
                        audit=(self._attempt(AuditAction.EDIT), agent_id,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                        detail={"current_revision": fresh.revision,
                                "provided_revision": base_revision},
                        current_revision=fresh.revision,
                        provided_revision=base_revision,
                    )

                new_rev = fresh.revision + 1
                updated = self.store.update_resource(
                    conn, fresh, content=content, revision=new_rev
                )
                self.store.add_revision(
                    conn,
                    resource_id=fresh.resource_id,
                    workspace_id=workspace_id,
                    revision_number=new_rev,
                    created_by=agent_id,
                    change_type="EDIT",
                    change_ref=op_id,
                )
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.EDIT.value,
                    actor=agent_id,
                    result=AuditResult.SUCCESS.value,
                    resource_id=fresh.resource_id,
                    detail={"from_revision": fresh.revision,
                            "to_revision": new_rev},
                )
                # 🔒 Event **不含文件内容** —— 它只是「发生了变化」
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.RESOURCE_UPDATED.value,
                    actor=agent_id,
                    resource_id=fresh.resource_id,
                    revision=new_rev,
                    detail={"path": updated.path},
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": updated.resource_id,
                        "path": updated.path,
                        "revision": new_rev,
                    },
                )

    # ==========================================================
    # DELETE
    # ==========================================================
    def delete(
        self, workspace_id: str, path: str, agent_id: str
    ) -> OperationResult:
        """
        文件：`Effective File Lock == Agent` **且** 父目录结构责任也在 Agent。
        目录：`Effective Directory Lock == Agent` **且**
              **不能绕过更具体的 Resource Boundary**。

        🔒 删除**不让资源瞬间消失** —— 进入 `DELETED` 状态，
           历史 Revision / Audit / Handoff 仍可查询（任务书 §21）。
        """
        op_id = new_operation_id()

        if not self._participant(workspace_id, agent_id):
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.NOT_TASK_PARTICIPANT,
                message="不是这个 Task Workspace 的 Participant",
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target

        if target.is_root:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                message="不能删除 Workspace 根目录",
            )

        with self.locks.hold_namespace(workspace_id, target.parent_id or "ROOT"):
            with self.db.transaction(write=True) as conn:
                tree = self.store.load_tree(workspace_id)
                fresh = tree.get(target.resource_id)
                if fresh is None or fresh.is_deleted:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_DELETED, "资源已被删除",
                        audit=(self._attempt(AuditAction.DELETE), agent_id,
                               target.resource_id),
                        workspace_id=workspace_id,
                    )

                # ---- ① 自身责任 ----
                holder = tree.effective_lock(fresh)
                if holder != agent_id:
                    return self._fail(
                        op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                        "不负责这个资源",
                        audit=(self._attempt(AuditAction.DELETE), agent_id,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                        lock_holder=holder,
                    )

                # ---- ② 父目录结构责任 ----
                if fresh.parent_id is not None:
                    parent = tree.get(fresh.parent_id)
                    if parent is not None:
                        parent_holder = tree.effective_lock(parent)
                        if parent_holder != agent_id:
                            return self._fail(
                                op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                                "父目录结构不由你负责",
                                audit=(self._attempt(AuditAction.DELETE),
                                       agent_id, fresh.resource_id),
                                workspace_id=workspace_id,
                                lock_holder=parent_holder,
                            )

                # ---- ③ 🔒 不能绕过别人的 Resource Boundary ----
                #
                # 规则统一为：**这个资源及其子树里，不能有任何
                # 不属于 actor 的 Explicit Lock。**
                #
                # （对 delete 而言，`fresh` 自身的 foreign 锁
                #   已经被上面的 `effective_lock == agent` 拦掉了，
                #   所以包含它只是让规则更直白，不改变结果。）
                foreign = tree.foreign_boundaries(fresh, agent_id)
                if foreign:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                        "这个资源里包含其他 Agent 独立负责的部分",
                        audit=(self._attempt(AuditAction.DELETE), agent_id,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                        detail={"foreign_boundaries": foreign},
                        foreign_boundaries=foreign,
                    )

                new_rev = fresh.revision + 1
                # 🔒 软删除：资源**不消失**
                self.store.update_resource(
                    conn, fresh, state=ResourceState.DELETED, revision=new_rev
                )
                # 子树一并标记删除（但记录仍在）
                # 🔒 每个被级联删除的子资源都要有自己的 revision 行 ——
                #    否则它的历史会出现空洞（Hardening 抓到过）。
                for node in tree.descendants(fresh):
                    if node.is_active:
                        self._bump_revision(
                            conn, node,
                            change_type="DELETE", actor=agent_id,
                            change_ref=op_id,
                            state=ResourceState.DELETED,
                        )

                self.store.add_revision(
                    conn,
                    resource_id=fresh.resource_id,
                    workspace_id=workspace_id,
                    revision_number=new_rev,
                    created_by=agent_id,
                    change_type="DELETE",
                    change_ref=op_id,
                )
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.DELETE.value,
                    actor=agent_id,
                    result=AuditResult.SUCCESS.value,
                    resource_id=fresh.resource_id,
                    detail={"path": fresh.path,
                            "type": fresh.resource_type.value},
                )
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.RESOURCE_DELETED.value,
                    actor=agent_id,
                    resource_id=fresh.resource_id,
                    revision=new_rev,
                    detail={"path": fresh.path},
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": fresh.resource_id,
                        "path": fresh.path,
                        "revision": new_rev,
                    },
                )

    # ==========================================================
    # RENAME（同一父目录内改名）
    # ==========================================================
    def rename(
        self, workspace_id: str, path: str, new_name: str, agent_id: str
    ) -> OperationResult:
        op_id = new_operation_id()

        if not self._participant(workspace_id, agent_id):
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.NOT_TASK_PARTICIPANT,
                message="不是这个 Task Workspace 的 Participant",
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target
        if target.is_root:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                message="不能重命名 Workspace 根目录",
            )

        with self.locks.hold_namespace(workspace_id, target.parent_id or "ROOT"):
            with self.db.transaction(write=True) as conn:
                tree = self.store.load_tree(workspace_id)
                fresh = tree.get(target.resource_id)
                if fresh is None or fresh.is_deleted:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_DELETED, "资源已被删除",
                        audit=(self._attempt(AuditAction.RENAME), agent_id,
                               target.resource_id),
                        workspace_id=workspace_id,
                    )

                parent = tree.get(fresh.parent_id) if fresh.parent_id else None

                # ---- ① 父目录结构责任 ----
                if parent is not None:
                    holder = tree.effective_lock(parent)
                    if holder != agent_id:
                        return self._fail(
                            op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                            "rename 需要父目录的 Directory Lock",
                            audit=(self._attempt(AuditAction.RENAME), agent_id,
                                   fresh.resource_id),
                            workspace_id=workspace_id,
                            lock_holder=holder,
                        )

                # ---- ② 不能动别人独立负责的资源 ----
                #
                # ============================================================
                # 🔒 **这一条不能 pop 掉 `fresh` 自身 —— Hardening 抓到的真实 bug**
                # ============================================================
                #
                #   第一版写的是：
                #
                #       foreign = tree.foreign_boundaries(fresh, agent_id)
                #       foreign.pop(fresh.resource_id, None)   ← ❌
                #
                #   那个 `pop` 本意是「自身的边界不算障碍」，
                #   但它把**落在资源自身上的别人的边界**也一起抹掉了。
                #
                #   后果：
                #       A 拥有父目录 `/src`
                #       B 显式拥有 `/src/x.py`（独立责任边界）
                #       → **A 可以把 `/src/x.py` 改名/搬走**
                #
                #   这与任务书明确禁止的
                #   「不让 A 控制目录/B 控制文件时 A 直接删 B 的文件」
                #   是**同一个错误**，只是换了 rename / move 两个入口。
                #
                #   正确规则：**资源和它的整棵子树里，不能有任何
                #   不属于 actor 的 Explicit Lock。**
                # ============================================================
                foreign = tree.foreign_boundaries(fresh, agent_id)
                if foreign:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                        "这个资源里包含其他 Agent 独立负责的部分",
                        audit=(self._attempt(AuditAction.RENAME), agent_id,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                        foreign_boundaries=foreign,
                    )

                parent_path = parent.path if parent else "/"
                try:
                    new_path = join_path(parent_path, new_name)
                except WorkspacePathError as exc:
                    return OperationResult.fail(
                        operation_id=op_id,
                        code=ErrorCode.INVALID_ARGUMENT,
                        message=str(exc),
                    )

                if self.store.find_by_path(workspace_id, new_path) is not None:
                    return self._fail(
                        op_id, ErrorCode.NAME_CONFLICT,
                        f"同名资源已存在：{new_path}",
                        audit=(self._attempt(AuditAction.RENAME), agent_id,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                    )

                old_path = fresh.path
                new_rev = fresh.revision + 1
                # 🔒 resource_id **不变**；path / name 变
                updated = self.store.update_resource(
                    conn, fresh, name=new_name, path=new_path,
                    revision=new_rev,
                )
                # 子树路径要跟着改（重新拼前缀）
                self._rebase_descendant_paths(
                    conn, tree, fresh, old_path, new_path
                )

                if parent is not None:
                    # 🔒 父目录 Namespace 变了 → 也要有 revision 行
                    self._bump_revision(
                        conn, parent,
                        change_type="RENAME", actor=agent_id,
                        change_ref=op_id,
                    )

                self.store.add_revision(
                    conn,
                    resource_id=fresh.resource_id,
                    workspace_id=workspace_id,
                    revision_number=new_rev,
                    created_by=agent_id,
                    change_type="RENAME",
                    change_ref=op_id,
                )
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.RENAME.value,
                    actor=agent_id,
                    result=AuditResult.SUCCESS.value,
                    resource_id=fresh.resource_id,
                    detail={"old_path": old_path, "new_path": new_path},
                )
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.RESOURCE_RENAMED.value,
                    actor=agent_id,
                    resource_id=fresh.resource_id,
                    revision=new_rev,
                    detail={"old_path": old_path, "new_path": new_path},
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": updated.resource_id,
                        "old_path": old_path,
                        "new_path": new_path,
                        "revision": new_rev,
                    },
                )

    # ==========================================================
    # MOVE
    # ==========================================================
    def move(
        self,
        workspace_id: str,
        path: str,
        new_parent_path: str,
        agent_id: str,
    ) -> OperationResult:
        """
        前置：**source parent 与 dest parent 的 Directory Lock 都是 Agent**。

        🔒 移动整棵 Directory Tree 时
           **Controller / File Lock / 子目录 Explicit Lock 全部保持** ——
           不因为 Move 就自动换责任人（任务书 §23）。
        """
        op_id = new_operation_id()

        if not self._participant(workspace_id, agent_id):
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.NOT_TASK_PARTICIPANT,
                message="不是这个 Task Workspace 的 Participant",
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target
        dest = self._resolve_path(workspace_id, new_parent_path, op_id)
        if isinstance(dest, OperationResult):
            return dest

        if target.is_root:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                message="不能移动 Workspace 根目录",
            )
        if dest.is_file:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_PARENT,
                message="目标父资源必须是目录",
            )
        if dest.resource_id == target.resource_id:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_PARENT,
                message="不能把资源移动到它自己里面",
            )

        # 🔒 同时锁 source 与 dest 的命名空间 ——
        #    `hold` 内部会**排序后再获取**，避免相反方向的两个 move 死锁
        with self.locks.hold_namespace(workspace_id, target.parent_id or "ROOT"):
            with self.locks.hold_namespace(workspace_id, dest.resource_id):
                with self.db.transaction(write=True) as conn:
                    tree = self.store.load_tree(workspace_id)
                    fresh = tree.get(target.resource_id)
                    dest_fresh = tree.get(dest.resource_id)

                    if fresh is None or fresh.is_deleted:
                        return self._fail(
                            op_id, ErrorCode.RESOURCE_DELETED, "资源已被删除",
                            audit=(self._attempt(AuditAction.MOVE), agent_id,
                                   target.resource_id),
                            workspace_id=workspace_id,
                        )

                    # ---- ① source parent 责任 ----
                    src_parent = tree.get(fresh.parent_id) if fresh.parent_id else None
                    if src_parent is not None:
                        holder = tree.effective_lock(src_parent)
                        if holder != agent_id:
                            return self._fail(
                                op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                                "move 需要 source 父目录的 Directory Lock",
                                audit=(self._attempt(AuditAction.MOVE),
                                       agent_id, fresh.resource_id),
                                workspace_id=workspace_id,
                                lock_holder=holder,
                            )

                    # ---- ② dest parent 责任 ----
                    dest_holder = tree.effective_lock(dest_fresh)
                    if dest_holder != agent_id:
                        return self._fail(
                            op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                            "move 需要 destination 父目录的 Directory Lock",
                            audit=(self._attempt(AuditAction.MOVE), agent_id,
                                   fresh.resource_id),
                            workspace_id=workspace_id,
                            lock_holder=dest_holder,
                        )

                    # ---- ③ 不能动别人的边界 ----
                    # 🔒 同样**不能** pop 掉 `fresh` 自身（见 rename 的说明）
                    foreign = tree.foreign_boundaries(fresh, agent_id)
                    if foreign:
                        return self._fail(
                            op_id, ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                            "这个资源里包含其他 Agent 独立负责的部分",
                            audit=(self._attempt(AuditAction.MOVE), agent_id,
                                   fresh.resource_id),
                            workspace_id=workspace_id,
                            foreign_boundaries=foreign,
                        )

                    # ---- ④ 不能把目录移进自己的子树 ----
                    if fresh.is_directory:
                        descendant_ids = {
                            d.resource_id for d in tree.descendants(fresh)
                        }
                        if dest_fresh.resource_id in descendant_ids:
                            return OperationResult.fail(
                                operation_id=op_id,
                                code=ErrorCode.INVALID_PARENT,
                                message="不能把目录移动到它自己的子树里",
                            )

                    parent_path = dest_fresh.path
                    try:
                        new_path = join_path(parent_path, fresh.name)
                    except WorkspacePathError as exc:
                        return OperationResult.fail(
                            operation_id=op_id,
                            code=ErrorCode.INVALID_ARGUMENT,
                            message=str(exc),
                        )

                    if self.store.find_by_path(workspace_id, new_path) is not None:
                        return self._fail(
                            op_id, ErrorCode.NAME_CONFLICT,
                            f"目标位置已有同名资源：{new_path}",
                            audit=(self._attempt(AuditAction.MOVE), agent_id,
                                   fresh.resource_id),
                            workspace_id=workspace_id,
                        )

                    old_path = fresh.path
                    new_rev = fresh.revision + 1
                    # 🔒 **只改 parent_id / path / revision ——
                    #    不动任何 Explicit Lock** ⇒
                    #    Controller / File Lock / 子目录显式锁全部保持
                    updated = self.store.update_resource(
                        conn, fresh,
                        parent_id=dest_fresh.resource_id,
                        path=new_path,
                        revision=new_rev,
                    )
                    self._rebase_descendant_paths(
                        conn, tree, fresh, old_path, new_path
                    )

                    # 🔒 source / destination 父目录的 Namespace 都变了 →
                    #    两个都要有 revision 行
                    #
                    # ⚠️ **必须按 resource_id 去重**：
                    #    在同一父目录内 move（`/a/x.py` → `/a`）时
                    #    `src_parent` 与 `dest_fresh` **是同一个资源** ——
                    #    不去重会递增两次、并写出**两条相同 revision_number**
                    #    的行（而 revision 是「这个资源第几次变化」，
                    #    一次操作只该 +1）。
                    seen_parents: set = set()
                    for touched in (src_parent, dest_fresh):
                        if touched is None:
                            continue
                        if touched.resource_id in seen_parents:
                            continue
                        seen_parents.add(touched.resource_id)
                        self._bump_revision(
                            conn, touched,
                            change_type="MOVE", actor=agent_id,
                            change_ref=op_id,
                        )

                    self.store.add_revision(
                        conn,
                        resource_id=fresh.resource_id,
                        workspace_id=workspace_id,
                        revision_number=new_rev,
                        created_by=agent_id,
                        change_type="MOVE",
                        change_ref=op_id,
                    )
                    self.store.add_audit(
                        conn,
                        operation_id=op_id,
                        workspace_id=workspace_id,
                        action=AuditAction.MOVE.value,
                        actor=agent_id,
                        result=AuditResult.SUCCESS.value,
                        resource_id=fresh.resource_id,
                        detail={"old_path": old_path, "new_path": new_path},
                    )
                    self.store.add_event(
                        conn,
                        workspace_id=workspace_id,
                        event_type=WorkspaceEventType.RESOURCE_MOVED.value,
                        actor=agent_id,
                        resource_id=fresh.resource_id,
                        revision=new_rev,
                        detail={"old_path": old_path, "new_path": new_path},
                    )

                    return OperationResult.ok(
                        operation_id=op_id,
                        result={
                            "resource_id": updated.resource_id,
                            "old_path": old_path,
                            "new_path": new_path,
                            "revision": new_rev,
                        },
                    )

    def _rebase_descendant_paths(
        self,
        conn,
        tree: ResourceTree,
        root: Resource,
        old_prefix: str,
        new_prefix: str,
    ) -> None:
        """
        改一个目录/文件的名字或位置后，把子树的 `path` 缓存一起重算。

        ⚠️ 只改 `path` 缓存 —— `resource_id` 与所有 Explicit Lock **不动**。
        """
        for node in tree.descendants(root):
            if node.is_deleted:
                continue
            if not node.path.startswith(old_prefix):
                continue
            rebased = new_prefix + node.path[len(old_prefix):]
            self.store.update_resource(conn, node, path=rebased)

    # ==========================================================
    # HANDOFF
    # ==========================================================
    def handoff(
        self,
        workspace_id: str,
        path: str,
        from_agent: str,
        to_agent: str,
        actor: str,
        *,
        reason: str = "",
        task_id: Optional[str] = None,
    ) -> OperationResult:
        """
        正式的责任交接（任务书 §24）。

        · Directory → **Controller = to 且 Directory Lock = to**
        · File      → **File Lock from → to**

        🔒 **接收方不能直接抢锁** —— 必须由当前责任人 / Controller /
           Human Governance 走这个正式入口。

        🔒 无更具体显式责任的后代**跟随**；**已有的 Explicit Child
           Lock / Controller 不被吞回** —— 这一点由「只改这一条
           显式锁记录」自动保证：子孙的锁记录根本没被碰。
        """
        op_id = new_operation_id()

        # ============================================================
        # 🔒 Hardening：**接收方必须是真实存在且属于这个 Workspace 的 Agent**
        #
        #   第一版没做这个校验 —— 于是
        #       handoff("W1", "/a.py", "A", "NOT_A_REAL_AGENT", "A")
        #   会**成功**，把一个资源的责任交给一个不存在的 Agent。
        #   更糟的是：责任从此挂在一个虚构的 holder 上，
        #   而 Effective Lock 解析会忠实地返回那个名字。
        #   （任务书 §9 Case 5 明确要求这种请求「必须明确失败」。）
        #
        #   校验顺序：先「存在」再「是 Participant」——
        #   因为两者的正确应对不同。
        # ============================================================
        if not self._agent_exists(to_agent):
            return self._fail(
                op_id, ErrorCode.UNKNOWN_AGENT,
                f"接收方 Agent 不存在：{to_agent}",
                audit=(self._attempt(AuditAction.HANDOFF), actor, None),
                workspace_id=workspace_id,
                detail={"to_agent": to_agent},
            )

        if not self._participant(workspace_id, to_agent):
            return self._fail(
                op_id, ErrorCode.NOT_TASK_PARTICIPANT,
                f"接收方 {to_agent} 不是这个 Task Workspace 的 Participant",
                audit=(self._attempt(AuditAction.HANDOFF), actor, None),
                workspace_id=workspace_id,
                detail={"to_agent": to_agent},
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target

        with self.locks.hold(workspace_id, target.resource_id):
            with self.db.transaction(write=True) as conn:
                tree = self.store.load_tree(workspace_id)
                fresh = tree.get(target.resource_id)
                if fresh is None or fresh.is_deleted:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_DELETED, "资源已被删除",
                        audit=(self._attempt(AuditAction.HANDOFF), actor,
                               target.resource_id),
                        workspace_id=workspace_id,
                    )

                old_holder = tree.effective_lock(fresh)
                if old_holder != from_agent:
                    return self._fail(
                        op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                        "from_agent 不是当前责任人",
                        audit=(self._attempt(AuditAction.HANDOFF), actor,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                        lock_holder=old_holder,
                    )

                kind = (LockKind.DIRECTORY if fresh.is_directory
                        else LockKind.FILE)
                self.store.set_explicit_lock(
                    conn,
                    resource_id=fresh.resource_id,
                    holder=to_agent,
                    kind=kind,
                    acquired_by=from_agent,
                )
                # 🔒 **责任变化不产生新的 Revision。**
                #
                #   任务书 §27 把统一生命周期分成三种，其中：
                #
                #       Mutation:
                #           ... → Update State → **Create Revision**
                #                            → Create Audit → Emit Event
                #       Responsibility:
                #           Validate → Change Responsibility
                #                    → Audit → Event       ← **没有 Revision**
                #       Read: 简化
                #
                #   Revision 是**内容/结构**的版本事实。
                #   「谁负责」变了，内容没变 —— 让 revision 递增会让
                #   B 接手后必须先 read() 才知道能改哪个版本，
                #   而那次 read 其实什么新内容都没有。
                #
                #   ⚠️ 这是从 §27 的**流程差异**推出的结论，不是拍脑袋。
                new_rev = fresh.revision          # ← 保持不变
                detail = {
                    "from": from_agent,
                    "to": to_agent,
                    "resource_id": fresh.resource_id,
                    "old_lock": old_holder,
                    "new_lock": to_agent,
                    "reason": reason,
                    "task_id": task_id,
                    "path": fresh.path,
                }
                if fresh.is_directory:
                    detail["old_controller"] = old_holder
                    detail["new_controller"] = to_agent

                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.HANDOFF.value,
                    actor=actor,
                    result=AuditResult.SUCCESS.value,
                    resource_id=fresh.resource_id,
                    detail=detail,
                    task_id=task_id,
                )
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.RESPONSIBILITY_HANDOFF.value,
                    actor=actor,
                    resource_id=fresh.resource_id,
                    revision=new_rev,
                    detail=detail,
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": fresh.resource_id,
                        "path": fresh.path,
                        "revision": new_rev,
                        "old_holder": old_holder,
                        "new_holder": to_agent,
                    },
                )

    # ==========================================================
    # RELEASE
    # ==========================================================
    def release(
        self, workspace_id: str, path: str, agent_id: str
    ) -> OperationResult:
        """
        **Release ≠ Handoff**（任务书 §14）。

        Handoff 是「明确交给 B」；Release 是「我不再保留这个**独立**责任」，
        然后**重新按父级规则解析 Effective Lock**。

        🔒 根的 release 被拒绝 —— 那会让整棵树失去责任源头。
           任务书 §25 明确要求「不能随意自动分配给某个 Agent」，
           而这属于 **Open Question**（Root release 的语义未定）。
        """
        op_id = new_operation_id()

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target

        if target.is_root:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.RESOURCE_BOUNDARY_VIOLATION,
                message=(
                    "Workspace 根目录不能 release —— "
                    "那会让整棵树失去责任源头（Open Question：Root release 语义未定）"
                ),
            )

        with self.locks.hold(workspace_id, target.resource_id):
            with self.db.transaction(write=True) as conn:
                tree = self.store.load_tree(workspace_id)
                fresh = tree.get(target.resource_id)
                if fresh is None or fresh.is_deleted:
                    return self._fail(
                        op_id, ErrorCode.RESOURCE_DELETED, "资源已被删除",
                        audit=(self._attempt(AuditAction.RELEASE), agent_id,
                               target.resource_id),
                        workspace_id=workspace_id,
                    )

                own = tree.explicit_lock_of(fresh.resource_id)
                if own is None or own.holder != agent_id:
                    return self._fail(
                        op_id, ErrorCode.NOT_EFFECTIVE_LOCK_HOLDER,
                        "只有持有该资源**显式**责任的人才能 release",
                        audit=(self._attempt(AuditAction.RELEASE), agent_id,
                               fresh.resource_id),
                        workspace_id=workspace_id,
                        lock_holder=own.holder if own else None,
                    )

                was_holder = own.holder
                self.store.clear_explicit_lock(conn, fresh.resource_id)

                # 🔒 同 handoff：责任变化**不产生新 Revision**（任务书 §27）
                new_rev = fresh.revision

                # 🔒 重新派生 —— 这正是 Release 的意义
                after = self.store.load_tree(workspace_id)
                new_holder = after.effective_lock(
                    after.get(fresh.resource_id)
                )

                detail = {
                    "released_by": was_holder,
                    "resource_id": fresh.resource_id,
                    "path": fresh.path,
                    "effective_lock_after": new_holder,
                }
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.RELEASE.value,
                    actor=agent_id,
                    result=AuditResult.SUCCESS.value,
                    resource_id=fresh.resource_id,
                    detail=detail,
                )
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.RESPONSIBILITY_RELEASED.value,
                    actor=agent_id,
                    resource_id=fresh.resource_id,
                    revision=new_rev,
                    detail=detail,
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": fresh.resource_id,
                        "path": fresh.path,
                        "revision": new_rev,
                        "released_by": was_holder,
                        "effective_lock_after": new_holder,
                    },
                )

    # ==========================================================
    # FORCE UNLOCK（Governance）
    # ==========================================================
    def force_unlock(
        self,
        workspace_id: str,
        path: str,
        actor: str,
        *,
        reason: str,
        authorized: bool = False,
    ) -> OperationResult:
        """
        🔒 **Governance 操作，不是普通工作操作**（任务书 §26 / ADR-0021）。

        ADR-0021 的语义**完整保留**：
            · 高权限（这里用显式的 `authorized=True` 表达）
            · **必须**有明确原因
            · **必须**审计

        当前版本由 **Human** 触发；未来可以让 Governance Agent 承担，
        但第一版**不自动实现**。

        ⚠️ 这里**不实现任何自动触发路径** —— 尤其是
           `Heartbeat Timeout → Auto Unlock`（任务书 §47 明确禁止）。
        """
        op_id = new_operation_id()

        if not authorized:
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.GOVERNANCE_REQUIRED,
                message="force_unlock 是治理操作，需要高权限授权",
            )
        if not reason.strip():
            return OperationResult.fail(
                operation_id=op_id,
                code=ErrorCode.INVALID_ARGUMENT,
                message="force_unlock 必须给出明确原因（ADR-0021）",
            )

        target = self._resolve_path(workspace_id, path, op_id)
        if isinstance(target, OperationResult):
            return target

        with self.locks.hold(workspace_id, target.resource_id):
            with self.db.transaction(write=True) as conn:
                tree = self.store.load_tree(workspace_id)
                fresh = tree.get(target.resource_id)
                own = tree.explicit_lock_of(fresh.resource_id)

                if own is None:
                    return OperationResult.fail(
                        operation_id=op_id,
                        code=ErrorCode.RESOURCE_NOT_FOUND,
                        message="该资源没有显式责任可解锁",
                    )

                previous_owner = own.holder
                self.store.clear_explicit_lock(conn, fresh.resource_id)

                # 🔒 治理操作同样**不产生新 Revision**（任务书 §27）
                new_rev = fresh.revision
                after = self.store.load_tree(workspace_id)
                new_holder = after.effective_lock(after.get(fresh.resource_id))

                detail = {
                    "revoked_by": actor,
                    "previous_owner": previous_owner,
                    "resource_id": fresh.resource_id,
                    "path": fresh.path,
                    "reason": reason,
                    "effective_lock_after": new_holder,
                }
                self.store.add_audit(
                    conn,
                    operation_id=op_id,
                    workspace_id=workspace_id,
                    action=AuditAction.FORCE_UNLOCK.value,
                    actor=actor,
                    result=AuditResult.SUCCESS.value,
                    resource_id=fresh.resource_id,
                    detail=detail,
                )
                self.store.add_event(
                    conn,
                    workspace_id=workspace_id,
                    event_type=WorkspaceEventType.GOVERNANCE_FORCE_UNLOCK.value,
                    actor=actor,
                    resource_id=fresh.resource_id,
                    revision=new_rev,
                    detail=detail,
                )

                return OperationResult.ok(
                    operation_id=op_id,
                    result={
                        "resource_id": fresh.resource_id,
                        "revision": new_rev,
                        "previous_owner": previous_owner,
                        "effective_lock_after": new_holder,
                    },
                )

"""
TaskCreationService —— Agent 自主创建 Root Task
================================================

V1 边界：

- 只创建 **Root Task**（没有 parent/root/depth）；
- participants 由创建者**直接指定**（V1 TEMPORARY）；
- Task.budget **不设置**（那是商业价格；人类预算是 TaskBudget，不是 Price）；
- DoD 写入 TaskSharedContext；
- 验收责任通过 ASSIGN 写入（可选；没有时完成会被 blocked）；
- `workspace_plan` 校验 root/directory/file owner 与 root lock / participants 一致。

🔒 这个服务由**组合根/Demand 层**调用；Task Core 本身不认识 Demand。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.task import Task
from core.task_shared_context import ContextChangeType
from services.assignment_ops import assign_acceptance


class TaskCreationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TaskCreationService:
    def __init__(self, runtime: Any, run: Any, *, max_agents: int = 5):
        self.rt = runtime
        self.run = run
        self.max_agents = int(max_agents)

    # ------------------------------------------------------------
    def create(
        self,
        *,
        creator: str,
        description: Any,
        participants: Any,
        acceptance_criteria: Any,
        producer: Any = None,
        acceptance_assignee: Any = None,
        workspace_plan: Any = None,
    ) -> Dict[str, Any]:
        try:
            values = self._validate(
                creator=creator,
                description=description,
                participants=participants,
                acceptance_criteria=acceptance_criteria,
                producer=producer,
                acceptance_assignee=acceptance_assignee,
                workspace_plan=workspace_plan,
            )
        except TaskCreationError as exc:
            return {"ok": False, "code": exc.code, "message": exc.message}

        if self.run.root_task_id:
            return {
                "ok": False, "code": "task_already_created",
                "message": (f"这个 DemandRun 已经创建了 Task "
                            f"{self.run.root_task_id}；V1 只允许一个 Root Task"),
                "task_id": self.run.root_task_id,
            }

        ws_id = f"WS-DEMAND-{self.run.run_id}"
        ensured = self.rt.workspace_gateway.ensure_workspace(
            ws_id, values["creator"])
        if not ensured.success:
            err = ensured.error or {}
            return {
                "ok": False, "code": "workspace_failed",
                "message": f"创建 Workspace 失败：{err.get('message') or err}",
            }

        task = Task.create(
            creator=values["creator"],
            description=values["description"],
            workspace_id=ws_id,
            participants=values["participants"],
            note=f"demand_run:{self.run.run_id}",
        )
        if values["producer"]:
            task.claim_producer(values["producer"])

        try:
            self.rt.create_task(task)
        except Exception as exc:                              # noqa: BLE001
            return {
                "ok": False, "code": "task_create_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }

        try:
            self.rt.budget_service.set_budget(
                task.task_id, float(self.run.budget_aic))
        except Exception:                                     # noqa: BLE001
            # 预算是观测，不是 Task 字段；失败不阻塞任务，但会记录。
            pass

        try:
            self._write_shared_context(
                task=task,
                creator=values["creator"],
                description=values["description"],
                criteria=values["criteria"],
            )
        except Exception as exc:                              # noqa: BLE001
            return {
                "ok": False, "code": "dod_failed",
                "message": f"任务已创建，但写 DoD 失败：{type(exc).__name__}: {exc}",
                "task_id": task.task_id,
                "workspace_id": ws_id,
            }

        # 🔒 把 Workspace 根目录的 Directory Lock 交给 producer。
        #    否则 producer 能读、不能写（创建资源需要父目录 Directory Lock）。
        #    这是 V1 直接指定 producer 的一部分；不是权限模型扩张。
        if values["producer"] != values["creator"]:
            moved = self.rt.workspace_gateway.handoff(
                ws_id, "/",
                from_agent=values["creator"],
                to_agent=values["producer"],
                actor=values["creator"],
                reason="Demand 创建时把生产根目录交给 producer（V1）",
                task_id=task.task_id,
            )
            if not moved.success:
                err = moved.error or {}
                return {
                    "ok": False, "code": "workspace_handoff_failed",
                    "message": (f"任务已创建，但根目录移交给 producer 失败："
                                f"{err.get('message') or err}"),
                    "task_id": task.task_id,
                    "workspace_id": ws_id,
                }

        if values["acceptor"]:
            assigned = assign_acceptance(
                self.rt.alignment,
                task.task_id,
                by=values["creator"],
                to=values["acceptor"],
                reason="Demand 创建时指派验收责任（V1 直接指定）",
                is_participant=lambda tid, aid: (
                    tid == task.task_id and aid in values["participants"]),
            )
            if not assigned.get("ok"):
                return {
                    "ok": False, "code": "acceptance_assign_failed",
                    "message": str(assigned.get("message") or "指派验收责任失败"),
                    "task_id": task.task_id,
                    "workspace_id": ws_id,
                }

        self.run.workspace_id = ws_id
        self.run.root_task_id = task.task_id
        self.run.workspace_plan = values["workspace_plan"]
        self.run.status = "RUNNING"
        return {
            "ok": True,
            "task_id": task.task_id,
            "workspace_id": ws_id,
            "description": task.description,
            "participants": list(task.participants),
            "producer": task.contractor,
            "acceptance_assignee": values["acceptor"],
            "workspace_plan": values["workspace_plan"],
            "status": task.status,
        }

    # ------------------------------------------------------------
    def _validate(
        self,
        *,
        creator: str,
        description: Any,
        participants: Any,
        acceptance_criteria: Any,
        producer: Any,
        acceptance_assignee: Any,
        workspace_plan: Any = None,
    ) -> Dict[str, Any]:
        creator = str(creator or "").strip()
        if not creator:
            raise TaskCreationError("bad_creator", "creator 不能为空")
        if not self._agent_entry(creator):
            raise TaskCreationError(
                "bad_creator", f"creator {creator!r} 不在 Agent Directory 里")

        text = str(description or "").strip()
        if not text:
            raise TaskCreationError("bad_description", "description 不能为空")

        if not isinstance(participants, (list, tuple)):
            raise TaskCreationError(
                "bad_participants", "participants 必须是字符串列表")
        people: List[str] = []
        for raw in participants:
            aid = str(raw or "").strip()
            if not aid:
                raise TaskCreationError(
                    "bad_participants", "participants 不能包含空字符串")
            if aid not in people:
                people.append(aid)
        if creator not in people:
            raise TaskCreationError(
                "bad_participants", "participants 必须包含 creator 自己")
        if len(people) < 2:
            raise TaskCreationError(
                "bad_participants",
                "至少需要 2 个参与者（生产者 != 验收人；不能自己验自己）")
        if len(people) > self.max_agents:
            raise TaskCreationError(
                "too_many_agents",
                f"参与者 {len(people)} 个，超过上限 {self.max_agents}")
        for aid in people:
            entry = self._agent_entry(aid)
            if entry is None:
                raise TaskCreationError(
                    "unknown_agent", f"{aid!r} 不在 Agent Directory 里")
            if not entry.available:
                raise TaskCreationError(
                    "unavailable_agent", f"{aid!r} 当前不可用")

        criteria: List[str] = []
        if not isinstance(acceptance_criteria, (list, tuple)):
            raise TaskCreationError(
                "no_acceptance_criteria",
                "acceptance_criteria 必须是非空字符串列表（Definition of Done）")
        for raw in acceptance_criteria:
            item = str(raw or "").strip()
            if item and item not in criteria:
                criteria.append(item)
        if not criteria:
            raise TaskCreationError(
                "no_acceptance_criteria",
                "至少需要一条 acceptance_criteria（否则完成会被 blocked）")
        if len(criteria) > 20:
            raise TaskCreationError(
                "too_many_criteria", "acceptance_criteria 最多 20 条")

        prod = str(producer or "").strip() or None
        if not prod:
            raise TaskCreationError(
                "bad_producer",
                "producer 必填：必须是 participants 之一，且负责实际生产")
        if prod not in people:
            raise TaskCreationError(
                "bad_producer", "producer 必须是 participants 之一")

        acceptor = str(acceptance_assignee or "").strip() or None
        if acceptor:
            if acceptor not in people:
                raise TaskCreationError(
                    "bad_acceptor", "acceptance_assignee 必须是 participants 之一")
            if acceptor == creator:
                raise TaskCreationError(
                    "bad_acceptor",
                    "不能把验收责任派给自己（assign_acceptance 的既有规则）")
            if prod and acceptor == prod:
                raise TaskCreationError(
                    "acceptor_is_producer",
                    "验收人不能是 producer（生产者不得自验）")

        plan = self._validate_workspace_plan(
            workspace_plan, people=people, producer=prod)

        return {
            "creator": creator,
            "description": text,
            "participants": people,
            "criteria": criteria,
            "producer": prod,
            "acceptor": acceptor,
            "workspace_plan": plan,
        }

    def _validate_workspace_plan(
        self, plan: Any, *, people: List[str], producer: str,
    ) -> Dict[str, Any]:
        """
        校验 Creator 的责任计划与 V1 的 root lock / participant 边界一致。

        🔒 这里不创建目录、不 handoff；只拒绝**结构上不可执行**的计划。
        """
        if not isinstance(plan, dict):
            raise TaskCreationError(
                "missing_workspace_plan",
                "task_create 必须提供 workspace_plan（root/directory/file owner）")

        root_owner = str(plan.get("root_owner") or "").strip()
        if not root_owner:
            raise TaskCreationError(
                "bad_workspace_plan", "workspace_plan.root_owner 不能为空")
        if root_owner != producer:
            raise TaskCreationError(
                "bad_workspace_plan",
                (f"workspace_plan.root_owner={root_owner!r} 必须等于 "
                 f"producer={producer!r}（V1 root lock 交给 producer）"))
        if root_owner not in people:
            raise TaskCreationError(
                "bad_workspace_plan",
                f"workspace_plan.root_owner={root_owner!r} 不是 participants")

        dirs_raw = plan.get("directories") or {}
        files_raw = plan.get("files") or {}
        if not isinstance(dirs_raw, dict) or not isinstance(files_raw, dict):
            raise TaskCreationError(
                "bad_workspace_plan",
                "workspace_plan.directories / files 必须是 path → owner 的对象")
        if not dirs_raw and not files_raw:
            raise TaskCreationError(
                "empty_workspace_plan",
                "workspace_plan 至少需要声明一个目录或文件")

        declared_dirs: Dict[str, str] = {}
        for raw_path, raw_owner in dirs_raw.items():
            path = self._normalize_plan_path(raw_path)
            if not path:
                raise TaskCreationError(
                    "bad_workspace_plan", "workspace_plan 目录路径不能为空")
            if path in declared_dirs:
                raise TaskCreationError(
                    "bad_workspace_plan",
                    f"workspace_plan 目录重复（规范化后）：{path}")
            owner = str(raw_owner or "").strip()
            if owner not in people:
                raise TaskCreationError(
                    "bad_workspace_plan",
                    f"目录 {path!r} 的 owner={owner!r} 不是 participants")
            declared_dirs[path] = owner

        for path in declared_dirs:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent and parent not in declared_dirs:
                raise TaskCreationError(
                    "bad_workspace_plan",
                    (f"目录 {path!r} 的父目录 {parent!r} "
                     "没有在 workspace_plan.directories 里声明"))

        normalized_files: Dict[str, str] = {}
        for raw_path, raw_owner in files_raw.items():
            path = self._normalize_plan_path(raw_path)
            if not path:
                raise TaskCreationError(
                    "bad_workspace_plan", "workspace_plan 文件路径不能为空")
            if path in declared_dirs or path in normalized_files:
                raise TaskCreationError(
                    "bad_workspace_plan",
                    f"workspace_plan 资源重复（规范化后）：{path}")
            owner = str(raw_owner or "").strip()
            if owner not in people:
                raise TaskCreationError(
                    "bad_workspace_plan",
                    f"文件 {path!r} 的 owner={owner!r} 不是 participants")
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent:
                if parent not in declared_dirs:
                    raise TaskCreationError(
                        "bad_workspace_plan",
                        (f"文件 {path!r} 的父目录 {parent!r} "
                         "没有在 workspace_plan.directories 里声明"))
            elif owner != root_owner:
                raise TaskCreationError(
                    "bad_workspace_plan",
                    (f"根级文件 {path!r} 的 owner={owner!r} 必须是 "
                     f"root_owner={root_owner!r}。"
                     "如果希望别人写根级文件，应由 root_owner 创建后 handoff，"
                     "或把 producer 改成该 Agent。"))
            normalized_files[path] = owner

        return {
            "root_owner": root_owner,
            "directories": dict(sorted(declared_dirs.items())),
            "files": dict(sorted(normalized_files.items())),
        }

    @staticmethod
    def _normalize_plan_path(raw: Any) -> str:
        text = str(raw or "").strip().replace("\\", "/").strip("/")
        parts: List[str] = []
        for segment in text.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                raise TaskCreationError(
                    "bad_workspace_plan", "workspace_plan 路径不能包含 '..'")
            parts.append(segment)
        return "/".join(parts)

    def _agent_entry(self, agent_id: str) -> Any:
        try:
            return self.rt.directory.get(agent_id)
        except Exception:                                     # noqa: BLE001
            return None

    # ------------------------------------------------------------
    def _write_shared_context(
        self, *, task: Task, creator: str, description: str,
        criteria: List[str],
    ) -> None:
        def propose_accept(change_type: str, value: Any, reason: str) -> None:
            current = self.rt.alignment.require_current(task.task_id)
            change = self.rt.alignment.propose(
                task.task_id,
                proposed_by=creator,
                change_type=change_type,
                new_value=value,
                expected_revision=current.revision,
                reason=reason,
            )
            self.rt.alignment.accept(
                task.task_id, change.change_id, decided_by=creator,
                reason="声明类变更，规则允许创建者自批")

        propose_accept("SET_OBJECTIVE", description, "写入 Demand 目标")
        for criterion in criteria:
            propose_accept(
                ContextChangeType.ADD_ACCEPTANCE_CRITERION, criterion,
                "Demand 创建时写入 Definition of Done（V1）")

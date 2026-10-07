"""
Task Workspace 的真实共享生产资料 —— **接线**，不是重设计
============================================================

【这个模块解决的真实问题】

    在它之前，这条多 Agent 生产线把 Agent 的产出**只留在 Python 内存里**：
    每轮 `generate()` 返回一段文本，打进 JSON 日志，然后丢掉。
    `Checkpoint.revision` 用的是实验器自己编的标签 `rev-session-1` ——
    **它不是 Workspace 的 revision，它只是实验器写的一个字符串。**

    所以那时我们其实**没有**证明「共享生产资料真的进了 Workspace」，
    也没有证明「revision 会因内容变化而变化」。

【它不做的事】

    🔒 不实现 Revision 系统 —— 复用 `WorkspaceGateway` + `WorkspaceStore`
    🔒 不实现权限 / ACL    —— 复用 Gateway 的 Participant 判定与 Lock 模型
    🔒 不把文件名写进架构 —— 文件清单是**本实验**的，不是平台规则

【为什么每个文件交给一个「责任人」（handoff）】

    Workspace Core v1 的写入前置条件是
    **Effective File Lock == Agent**（任务书 §20）。

    所以「5 个 Agent 都能往同一个 Workspace 写」不会自然成立 ——
    必须有人把每个文件的**正式责任**交给它。

    这里做的是：Workspace 根 Controller 创建 5 个文件，
    然后 `handoff` 给各自的自然责任人。

    ⚠️ 这不是 Workflow：
        · 它规定的是「谁**负责**这个文件」，不是「谁**什么时候**写」
        · 读取对全部 Participant 开放（无 READ ACL）
        · 一个 Agent 想改**别人负责**的文件 → 只能去沟通，
          由对方决定改不改 —— 于是协作是**被结构逼出来的**，不是被编排出来的
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

#: 本实验的共享生产资料清单 —— **文件名只属于这个实验**
#:
#: 🔒 这里定义的是「谁负责哪个文件」，**不是**「按什么顺序产出」。
#:    一个 Agent 完全可以一轮都不写自己的文件；
#:    也可以主动去联系别人要求改别人的文件。
ARTIFACTS: Dict[str, str] = {
    "world_bible.md": "novel_world_builder",
    "plot_outline.md": "novel_plot_architect",
    "manuscript.md": "novel_author",
    "editor_notes.md": "novel_editor",
    "reader_report.md": "novel_reader",
}

#: 每个文件在 Prompt 里最多展示多少字符（共享事实要看得见，但不能吃掉整个上下文）
EXCERPT_CHARS = 1200


def artifact_owner(path: str) -> Optional[str]:
    """这个文件由谁负责。不是清单里的文件返回 None。"""
    return ARTIFACTS.get(str(path or "").strip().lstrip("/"))


def provision(gateway: Any, workspace_id: str, owner: str,
              *, agent_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    把 5 份共享生产资料**真正**建进 Task Workspace，并交接给责任人。

    幂等：已经存在的文件不重复创建（但会补做一次 handoff 尝试）。

    返回每个文件的实际结果（含 **真实 revision**）。
    注意 revision 来自 Gateway 的返回，**不是**这里编的。
    """
    out: List[Dict[str, Any]] = []
    for path, agent_id in ARTIFACTS.items():
        if agent_ids is not None and agent_id not in agent_ids:
            continue

        # ---- ① 创建（需要父目录 `/` 的 Directory Lock）----
        created = gateway.create(
            workspace_id, "/", path, "file", owner,
            content=f"# {path}\n\n（尚无内容）\n",
        )

        if created.success:
            rev = int((created.result or {}).get("revision", 0))
            status = "created"
        elif str((created.error or {}).get("code", "")) == "NAME_CONFLICT":
            # 已存在 —— 幂等路径，读回**真实**当前 revision
            got = gateway.read(workspace_id, f"/{path}", owner)
            rev = int((got.result or {}).get("revision", 0)) if got.success else 0
            status = "exists"
        else:
            out.append({"path": path, "owner": agent_id, "ok": False,
                        "status": "create_failed",
                        "error": dict(created.error or {})})
            continue

        # ---- ② 正式交接：把文件的**责任**交给它的自然责任人 ----
        #
        #     🔒 `handoff` 是 Workspace Core 里**已有的**正式入口。
        #        接收方不能自己抢锁（任务书 §24）—— 所以这里由
        #        当前责任人（root Controller）发起。
        if agent_id == owner:
            # 责任人就是创建者 → 责任已经在它手上，不需要（也不应该）交接
            moved_ok, moved_err = True, None
        else:
            moved = gateway.handoff(
                workspace_id, f"/{path}", from_agent=owner, to_agent=agent_id,
                actor=owner, reason="initial responsibility assignment",
            )
            moved_ok, moved_err = bool(moved.success), (
                None if moved.success else dict(moved.error or {}))
            if moved.success:
                rev = int((moved.result or {}).get("revision", rev))

        out.append({
            "path": path, "owner": agent_id, "ok": True, "status": status,
            "revision": rev,
            "handoff_ok": moved_ok,
            "handoff_error": moved_err,
        })
    return out


def snapshot(gateway: Any, workspace_id: str, reader: str) -> Dict[str, Any]:
    """
    读取 Workspace 的**真实** revision 快照。

    🔒 这是 `Checkpoint.revision` 唯一合法的来源。
       它形如 `ws:<workspace_id>@<hash>`，其中 hash 由每个文件的
       **真实 revision 号**算出 —— 内容一变，revision 变，hash 就变。
    """
    files: Dict[str, int] = {}
    for path in ARTIFACTS:
        got = gateway.read(workspace_id, f"/{path}", reader)
        if got.success:
            files[path] = int((got.result or {}).get("revision", 0))
    return {
        "workspace_id": workspace_id,
        "files": files,
        "revision": fingerprint(workspace_id, files),
        "sum": sum(files.values()),
    }


def fingerprint(workspace_id: str, files: Dict[str, int]) -> str:
    """
    把「每个文件的真实 revision 号」压成一个**可比较的 checkpoint revision**。

    🔒 刻意用确定性摘要而不是随机 ID：
        · 内容没变 → revision 不变（同一个 hash）
        · 内容变了 → revision 一定变
    这正是 §六 要求的「真实 Workspace Revision」。
    """
    import hashlib
    payload = json.dumps(sorted(files.items()), ensure_ascii=False)
    digest = hashlib.sha256(
        f"{workspace_id}|{payload}".encode("utf-8")).hexdigest()[:16]
    return f"ws:{workspace_id}@{digest}"


def render(gateway: Any, workspace_id: str, reader: str,
           *, excerpt_chars: int = EXCERPT_CHARS) -> str:
    """
    把共享 Workspace 渲染成 Cognitive Context 里的一段**状态**。

    🔒 语义纪律（与 `stage/context.py` 同一条）：
        这是「共享生产资料现在是什么样」——
        **不是**「你现在应该去改哪个文件」。
    """
    lines: List[str] = []
    for path in ARTIFACTS:
        got = gateway.read(workspace_id, f"/{path}", reader)
        if not got.success:
            lines.append(f"- {path}: (不可读)")
            continue
        res = got.result or {}
        body = str(res.get("content") or "").strip()
        if len(body) > excerpt_chars:
            body = body[:excerpt_chars] + "…（截断）"
        lines.append(
            f"- {path}  [revision={res.get('revision')}] "
            f"负责人={ARTIFACTS[path]}")
        if body:
            lines.append("  ```")
            for ln in body.splitlines():
                lines.append(f"  {ln}")
            lines.append("  ```")
    return "\n".join(lines)


def render_index(gateway: Any, workspace_id: str, reader: str) -> str:
    """只要 revision，不要正文（用于 Checkpoint 报告）。"""
    snap = snapshot(gateway, workspace_id, reader)
    body = ", ".join(f"{p}=R{r}" for p, r in sorted(snap["files"].items()))
    return f"{snap['revision']}  ({body})"


def apply_write(gateway: Any, workspace_id: str, agent_id: str,
                path: str, content: str,
                *, allow_empty: bool = False) -> Dict[str, Any]:
    """
    让 Agent 的产出**真正落进共享 Workspace**。

    用 `edit` 而不是 `create`：文件已由 `provision()` 建好，
    `edit` 会校验 `base_revision`（乐观锁）并把 revision +1。

    🔒 `base_revision` 每次**现读**，不缓存 ——
       缓存会让并发写入静默失败（STALE_REVISION）。

    🔴 **空内容默认拒绝（实测事故）**

        真实实验 `delegation_experiment --run 3`：Provider 的 LLM 调用
        返回 `EMPTY_CONTENT`（推理模型把 4096 token 全用在 reasoning 上），
        而这里**照样写入**，把 `editor_notes.md` 从有内容覆盖成 **0 字节**，
        并且 revision 从 2 涨到 3 —— 一次**看起来成功**的破坏性写入。

        这正是本项目反复栽的「接口成功掩盖语义 no-op」，
        只不过这次它**真的毁掉了共享生产资料**。

        所以默认拒绝空内容。要清空文件必须显式 `allow_empty=True` ——
        让「我确实想清空」成为一个**必须说出来**的决定。
    """
    norm = str(path or "").strip().lstrip("/")
    if not str(content or "").strip() and not allow_empty:
        return {"ok": False, "path": norm, "stage": "guard",
                "error": {"code": "EMPTY_CONTENT_NOT_WRITTEN",
                          "message": ("内容为空，拒绝写入共享 Workspace —— "
                                      "空产出不得覆盖已有的共享生产资料。"
                                      "确实要清空请显式 allow_empty=True。")}}
    got = gateway.read(workspace_id, f"/{norm}", agent_id)
    if not got.success:
        return {"ok": False, "path": norm,
                "error": dict(got.error or {}), "stage": "read"}
    base = int((got.result or {}).get("revision", 0))

    res = gateway.edit(workspace_id, f"/{norm}", base, content, agent_id)
    if not res.success:
        return {"ok": False, "path": norm, "base_revision": base,
                "error": dict(res.error or {}), "stage": "edit"}
    return {"ok": True, "path": norm, "base_revision": base,
            "revision": int((res.result or {}).get("revision", base + 1)),
            "changed": True}

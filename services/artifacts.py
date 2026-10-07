"""
Artifact Write Modes —— CREATE / REPLACE / APPEND
========================================================

【🔴 它解决的真实问题】

    `shared_workspace.apply_write()` 原先只有 **REPLACE** 语义。
    结果（Session 6 实测）：`manuscript.md` 的 revision 涨到 **R29**，
    字数却始终停在 **2,000～3,500** —— 因为 Agent 每轮都在
    「整体重写」而不是「往下写」。

    **按那个协议，10,000 字永远不可能达成。**

【🔒 它不是什么】

    ❌ 不是 WorkflowEngine / ArtifactPipeline / ChapterManager
    ❌ 不是经济行为 —— CREATE / REPLACE / APPEND **不是三种交易**
    ❌ 不猜 Agent 的意图：**write_mode 必须由 Agent 显式给出**
       （不许根据 path / revision / content 长度去推断）
    ❌ 不自动把所有写入变成 APPEND

【它是什么】

    一层**很薄**的语义层，把「这次写入是什么意思」翻译成
    Workspace Gateway 的既有调用：

        CREATE   → gateway.create(...)              文件不存在时
        REPLACE  → gateway.edit(base_revision, new) 整体替换
        APPEND   → gateway.edit(base_revision, old + new)

    🔒 三种模式都**只**经过 Workspace Gateway ——
       Path Validation / Authorization / Lock / Revision 全部照旧。
       它**没有**、也不该有主机文件系统权限。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: 显式写入模式（§三：Runtime 不猜）。
MODE_CREATE = "create"
MODE_REPLACE = "replace"
MODE_APPEND = "append"

VALID_WRITE_MODES = (MODE_CREATE, MODE_REPLACE, MODE_APPEND)

#: APPEND 时插在旧内容与新内容之间的分隔符。
#:
#: 🔒 用两个换行（Markdown 段落分隔），**不是**为了美观 ——
#:    是为了让「旧内容完整保留」这件事在文本层面**可验证**：
#:    追加后 `new_content.startswith(old_content)` 必须成立。
APPEND_SEPARATOR = "\n\n"


class ArtifactError(ValueError):
    """Artifact 写入的语义错误（不是 Gateway 的权限/锁错误）。"""


def _fail(path: str, code: str, message: str, **extra: Any) -> Dict[str, Any]:
    out = {"ok": False, "path": path, "code": code, "message": message}
    out.update(extra)
    return out


def validate_artifact_path(path: Any) -> Optional[str]:
    """
    检查一个**逻辑** Artifact 路径是否合法。

    返回 None 表示合法；否则返回拒绝理由。

    🔒 §九：这里**不扩张** Path Policy —— 只做「这是不是一个
       合法的 Workspace 逻辑路径」这一件事。

    必须拒绝：
        · 空 / 非字符串
        · **绝对路径**（`/Users/...`、`C:\\...`、任何以 `/` 开头的宿主路径形状）
        · `..` 逃逸
        · `~` 展开
        · 反斜杠（Windows 宿主路径形状）
        · 控制字符

    ⚠️ 注意：Workspace 的**逻辑**路径本来就可以有 `/`（如 `world/characters.md`），
       所以「有斜杠」不是问题；**以斜杠开头**才是。
       Agent 永远看不到宿主路径（任务书 §8）。
    """
    if not isinstance(path, str):
        return "path 必须是字符串"
    raw = path.strip()
    if not raw:
        return "path 不能为空"
    if raw.startswith(("/", "\\", "~")):
        return ("不允许绝对路径 / 家目录路径 —— "
                "Artifact 路径是 Workspace 的**逻辑**路径，"
                "不是宿主机路径")
    if "\\" in raw:
        return "不允许反斜杠（那是宿主机路径形状）"
    # Windows 盘符形状（C:/...、D:\...）
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        return "不允许盘符路径（那是宿主机路径形状）"
    if any(ord(c) < 32 for c in raw):
        return "path 含控制字符"
    segments = [s for s in raw.split("/") if s not in ("", ".")]
    if not segments:
        return "path 规范化后为空"
    if any(s == ".." for s in segments):
        return "path 不允许 '..'（Workspace 是封闭命名空间）"
    if len(segments) > 8:
        return "path 层级过深（>8）"
    return None


def write_artifact(
    gateway: Any,
    workspace_id: str,
    agent_id: str,
    path: str,
    content: str,
    *,
    write_mode: str = MODE_REPLACE,
    allow_empty: bool = False,
) -> Dict[str, Any]:
    """
    按**显式** `write_mode` 写一个 Artifact。

    返回统一结构：
        成功 → {"ok": True, "mode": ..., "path": ..., "revision": ...,
                "previous_revision": ..., "chars": ..., "appended_chars": ...}
        失败 → {"ok": False, "path": ..., "code": ..., "message": ...}

    🔒 失败原因**分类明确**（`code`），不做模糊的「失败了」：
        bad_path / bad_mode / empty_content / already_exists /
        not_found / locked_by_other / not_participant / unknown
    """
    mode = str(write_mode or "").strip().lower()
    if mode not in VALID_WRITE_MODES:
        return _fail(_norm(path), "bad_mode",
                     f"未知 write_mode {write_mode!r}；"
                     f"合法取值：{list(VALID_WRITE_MODES)}")

    path_err = validate_artifact_path(path)
    if path_err:
        return _fail(_norm(path), "bad_path", path_err)

    target = _norm(path)
    body = content if isinstance(content, str) else str(content or "")

    # ---- §四：EMPTY_CONTENT 对三种模式一律默认拒绝 ----
    if not body.strip() and not allow_empty:
        return _fail(target, "empty_content",
                     "内容为空，拒绝写入 —— 空产出不得创建/覆盖/追加 Artifact。"
                     "确实要清空请显式 allow_empty=True。")

    if mode == MODE_CREATE:
        return _create(gateway, workspace_id, agent_id, target, body)
    if mode == MODE_APPEND:
        return _append(gateway, workspace_id, agent_id, target, body)
    return _replace(gateway, workspace_id, agent_id, target, body)


# ================================================================
def _read(gateway: Any, workspace_id: str, agent_id: str,
          path: str) -> Dict[str, Any]:
    got = gateway.read(workspace_id, "/" + path, agent_id)
    if not got.success:
        return {"ok": False, "error": dict(got.error or {})}
    return {"ok": True, "result": dict(got.result or {})}


def _ensure_parents(gateway: Any, workspace_id: str, agent_id: str,
                    path: str) -> Optional[Dict[str, Any]]:
    """
    按需创建缺失的**中间目录**（逐层，全部经过 Gateway）。

    返回 None 表示父目录链已就绪；否则返回一个失败结果。

    【🔴 为什么需要它（真实 smoke 发现）】

        实测：Agent 自主发明了 `novel/chapters/chapter-01-opening.md`，
        但 `novel/chapters/` 不存在 → `not_found`。

        §七 的示例（`/manuscript/chapter_01.md`）只有一层，
        但 Agent **很自然会发明更深的结构**。
        只支持一层 = 动态创建事实上不可用。

    🔒 逐层 `gateway.create(..., "directory", ...)` ——
       权限、锁、revision 语义**完全照旧**：
       建不了目录就说明这个 Agent 在那层没有 Directory Lock，
       此时如实返回 `locked_by_other`，**不绕过**。
    """
    parts = [p for p in path.split("/")[:-1] if p]
    parent_path = "/"
    for seg in parts:
        child_path = parent_path.rstrip("/") + "/" + seg

        # 🔴 **先看它是否已经存在**，再决定要不要建。
        #
        #    【为什么不能靠「create 返回 NAME_CONFLICT」来判断】
        #    实测：`gateway.create` 的检查顺序是
        #        participant → resolve parent → **父目录 Lock** → 同名冲突
        #    所以当一个目录**已经存在**、而调用者不持有**它的父目录**锁时，
        #    create 会在 Lock 那一步就失败，**根本走不到**同名冲突 ——
        #    于是「已存在」被误读成「无权限」。
        #    这不是 Gateway 的 bug（它对「新建」而言判断是对的），
        #    而是**本层不该用 create 去探测存在性**。
        existing = gateway.read(workspace_id, child_path, agent_id)
        if existing.success:
            parent_path = child_path
            continue

        res = gateway.create(workspace_id, parent_path, seg, "directory",
                            agent_id)
        if res.success:
            parent_path = child_path
            continue
        code = str((res.error or {}).get("code") or "")
        if code == "NAME_CONFLICT":
            parent_path = child_path
            continue
        return _fail(path, _map_code(code),
                     (res.error or {}).get("message") or "父目录创建失败",
                     gateway_code=code)
    return None


def _create(gateway: Any, workspace_id: str, agent_id: str,
            path: str, body: str) -> Dict[str, Any]:
    """CREATE：只在**不存在**时创建（缺失的中间目录会被逐层建出来）。"""
    blocked = _ensure_parents(gateway, workspace_id, agent_id, path)
    if blocked is not None:
        return blocked

    parent, _, name = path.rpartition("/")
    parent_path = "/" + parent if parent else "/"

    res = gateway.create(workspace_id, parent_path, name, "file", agent_id,
                         content=body)
    if res.success:
        r = dict(res.result or {})
        return {"ok": True, "mode": MODE_CREATE, "path": path,
                "revision": r.get("revision"), "previous_revision": None,
                "chars": len(body)}
    err = dict(res.error or {})
    code = str(err.get("code") or "")
    if code == "NAME_CONFLICT":
        # 🔒 CREATE 撞已有文件 → **拒绝**，绝不悄悄降级成 REPLACE
        return _fail(path, "already_exists",
                     f"{path} 已存在 —— CREATE 不会覆盖它。"
                     f"想替换请显式用 write_mode=replace。",
                     gateway_code=code)
    return _fail(path, _map_code(code), err.get("message") or "创建失败",
                 gateway_code=code)


def _replace(gateway: Any, workspace_id: str, agent_id: str,
             path: str, body: str) -> Dict[str, Any]:
    """REPLACE：整体替换**已存在**的 Artifact。"""
    cur = _read(gateway, workspace_id, agent_id, path)
    if not cur["ok"]:
        code = str(cur["error"].get("code") or "")
        return _fail(path, "not_found" if "NOT_FOUND" in code
                     else _map_code(code),
                     cur["error"].get("message") or "读取失败",
                     gateway_code=code)
    base = int(cur["result"].get("revision") or 0)

    res = gateway.edit(workspace_id, "/" + path, base, body, agent_id)
    if res.success:
        return {"ok": True, "mode": MODE_REPLACE, "path": path,
                "revision": int((res.result or {}).get("revision", base + 1)),
                "previous_revision": base,
                "previous_chars": len(cur["result"].get("content") or ""),
                "chars": len(body)}
    err = dict(res.error or {})
    return _fail(path, _map_code(str(err.get("code") or "")),
                 err.get("message") or "替换失败",
                 gateway_code=err.get("code"))


def _append(gateway: Any, workspace_id: str, agent_id: str,
            path: str, body: str) -> Dict[str, Any]:
    """
    APPEND：旧内容**完整保留**，新内容接在后面。

    🔒 关键不变式：追加后 `new_content.startswith(old_content)`。
       测试直接断言这一点，而不是只比长度。
    """
    cur = _read(gateway, workspace_id, agent_id, path)
    if not cur["ok"]:
        code = str(cur["error"].get("code") or "")
        return _fail(path, "not_found" if "NOT_FOUND" in code
                     else _map_code(code),
                     f"{path} 不存在 —— APPEND 需要已有 Artifact。"
                     f"第一次写请用 write_mode=create。",
                     gateway_code=code)

    old = cur["result"].get("content") or ""
    base = int(cur["result"].get("revision") or 0)

    # 已有内容为空时不要凭空多出分隔符
    merged = (old + APPEND_SEPARATOR + body) if old.strip() else body

    res = gateway.edit(workspace_id, "/" + path, base, merged, agent_id)
    if res.success:
        return {"ok": True, "mode": MODE_APPEND, "path": path,
                "revision": int((res.result or {}).get("revision", base + 1)),
                "previous_revision": base,
                "previous_chars": len(old), "appended_chars": len(body),
                "chars": len(merged)}
    err = dict(res.error or {})
    return _fail(path, _map_code(str(err.get("code") or "")),
                 err.get("message") or "追加失败",
                 gateway_code=err.get("code"))


def _norm(path: Any) -> str:
    return str(path or "").strip().lstrip("/")


def _map_code(gateway_code: str) -> str:
    """把 Gateway 的错误码翻译成 Artifact 层可读的分类。"""
    table = {
        "LOCKED_BY_OTHER_AGENT": "locked_by_other",
        "NOT_EFFECTIVE_LOCK_HOLDER": "locked_by_other",
        "NOT_TASK_PARTICIPANT": "not_participant",
        "RESOURCE_NOT_FOUND": "not_found",
        "RESOURCE_DELETED": "not_found",
        "NAME_CONFLICT": "already_exists",
        "STALE_REVISION": "stale_revision",
        "INVALID_ARGUMENT": "bad_path",
        "OUTSIDE_WORKSPACE": "bad_path",
        "INVALID_PARENT": "bad_path",
        "UNKNOWN_AGENT": "unknown_agent",
    }
    return table.get(gateway_code, "unknown" if gateway_code else "unknown")


# ================================================================
# E. Dynamic Artifact —— 每 Agent 的命名空间
# ================================================================
#: 🔒 **动态创建的落地方式：给每个 Agent 一个自己的目录并 handoff 给它。**
#:
#:    为什么是「目录」而不是「放宽 create 的权限」：
#:        放宽权限会破坏 Workspace Core 的 Lock 模型
#:        （create 需要父目录的 Directory Lock）。
#:        而「建目录 + handoff」用的**全是已有的正式入口**，
#:        Lock / Authorization / Revision 语义一个字都不用改。
#:
#: 🔒 这不是 Workflow：目录只表达「谁负责哪片区域」，
#:    不规定「必须创建哪些文件」。Agent 自己决定要不要建、建什么名。
ARTIFACT_NAMESPACES = {
    "world": "novel_world_builder",
    "plot": "novel_plot_architect",
    "manuscript": "novel_author",
    "notes": "novel_editor",
    "reader": "novel_reader",
}


def provision_namespaces(gateway: Any, workspace_id: str, owner: str,
                         *, budget: int = 64) -> list:
    """
    为每个 Agent 建一个命名空间目录并 handoff —— **幂等**。

    返回每个目录的结果（含**真实 revision**）。
    """
    out = []
    from core.workspace_core import normalize_path  # 复用既有实现
    for name, agent_id in ARTIFACT_NAMESPACES.items():
        res = gateway.create(workspace_id, "/", name, "directory", owner)
        if res.success:
            status, rev = "created", int((res.result or {}).get("revision", 0))
        elif str((res.error or {}).get("code")) == "NAME_CONFLICT":
            status = "exists"
            got = gateway.read(workspace_id, "/" + normalize_path(name), owner)
            rev = int((got.result or {}).get("revision", 0)) if got.success else 0
        else:
            out.append({"dir": name, "owner": agent_id, "ok": False,
                        "status": "create_failed",
                        "error": dict(res.error or {})})
            continue

        if agent_id == owner:
            moved_ok, moved_err = True, None
        else:
            moved = gateway.handoff(workspace_id, "/" + normalize_path(name),
                                    from_agent=owner, to_agent=agent_id,
                                    actor=owner,
                                    reason="artifact namespace ownership")
            moved_ok = bool(moved.success)
            moved_err = None if moved.success else dict(moved.error or {})
            if moved.success:
                rev = int((moved.result or {}).get("revision", rev))

        out.append({"dir": name, "owner": agent_id, "ok": True,
                    "status": status, "revision": rev,
                    "handoff_ok": moved_ok, "handoff_error": moved_err})
    return out

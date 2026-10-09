"""
资源结构 / 责任归属的操作（**不是**内容读写）
================================================

【它和 `services/artifacts.py` 的分工】

    artifacts.py          管**内容**：写 / 读一个 Artifact
    resource_control.py   管**结构与责任**：
                              · 建一个目录
                              · 把责任交给别人（handoff）
                              · 放掉自己的独立责任（release）

    🔒 两者都**只**经过 `Workspace Gateway` ——
       Path Validation / Lock / Revision / 审计全部照旧。
       本模块**没有**、也不该有主机文件系统权限。

【🔒 路径规则复用 `artifacts` 的那一份，不复制】

    `validate_artifact_path` / `map_gateway_code` 从 `artifacts` 导入。
    复制一份会得到**两套路径语义** —— 而它们说的是同一种东西
    （Task Workspace 的逻辑路径）。

    适配工作只有一件：Gateway 的规范语法**带前导 `/`**
    （`"/plot"`、根是 `"/"`），所以本模块在调用 Gateway 前补上它。

【Release ≠ Handoff（§14）】

    Handoff  = 「我明确交给 B」
    Release  = 「我不再保留这个**独立**责任」→ 之后按父级规则重新解析

    🔒 根的 release 被 Gateway 拒绝（会让整棵树失去责任源头）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from services.artifacts import (
    ensure_directories, map_gateway_code, validate_artifact_path,
)


def _fail(path: str, code: str, message: str,
          gateway_code: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "path": path,
                           "code": code, "message": message}
    if gateway_code:
        out["gateway_code"] = gateway_code
    return out


def _clean(path: Any) -> str:
    return str(path or "").strip().lstrip("/")


def _gw_path(clean: str) -> str:
    """把本层的逻辑路径转成 **Gateway 的规范语法**（带前导 `/`）。"""
    return "/" + clean.lstrip("/")


def _from_result(path: str, res: Any, *, what: str,
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把 Gateway 的 OperationResult 翻成本层统一形状。"""
    if getattr(res, "success", False):
        out: Dict[str, Any] = {"ok": True, "path": path}
        body = getattr(res, "result", None)
        if isinstance(body, dict):
            out.update(body)
        if extra:
            out.update(extra)
        return out
    err = getattr(res, "error", None) or {}
    code = str(err.get("code") or "")
    return _fail(path, map_gateway_code(code),
                 str(err.get("message") or f"{what}失败"), gateway_code=code)


# ================================================================
# 1. 建目录
# ================================================================
def create_directory(gateway: Any, workspace_id: str, agent_id: str,
                     path: Any) -> Dict[str, Any]:
    """
    在 Task Workspace 里创建一个**目录**（缺失的中间层一并建出来）。

    【🔴 为什么不能靠 `write_artifact` 代替】

        `write_artifact` 会为了放一个文件而建出**它的父目录**
        （`ensure_directories`），但它**建不出空目录** ——
        而「起一个项目骨架」（`src/ tests/ docs/`）本来就不该
        靠往里写一个占位文件来实现。

    🔒 权限照旧：建不出来通常是因为**该层没有 Directory Lock**，
       此时如实返回 `locked_by_other`，**不绕过**。
    """
    clean = _clean(path)
    reason = validate_artifact_path(path)
    if reason:
        return _fail(clean, "bad_path", reason)

    blocked = ensure_directories(gateway, workspace_id, agent_id, clean)
    if blocked is not None:
        return blocked
    return {"ok": True, "path": clean, "type": "directory"}


# ================================================================
# 2. Handoff —— 把责任**明确交给**别人
# ================================================================
def handoff_resource(gateway: Any, workspace_id: str, agent_id: str,
                     path: Any, to_agent: Any, *,
                     reason: str = "", task_id: Optional[str] = None
                     ) -> Dict[str, Any]:
    """
    正式的责任交接（任务书 §24）。

        · Directory → **Controller = 接收方 且 Directory Lock = 接收方**
        · File      → **File Lock 从自己 → 接收方**

    🔒 **接收方必须是这个 Workspace 的参与者**，否则 Gateway 明确失败
       —— 所以「交接」顺带也是一次**真实的准入检查**。

    🔒 `actor` 就是 `agent_id`（发起人=当前责任人）。本层**不**提供
       「替别人交接」的形态 —— 那属于治理动作，不是正常干活。
    """
    clean = _clean(path)
    reason_err = validate_artifact_path(path)
    if reason_err:
        return _fail(clean, "bad_path", reason_err)

    to = str(to_agent or "").strip()
    if not to:
        return _fail(clean, "bad_argument", "to 不能为空（要交给谁）")
    if to == agent_id:
        return _fail(clean, "bad_argument",
                     "to 不能是你自己 —— 交接是**交给别人**")

    res = gateway.handoff(workspace_id, _gw_path(clean),
                          from_agent=agent_id, to_agent=to, actor=agent_id,
                          reason=str(reason or ""), task_id=task_id)
    return _from_result(clean, res, what="交接", extra={"to": to})


# ================================================================
# 3. Release —— 放掉自己的**独立**责任
# ================================================================
def release_resource(gateway: Any, workspace_id: str, agent_id: str,
                     path: Any) -> Dict[str, Any]:
    """
    `Release ≠ Handoff`（任务书 §14）：

        Handoff = 「我明确交给 B」
        Release = 「我不再保留这个**独立**责任」
                  → 之后**按父级规则重新解析** Effective Lock

    🔒 根的 release 由 Gateway 拒绝（会让整棵树失去责任源头）——
       本层**不拦截**，如实把 Gateway 的拒绝翻出来。
    """
    clean = _clean(path)
    reason = validate_artifact_path(path)
    if reason:
        return _fail(clean, "bad_path", reason)

    res = gateway.release(workspace_id, _gw_path(clean), agent_id)
    return _from_result(clean, res, what="释放责任")

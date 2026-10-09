"""
Agent 私有 Workspace —— 读 / 写 / 列（**只碰自己的**）
========================================================

【它是什么】

    Agent **自己的**文件空间（`agents/<自己的 id>/workspace/`）。
    `core/workspace.py` 早就实现了它（Step 1 就有），
    `BaseAgent` 上也早就有一整套转发方法。

    🔴 **缺的从来不是能力，是出口** ——
       设计 §D 的 Implicit 表里写着：
           `write`（Private Workspace） | 谁在替 Agent 做：`three-agent-demo`
       也就是「**能力在、通道没接**」。

【🔒 它为什么与 Task Workspace 是两套东西】

    | | Agent 私有 Workspace | Task Workspace |
    |---|---|---|
    | 存在形式 | **宿主机目录** | SQLite 资源树 |
    | 归属 | **每个 Agent 一个** | Task 共享 |
    | 锁 / 修订 / 审计 / 参与者 | ❌ 都没有 | ✅ 都有 |
    | 安全边界 | **路径逃逸** | 参与者 + 锁 + 修订 |

    ⇒ 所以它们是**两套动作类型**，**不是**一个动作加 `scope` 参数。
      共用返回结构会**假装**私有侧有 revision —— 那正是本项目
      反复吃过的「接口成功掩盖语义空转」。

    🔒 设计上：私有 Workspace 是 **Agent 的长期私有资产空间**（§16），
       `World ≠ Agent`（不变量 #9）—— 它**不是** World。

【🔒 返回结构统一】

    成功 → {"ok": True, "path": ..., "chars": ..., "content": ...}
    失败 → {"ok": False, "path": ..., "code": ..., "message": ...}

    （与 `services/artifacts.py` 同一形状。）

【🔒 越界由 `Workspace` 拒绝，不由这里判断】

    `_resolve()` / `memory_path()` 里的路径安全是**唯一**定义处。
    这里再判一次就会出现**两套安全语义** —— 迟早跑偏。
"""

from __future__ import annotations

from typing import Any, Dict, List

#: 越界读写 `..` / 绝对路径 / symlink 逃逸 —— 一律拒绝。
from core.workspace import WorkspaceViolationError


def _fail(path: str, code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "path": path, "code": code, "message": message}


def write_private(ws: Any, path: Any, content: Any) -> Dict[str, Any]:
    """写自己的私有文件（父目录自动创建）。"""
    target = str(path or "").strip()
    body = content if isinstance(content, str) else str(content or "")
    if not target:
        return _fail(target, "bad_path", "path 不能为空")
    if not body.strip():
        # 🔒 与 Task Workspace 一致：空产出不落盘（§四）。
        return _fail(target, "empty_content", "内容为空，拒绝写入")
    try:
        ws.write_text(target, body)
    except WorkspaceViolationError as exc:
        return _fail(target, "bad_path", str(exc))
    except Exception as exc:                                  # noqa: BLE001
        return _fail(target, "write_failed", f"{type(exc).__name__}: {exc}")
    return {"ok": True, "path": target, "chars": len(body), "content": body}


def read_private(ws: Any, path: Any) -> Dict[str, Any]:
    """读自己的私有文件。"""
    target = str(path or "").strip()
    if not target:
        return _fail(target, "bad_path", "path 不能为空")
    try:
        content = ws.read_text(target)
    except WorkspaceViolationError as exc:
        return _fail(target, "bad_path", str(exc))
    except FileNotFoundError:
        out = _fail(target, "not_found", "没有这个文件")
        out["available"] = list_private(ws).get("paths", [])
        return out
    except Exception as exc:                                  # noqa: BLE001
        return _fail(target, "read_failed", f"{type(exc).__name__}: {exc}")
    return {"ok": True, "path": target,
            "chars": len(content), "content": content}


def list_private(ws: Any) -> Dict[str, Any]:
    """列出自己私有空间里的文件（逻辑相对路径）。"""
    try:
        paths: List[str] = list(ws.list_files())
    except Exception as exc:                                  # noqa: BLE001
        return _fail("", "list_failed", f"{type(exc).__name__}: {exc}")
    return {"ok": True, "paths": paths, "count": len(paths)}

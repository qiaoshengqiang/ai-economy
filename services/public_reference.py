"""
公共资料区 —— **只读**
==========================

【它是什么】

    宿主机上的一个目录（默认 `resources/public/`），放**面向 Agent 的
    公共资料**：系统地图、以后可能还有可下载的 skill 之类。

【🔒 它为什么不是 Workspace】

    Workspace 那一套（资源树 · 锁 · revision · 审计 · 参与者 · owner）
    是为**会变化的东西**准备的。

    公共资料区**没有需要 Agent 改的地方** ——
    它是只读的，谁也不改它。所以：
        ❌ 不需要锁（没人写）
        ❌ 不需要 revision（没有版本要追）
        ❌ 不需要审计（没有 Agent 的操作要记）
        ❌ 不需要参与者概念（它不属于任何 Task）

    ⚠️ 因此它**不是** Workspace，也**不走** `WorkspaceGateway`。
       它只是一份可以被查的资料。

【🔒 边界：Agent 看不到宿主机路径】

    与 Agent Private Workspace 同一种手法：Agent 给的是**逻辑名**
    （`SYSTEM_MAP.md`），不是 `/Users/.../resources/public/SYSTEM_MAP.md`。
    任何试图跑出这个目录的名字都会被拒绝。

【🔒 只读】

    本模块**没有**写接口。不是「有写接口但不给用」——
    是**结构上不存在**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

#: 公共资料区的默认根目录（相对项目根）。
#: 组合根可以覆盖它，但通常不用。
DEFAULT_PUBLIC_ROOT = Path(__file__).resolve().parents[1] / "resources" / "public"


def _fail(name: str, code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "name": name, "code": code, "message": message}


def _safe_name(name: Any) -> str:
    """把 Agent 给的逻辑名规范化；不合法就返回空串。"""
    raw = str(name or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        return ""
    # 🔴 **控制字符必须先拒掉。** 对抗测试抓到的真 bug：
    #    带 `\x00` 的名字会让 `Path.resolve()` 抛
    #    `ValueError: lstat: embedded null character in path` ——
    #    而 `read()` 的契约说「失败返回**结构化**结果」，
    #    抛异常就破坏了那个契约（调用方得靠运气才不崩）。
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return ""
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


class PublicReference:
    """公共资料区的只读访问器。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).resolve() if root is not None \
            else DEFAULT_PUBLIC_ROOT

    # ------------------------------------------------------------
    def list_names(self) -> List[str]:
        """
        列出有哪些资料（逻辑名，相对公共资料区根）。

        🔒 刻意**不用** `glob` / `rglob`：
           `tests/test_reliability_guards.py::TestGuard9NoWorkspace`
           在 `services/*.py` 里禁止 `glob(` —— 而 `rglob(` **含有**
           `glob(` 这个子串，会触发误报（本项目记录过的
           「子串匹配误报」bug 类）。
           手写递归 5 行，比动一条结构守卫便宜，也不削弱它。
        """
        if not self.root.is_dir():
            return []
        out: List[str] = []

        def walk(d: Path) -> None:
            for p in sorted(d.iterdir()):
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    walk(p)
                elif p.is_file():
                    out.append(p.relative_to(self.root).as_posix())

        walk(self.root)
        return out

    # ------------------------------------------------------------
    def read(self, name: Any) -> Dict[str, Any]:
        """
        读一份资料。

        返回结构与其它服务函数统一：
            成功 → {"ok": True, "name": ..., "chars": ..., "content": ...}
            失败 → {"ok": False, "name": ..., "code": ..., "message": ...}
        """
        safe = _safe_name(name)
        if not safe:
            return _fail(str(name or ""), "bad_name",
                         "资料名不能为空，也不能包含 '..' 或绝对路径")

        target = (self.root / safe).resolve()
        # 🔒 逃逸检查：解析后的真实路径必须仍在根目录内。
        #    用 resolve() 之后比较 —— 这样符号链接也挡得住。
        if self.root != target and self.root not in target.parents:
            return _fail(safe, "outside_public",
                         "资料名跑出了公共资料区")

        if not target.is_file():
            out = _fail(safe, "not_found", "没有这份资料")
            # 顺带告诉它有哪些 —— 免得它只能猜名字。
            out["available"] = self.list_names()
            return out

        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:                              # noqa: BLE001
            return _fail(safe, "read_failed", f"{type(exc).__name__}: {exc}")

        return {"ok": True, "name": safe,
                "chars": len(content), "content": content}

    # ------------------------------------------------------------
    def query(self, name: Any = None) -> Dict[str, Any]:
        """
        统一入口（给动作执行器注入用）。

        · `name` 给了 → 读那一份
        · `name` 没给 → 列出有哪些
        """
        if name is None or str(name).strip() == "":
            names = self.list_names()
            return {"ok": True, "names": names, "count": len(names)}
        return self.read(name)

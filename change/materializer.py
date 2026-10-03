"""
Materialization V0 —— 逻辑资源 → Git worktree 物理文件
============================================================

【为什么需要它（hello-demo 发现的真实缺口）】

    Workspace Gateway 写的是**逻辑资源**（Workspace DB）。
    它**不会**把内容落到 Git worktree 的物理目录上。

    hello-demo 当时用的是「Demo 里的临时替身」——
    本文件把它**提出来、可复用、边界清楚**。

【🔒 它**不是**什么（任务书 §18 明确禁止的方向）】

    ❌ scan_everything() → filesystem changes → workspace resources
    ❌ 扫描整个工作区
    ❌ 反向把 Git 当成 Workspace Core 的 source of truth

    ✅ **调用方明确给出要 materialize 哪些 logical resource。**
       这是一条**单向、显式、有限**的路：

           Workspace logical resource
                     ↓  只读
           Materializer
                     ↓  只写
           Git worktree 物理文件

【🔒 七条硬约束（任务书 §17）】

    ① 只处理调用方列出的资源 —— **不扫描**
    ② 不产生 Workspace Revision 的伪变化 —— 它对 Workspace 侧**只读**
    ③ Agent 拿不到 physical path —— 本类**不是** Agent-facing
    ④ 内部使用 WorktreeManager 记录的物理路径
    ⑤ 不执行任何 shell command
    ⑥ 路径必须先过 Workspace 的安全规则（`normalize_path` 拒绝 `..`）
    ⑦ 单向：Workspace → Git，**不反向**

【与未来 Patch 阶段的关系】

    这是 **V0**：粗糙但边界正确。
    将来 Patch 阶段的正式 Materializer 应当**替换**它 ——
    接口（`materialize(workspace_id, worktree_id, paths)`）刻意保持最小，
    就是为了让替换是「换实现」而不是「改调用方」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from change.worktree import WorktreeError
from core.logging_setup import get_logger

logger = get_logger(__name__)


class MaterializationError(WorktreeError):
    """物化失败。"""


@dataclass(frozen=True)
class MaterializeReport:
    """物化的结果。**`target_root` 是物理路径 —— 只给 Human/内部用。**"""

    written: list[str] = field(default_factory=list)      # 逻辑路径
    missing: list[str] = field(default_factory=list)      # 逻辑上不存在
    target_root: str = ""                                  # 🔒 内部/Human only

    @property
    def count(self) -> int:
        return len(self.written)


#: `read(workspace_id, logical_path) -> content | None`
ReadResource = Callable[[str, str], Optional[str]]
#: `worktree_path(worktree_id) -> physical path | None`
WorktreePathFor = Callable[[str], Optional[str]]


class WorktreeMaterializer:
    """
    把**指定**的逻辑资源写到 worktree 的物理目录。

    用法（组合根装配）：

        materializer = WorktreeMaterializer(
            read_resource=...,        # 通常包一层 workspace_gateway.read
            worktree_path_for=...,    # 通常包一层 worktree_store.get
        )
        report = materializer.materialize(ws, wt_id, ["index.html", ...])
    """

    def __init__(
        self,
        *,
        read_resource: ReadResource,
        worktree_path_for: WorktreePathFor,
    ) -> None:
        if not callable(read_resource) or not callable(worktree_path_for):
            raise ValueError(
                "read_resource 与 worktree_path_for 都是必需依赖（fail-closed）"
            )
        self._read = read_resource
        self._path_for = worktree_path_for

    # ----------------------------------------------------------
    def materialize(
        self,
        workspace_id: str,
        worktree_id: str,
        logical_paths: Iterable[str],
    ) -> MaterializeReport:
        """
        🔒 **只写 `logical_paths` 里列出的东西。**

            · 不做目录遍历
            · 不读 Workspace 的其它资源
            · 不改任何 Workspace 状态（对 Workspace 侧只读）
        """
        root = self._path_for(worktree_id)
        if not root:
            raise MaterializationError(
                f"worktree {worktree_id} 没有可用的物理路径"
            )
        root = os.path.realpath(root)

        written: list[str] = []
        missing: list[str] = []

        for raw in logical_paths:
            logical = self._safe_logical_path(raw)

            content = self._read(workspace_id, logical)
            if content is None:
                missing.append(logical)
                continue

            target = self._safe_target(root, logical)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(logical)

        if missing and not written:
            raise MaterializationError(f"没有任何资源可物化：{missing}")

        logger.debug("materialized %d 个文件到 worktree %s", len(written),
                     worktree_id)
        return MaterializeReport(written=written, missing=missing,
                                 target_root=root)

    # ----------------------------------------------------------
    # 🔒 路径安全 —— 复用 Workspace 的规则，不自己发明一套
    # ----------------------------------------------------------
    @staticmethod
    def _safe_logical_path(raw: str) -> str:
        """
        逻辑路径必须过 `normalize_path`（它拒绝 `..`，见 Workspace Core）。

        ⚠️ 这样「哪些路径合法」只有**一个**定义处 ——
           在 Workspace Core 里，而不是在 Materializer 里再来一套。
        """
        from core.workspace_core import WorkspacePathError, normalize_path

        # 🔒 绝对路径直接拒 —— logical path **必须是相对的**。
        #    这是 §17⑦「路径必须经过 Workspace 安全规则」的**加强**：
        #    Workspace 的规则管的是 `..` 逃逸，
        #    这里额外管「看起来像宿主路径」的输入。
        raw_str = str(raw)
        if raw_str.startswith("/") or raw_str.startswith("\\"):
            raise MaterializationError(f"不允许绝对路径：{raw!r}")

        try:
            normalized = normalize_path(str(raw))
        except WorkspacePathError as exc:
            raise MaterializationError(f"非法逻辑路径 {raw!r}：{exc}") from exc

        if normalized == "/" or normalized.endswith("/"):
            raise MaterializationError(f"不能把目录当文件物化：{raw!r}")
        # 去掉前导 "/" —— 它相对 worktree 根
        return normalized.lstrip("/")

    @staticmethod
    def _safe_target(root: str, logical: str) -> str:
        """
        🔒 第二道闸：写之前再确认**落点仍在 worktree 里**。

            即使逻辑路径已经规范化过，也要防
            「符号链接指到外面」这类情况 —— 用 `realpath` 比较前缀。
        """
        target = os.path.realpath(os.path.join(root, logical))
        prefix = root + os.sep
        if target != root and not target.startswith(prefix):
            raise MaterializationError(
                f"物化落点跑出了 worktree：{logical!r}"
            )
        return target

"""
Pi Work 的记忆桥
================

Pi 使用原生 write/bash，不接 AIEOS 的 memory action；所以记忆通过**文件协议**：

    <agent>/memory/pi/context.md   AIEOS 生成，只读快照
    <agent>/memory/pi/working.md   Pi 可读写，任务级工作记忆
    <agent>/memory/pi/inbox/*.md   Pi 写入候选记忆；任务结束后 AIEOS 摄取

🔒 AIEOS 仍是记忆权威：
    · Pi 不直接读写结构化记忆 DB；
    · inbox 不是自动可信的 Long-term；
    · 任务结束后由 AIEOS 决定如何合并。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.private_working_memory import (
    ConcurrentPrivateWorkingMemoryModification,
)


class PiMemoryBridge:
    def __init__(
        self,
        runtime: Any,
        *,
        agent_id: str,
        task: Any,
        task_workspace: Path,
    ) -> None:
        self.rt = runtime
        self.agent_id = str(agent_id)
        self.task = task
        self.task_workspace = Path(task_workspace)
        agent = self._agent()
        workspace = getattr(agent, "workspace", None)
        self.private_workspace = (
            Path(getattr(workspace, "root", "")) if workspace else None)
        memory_base = (
            Path(getattr(workspace, "memory_dir", "")) if workspace else None)
        self.memory_dir = (
            memory_base / "pi" if memory_base else None)

    def _agent(self) -> Any:
        try:
            return self.rt.economy.agent(self.agent_id)
        except Exception:                                     # noqa: BLE001
            return None

    # ------------------------------------------------------------
    def paths(self) -> Dict[str, str]:
        return {
            "task_workspace": str(self.task_workspace),
            "private_workspace": (
                str(self.private_workspace) if self.private_workspace else ""),
            "memory_dir": str(self.memory_dir) if self.memory_dir else "",
        }

    def prepare(self) -> str:
        """生成 context.md / working.md / README.md，并返回上下文文本。"""
        if self.memory_dir is None:
            return ""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "inbox").mkdir(exist_ok=True)

        working = ""
        short = ""
        long = ""
        try:
            working = self.rt.private_working_memory.render(
                self.agent_id, self.task.task_id)
        except Exception:                                     # noqa: BLE001
            working = "（读取工作记忆失败）"
        try:
            short = self.rt.private_short_term_memory.render(self.agent_id)
        except Exception:                                     # noqa: BLE001
            short = "（读取短期记忆失败）"
        try:
            long = self.rt.private_long_term_memory.render_top(self.agent_id)
        except Exception:                                     # noqa: BLE001
            long = "（读取长期记忆失败）"

        context = f"""# AIEOS Work Memory

Agent：{self.agent_id}
任务：{self.task.task_id}
Task 工作目录：{self.task_workspace}
私有 workspace：{self.private_workspace or "（不可用）"}
记忆目录：{self.memory_dir}

## 记忆协议

- `working.md`：本任务的工作记忆，可读写。
- `inbox/*.md`：候选记忆，任务结束后由 AIEOS 摄取；不要直接当权威。
- 不要读取其他 Agent 的目录。

## 私有工作记忆

{working}

## 短期记忆

{short}

## 长期记忆（top）

{long}
"""
        (self.memory_dir / "context.md").write_text(context, encoding="utf-8")
        working_file = self.memory_dir / "working.md"
        if not working_file.exists():
            working_file.write_text(
                f"# 工作记忆\n\n任务：{self.task.task_id}\n\n"
                f"{working}\n",
                encoding="utf-8")
        (self.memory_dir / "README.md").write_text(
            "# Pi 记忆协议\n\n"
            "- context.md 只读（由 AIEOS 生成）。\n"
            "- working.md 是本任务可读写的工作记忆。\n"
            "- 把候选记忆写入 inbox/*.md。\n"
            "- 不要把 inbox 当作权威的长期记忆。\n",
            encoding="utf-8")
        return context

    # ------------------------------------------------------------
    def ingest(self) -> Dict[str, Any]:
        """任务结束后把 working.md / inbox 交给 AIEOS 记忆服务。"""
        if self.memory_dir is None:
            return {"ok": False, "reason": "no_memory_dir"}
        working_file = self.memory_dir / "working.md"
        try:
            working_text = (
                working_file.read_text(encoding="utf-8")
                if working_file.is_file() else "")
        except OSError:
            working_text = ""
        inbox_texts: List[str] = []
        inbox = self.memory_dir / "inbox"
        if inbox.is_dir():
            for path in sorted(inbox.glob("*.md")):
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if text:
                    inbox_texts.append(f"### {path.name}\n{text}")
        combined = working_text.strip()
        if inbox_texts:
            combined += "\n\n## Inbox notes\n\n" + "\n\n".join(inbox_texts)
        if not combined:
            return {"ok": True, "updated": False}

        memory_service = getattr(self.rt, "private_working_memory", None)
        updated = None
        if memory_service is not None:
            for attempt in range(2):
                try:
                    current = memory_service.read(
                        self.agent_id, self.task.task_id)
                    updated = memory_service.update(
                        self.agent_id, self.task.task_id,
                        expected_revision=current.revision,
                        nodes={"pi_work": {
                            "kind": "note",
                            "title": "Pi 工作笔记",
                            "content": {
                                "markdown": combined[:20000],
                            },
                        }},
                    )
                    break
                except ConcurrentPrivateWorkingMemoryModification:
                    if attempt:
                        break
                except Exception:                         # noqa: BLE001
                    break

        short_ok = False
        long_ok = False
        short_service = getattr(self.rt, "private_short_term_memory", None)
        long_service = getattr(self.rt, "private_long_term_memory", None)
        if updated is not None and short_service is not None:
            try:
                short_service.add_from_final(
                    self.agent_id, self.task.task_id,
                    updated.to_dict())
                short_ok = True
            except Exception:                             # noqa: BLE001
                short_ok = False
        # 短期记忆超过保留阈值时，自动抽象成长期记忆并归档旧条目。
        if short_ok and long_service is not None:
            try:
                current = short_service.read(self.agent_id)
                if getattr(current, "needs_compaction", False):
                    long_service.consolidate(
                        self.agent_id,
                        task_id=self.task.task_id,
                        expected_revision=int(current.revision),
                        summary=(
                            f"Pi task {self.task.task_id}: "
                            + combined.strip()[:1200]),
                        tasks=[self.task.task_id],
                        events=[f"pi:{self.task.task_id}"],
                    )
                    long_ok = True
            except Exception:                             # noqa: BLE001
                long_ok = False

        # 清空已摄取的 inbox，避免下次重复合并。
        if inbox_texts:
            for path in inbox.glob("*.md"):
                try:
                    path.unlink()
                except OSError:
                    pass

        return {
            "ok": True,
            "updated": updated is not None,
            "short_term": short_ok,
            "long_term": long_ok,
            "inbox": len(inbox_texts),
        }


__all__ = ["PiMemoryBridge"]

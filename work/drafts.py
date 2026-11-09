"""
TaskDraftStore —— 按 (agent_id, task_id) 持久化的私有草稿
==========================================================

与 `demand.drafts.DraftBuffer` 的区别：

    DraftBuffer      按 Agent 隔离，run 结束即销毁（Phase 1）
    TaskDraftStore   按 (agent_id, task_id) 隔离，落到 Agent 私有空间

存放位置：

    agents/<agent_id>/workspace/.drafts/<task_id>.json

🔒 边界：

    · 草稿仍在 Agent 私有空间，不进入 Task Workspace；
    · 只有显式 `workspace_promote_drafts` 才会写项目文件；
    · 持久化使用 Workspace.write_text，所以路径边界仍然成立。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.identity import utc_now_iso
from demand.drafts import (
    DEFAULT_READ_CHARS,
    MAX_DRAFT_CHARS,
    MAX_DRAFTS_PER_AGENT,
    MAX_READ_CHARS,
    Draft,
)


def _safe_task_part(task_id: str) -> str:
    raw = str(task_id or "").strip() or "task"
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", raw):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class TaskDraftStore:
    """单 Task 的私有草稿存储（持久化到每个 Agent 的私有空间）。"""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_of: Callable[[str], Any],
        max_draft_chars: int = MAX_DRAFT_CHARS,
        max_drafts_per_agent: int = MAX_DRAFTS_PER_AGENT,
    ) -> None:
        self.task_id = str(task_id)
        self.workspace_of = workspace_of
        self.max_draft_chars = int(max_draft_chars)
        self.max_drafts_per_agent = int(max_drafts_per_agent)
        self._lock = threading.RLock()
        self._loaded: Dict[str, Dict[str, Draft]] = {}

    # ------------------------------------------------------------
    # 路径 / 持久化
    # ------------------------------------------------------------
    @property
    def _relative_path(self) -> str:
        return f".drafts/{_safe_task_part(self.task_id)}.json"

    def _read_file(self, agent_id: str) -> Dict[str, Draft]:
        workspace = self.workspace_of(str(agent_id))
        if workspace is None:
            return {}
        try:
            raw = workspace.read_text(self._relative_path)
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        drafts: Dict[str, Draft] = {}
        for item in data.get("drafts") or []:
            try:
                draft = Draft(
                    draft_id=str(item["draft_id"]),
                    agent_id=str(agent_id),
                    label=str(item.get("label") or ""),
                    content=str(item.get("content") or ""),
                    created_at=str(item.get("created_at") or utc_now_iso()),
                    updated_at=str(item.get("updated_at") or utc_now_iso()),
                )
            except Exception:
                continue
            drafts[draft.draft_id] = draft
        return drafts

    def _write_file(self, agent_id: str, drafts: Dict[str, Draft]) -> None:
        workspace = self.workspace_of(str(agent_id))
        if workspace is None:
            return
        payload = {
            "task_id": self.task_id,
            "agent_id": str(agent_id),
            "updated_at": utc_now_iso(),
            "drafts": [
                {
                    "draft_id": draft.draft_id,
                    "label": draft.label,
                    "content": draft.content,
                    "created_at": draft.created_at,
                    "updated_at": draft.updated_at,
                }
                for draft in sorted(drafts.values(),
                                    key=lambda d: d.updated_at)
            ],
        }
        workspace.write_text(
            self._relative_path,
            json.dumps(payload, ensure_ascii=False),
        )

    def _bucket(self, agent_id: str) -> Dict[str, Draft]:
        key = str(agent_id)
        if key not in self._loaded:
            self._loaded[key] = self._read_file(key)
        return self._loaded[key]

    def _save_bucket(self, agent_id: str) -> None:
        self._write_file(agent_id, self._bucket(agent_id))

    # ------------------------------------------------------------
    # 与 DraftBuffer 相同的动作
    # ------------------------------------------------------------
    def write(self, agent_id: str, content: Any,
              *, label: Any = "") -> Dict[str, Any]:
        body = content if isinstance(content, str) else str(content or "")
        if not body.strip():
            return {"ok": False, "code": "empty_content",
                    "message": "草稿内容为空，拒绝创建"}
        if len(body) > self.max_draft_chars:
            return {
                "ok": False, "code": "draft_too_large",
                "message": (f"草稿 {len(body)} 字符，超过上限 "
                            f"{self.max_draft_chars}；请分块 draft_append"),
            }
        with self._lock:
            bucket = self._bucket(agent_id)
            if len(bucket) >= self.max_drafts_per_agent:
                return {
                    "ok": False, "code": "too_many_drafts",
                    "message": (f"草稿数已达上限 {self.max_drafts_per_agent}；"
                                "请先 draft_delete 不再需要的草稿"),
                }
            now = utc_now_iso()
            draft = Draft(
                draft_id=f"draft_{uuid.uuid4().hex[:12]}",
                agent_id=str(agent_id),
                label=str(label or "").strip()[:120],
                content=body,
                created_at=now,
                updated_at=now,
            )
            bucket[draft.draft_id] = draft
            self._save_bucket(agent_id)
            return {"ok": True, **draft.to_meta()}

    def append(self, agent_id: str, draft_id: Any,
               content: Any) -> Dict[str, Any]:
        body = content if isinstance(content, str) else str(content or "")
        if not body.strip():
            return {"ok": False, "code": "empty_content",
                    "message": "追加内容为空，拒绝"}
        with self._lock:
            bucket = self._bucket(agent_id)
            did = str(draft_id or "").strip()
            draft = bucket.get(did)
            if draft is None:
                return {"ok": False, "code": "draft_not_found",
                        "message": f"没有草稿 {did!r}"}
            if draft.chars + len(body) > self.max_draft_chars:
                return {
                    "ok": False, "code": "draft_too_large",
                    "message": (f"追加后 {draft.chars + len(body)} 字符，"
                                f"超过上限 {self.max_draft_chars}"),
                }
            updated = Draft(
                draft_id=draft.draft_id,
                agent_id=draft.agent_id,
                label=draft.label,
                content=draft.content + body,
                created_at=draft.created_at,
                updated_at=utc_now_iso(),
            )
            bucket[did] = updated
            self._save_bucket(agent_id)
            return {"ok": True, **updated.to_meta()}

    def read(self, agent_id: str, draft_id: Any, *,
             offset: Any = 0, limit: Any = DEFAULT_READ_CHARS) -> Dict[str, Any]:
        with self._lock:
            draft = self._bucket(agent_id).get(str(draft_id or "").strip())
        if draft is None:
            return {"ok": False, "code": "draft_not_found",
                    "message": f"没有草稿 {draft_id!r}"}
        try:
            start = int(offset) if offset is not None else 0
            count = int(limit) if limit is not None else DEFAULT_READ_CHARS
        except (TypeError, ValueError):
            return {"ok": False, "code": "bad_pagination",
                    "message": "offset / limit 必须是整数"}
        if start < 0:
            return {"ok": False, "code": "bad_pagination",
                    "message": "offset 必须 >= 0"}
        if count <= 0:
            return {"ok": False, "code": "bad_pagination",
                    "message": "limit 必须 > 0"}
        count = min(count, MAX_READ_CHARS)
        body = draft.content[start:start + count]
        end = start + len(body)
        return {
            "ok": True,
            "draft_id": draft.draft_id,
            "label": draft.label,
            "offset": start,
            "limit": count,
            "chars": len(body),
            "total_chars": draft.chars,
            "content": body,
            "truncated": end < draft.chars,
            "sha256": draft.sha256,
        }

    def list(self, agent_id: str) -> Dict[str, Any]:
        with self._lock:
            items = [
                draft.to_meta()
                for draft in sorted(
                    self._bucket(agent_id).values(),
                    key=lambda d: d.updated_at, reverse=True)
            ]
        return {"ok": True, "drafts": items, "count": len(items)}

    def get(self, agent_id: str, draft_id: Any) -> Optional[Draft]:
        with self._lock:
            return self._bucket(agent_id).get(str(draft_id or "").strip())

    def delete(self, agent_id: str, draft_id: Any) -> Dict[str, Any]:
        with self._lock:
            bucket = self._bucket(agent_id)
            did = str(draft_id or "").strip()
            if did not in bucket:
                return {"ok": False, "code": "draft_not_found",
                        "message": f"没有草稿 {did!r}"}
            del bucket[did]
            self._save_bucket(agent_id)
            return {"ok": True, "draft_id": did}

    def meta(self, agent_id: str) -> List[Dict[str, Any]]:
        return self.list(agent_id).get("drafts", [])

"""
Draft Buffer —— Agent 私有草稿区（不是项目文件，不是 Memory 四层）
=====================================================================

目标：

    API 先产出内容 → 存成 draft
    → 下一次调用只看到 draft 元数据 / 可按块读取
    → 模型决定路径后，用 workspace_promote_drafts 一次落盘多个文件

🔒 边界：

    · draft 属于单个 Agent，其他 Agent 读不到；
    · draft 不是 Task Workspace artifact，不参与验收 / Plan 完成度；
    · draft 不会被系统自动写入项目文件；
    · promotion 必须显式动作，并且仍然走 Workspace Gateway；
    · 本 V1 存在内存中，随 run 结束销毁；需要长期保存请显式写 private workspace。

内容大小有上限，避免 Agent 用草稿区无界占用内存。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso

#: 单个草稿最大字符数（也是单次 promote 可写入的最大内容）。
#: 64k token 的生成内容通常远小于这个上限；这里防的是无界内存。
MAX_DRAFT_CHARS = 200_000
#: 每个 Agent 最多同时保留多少草稿。
MAX_DRAFTS_PER_AGENT = 16
#: draft_read 单次默认 / 最大返回字符数。
DEFAULT_READ_CHARS = 12_000
MAX_READ_CHARS = 32_000


@dataclass(frozen=True)
class Draft:
    draft_id: str
    agent_id: str
    label: str
    content: str
    created_at: str
    updated_at: str

    @property
    def chars(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            self.content.encode("utf-8")).hexdigest()

    def to_meta(self, *, preview_chars: int = 240) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "label": self.label,
            "chars": self.chars,
            "sha256": self.sha256,
            "preview": self.content[:preview_chars],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DraftBuffer:
    """按 Agent 隔离的内存草稿区。"""

    def __init__(
        self,
        *,
        max_draft_chars: int = MAX_DRAFT_CHARS,
        max_drafts_per_agent: int = MAX_DRAFTS_PER_AGENT,
    ) -> None:
        self.max_draft_chars = int(max_draft_chars)
        self.max_drafts_per_agent = int(max_drafts_per_agent)
        self._by_agent: Dict[str, Dict[str, Draft]] = {}

    # ------------------------------------------------------------
    def _bucket(self, agent_id: str) -> Dict[str, Draft]:
        return self._by_agent.setdefault(str(agent_id), {})

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
        return {"ok": True, **draft.to_meta()}

    def append(self, agent_id: str, draft_id: Any,
               content: Any) -> Dict[str, Any]:
        bucket = self._bucket(agent_id)
        did = str(draft_id or "").strip()
        draft = bucket.get(did)
        if draft is None:
            return {"ok": False, "code": "draft_not_found",
                    "message": f"没有草稿 {did!r}"}
        body = content if isinstance(content, str) else str(content or "")
        if not body.strip():
            return {"ok": False, "code": "empty_content",
                    "message": "追加内容为空，拒绝"}
        if draft.chars + len(body) > self.max_draft_chars:
            return {
                "ok": False, "code": "draft_too_large",
                "message": (f"追加后 {draft.chars + len(body)} 字符，超过上限 "
                            f"{self.max_draft_chars}"),
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
        return {"ok": True, **updated.to_meta()}

    def read(self, agent_id: str, draft_id: Any, *,
             start_char: Any = None,
             max_chars: Any = None,
             offset: Any = None,
             limit: Any = None) -> Dict[str, Any]:
        bucket = self._bucket(agent_id)
        did = str(draft_id or "").strip()
        draft = bucket.get(did)
        if draft is None:
            return {"ok": False, "code": "draft_not_found",
                    "message": f"没有草稿 {did!r}"}
        if start_char is not None and offset is not None:
            try:
                if int(start_char) != int(offset):
                    return {"ok": False, "code": "bad_pagination",
                            "message": "不要同时使用 start_char 和 offset"}
            except (TypeError, ValueError):
                return {"ok": False, "code": "bad_pagination",
                        "message": "start_char / offset 必须是整数"}
        if max_chars is not None and limit is not None:
            try:
                if int(max_chars) != int(limit):
                    return {"ok": False, "code": "bad_pagination",
                            "message": "不要同时使用 max_chars 和 limit"}
            except (TypeError, ValueError):
                return {"ok": False, "code": "bad_pagination",
                        "message": "max_chars / limit 必须是整数"}
        if start_char is None:
            start_char = offset if offset is not None else 0
        if max_chars is None:
            max_chars = limit if limit is not None else DEFAULT_READ_CHARS
        try:
            start = int(start_char)
            count = int(max_chars)
        except (TypeError, ValueError):
            return {"ok": False, "code": "bad_pagination",
                    "message": "start_char / max_chars 必须是整数"}
        if start < 0:
            return {"ok": False, "code": "bad_pagination",
                    "message": "start_char 必须 >= 0"}
        if count <= 0:
            return {"ok": False, "code": "bad_pagination",
                    "message": "max_chars 必须 > 0"}
        count = min(count, MAX_READ_CHARS)
        body = draft.content[start:start + count]
        end = start + len(body)
        return {
            "ok": True,
            "draft_id": did,
            "label": draft.label,
            "start_char": start,
            "max_chars": count,
            "next_start_char": end if end < draft.chars else None,
            "chars": len(body),
            "total_chars": draft.chars,
            "content": body,
            "truncated": end < draft.chars,
            "sha256": draft.sha256,
        }

    def list(self, agent_id: str) -> Dict[str, Any]:
        bucket = self._bucket(agent_id)
        items = [
            draft.to_meta()
            for draft in sorted(bucket.values(),
                                key=lambda d: d.updated_at, reverse=True)
        ]
        return {"ok": True, "drafts": items, "count": len(items)}

    def get(self, agent_id: str, draft_id: Any) -> Optional[Draft]:
        return self._bucket(agent_id).get(str(draft_id or "").strip())

    def delete(self, agent_id: str, draft_id: Any) -> Dict[str, Any]:
        bucket = self._bucket(agent_id)
        did = str(draft_id or "").strip()
        if did not in bucket:
            return {"ok": False, "code": "draft_not_found",
                    "message": f"没有草稿 {did!r}"}
        del bucket[did]
        return {"ok": True, "draft_id": did}

    def meta(self, agent_id: str) -> List[Dict[str, Any]]:
        return self.list(agent_id).get("drafts", [])


__all__ = [
    "Draft", "DraftBuffer",
    "MAX_DRAFT_CHARS", "MAX_DRAFTS_PER_AGENT",
    "DEFAULT_READ_CHARS", "MAX_READ_CHARS",
]

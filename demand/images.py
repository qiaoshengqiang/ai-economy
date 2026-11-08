"""把 run_tool 的图片元数据解析成 data URL（Demand 版）。"""

from __future__ import annotations

import base64
import mimetypes
from typing import Any, Optional

from tool_runtime import safe_join

MAX_VISUAL_BYTES = 4_000_000
_VISUAL_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def make_image_resolver(runtime: Any):
    def _resolve(run_id: str, relative_path: str) -> Optional[str]:
        try:
            run_id = str(run_id or "")
            if not run_id or "/" in run_id or "\\" in run_id:
                return None
            root = (runtime.tool_runner.run_root / run_id / "work").resolve()
            target = safe_join(root, str(relative_path or ""))
            if not target.is_file():
                return None
            if target.stat().st_size > MAX_VISUAL_BYTES:
                return None
            mime = _VISUAL_MIME.get(target.suffix.lower())
            if mime is None:
                mime = mimetypes.guess_type(target.name)[0] or ""
            if not mime.startswith("image/"):
                return None
            data = base64.b64encode(target.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{data}"
        except Exception:                                     # noqa: BLE001
            return None

    return _resolve

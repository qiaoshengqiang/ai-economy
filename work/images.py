"""把 run_tool 产生的截图解析成 data URL（Work 版）。

Vision Perception V1 的 Provider / 消息格式 / 回喂机制已经在
`actions/read_payload.py` 与 `actions/run.py` 实现；缺的只是
**组合根注入**。Work 层不能依赖 `demand/`，所以这里提供一个
只依赖 `tool_runtime` 的解析器。

🔒 只解析 run 目录里的新图片；不读 Task Workspace 的历史图片，
   也不把 base64 写进 DB / Experience。
"""

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
    """返回 `(run_id, relative_path) -> data URL | None`。"""

    def _resolve(run_id: str, relative_path: str) -> Optional[str]:
        try:
            tool_runner = getattr(runtime, "tool_runner", None)
            run_root = getattr(tool_runner, "run_root", None)
            if run_root is None:
                return None
            run_id = str(run_id or "")
            if not run_id or "/" in run_id or "\\" in run_id:
                return None
            root = (run_root / run_id / "work").resolve()
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

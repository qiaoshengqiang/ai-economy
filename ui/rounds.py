"""UI 任务轮次日志。

每个 Task 一个 JSON 文件：

    runs/ui/rounds/<task_id>.json
    [
      {"round": 1, "segments": [...], "summary": "...", ...},
      ...
    ]

🔒 只保存 Work run 已经产生的事实：模型原文、动作、结果、总结。
   不重新生成、不推断、不改写。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List


class TaskRoundLog:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _file(self, task_id: str) -> Path:
        safe = "".join(
            char for char in str(task_id or "")
            if char.isalnum() or char in ("_", "-", ".")
        )
        return self.root / f"{safe or 'unknown'}.json"

    def append(self, task_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            path = self._file(task_id)
            try:
                data = (
                    json.loads(path.read_text("utf-8"))
                    if path.exists() else []
                )
            except (OSError, ValueError):
                data = []
            if not isinstance(data, list):
                data = []
            data.append(payload)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def read(self, task_id: str) -> List[Dict[str, Any]]:
        path = self._file(task_id)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

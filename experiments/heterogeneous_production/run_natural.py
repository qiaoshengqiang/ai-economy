#!/usr/bin/env python3
"""
真实实验启动器 —— 载入 .env 后跑 production.py。

🔒 生产源码 `production.py` 自己**不**加载 .env（它假定环境已就绪，
   与 `main.py` 的分工一致）。这个启动器只做一件事：把 .env 装进
   `os.environ`，然后以 `__main__` 跑 production.py。

⚠️ 没有它的话 `load_resolved_config` 会看不到任何凭据，
   实验会以「provider 不可用」失败 —— 那不是本实验要测的东西。
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # 项目根（ai-economy/）

# 🔒 runpy.run_path 会把 sys.path[0] 设成脚本所在目录，
#    于是 `import communication` 之类找不到 —— 必须显式把项目根放进去。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

sys.argv = ["production.py"]
runpy.run_path(str(HERE / "production.py"), run_name="__main__")

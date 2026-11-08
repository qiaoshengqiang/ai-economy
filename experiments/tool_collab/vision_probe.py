#!/usr/bin/env python3
"""
Vision Probe —— 验证 deepseek-flash 的读图能力
=================================================

这不是 Tool Runtime 的公共工具，也不是 Agent 动作；
它只是一个**独立探针**，用来回答：

    「我们的 Provider / 模型，到底能不能读一张截图？」

用法：

    ./.venv/bin/python experiments/tool_collab/vision_probe.py \
        --image experiments/tool_collab/runs/.../screenshots/actual_default.png \
        --question "这个页面有没有编辑面板？屋顶是否完整？"

Key 只从环境变量读（默认 `DEEPSEEK_KEY_AGENT_A`），不打印。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

DEFAULT_QUESTION = (
    "这是一张网页截图。请只描述你实际看到的内容，并指出明显的 UI/3D 问题。"
    "特别检查：(1) 页面上有没有可操作的编辑面板/按钮；"
    "(2) 小屋屋顶是否完整。用 3-6 条中文列出。"
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vision_probe.py")
    ap.add_argument("--image", required=True)
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--detail", default="low", choices=["low", "high", "original", "auto"])
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--key-env", default="DEEPSEEK_KEY_AGENT_A")
    ap.add_argument("--base-url", default="https://api.deepseek.com")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    key = os.environ.get(args.key_env)
    if not key:
        print(f"缺少环境变量 {args.key_env}", file=sys.stderr)
        return 2

    path = Path(args.image).expanduser().resolve()
    if not path.is_file():
        print(f"图片不存在：{path}", file=sys.stderr)
        return 2
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    payload = {
        "model": args.model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": args.question},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}",
                               "detail": args.detail}},
            ],
        }],
        "max_tokens": int(args.max_tokens),
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print(content or "（空内容：可能是 reasoning 吃满了 max_tokens）")
    print()
    print("usage:", json.dumps(data.get("usage"), ensure_ascii=False))
    return 0 if content else 1


if __name__ == "__main__":
    raise SystemExit(main())

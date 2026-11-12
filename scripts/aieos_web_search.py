#!/usr/bin/env python3
"""Pi 的 web_search CLI 桥 —— 调用 AIEOS 本地搜索端点。

环境变量（由 AIEOS UI 注入）：
    AIEOS_WEB_SEARCH_URL
    AIEOS_WEB_SEARCH_TOKEN
    AIEOS_AGENT_ID

🔒 这里没有 provider key；key 只在 AIEOS UI 进程内。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _request(path: str, payload: dict) -> dict:
    base = os.environ.get("AIEOS_WEB_SEARCH_URL", "").rstrip("/")
    token = os.environ.get("AIEOS_WEB_SEARCH_TOKEN", "")
    if not base or not token:
        raise RuntimeError("AIEOS web search endpoint 未配置")
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-AIEOS-Search-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    if not isinstance(data, dict) or not data.get("ok", True):
        raise RuntimeError(str(data.get("error") or "搜索失败"))
    return data


def _markdown(data: dict) -> str:
    lines = []
    for index, item in enumerate(data.get("results") or [], start=1):
        title = item.get("title") or "(untitled)"
        url = item.get("url") or ""
        summary = item.get("summary") or item.get("snippet") or ""
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if summary:
            lines.append(f"   {summary[:1200]}")
        content = item.get("content") or ""
        if content:
            lines.append("   内容摘要：")
            lines.append(content[:2000])
        lines.append("")
    if not lines:
        return "（没有搜索结果）"
    return "\n".join(lines).rstrip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="AIEOS web search / fetch bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="搜索网页")
    search.add_argument("query")
    search.add_argument("--count", type=int, default=5)
    search.add_argument("--content", action="store_true")
    search.add_argument("--ori-question", default="")
    search.add_argument("--json", action="store_true")
    fetch = sub.add_parser("fetch", help="抓取单个公开网页")
    fetch.add_argument("url")
    fetch.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            data = _request("/internal/web-search", {
                "query": args.query,
                "count": args.count,
                "need_content": bool(args.content),
                "ori_question": args.ori_question,
                "agent_id": os.environ.get("AIEOS_AGENT_ID", ""),
            })
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(_markdown(data))
        else:
            data = _request("/internal/web-fetch", {
                "url": args.url,
                "agent_id": os.environ.get("AIEOS_AGENT_ID", ""),
            })
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(f"# {data.get('title') or args.url}")
                print()
                print(str(data.get("content") or "")[:20000])
    except Exception as exc:                                  # noqa: BLE001
        print(f"web tool failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

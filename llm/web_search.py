"""
DeepSeek Search / Fetch 客户端
==============================

官方接口：

    POST https://api.deepseek.com/v1/tools/search
    POST https://api.deepseek.com/v1/tools/fetch

🔒 API key 只在 AIEOS 主进程内使用；Pi 通过本地端点间接调用。
🔒 不把搜索内容当成 Task 状态或 Shared Context；它只是只读外部证据。
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from core.logging_setup import get_logger

logger = get_logger(__name__)


class WebSearchError(RuntimeError):
    """搜索 / Fetch 失败。"""

    def __init__(self, message: str, *, status: int = 0,
                 code: str = "") -> None:
        super().__init__(message)
        self.status = int(status or 0)
        self.code = str(code or "")


def _validate_public_url(url: str) -> str:
    value = str(url or "").strip()
    if len(value) > 8192:
        raise WebSearchError("URL 超过 8192 个字符。", code="invalid_url")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebSearchError("只允许 http/https URL。", code="invalid_url")
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise WebSearchError("不允许 fetch 本地主机。", code="invalid_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_unspecified
    ):
        raise WebSearchError("不允许 fetch 私有/保留 IP。", code="invalid_url")
    if parsed.username or parsed.password:
        raise WebSearchError("URL 不能包含用户名或密码。", code="invalid_url")
    return value


class WebSearchClient:
    """DeepSeek Search / Fetch 的最小客户端。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "https://api.deepseek.com").rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        if not self.api_key:
            raise WebSearchError("缺少 DeepSeek API key。", code="missing_key")

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except OSError:
                pass
            logger.warning(
                "web_search http_error path=%s status=%s", path, exc.code)
            raise WebSearchError(
                f"DeepSeek 搜索请求失败（HTTP {exc.code}）"
                + (f"：{detail}" if detail else ""),
                status=exc.code,
                code="http_error",
            ) from exc
        except Exception as exc:                              # noqa: BLE001
            logger.warning(
                "web_search network_error path=%s type=%s",
                path, type(exc).__name__)
            raise WebSearchError(
                f"DeepSeek 搜索请求失败：{type(exc).__name__}: {exc}",
                code="network_error",
            ) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebSearchError(
                "DeepSeek 搜索返回了非 JSON 内容。",
                code="invalid_response",
            ) from exc
        if not isinstance(data, dict):
            raise WebSearchError(
                "DeepSeek 搜索返回格式不正确。",
                code="invalid_response",
            )
        return data

    def search(
        self,
        query: str,
        *,
        count: int = 5,
        need_content: bool = False,
        ori_question: str = "",
    ) -> Dict[str, Any]:
        value = str(query or "").strip()
        if not (1 <= len(value) <= 128):
            raise WebSearchError(
                "query 必须是 1..128 个字符。", code="invalid_query")
        result_count = int(count)
        if result_count < 1 or result_count > 50:
            raise WebSearchError(
                "count 必须在 1..50。", code="invalid_count")
        payload: Dict[str, Any] = {
            "query": value,
            "count": result_count,
            "need_content": bool(need_content),
        }
        if ori_question:
            payload["ori_question"] = str(ori_question)[:128]
        data = self._post("/v1/tools/search", payload)
        raw_results = data.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise WebSearchError(
                "DeepSeek 搜索返回的 results 不是列表。",
                code="invalid_response")
        results: List[Dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            results.append({
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or ""),
                "summary": str(item.get("summary") or ""),
                "content": str(item.get("content") or "")[:12000],
                "site_name": item.get("site_name"),
                "date": item.get("date"),
                "authority": item.get("authority"),
                "rerank_score": item.get("rerank_score"),
            })
        logger.info(
            "web_search query=%r count=%s results=%s",
            value[:120], result_count, len(results))
        return {
            "query": value,
            "count": result_count,
            "results": results,
        }

    def fetch(self, url: str) -> Dict[str, Any]:
        value = _validate_public_url(url)
        data = self._post("/v1/tools/fetch", {"url": value})
        content = str(data.get("content") or "")[:20000]
        logger.info("web_fetch url=%s chars=%s", value[:200], len(content))
        return {
            "url": value,
            "title": str(data.get("title") or ""),
            "content": content,
        }


__all__ = ["WebSearchClient", "WebSearchError"]

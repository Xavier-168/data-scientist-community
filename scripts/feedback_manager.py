#!/usr/bin/env python3
"""Feedback forwarding for packaged desktop releases."""

from __future__ import annotations

import json
import ssl
import time
from typing import Any
from urllib.request import Request, urlopen

from update_manager import current_package_info


FEEDBACK_TIMEOUT_SECONDS = 12
MAX_MESSAGE_CHARS = 4000


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CONTEXT = _build_ssl_context()


def _safe_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _feedback_endpoints(package_info: dict[str, Any]) -> list[str]:
    endpoints: list[str] = []
    explicit = _safe_text(package_info.get("feedback_endpoint"), 1000).rstrip("/")
    if explicit:
        endpoints.append(explicit)
    for server in package_info.get("activation_servers") or []:
        base = _safe_text(server, 1000).rstrip("/")
        if base:
            endpoints.append(f"{base}/feedback/send")

    deduped: list[str] = []
    seen = set()
    for endpoint in endpoints:
        if endpoint not in seen:
            deduped.append(endpoint)
            seen.add(endpoint)
    return deduped


def send_feedback(
    base_dir: str,
    *,
    message: str,
    customer_name: str = "",
    workspace_name: str = "",
    page_path: str = "",
    user_agent: str = "",
    license_customer_name: str = "",
) -> dict[str, Any]:
    clean_message = _safe_text(message, MAX_MESSAGE_CHARS)
    if not clean_message:
        return {"ok": False, "error": "empty_feedback", "message": "请先写下反馈内容。"}

    package_info = current_package_info(base_dir)
    endpoints = _feedback_endpoints(package_info)
    if not endpoints:
        return {"ok": False, "error": "feedback_gateway_missing", "message": "反馈服务暂未配置。"}

    payload = {
        "message": clean_message,
        "customer_name": _safe_text(customer_name, 120) or _safe_text(license_customer_name, 120),
        "workspace_name": _safe_text(workspace_name, 120),
        "page_path": _safe_text(page_path, 300),
        "user_agent": _safe_text(user_agent, 500),
        "package": package_info,
        "sent_at": int(time.time()),
    }

    errors = []
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for endpoint in endpoints:
        try:
            req = Request(
                endpoint,
                data=raw,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "User-Agent": "YRG-Desktop-Feedback/1.0",
                },
                method="POST",
            )
            with urlopen(req, timeout=FEEDBACK_TIMEOUT_SECONDS, context=_SSL_CONTEXT) as resp:
                body = resp.read(1024 * 1024).decode("utf-8")
            result = json.loads(body) if body.strip() else {}
            if isinstance(result, dict) and result.get("ok"):
                return {"ok": True, "message": "反馈已发送给产品团队。", "server": endpoint}
            errors.append({"server": endpoint, "error": result.get("error") if isinstance(result, dict) else "invalid_response"})
        except Exception as exc:
            errors.append({"server": endpoint, "error": str(exc)})

    return {
        "ok": False,
        "error": "feedback_send_failed",
        "message": "反馈暂时发送失败，请稍后重试。",
        "errors": errors,
    }

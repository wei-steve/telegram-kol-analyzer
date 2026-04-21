"""Grounded chat helpers for the Telegram web workbench."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class LLMProxyConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float


def load_llm_proxy_config(environ: dict[str, str] | None = None) -> LLMProxyConfig:
    """Load LLM proxy settings from environment variables."""

    env = environ or os.environ
    return LLMProxyConfig(
        base_url=env.get("TELEGRAM_KOL_LLM_BASE_URL", "http://127.0.0.1:8317"),
        api_key=env.get("TELEGRAM_KOL_LLM_API_KEY", ""),
        model=env.get("TELEGRAM_KOL_LLM_MODEL", "gpt-4.1-mini"),
        timeout_seconds=float(env.get("TELEGRAM_KOL_LLM_TIMEOUT_SECONDS", "60")),
    )


def build_scope_context(messages: list[dict[str, Any]]) -> str:
    """Render scoped message records into a bounded prompt context."""

    parts: list[str] = [
        "Messages are ordered chronologically. Later entries are newer and should be weighted more heavily for the latest state and recent changes.",
        "",
    ]
    for index, message in enumerate(messages, start=1):
        parts.append(f"Source [{index}] raw_message_id={message.get('raw_message_id')}")
        parts.append(f"message_id={message.get('message_id')}")
        parts.append(f"sender={message.get('sender_name') or 'Unknown'}")
        text = (message.get("text") or "").strip()
        if text:
            parts.append(f"text={text}")
        reply_context = message.get("reply_context") or {}
        if reply_context:
            reply_text = reply_context.get("text")
            if reply_text:
                parts.append(f"reply_context={reply_text}")
        media_assets = message.get("media_assets") or []
        for media_asset in media_assets:
            ocr_text = (media_asset.get("ocr_text") or "").strip()
            if ocr_text:
                parts.append(f"ocr_text={ocr_text}")
        parts.append("")
    return "\n".join(parts).strip()


def extract_recent_message_limit(question: str) -> int | None:
    """Extract an explicit recent-message count override from question text."""

    patterns = (
        r"最近\s*(\d+)\s*条",
        r"recent\s+(\d+)\s+messages?",
    )
    for pattern in patterns:
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def build_proxy_chat_payload(
    *, question: str, scope_context: str, model: str, group_prompt: str | None = None
) -> dict[str, Any]:
    """Build an OpenAI-compatible chat payload for the proxy."""

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are an analyst for Telegram trading group research. "
                "Answer using only the provided source context and cite sources like [1], [2]."
            ),
        }
    ]
    if group_prompt and group_prompt.strip():
        messages.append(
            {
                "role": "system",
                "content": f"Group prompt:\n{group_prompt.strip()}",
            }
        )
    messages.extend(
        [
            {
                "role": "user",
                "content": f"Source context:\n{scope_context}",
            },
            {
                "role": "user",
                "content": question,
            },
        ]
    )
    return {
        "model": model,
        "messages": messages,
    }


def build_source_reference_map(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build source reference metadata for UI citation rendering."""

    references: list[dict[str, Any]] = []
    for index, message in enumerate(messages, start=1):
        preview_text = (message.get("text") or "").strip()
        preview = preview_text[:120] if preview_text else "(no text)"
        references.append(
            {
                "index": index,
                "label": f"[{index}] {message.get('sender_name') or 'Unknown'}",
                "raw_message_id": message.get("raw_message_id"),
                "message_id": message.get("message_id"),
                "preview": preview,
            }
        )
    return references


def request_grounded_chat_answer(
    *,
    config: LLMProxyConfig,
    question: str,
    scope_context: str,
    group_prompt: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Send a grounded chat request through an OpenAI-compatible proxy."""

    payload = build_proxy_chat_payload(
        question=question,
        scope_context=scope_context,
        model=config.model,
        group_prompt=group_prompt,
    )
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    created_client = client is None
    active_client = client or httpx.Client(timeout=config.timeout_seconds)
    try:
        response = active_client.post(
            f"{config.base_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    finally:
        if created_client:
            active_client.close()

    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    _raise_for_error_like_answer(content)
    return content


def _raise_for_error_like_answer(content: str) -> None:
    lowered = content.lower()
    if "does not support image input" in lowered:
        raise httpx.HTTPError(content)

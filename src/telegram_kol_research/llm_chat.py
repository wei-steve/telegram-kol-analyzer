"""Grounded chat helpers for the Telegram web workbench."""

from __future__ import annotations

import os
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


@dataclass(slots=True)
class LLMProxyConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float


class RuntimeAgentLLMConfigError(ValueError):
    """Dedicated Runtime Agent provider configuration is incomplete."""


_RUNTIME_AGENT_LLM_CONFIG_ERROR = (
    "dedicated Runtime Agent provider configuration is invalid"
)


_FINAL_DIAGNOSIS_TOOL_NAME = "submit_runtime_diagnosis"
_FINAL_DIAGNOSIS_TOOL = {
    "type": "function",
    "function": {
        "name": _FINAL_DIAGNOSIS_TOOL_NAME,
        "description": "Submit the final closed read-only incident diagnosis.",
        "parameters": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer"},
                "diagnosis_hypothesis": {"type": "string"},
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "evidence_references": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "missing_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "recommended_playbook_name": {
                    "type": ["string", "null"],
                },
                "auto_handle_eligible": {"type": "boolean"},
                "codex_handoff_required": {"type": "boolean"},
                "remaining_risk": {"type": "string"},
            },
            "required": [
                "incident_id",
                "diagnosis_hypothesis",
                "confidence",
                "evidence_references",
                "missing_evidence",
                "recommended_playbook_name",
                "auto_handle_eligible",
                "codex_handoff_required",
                "remaining_risk",
            ],
            "additionalProperties": False,
        },
    },
}


def load_llm_proxy_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> LLMProxyConfig:
    """Load LLM proxy settings from environment variables."""

    env = dict(_load_env_file_values(env_file_paths))
    env.update(environ or os.environ)
    return LLMProxyConfig(
        base_url=env.get("TELEGRAM_KOL_LLM_BASE_URL", "http://127.0.0.1:8317"),
        api_key=env.get("TELEGRAM_KOL_LLM_API_KEY", ""),
        model=env.get("TELEGRAM_KOL_LLM_MODEL", "gpt-4.1-mini"),
        timeout_seconds=float(env.get("TELEGRAM_KOL_LLM_TIMEOUT_SECONDS", "60")),
    )


def load_runtime_agent_llm_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> LLMProxyConfig:
    """Load only the dedicated, fail-closed Runtime Agent provider."""

    paths = (
        [".env", "config/llm.env", "config/runtime_incident_agent.env"]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(_load_env_file_values(paths))
    env.update(os.environ if environ is None else environ)
    base_url = env.get(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL", ""
    ).strip()
    api_key = env.get(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY", ""
    ).strip()
    model = env.get("TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL", "").strip()
    try:
        timeout_seconds = float(
            env.get(
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_TIMEOUT_SECONDS", "30"
            )
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeAgentLLMConfigError(
            _RUNTIME_AGENT_LLM_CONFIG_ERROR
        ) from exc
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.path.rstrip("/") not in {"", "/v1"}
        or parsed_url.query
        or parsed_url.fragment
        or not api_key
        or not model
        or not math.isfinite(timeout_seconds)
    ):
        raise RuntimeAgentLLMConfigError(_RUNTIME_AGENT_LLM_CONFIG_ERROR)
    return LLMProxyConfig(
        base_url=f"{parsed_url.scheme}://{parsed_url.netloc}",
        api_key=api_key,
        model=model,
        timeout_seconds=max(5.0, min(timeout_seconds, 120.0)),
    )


def _load_env_file_values(
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    candidate_paths = env_file_paths or [
        ".env",
        "config/llm.env",
    ]
    for raw_path in candidate_paths:
        path = os.fspath(raw_path)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


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
    *, question: str, scope_context: str, model: str, system_prompt: str,
    group_prompt: str | None = None
) -> dict[str, Any]:
    """Build an OpenAI-compatible chat payload for the proxy."""

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": system_prompt.strip(),
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
    system_prompt: str,
    group_prompt: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Send a grounded chat request through an OpenAI-compatible proxy."""
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    created_client = client is None
    active_client = client or httpx.Client(timeout=config.timeout_seconds)
    try:
        data = _request_chat_completion(
            active_client=active_client,
            config=config,
            question=question,
            scope_context=scope_context,
            system_prompt=system_prompt,
            group_prompt=group_prompt,
            headers=headers,
        )
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


def request_structured_chat_turn(
    *,
    config: LLMProxyConfig,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    timeout_seconds: float | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Return one normalized tool call or final JSON object from the proxy."""

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload = {
        "model": config.model,
        "messages": messages,
    }
    if tool_schemas:
        payload.update(
            {
                "tools": tool_schemas,
                "tool_choice": "auto",
                # The runtime worker intentionally executes one bounded
                # read-only projection per turn.
                "parallel_tool_calls": False,
            }
        )
    else:
        payload.update(
            {
                "tools": [_FINAL_DIAGNOSIS_TOOL],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": _FINAL_DIAGNOSIS_TOOL_NAME},
                },
                "parallel_tool_calls": False,
            }
        )
    created_client = client is None
    active_client = client or httpx.Client(
        timeout=timeout_seconds or config.timeout_seconds
    )
    try:
        response = active_client.post(
            f"{config.base_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout_seconds or config.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
    finally:
        if created_client:
            active_client.close()

    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("structured chat response must contain one choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("structured chat response is missing a message")
    tool_calls = message.get("tool_calls")
    if tool_calls:
        if not isinstance(tool_calls, list) or not tool_calls:
            raise ValueError("structured chat response tool calls are invalid")
        # Some compatible providers ignore parallel_tool_calls=false. Serialize
        # their response by accepting only the first request; no additional
        # requested tool is executed or added to the transcript.
        tool_call = tool_calls[0]
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            raise ValueError("structured tool call is invalid")
        arguments = function.get("arguments")
        try:
            parsed_arguments = (
                json.loads(arguments) if isinstance(arguments, str) else arguments
            )
        except json.JSONDecodeError as exc:
            raise ValueError("structured tool arguments are invalid JSON") from exc
        normalized = {
            "tool_call": {
                "id": tool_call.get("id"),
                "name": function.get("name"),
                "arguments": parsed_arguments,
            }
        }
        if function.get("name") == _FINAL_DIAGNOSIS_TOOL_NAME:
            return {"final": parsed_arguments}
        return normalized
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("structured chat response has no final JSON")
    try:
        final = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("structured chat final is invalid JSON") from exc
    if not isinstance(final, dict):
        raise ValueError("structured chat final must be an object")
    return {"final": final}


def _request_chat_completion(
    *,
    active_client: httpx.Client,
    config: LLMProxyConfig,
    question: str,
    scope_context: str,
    system_prompt: str,
    group_prompt: str | None,
    headers: dict[str, str],
) -> dict[str, Any]:
    payload = build_proxy_chat_payload(
        question=question,
        scope_context=scope_context,
        model=config.model,
        system_prompt=system_prompt,
        group_prompt=group_prompt,
    )
    response = active_client.post(
        f"{config.base_url.rstrip('/')}/v1/chat/completions",
        json=payload,
        headers=headers,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if not _is_unknown_model_error(exc):
            raise
        fallback_model = _resolve_supported_model(
            active_client=active_client,
            config=config,
            headers=headers,
        )
        if not fallback_model or fallback_model == config.model:
            raise
        config.model = fallback_model
        payload = build_proxy_chat_payload(
            question=question,
            scope_context=scope_context,
            model=config.model,
            system_prompt=system_prompt,
            group_prompt=group_prompt,
        )
        response = active_client.post(
            f"{config.base_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
    return response.json()


def _is_unknown_model_error(exc: httpx.HTTPStatusError) -> bool:
    try:
        payload = exc.response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return isinstance(message, str) and "unknown provider for model" in message.lower()


def _resolve_supported_model(
    *,
    active_client: httpx.Client,
    config: LLMProxyConfig,
    headers: dict[str, str],
) -> str | None:
    response = active_client.get(
        f"{config.base_url.rstrip('/')}/v1/models",
        headers=headers,
    )
    response.raise_for_status()
    payload = response.json()
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return None
    available_models = [
        model_id
        for item in raw_models
        if isinstance(item, dict)
        for model_id in [item.get("id")]
        if isinstance(model_id, str) and model_id
    ]
    if config.model in available_models:
        return config.model

    preferred_models = (
        "gpt-5.4-mini",
        "gpt-5.4",
        "gpt-5.2",
        "gpt-4.1-mini",
        "gpt-4.1",
    )
    for candidate in preferred_models:
        if candidate in available_models:
            return candidate

    for candidate in available_models:
        if "codex" not in candidate.lower():
            return candidate

    return available_models[0] if available_models else None


def _raise_for_error_like_answer(content: str) -> None:
    lowered = content.lower()
    if "does not support image input" in lowered:
        raise httpx.HTTPError(content)

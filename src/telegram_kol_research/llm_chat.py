"""The LLM proxy the runtime incident agent talks to.

This module used to also serve the Web group-message chat. That feature's
page entry was deleted on 2026-06-14 and its endpoint logged zero calls in
the thirty days before 2026-09-14, so phase 8 removed it; what is left is
the runtime agent's own fail-closed provider and the structured tool-call
turn it takes."""

from __future__ import annotations

import os
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from telegram_kol_research.ai_endpoints import (
    chat_completions_url,
    provider_append_v1,
)
from telegram_kol_research.env_file_readability import (
    note_unreadable_config_file,
)
from telegram_kol_research.runtime_agent_contracts import (
    RuntimeAgentFinalResponseError,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMProxyConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    egress_socket_path: str | None = None
    #: The provider's ``/v1`` switch when this came from a stage chain.
    #: ``None`` -- an environment-configured proxy -- keeps the inferred rule,
    #: which is the URL this has always sent.
    append_v1: bool | None = None


class RuntimeAgentLLMConfigError(ValueError):
    """Dedicated Runtime Agent provider configuration is incomplete."""


_RUNTIME_AGENT_LLM_CONFIG_ERROR = (
    "dedicated Runtime Agent provider configuration is invalid"
)
_RUNTIME_AGENT_MODEL_EGRESS_SOCKET = (
    "/run/telegram-kol-agent-model-egress.sock"
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
    active_environment = os.environ if environ is None else environ
    dedicated_names = (
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL",
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY",
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL",
    )
    if all(str(active_environment.get(name, "")).strip() for name in dedicated_names):
        env: dict[str, str] = {}
    else:
        env = dict(_load_env_file_values(paths))
    env.update(active_environment)
    base_url = env.get(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL", ""
    ).strip()
    api_key = env.get(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY", ""
    ).strip()
    model = env.get("TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL", "").strip()
    egress_socket_path = env.get(
        "TELEGRAM_KOL_RUNTIME_AGENT_MODEL_EGRESS_SOCKET",
        _RUNTIME_AGENT_MODEL_EGRESS_SOCKET,
    ).strip()
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
        or parsed_url.netloc.lower() != "api.xiaomimimo.com"
        or parsed_url.path.rstrip("/") not in {"", "/v1"}
        or parsed_url.query
        or parsed_url.fragment
        or not api_key
        or model != "mimo-v2.5"
        or egress_socket_path != _RUNTIME_AGENT_MODEL_EGRESS_SOCKET
        or not math.isfinite(timeout_seconds)
    ):
        raise RuntimeAgentLLMConfigError(_RUNTIME_AGENT_LLM_CONFIG_ERROR)
    return LLMProxyConfig(
        base_url=f"{parsed_url.scheme}://{parsed_url.netloc}",
        api_key=api_key,
        model=model,
        timeout_seconds=max(5.0, min(timeout_seconds, 120.0)),
        egress_socket_path=egress_socket_path,
    )


def _build_runtime_agent_http_client(
    config: LLMProxyConfig,
    *,
    timeout_seconds: float,
) -> httpx.Client:
    if config.egress_socket_path != _RUNTIME_AGENT_MODEL_EGRESS_SOCKET:
        raise RuntimeAgentLLMConfigError(_RUNTIME_AGENT_LLM_CONFIG_ERROR)
    return httpx.Client(
        timeout=timeout_seconds,
        transport=httpx.HTTPTransport(uds=config.egress_socket_path),
    )


def _load_env_file_values(
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> dict[str, str]:
    """Read whatever candidate files this process can actually read.

    An env file this process cannot read is treated exactly like one that is
    not there.  Environment variables already take precedence over every file
    here, so a deployment that supplies its values through systemd loses
    nothing -- while a file it is not allowed to read used to raise straight
    through recognition and fail the message.

    Production ran that failure from 2026-09-04: the worker runs as
    ``telegram-kol-worker`` and ``config/telegram.env`` is ``root`` 0600, so
    every authoritative message that reached ``load_multi_target_management_
    config`` died on ``PermissionError`` and its attempt landed
    ``authoritative_execution_outcome_unknown``.
    """

    values: dict[str, str] = {}
    candidate_paths = (
        [
            ".env",
            "config/llm.env",
        ]
        if env_file_paths is None
        else env_file_paths
    )
    for raw_path in candidate_paths:
        path = os.fspath(raw_path)
        if not os.path.isfile(path):
            continue
        if not os.access(path, os.R_OK):
            note_unreadable_config_file(path, logger)
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if (
                        not stripped
                        or stripped.startswith("#")
                        or "=" not in stripped
                    ):
                        continue
                    key, value = stripped.split("=", 1)
                    values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            # The access check can race, and unreadable is not the only way a
            # path can refuse to open. Only the path is recorded, never content.
            note_unreadable_config_file(path, logger)
    return values


def request_structured_chat_turn(
    *,
    config: LLMProxyConfig,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    timeout_seconds: float | None = None,
    client: httpx.Client | None = None,
    max_completion_tokens: int | None = None,
    usage_callback=None,
) -> dict[str, Any]:
    """Return one normalized tool call or final JSON object from the proxy."""

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload = {
        "model": config.model,
        "messages": messages,
    }
    if max_completion_tokens is not None:
        payload["max_tokens"] = max(64, min(int(max_completion_tokens), 32768))
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
                "response_format": {"type": "json_object"},
            }
        )
    created_client = client is None
    active_client = client or _build_runtime_agent_http_client(
        config,
        timeout_seconds=timeout_seconds or config.timeout_seconds,
    )
    try:
        response = active_client.post(
            chat_completions_url(config.base_url, provider_append_v1(config)),
            json=payload,
            headers=headers,
            timeout=timeout_seconds or config.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
    finally:
        if created_client:
            active_client.close()

    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict) and usage_callback is not None:
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (prompt_tokens, completion_tokens, total_tokens)
        ) and prompt_tokens + completion_tokens == total_tokens:
            usage_callback(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

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
            if function.get("name") == _FINAL_DIAGNOSIS_TOOL_NAME:
                raise RuntimeAgentFinalResponseError(
                    "structured chat final response is invalid"
                ) from exc
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
        raise RuntimeAgentFinalResponseError(
            "structured chat final response is invalid"
        )
    try:
        final = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeAgentFinalResponseError(
            "structured chat final response is invalid"
        ) from exc
    if not isinstance(final, dict):
        raise RuntimeAgentFinalResponseError(
            "structured chat final response is invalid"
        )
    return {"final": final}

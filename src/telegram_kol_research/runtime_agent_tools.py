"""Closed registry for bounded, read-only runtime incident evidence tools."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from telegram_kol_research.runtime_agent_contracts import (
    RuntimeAgentContractError,
    validate_evidence_references,
)


READ_ONLY_RUNTIME_AGENT_TOOL_NAMES = frozenset(
    {
        "get_incident_summary",
        "get_lifecycle_state",
        "get_worker_state",
        "get_service_audit_state",
        "get_journal_summary",
        "get_exchange_snapshot",
        "compare_local_exchange",
        "get_prior_attempts",
    }
)
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|credential|cookie|session|api.?key|password|passphrase|"
    r"authorization|private.?key)",
    re.IGNORECASE,
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{12,}|authorization\s*:\s*bearer\s+\S+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


class RuntimeAgentToolError(ValueError):
    """Raised when a tool request or result fails the read-only policy."""


@dataclass(frozen=True, slots=True)
class RuntimeAgentToolResult:
    data: dict[str, Any]
    evidence_refs: tuple[str, ...]

    def as_model_payload(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "evidence_refs": list(self.evidence_refs),
        }


def _walk(value, *, depth=0):
    if depth > 8:
        raise RuntimeAgentToolError("tool output is not bounded")
    if isinstance(value, dict):
        if len(value) > 64:
            raise RuntimeAgentToolError("tool output is not bounded")
        for key, nested in value.items():
            if not isinstance(key, str):
                raise RuntimeAgentToolError("tool output keys must be text")
            if _SENSITIVE_KEY_PATTERN.search(key):
                raise RuntimeAgentToolError("tool output contains sensitive material")
            yield key
            yield from _walk(nested, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise RuntimeAgentToolError("tool output is not bounded")
        for nested in value:
            yield from _walk(nested, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > 512:
            raise RuntimeAgentToolError("tool output is not bounded")
        if _CREDENTIAL_PATTERN.search(value):
            raise RuntimeAgentToolError("tool output contains sensitive material")
        yield value
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise RuntimeAgentToolError("tool output contains unsupported values")


class RuntimeAgentToolRegistry:
    def __init__(
        self,
        *,
        providers: Mapping[str, Callable[..., Mapping[str, Any]]],
        max_output_bytes: int = 8192,
    ) -> None:
        unknown = set(providers) - READ_ONLY_RUNTIME_AGENT_TOOL_NAMES
        if unknown:
            raise RuntimeAgentToolError(f"unknown tool providers: {sorted(unknown)!r}")
        self._providers = dict(providers)
        self.max_output_bytes = max(256, min(int(max_output_bytes), 32_768))

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(self._providers)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (
                        "Return one bounded redacted read-only projection for "
                        "the current runtime incident."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "incident_id": {"type": "integer", "minimum": 1}
                        },
                        "required": ["incident_id"],
                        "additionalProperties": False,
                    },
                },
            }
            for name in sorted(self._providers)
        ]

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        expected_incident_id: int,
    ) -> RuntimeAgentToolResult:
        if name not in READ_ONLY_RUNTIME_AGENT_TOOL_NAMES:
            raise RuntimeAgentToolError("unknown tool")
        if set(arguments) != {"incident_id"} or arguments.get(
            "incident_id"
        ) != int(expected_incident_id):
            raise RuntimeAgentToolError("tool arguments do not match incident")
        provider = self._providers.get(name)
        if provider is None:
            raise RuntimeAgentToolError("tool is not configured")
        raw = provider(incident_id=int(expected_incident_id))
        if not isinstance(raw, Mapping) or set(raw) != {"data", "evidence_refs"}:
            raise RuntimeAgentToolError("tool result contract is invalid")
        data = raw["data"]
        if not isinstance(data, dict):
            raise RuntimeAgentToolError("tool data must be an object")
        tuple(_walk(data))
        try:
            references = validate_evidence_references(raw["evidence_refs"])
        except RuntimeAgentContractError as exc:
            raise RuntimeAgentToolError(str(exc)) from exc
        payload = {
            "data": data,
            "evidence_refs": list(references),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self.max_output_bytes:
            raise RuntimeAgentToolError("tool output is not bounded")
        return RuntimeAgentToolResult(data=dict(data), evidence_refs=references)

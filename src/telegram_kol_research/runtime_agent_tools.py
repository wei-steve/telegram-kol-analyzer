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
        "get_protection_summary",
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


def _safe_scalar(value: Any, *, maximum: int = 64):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:maximum]


def build_local_exchange_comparison(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Summarize bounded local and durable exchange observations coherently."""

    projected = []
    for raw in tuple(rows)[:20]:
        local_status = _safe_scalar(raw.get("local_status"))
        exchange_status = _safe_scalar(raw.get("exchange_status"))
        if exchange_status is None:
            comparison = "unknown"
        elif str(exchange_status).lower() == str(local_status).lower():
            comparison = "match"
        else:
            comparison = "mismatch"
        projected.append(
            {
                "record_id": _safe_scalar(raw.get("record_id")),
                "local_status": local_status,
                "exchange_status": exchange_status,
                "exchange_size": _safe_scalar(raw.get("exchange_size")),
                "comparison": comparison,
            }
        )
    counts = {
        label: sum(row["comparison"] == label for row in projected)
        for label in ("match", "mismatch", "unknown")
    }
    return {
        "total": len(projected),
        "comparable": counts["match"] + counts["mismatch"],
        "matches": counts["match"],
        "mismatches": counts["mismatch"],
        "unknown": counts["unknown"],
        "rows": projected,
    }


def build_worker_history_summary(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Return counts plus a bounded history without prompts or provider bodies."""

    projected = []
    status_counts: dict[str, int] = {}
    attempts_total = 0
    for raw in tuple(rows)[:10]:
        status = str(raw.get("status") or "unknown")[:32]
        attempts = raw.get("attempts", 0)
        attempts = (
            max(0, int(attempts))
            if isinstance(attempts, (int, float)) and not isinstance(attempts, bool)
            else 0
        )
        status_counts[status] = status_counts.get(status, 0) + 1
        attempts_total += attempts
        projected.append(
            {
                "record_id": _safe_scalar(raw.get("record_id")),
                "status": status,
                "attempts": attempts,
                "error_class": _safe_scalar(raw.get("error_class")),
                "updated_at": _safe_scalar(raw.get("updated_at")),
            }
        )
    return {
        "total": len(projected),
        "status_counts": dict(sorted(status_counts.items())),
        "attempts_total": attempts_total,
        "history": projected,
    }


def build_prior_attempts_summary(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Aggregate prior same-fingerprint incident generations."""

    projected = []
    agent_attempts_total = 0
    for raw in tuple(rows)[:10]:
        attempts = raw.get("agent_attempt_count", 0)
        attempts = (
            max(0, int(attempts))
            if isinstance(attempts, (int, float)) and not isinstance(attempts, bool)
            else 0
        )
        agent_attempts_total += attempts
        projected.append(
            {
                "incident_id": _safe_scalar(raw.get("incident_id")),
                "generation": _safe_scalar(raw.get("generation")),
                "status": _safe_scalar(raw.get("status")),
                "recovery_status": _safe_scalar(raw.get("recovery_status")),
                "agent_attempt_count": attempts,
            }
        )
    return {
        "total": len(projected),
        "diagnosed": sum(row["status"] == "diagnosed" for row in projected),
        "escalated": sum(row["status"] == "escalated" for row in projected),
        "agent_attempts_total": agent_attempts_total,
        "attempts": projected,
    }


def build_protection_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Project protection evidence without returning order identifiers."""

    def count(name: str) -> int:
        value = evidence.get(name)
        return min(len(value), 20) if isinstance(value, list) else 0

    take_profit_count = count("take_profit_order_ids")
    stop_loss_count = count("stop_loss_order_ids")
    missing_stop = evidence.get("missing_stop_loss")
    missing_take_profit = evidence.get("missing_take_profit")
    stop_evidence_available = (
        "missing_stop_loss" in evidence or "stop_loss_order_ids" in evidence
    )
    take_profit_evidence_available = (
        "missing_take_profit" in evidence
        or "take_profit_order_ids" in evidence
    )
    return {
        "reason_code": _safe_scalar(evidence.get("reason_code")),
        "missing_stop_loss": (
            (
                bool(missing_stop)
                if missing_stop is not None
                else stop_loss_count == 0
            )
            if stop_evidence_available
            else None
        ),
        "missing_take_profit": (
            (
                bool(missing_take_profit)
                if missing_take_profit is not None
                else take_profit_count == 0
            )
            if take_profit_evidence_available
            else None
        ),
        "take_profit_count": take_profit_count,
        "stop_loss_count": stop_loss_count,
        "position_size_present": evidence.get("position_size") not in (None, ""),
    }


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

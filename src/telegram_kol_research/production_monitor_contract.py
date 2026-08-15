"""Closed, secret-free contract shared by the monitor-v2 loopback peers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any


MONITOR_SCHEMA_VERSION = 2
MONITOR_MAX_PROJECTION_BYTES = 4096
MONITOR_MAX_TIMESTAMP_LENGTH = 40

MONITOR_ADAPTER_NAMES = frozenset(
    {
        "service",
        "head",
        "settings",
        "journal",
        "events",
        "audit",
        "exchange_snapshot",
        "composite",
        "entry_preamble",
        "coverage",
        "readiness",
    }
)
MONITOR_POLICY_NAMES = frozenset(
    {"IMMEDIATE", "SETTLING", "EVIDENCE_UNKNOWN"}
)
SENTINEL_REASON_CODES = frozenset(
    {
        "adapter_failure",
        "adjacent_entry_invariant_scan_incomplete",
        "authoritative_processor_required",
        "audit_abnormal",
        "audit_incomplete",
        "auto_trade_enabled_drift",
        "completed_batch_missing_component_evidence",
        "composite_position_without_verified_stop",
        "consumed_entry_fragment_missing_assembly",
        "contract_violation_missing_stage1",
        "duplicate_composite_close_submission",
        "duplicate_manual_close",
        "entry_message_assembly_v2_mode_drift",
        "entry_preamble_ambiguous",
        "entry_preamble_mode_drift",
        "entry_revision_replacement_before_old_terminal",
        "entry_revision_risk_budget_exceeded",
        "entry_revision_v2_mode_drift",
        "exchange_snapshot_incomplete",
        "exchange_snapshot_stale",
        "exchange_snapshot_temporally_incoherent",
        "exchange_snapshot_unavailable",
        "event_recovery_status",
        "event_unknown_status",
        "executable_message_missing_contract",
        "instruction_execution_contradiction",
        "journal_errors",
        "live_entry_assembly_binding_evidence_missing",
        "live_entry_preamble_binding_evidence_missing",
        "live_entry_revision_protection_unverified",
        "live_position_retained_tp_oversized",
        "malformed_snapshot",
        "management_execution_mode_drift",
        "max_concurrent_positions_drift",
        "message_operation_coverage_incomplete",
        "message_operation_incident_missing_terminal",
        "message_operation_supervisor_policy_invalid",
        "message_operation_supervisor_stale",
        "monitor_clock_rollback",
        "readiness_unavailable",
        "reviewed_sha_drift",
        "sentinel_timer_late",
        "service_inactive",
        "service_starting",
        "snapshot_refresh_overlap",
        "stale_adjacent_entry_admission",
        "stale_entry_preamble_unresolved",
        "stalled_composite_component",
        "state_invalid",
    }
)
MONITOR_EXECUTION_STATUSES = frozenset({"COMPLETED", "FAILED"})
MONITOR_OBSERVED_HEALTH_STATUSES = frozenset(
    {"HEALTHY", "UNHEALTHY", "UNKNOWN"}
)
MONITOR_FALLBACK_REASONS = frozenset(
    {
        "incident_intake_unavailable",
        "deterministic_notification_unavailable",
    }
)
MONITOR_PROJECTION_V2_FIELDS = frozenset(
    {
        "schema_version",
        "submission_id",
        "checked_at",
        "observation_generation",
        "anomaly_fingerprint",
        "execution_status",
        "observed_health",
        "reason_codes",
        "adapter_failures",
        "fallback_reason",
    }
)
MONITOR_PROJECTION_V2_INPUT_FIELDS = MONITOR_PROJECTION_V2_FIELDS - {
    "schema_version",
    "submission_id",
}
MONITOR_MAX_REASON_CODES = len(SENTINEL_REASON_CODES)
MONITOR_MAX_ADAPTER_FAILURES = len(MONITOR_ADAPTER_NAMES)

# Phase-one only: the active legacy producer and receiver remain deliberately
# frozen to their original six adapters.  V2's wider authority must never leak
# into a schema-v1 payload while the old production timer is still active.
LEGACY_MONITOR_ADAPTER_NAMES_V1 = frozenset(
    {"service", "head", "settings", "journal", "events", "audit"}
)
LEGACY_MONITOR_REASON_CODES_V1 = frozenset(
    {"adapter_failure", "audit_incomplete"}
)
LEGACY_MONITOR_NOTIFICATION_ERRORS_V1 = frozenset(
    {"notification_config_missing", "notification_delivery_failed"}
)
LEGACY_MONITOR_PROJECTION_V1_FIELDS = frozenset(
    {
        "schema_version",
        "checked_at",
        "reason_codes",
        "adapter_failures",
        "notification_error",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _closed_sequence(
    value: object,
    *,
    field: str,
    allowed: frozenset[str],
    maximum: int,
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise ValueError(f"{field} must be a sequence")
    items = list(value)
    if len(items) > maximum:
        raise ValueError(f"too many {field.replace('_', ' ')}")
    if any(not isinstance(item, str) or item not in allowed for item in items):
        raise ValueError(f"invalid {field}")
    if len(set(items)) != len(items):
        raise ValueError(f"duplicate {field}")
    return sorted(items)


def _canonical_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= MONITOR_MAX_TIMESTAMP_LENGTH:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("checked_at is invalid") from exc
    else:
        raise ValueError("checked_at is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    canonical = parsed.astimezone(UTC).isoformat()
    if len(canonical) > MONITOR_MAX_TIMESTAMP_LENGTH:
        raise ValueError("checked_at is invalid")
    return canonical


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _projection_without_id(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != MONITOR_PROJECTION_V2_INPUT_FIELDS:
        raise ValueError("invalid projection input fields")
    execution_status = value["execution_status"]
    observed_health = value["observed_health"]
    fallback_reason = value["fallback_reason"]
    observation_generation = value["observation_generation"]
    anomaly_fingerprint = value["anomaly_fingerprint"]
    if (
        type(observation_generation) is not int
        or observation_generation < 1
    ):
        raise ValueError("invalid observation_generation")
    if (
        not isinstance(anomaly_fingerprint, str)
        or _SHA256.fullmatch(anomaly_fingerprint) is None
    ):
        raise ValueError("invalid anomaly_fingerprint")
    if (
        not isinstance(execution_status, str)
        or execution_status not in MONITOR_EXECUTION_STATUSES
    ):
        raise ValueError("invalid execution_status")
    if (
        not isinstance(observed_health, str)
        or observed_health not in MONITOR_OBSERVED_HEALTH_STATUSES
    ):
        raise ValueError("invalid observed_health")
    if fallback_reason is not None and (
        not isinstance(fallback_reason, str)
        or fallback_reason not in MONITOR_FALLBACK_REASONS
    ):
        raise ValueError("invalid fallback_reason")
    reason_codes = _closed_sequence(
        value["reason_codes"],
        field="reason_codes",
        allowed=SENTINEL_REASON_CODES,
        maximum=MONITOR_MAX_REASON_CODES,
    )
    adapter_failures = _closed_sequence(
        value["adapter_failures"],
        field="adapter_failures",
        allowed=MONITOR_ADAPTER_NAMES,
        maximum=MONITOR_MAX_ADAPTER_FAILURES,
    )
    if observed_health == "HEALTHY" and (
        execution_status != "COMPLETED"
        or reason_codes
        or adapter_failures
        or fallback_reason is not None
    ):
        raise ValueError("HEALTHY projection is semantically inconsistent")
    return {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "checked_at": _canonical_timestamp(value["checked_at"]),
        "observation_generation": observation_generation,
        "anomaly_fingerprint": anomaly_fingerprint,
        "execution_status": execution_status,
        "observed_health": observed_health,
        "reason_codes": reason_codes,
        "adapter_failures": adapter_failures,
        "fallback_reason": fallback_reason,
    }


def build_monitor_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Build one canonical v2 projection and its deterministic identity."""

    if not isinstance(value, Mapping):
        raise ValueError("projection input must be a mapping")
    canonical = _projection_without_id(value)
    submission_id = hashlib.sha256(_canonical_bytes(canonical)).hexdigest()
    return {
        "schema_version": canonical["schema_version"],
        "submission_id": submission_id,
        "checked_at": canonical["checked_at"],
        "observation_generation": canonical["observation_generation"],
        "anomaly_fingerprint": canonical["anomaly_fingerprint"],
        "execution_status": canonical["execution_status"],
        "observed_health": canonical["observed_health"],
        "reason_codes": canonical["reason_codes"],
        "adapter_failures": canonical["adapter_failures"],
        "fallback_reason": canonical["fallback_reason"],
    }


def parse_monitor_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate a canonical v2 projection from an untrusted peer."""

    if not isinstance(value, Mapping) or set(value) != MONITOR_PROJECTION_V2_FIELDS:
        raise ValueError("invalid projection fields")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != MONITOR_SCHEMA_VERSION
    ):
        raise ValueError("invalid schema_version")
    submission_id = value["submission_id"]
    if not isinstance(submission_id, str) or _SHA256.fullmatch(submission_id) is None:
        raise ValueError("invalid submission_id")
    input_value = {
        field: value[field] for field in MONITOR_PROJECTION_V2_INPUT_FIELDS
    }
    expected = build_monitor_projection(input_value)
    if value["reason_codes"] != expected["reason_codes"] or value[
        "adapter_failures"
    ] != expected["adapter_failures"]:
        raise ValueError("projection lists are not canonical")
    if value["checked_at"] != expected["checked_at"]:
        raise ValueError("checked_at is not canonical")
    if not hmac.compare_digest(submission_id, expected["submission_id"]):
        raise ValueError("submission_id does not match projection")
    return expected


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def parse_monitor_projection_json(body: bytes) -> dict[str, Any]:
    """Decode one bounded JSON body while preserving duplicate-key rejection."""

    if not isinstance(body, bytes) or not (
        1 <= len(body) <= MONITOR_MAX_PROJECTION_BYTES
    ):
        raise ValueError("invalid projection body size")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid projection JSON") from exc
    return parse_monitor_projection(value)

import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research.production_monitor_contract import (
    LEGACY_MONITOR_ADAPTER_NAMES_V1,
    MONITOR_ADAPTER_NAMES,
    MONITOR_MAX_PROJECTION_BYTES,
    MONITOR_POLICY_NAMES,
    MONITOR_PROJECTION_V2_FIELDS,
    SENTINEL_REASON_CODES,
    build_monitor_projection,
    parse_monitor_projection,
    parse_monitor_projection_json,
)


def _projection_input(**overrides):
    value = {
        "checked_at": datetime(2026, 8, 14, 20, 0, tzinfo=UTC),
        "observation_generation": 7,
        "anomaly_fingerprint": "f" * 64,
        "execution_status": "COMPLETED",
        "observed_health": "UNHEALTHY",
        "reason_codes": ["audit_incomplete", "adapter_failure"],
        "adapter_failures": ["coverage", "composite"],
        "fallback_reason": None,
    }
    value.update(overrides)
    return value


def test_v2_contract_has_one_adapter_authority():
    assert MONITOR_ADAPTER_NAMES == frozenset(
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
    assert LEGACY_MONITOR_ADAPTER_NAMES_V1 == frozenset(
        {"service", "head", "settings", "journal", "events", "audit"}
    )


def test_v2_contract_has_closed_policy_and_reason_names():
    assert MONITOR_POLICY_NAMES == frozenset(
        {"IMMEDIATE", "SETTLING", "EVIDENCE_UNKNOWN"}
    )
    assert SENTINEL_REASON_CODES == frozenset(
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


def test_projection_submission_id_is_deterministic_and_lists_are_canonical():
    first = build_monitor_projection(_projection_input())
    second = build_monitor_projection(
        _projection_input(
            reason_codes=["adapter_failure", "audit_incomplete"],
            adapter_failures=["composite", "coverage"],
        )
    )

    assert first == second
    assert set(first) == MONITOR_PROJECTION_V2_FIELDS
    assert first["reason_codes"] == ["adapter_failure", "audit_incomplete"]
    assert first["adapter_failures"] == ["composite", "coverage"]
    assert len(first["submission_id"]) == 64


@pytest.mark.parametrize("generation", [None, True, 0, -1, 1.0, "1"])
def test_v2_projection_requires_exact_positive_observation_generation(generation):
    with pytest.raises(ValueError, match="observation_generation"):
        build_monitor_projection(
            _projection_input(observation_generation=generation)
        )


@pytest.mark.parametrize(
    "fingerprint",
    [None, "", "a" * 63, "A" * 64, "g" * 64, 7],
)
def test_v2_projection_requires_exact_sha256_anomaly_fingerprint(fingerprint):
    with pytest.raises(ValueError, match="anomaly_fingerprint"):
        build_monitor_projection(
            _projection_input(anomaly_fingerprint=fingerprint)
        )


def test_v2_submission_id_binds_generation_and_anomaly_fingerprint():
    baseline = build_monitor_projection(_projection_input())
    next_generation = build_monitor_projection(
        _projection_input(observation_generation=8)
    )
    changed_anomaly = build_monitor_projection(
        _projection_input(anomaly_fingerprint="e" * 64)
    )

    assert baseline["submission_id"] != next_generation["submission_id"]
    assert baseline["submission_id"] != changed_anomaly["submission_id"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason_codes", ["adapter_failure", "adapter_failure"]),
        ("adapter_failures", ["coverage", "coverage"]),
    ],
)
def test_projection_rejects_duplicate_closed_values(field, value):
    with pytest.raises(ValueError, match="duplicate"):
        build_monitor_projection(_projection_input(**{field: value}))


def test_projection_rejects_values_beyond_closed_maximums():
    with pytest.raises(ValueError, match="too many reason codes"):
        build_monitor_projection(
            _projection_input(reason_codes=sorted(SENTINEL_REASON_CODES) + ["future"])
        )
    with pytest.raises(ValueError, match="too many adapter failures"):
        build_monitor_projection(
            _projection_input(
                adapter_failures=sorted(MONITOR_ADAPTER_NAMES) + ["future"]
            )
        )


def test_parser_rejects_unknown_fields_and_noncanonical_order():
    projection = build_monitor_projection(_projection_input())
    with pytest.raises(ValueError, match="fields"):
        parse_monitor_projection({**projection, "unexpected": True})
    with pytest.raises(ValueError, match="canonical"):
        parse_monitor_projection(
            {**projection, "reason_codes": list(reversed(projection["reason_codes"]))}
        )


def test_parser_compares_submission_id_against_canonical_payload():
    projection = build_monitor_projection(_projection_input())
    assert parse_monitor_projection(projection) == projection

    with pytest.raises(ValueError, match="submission_id"):
        parse_monitor_projection({**projection, "submission_id": "0" * 64})


@pytest.mark.parametrize("schema_version", [True, 2.0, "2"])
def test_parser_requires_an_exact_integer_schema_version(schema_version):
    projection = build_monitor_projection(_projection_input())

    with pytest.raises(ValueError, match="schema_version"):
        parse_monitor_projection(
            {**projection, "schema_version": schema_version}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_status", []),
        ("observed_health", {}),
        ("fallback_reason", []),
    ],
)
def test_builder_rejects_unhashable_enum_values_as_invalid(field, value):
    with pytest.raises(ValueError, match=field):
        build_monitor_projection(_projection_input(**{field: value}))


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "execution_status": "FAILED",
            "reason_codes": [],
            "adapter_failures": [],
            "fallback_reason": None,
        },
        {
            "execution_status": "COMPLETED",
            "reason_codes": ["service_inactive"],
            "adapter_failures": [],
            "fallback_reason": None,
        },
        {
            "execution_status": "COMPLETED",
            "reason_codes": [],
            "adapter_failures": ["service"],
            "fallback_reason": None,
        },
        {
            "execution_status": "COMPLETED",
            "reason_codes": [],
            "adapter_failures": [],
            "fallback_reason": "incident_intake_unavailable",
        },
    ],
)
def test_healthy_projection_rejects_semantic_contradictions(overrides):
    with pytest.raises(ValueError, match="HEALTHY projection"):
        build_monitor_projection(
            _projection_input(observed_health="HEALTHY", **overrides)
        )


def test_json_parser_rejects_duplicate_keys_and_oversized_body():
    with pytest.raises(ValueError, match="duplicate"):
        parse_monitor_projection_json(
            b'{"schema_version":2,"schema_version":2}'
        )
    with pytest.raises(ValueError, match="size"):
        parse_monitor_projection_json(b" " * (MONITOR_MAX_PROJECTION_BYTES + 1))


def test_json_parser_round_trips_canonical_projection():
    projection = build_monitor_projection(_projection_input())
    encoded = json.dumps(projection, separators=(",", ":")).encode("utf-8")

    assert parse_monitor_projection_json(encoded) == projection

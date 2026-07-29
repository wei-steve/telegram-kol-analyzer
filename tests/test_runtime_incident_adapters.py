from __future__ import annotations

from datetime import UTC, datetime

import pytest

from telegram_kol_research.config import (
    RuntimeIncidentConfig,
    load_runtime_incident_config,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident
from telegram_kol_research.runtime_incident_adapters import (
    capture_runtime_incident_best_effort,
    capture_context_worker_state,
    capture_management_state,
    capture_monitor_state,
    capture_notification_failure,
    capture_protection_state,
    capture_provider_failure,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _enabled(*incident_types: str) -> RuntimeIncidentConfig:
    if incident_types == ("*",):
        incident_types = (
            "context_worker_exhausted",
            "management_submit_unknown",
            "management_partial_failed",
            "management_recovery_required",
            "monitor_adapter_failure",
            "monitor_audit_incomplete",
            "notification_delivery_failure",
            "provider_retry_exhausted",
            "severe_protection_incident",
        )
    return RuntimeIncidentConfig(capture_types=frozenset(incident_types))


def _rows(session_factory) -> list[RuntimeIncident]:
    with session_factory() as session:
        return session.query(RuntimeIncident).order_by(RuntimeIncident.id).all()


def test_runtime_incident_flags_are_dormant_by_default_and_parse_allowlist():
    default = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES": (
                "context_worker_exhausted,monitor_audit_incomplete"
            ),
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_AGENT_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_AGENT_MAX_TOOL_STEPS": "99",
            "TELEGRAM_KOL_RUNTIME_AGENT_MAX_WALL_SECONDS": "12",
        },
        env_file_paths=[],
    )

    assert default.capture_types == frozenset()
    assert default.telegram_notifications_enabled is False
    assert default.agent_enabled is False
    assert enabled.capture_types == frozenset(
        {"context_worker_exhausted", "monitor_audit_incomplete"}
    )
    assert enabled.telegram_notifications_enabled is True
    assert enabled.agent_enabled is True
    assert enabled.agent_max_tool_steps == 4
    assert enabled.agent_max_wall_seconds == 12.0
    assert enabled.prompt_version == "runtime-agent-prompt-v3"
    assert enabled.tool_policy_version == "runtime-agent-tools-v2"
    assert RuntimeIncidentConfig(
        capture_types=frozenset({"*"})
    ).captures("management_recovery_required") is False


@pytest.mark.parametrize("status", ["unresolved", "hold", "pending_reanalysis"])
def test_contextual_business_outcomes_never_create_runtime_incidents(
    tmp_path,
    status,
):
    session_factory = create_session_factory(tmp_path / "context-excluded.db")

    captured = capture_context_worker_state(
        session_factory,
        config=_enabled("*"),
        attempt_id=41,
        raw_message_id=92,
        status=status,
        occurred_at=NOW,
        error_type=None,
    )

    assert captured is None
    assert _rows(session_factory) == []


def test_context_worker_exhaustion_is_deduplicated_from_stable_source_state(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "context-exhausted.db")
    config = _enabled("context_worker_exhausted")

    first = capture_context_worker_state(
        session_factory,
        config=config,
        attempt_id=41,
        raw_message_id=92,
        status="exhausted",
        occurred_at=NOW,
        error_type="TimeoutError",
    )
    second = capture_context_worker_state(
        session_factory,
        config=config,
        attempt_id=41,
        raw_message_id=92,
        status="exhausted",
        occurred_at=NOW,
        error_type="TimeoutError",
    )

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert second.repeat_count == 2
    assert second.incident_type == "context_worker_exhausted"
    assert second.source_record_id == "41"


@pytest.mark.parametrize(
    ("status", "incident_type", "severity"),
    [
        ("submit_unknown", "management_submit_unknown", "critical"),
        ("partial_failed", "management_partial_failed", "high"),
        ("recovery_required", "management_recovery_required", "critical"),
    ],
)
def test_management_runtime_states_map_to_fixed_incident_types(
    tmp_path,
    status,
    incident_type,
    severity,
):
    session_factory = create_session_factory(tmp_path / f"{status}.db")

    captured = capture_management_state(
        session_factory,
        config=_enabled("*"),
        batch_id=71,
        status=status,
        reason_code="fixed_reason",
        occurred_at=NOW,
    )

    assert captured is not None
    assert captured.incident_type == incident_type
    assert captured.severity == severity
    assert captured.source_kind == "strategy_management_batch"


@pytest.mark.parametrize("status", ["succeeded", "blocked", "unresolved"])
def test_non_runtime_management_states_are_ignored(tmp_path, status):
    session_factory = create_session_factory(tmp_path / f"ignored-{status}.db")

    captured = capture_management_state(
        session_factory,
        config=_enabled("*"),
        batch_id=71,
        status=status,
        reason_code="ordinary_outcome",
        occurred_at=NOW,
    )

    assert captured is None
    assert _rows(session_factory) == []


@pytest.mark.parametrize(
    ("reason_codes", "expected_type"),
    [
        (("adapter_failure",), "monitor_adapter_failure"),
        (("audit_incomplete",), "monitor_audit_incomplete"),
    ],
)
def test_monitor_technical_failures_create_runtime_incidents(
    tmp_path,
    reason_codes,
    expected_type,
):
    session_factory = create_session_factory(tmp_path / f"{expected_type}.db")

    captured = capture_monitor_state(
        session_factory,
        config=_enabled("*"),
        checked_at=NOW,
        reason_codes=reason_codes,
        adapter_failures=("audit",),
    )

    assert [row.incident_type for row in captured] == [expected_type]


def test_monitor_ordinary_audit_abnormality_is_not_a_technical_incident(tmp_path):
    session_factory = create_session_factory(tmp_path / "audit-abnormal.db")

    captured = capture_monitor_state(
        session_factory,
        config=_enabled("*"),
        checked_at=NOW,
        reason_codes=("audit_abnormal",),
        adapter_failures=(),
    )

    assert captured == ()
    assert _rows(session_factory) == []


def test_only_severe_protection_incidents_are_captured(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection.db")

    low = capture_protection_state(
        session_factory,
        config=_enabled("*"),
        source_record_id="protection-1",
        severity="medium",
        reason_code="temporary_gap",
        occurred_at=NOW,
    )
    high = capture_protection_state(
        session_factory,
        config=_enabled("*"),
        source_record_id="protection-2",
        severity="high",
        reason_code="protection_recovery_required",
        occurred_at=NOW,
    )

    assert low is None
    assert high is not None
    assert high.incident_type == "severe_protection_incident"


def test_provider_and_notification_failures_store_only_bounded_error_type(tmp_path):
    session_factory = create_session_factory(tmp_path / "failures.db")

    provider = capture_provider_failure(
        session_factory,
        config=_enabled("*"),
        source_kind="semantic_review",
        source_record_id="raw-8",
        provider_status="retry_exhausted",
        error_type="Authorization: bearer secret-value",
        occurred_at=NOW,
    )
    notification = capture_notification_failure(
        session_factory,
        config=_enabled("*"),
        source_kind="strategy_management_notification",
        source_record_id="17",
        error_type="ConnectError",
        occurred_at=NOW,
    )

    assert provider is not None
    assert notification is not None
    assert "secret-value" not in provider.redacted_summary
    assert "ConnectError" in notification.redacted_summary


def test_disabled_or_failing_capture_never_changes_or_raises_to_source_flow(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "best-effort.db")
    source_state = {"status": "partial_failed", "reason": "already_persisted"}

    disabled = capture_management_state(
        session_factory,
        config=RuntimeIncidentConfig(),
        batch_id=71,
        status=source_state["status"],
        reason_code=source_state["reason"],
        occurred_at=NOW,
    )
    failed = capture_management_state(
        session_factory,
        config=_enabled("*"),
        batch_id=71,
        status=source_state["status"],
        reason_code=source_state["reason"],
        occurred_at=NOW,
        recorder=lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("ledger unavailable")
        ),
    )

    assert disabled is None
    assert failed is None
    assert source_state == {
        "status": "partial_failed",
        "reason": "already_persisted",
    }
    assert _rows(session_factory) == []


@pytest.mark.parametrize("failure_site", ["config", "adapter"])
def test_entire_source_adapter_boundary_fails_open(failure_site):
    source_state = {"status": "already_committed"}
    calls = []

    def load_config():
        if failure_site == "config":
            raise OSError("configuration unavailable")
        return _enabled("management_partial_failed")

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        if failure_site == "adapter":
            raise RuntimeError("adapter unavailable")
        raise AssertionError("capture must not run after config failure")

    result = capture_runtime_incident_best_effort(
        capture,
        "session-factory",
        config_loader=load_config,
        batch_id=71,
        status="partial_failed",
    )

    assert result is None
    assert source_state == {"status": "already_committed"}
    assert len(calls) == (0 if failure_site == "config" else 1)

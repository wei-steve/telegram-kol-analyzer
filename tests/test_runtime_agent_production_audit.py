from __future__ import annotations

import pytest

from telegram_kol_research.runtime_agent_production_audit import (
    RuntimeAgentProductionAuditError,
    RuntimeAgentProductionAuditRefresh,
)


def _audit(**overrides):
    value = {
        "snapshot_status": "stable",
        "snapshot_validation": "ok",
        "output_complete": True,
        "malformed_row_count": 0,
        "counts": {
            "batches_total": 80,
            "submit_unknown": 0,
            "partial_failed": 1,
            "recovery_required": 5,
        },
    }
    value.update(overrides)
    return value


def test_rerun_captures_one_bounded_complete_audit_proof():
    calls = []
    refresh = RuntimeAgentProductionAuditRefresh(
        runner=lambda: calls.append("run") or _audit()
    )

    assert refresh.rerun(
        incident_id=9,
        idempotency_key="runtime-incident:9:audit:v1",
        expected_fingerprint="a" * 64,
    )
    verification = refresh.consume_verification(incident_id=9)

    assert calls == ["run"]
    assert verification == {
        "available": True,
        "audit_run_completed": True,
        "complete": True,
        "monitor_error": None,
        "snapshot_status": "stable",
        "snapshot_validation": "ok",
        "output_complete": True,
        "malformed_row_count": 0,
        "counts": {
            "batches_total": 80,
            "submit_unknown": 0,
            "partial_failed": 1,
            "recovery_required": 5,
        },
    }
    assert refresh.has_capture(9) is False
    assert refresh.consume_verification(incident_id=9) is None


def test_historical_abnormal_counts_do_not_make_a_complete_audit_incomplete():
    refresh = RuntimeAgentProductionAuditRefresh(
        runner=lambda: _audit(
            counts={
                "batches_total": 80,
                "submit_unknown": 0,
                "partial_failed": 1,
                "recovery_required": 5,
                "unexpected_secret": "omitted",
            }
        )
    )

    refresh.rerun(
        incident_id=11,
        idempotency_key="runtime-incident:11:audit:v1",
        expected_fingerprint="b" * 64,
    )
    verification = refresh.consume_verification(incident_id=11)

    assert verification["complete"] is True
    assert verification["monitor_error"] is None
    assert set(verification["counts"]) == {
        "batches_total",
        "submit_unknown",
        "partial_failed",
        "recovery_required",
    }


@pytest.mark.parametrize(
    ("overrides", "error"),
    (
        ({"snapshot_status": "snapshot_unstable"}, "audit_incomplete"),
        ({"snapshot_validation": "not_run"}, "audit_incomplete"),
        ({"output_complete": False}, "audit_incomplete"),
        ({"malformed_row_count": 1}, "audit_incomplete"),
    ),
)
def test_incomplete_audit_is_captured_for_fail_closed_verification(
    overrides,
    error,
):
    refresh = RuntimeAgentProductionAuditRefresh(
        runner=lambda: _audit(**overrides)
    )

    refresh.rerun(
        incident_id=12,
        idempotency_key="runtime-incident:12:audit:v1",
        expected_fingerprint="c" * 64,
    )
    verification = refresh.consume_verification(incident_id=12)

    assert verification["audit_run_completed"] is True
    assert verification["complete"] is False
    assert verification["monitor_error"] == error


@pytest.mark.parametrize(
    "result",
    (
        None,
        {},
        _audit(counts=[]),
        _audit(counts={"batches_total": -1}),
        _audit(malformed_row_count=True),
        _audit(output_complete="yes"),
    ),
)
def test_malformed_or_unbounded_audit_result_is_rejected(result):
    refresh = RuntimeAgentProductionAuditRefresh(runner=lambda: result)

    with pytest.raises(RuntimeAgentProductionAuditError):
        refresh.rerun(
            incident_id=13,
            idempotency_key="runtime-incident:13:audit:v1",
            expected_fingerprint="d" * 64,
        )

    assert refresh.has_capture(13) is False


def test_invalid_executor_identity_fails_before_running_audit():
    calls = []
    refresh = RuntimeAgentProductionAuditRefresh(
        runner=lambda: calls.append("run") or _audit()
    )

    with pytest.raises(RuntimeAgentProductionAuditError):
        refresh.rerun(
            incident_id=0,
            idempotency_key="",
            expected_fingerprint="not-a-fingerprint",
        )

    assert calls == []


def test_runner_failure_is_reduced_to_a_generic_error():
    def unavailable():
        raise OSError("sensitive path")

    refresh = RuntimeAgentProductionAuditRefresh(runner=unavailable)

    with pytest.raises(
        RuntimeAgentProductionAuditError,
        match="production audit unavailable",
    ):
        refresh.rerun(
            incident_id=14,
            idempotency_key="runtime-incident:14:audit:v1",
            expected_fingerprint="e" * 64,
        )


def test_ephemeral_audit_captures_are_bounded_to_32():
    refresh = RuntimeAgentProductionAuditRefresh(runner=_audit)

    for incident_id in range(1, 34):
        refresh.rerun(
            incident_id=incident_id,
            idempotency_key=f"runtime-incident:{incident_id}:audit:v1",
            expected_fingerprint="f" * 64,
        )

    assert refresh.has_capture(1) is False
    assert refresh.has_capture(2) is True
    assert refresh.has_capture(33) is True


from __future__ import annotations

import inspect

import pytest

from telegram_kol_research.deployment_work_evidence import (
    MAX_EVIDENCE_COUNT,
    DeploymentEvidenceCounts,
    decide_deployment,
)


@pytest.mark.parametrize(
    ("counts", "writer_changed", "expected_decision", "expected_reason"),
    [
        (
            DeploymentEvidenceCounts(active_write=1),
            False,
            "BLOCK",
            "active_exchange_write",
        ),
        (
            DeploymentEvidenceCounts(unknown_outcome=1),
            False,
            "BLOCK",
            "unknown_exchange_outcome",
        ),
        (
            DeploymentEvidenceCounts(invalid_evidence=1),
            False,
            "BLOCK",
            "invalid_registered_evidence",
        ),
        (
            DeploymentEvidenceCounts(queued_work=1),
            True,
            "BLOCK",
            "writer_changed_with_queued_work",
        ),
        (
            DeploymentEvidenceCounts(queued_work=1),
            False,
            "WARN",
            "queued_work_with_unchanged_writer",
        ),
        (DeploymentEvidenceCounts(inactive=9), False, "PASS", None),
    ],
)
def test_decision_matrix(
    counts: DeploymentEvidenceCounts,
    writer_changed: bool,
    expected_decision: str,
    expected_reason: str | None,
) -> None:
    result = decide_deployment(counts=counts, writer_changed=writer_changed)

    assert result.decision == expected_decision
    if expected_reason is None:
        assert result.reason_codes == ()
    else:
        assert expected_reason in result.reason_codes


def test_blocking_reasons_follow_fixed_safety_order() -> None:
    result = decide_deployment(
        counts=DeploymentEvidenceCounts(
            active_write=1,
            unknown_outcome=1,
            queued_work=1,
            invalid_evidence=1,
        ),
        writer_changed=True,
    )

    assert result.decision == "BLOCK"
    assert result.reason_codes == (
        "invalid_registered_evidence",
        "active_exchange_write",
        "unknown_exchange_outcome",
        "writer_changed_with_queued_work",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_write", -1),
        ("unknown_outcome", True),
        ("queued_work", MAX_EVIDENCE_COUNT + 1),
        ("inactive", 1.5),
        ("invalid_evidence", "1"),
    ],
)
def test_counts_reject_invalid_values(field: str, value: object) -> None:
    values = {
        "active_write": 0,
        "unknown_outcome": 0,
        "queued_work": 0,
        "inactive": 0,
        "invalid_evidence": 0,
    }
    values[field] = value

    with pytest.raises(ValueError, match="evidence_count_invalid"):
        DeploymentEvidenceCounts(**values)


def test_counts_reject_unknown_fields() -> None:
    with pytest.raises(TypeError):
        DeploymentEvidenceCounts(unregistered=1)


def test_decision_rejects_non_boolean_writer_change() -> None:
    with pytest.raises(ValueError, match="writer_changed_invalid"):
        decide_deployment(
            counts=DeploymentEvidenceCounts(),
            writer_changed=1,
        )


def test_decision_has_no_operator_override_or_time_inputs() -> None:
    parameters = inspect.signature(decide_deployment).parameters

    assert set(parameters) == {"counts", "writer_changed"}
    assert not {"override", "change_class", "created_at", "updated_at"} & set(
        parameters
    )

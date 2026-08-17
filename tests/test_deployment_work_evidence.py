from __future__ import annotations

import pytest

from telegram_kol_research.deployment_work_evidence import (
    DeploymentWorkEvidenceError,
    classify_deployment_work,
)


@pytest.mark.parametrize("change_class", ["code", "schema_compatible"])
def test_restart_safe_wait_warns_for_non_writer_changes(change_class):
    result = classify_deployment_work(
        counts={"restart_safe_wait": {"management_batches": 1}},
        change_class=change_class,
    )

    assert result.blocking_reason_codes == ()
    assert result.warning_reason_codes == ("deployment_restart_safe_wait",)


@pytest.mark.parametrize("change_class", ["execution_writer", "live_promotion"])
def test_restart_safe_wait_blocks_writer_changes(change_class):
    result = classify_deployment_work(
        counts={"restart_safe_wait": {"management_batches": 1}},
        change_class=change_class,
    )

    assert result.blocking_reason_codes == ("deployment_restart_safe_wait",)
    assert result.warning_reason_codes == ()


@pytest.mark.parametrize(
    ("classification", "reason"),
    [
        ("in_flight_write", "deployment_in_flight_write"),
        ("unknown_outcome", "deployment_unknown_outcome"),
        ("malformed", "deployment_evidence_malformed"),
    ],
)
def test_hard_safety_facts_always_block(classification, reason):
    result = classify_deployment_work(
        counts={classification: {"execution_order_legs": 1}},
        change_class="code",
    )

    assert result.blocking_reason_codes == (reason,)
    assert result.warning_reason_codes == ()


def test_terminal_only_work_adds_no_reason_code():
    result = classify_deployment_work(
        counts={"terminal": {"execution_order_legs": 3}},
        change_class="live_promotion",
    )

    assert result.blocking_reason_codes == ()
    assert result.warning_reason_codes == ()


@pytest.mark.parametrize(
    "counts",
    [
        {"unexpected": {"execution_order_legs": 1}},
        {"in_flight_write": {"execution_order_legs": -1}},
        {"in_flight_write": {"execution_order_legs": True}},
        {"in_flight_write": {"execution_order_legs": "1"}},
    ],
)
def test_malformed_work_counts_are_rejected(counts):
    with pytest.raises(DeploymentWorkEvidenceError, match="deployment_evidence_malformed"):
        classify_deployment_work(counts=counts, change_class="code")

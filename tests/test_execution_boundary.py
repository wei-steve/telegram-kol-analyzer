from __future__ import annotations

import pytest

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinRequestOutcomeUnknown,
)
from telegram_kol_research.execution_boundary import (
    ExecutionBoundaryOutcome,
    ExecutionBoundaryTracker,
    TrackedDeepcoinClient,
    build_execution_boundary_outcome,
)


class _Client:
    def __init__(self, result=None, error=None):
        self.result = result or {"data": {"ordId": "order-7"}}
        self.error = error

    def place_order(self, payload):
        if self.error is not None:
            raise self.error
        return self.result

    def list_positions(self, **kwargs):
        return []


def test_boundary_envelope_rejects_effect_claim_without_durable_evidence():
    with pytest.raises(ValueError, match="evidence"):
        ExecutionBoundaryOutcome(
            status="completed",
            exchange_effect="confirmed_applied",
            raw_status="submitted",
            reason_code="entry_submitted",
            evidence_refs=(),
            public_result={"status": "submitted"},
        )


def test_boundary_envelope_proves_no_write_when_only_reads_occur():
    tracker = ExecutionBoundaryTracker()
    client = TrackedDeepcoinClient(_Client(), tracker)

    assert client.list_positions(inst_id="BTC-USDT-SWAP") == []
    outcome = build_execution_boundary_outcome(
        {"status": "blocked", "reason": "policy"}, tracker
    )

    assert outcome.exchange_effect == "not_started"
    assert outcome.public_result == {"status": "blocked", "reason": "policy"}


def test_boundary_envelope_records_confirmed_write_without_persisting_payload():
    tracker = ExecutionBoundaryTracker()
    client = TrackedDeepcoinClient(_Client(), tracker)

    client.place_order({"api_key": "must-not-persist", "clientOrderId": "client-7"})
    outcome = build_execution_boundary_outcome(
        {"status": "submitted", "reason": "entry"}, tracker
    )

    assert outcome.exchange_effect == "confirmed_applied"
    assert outcome.evidence_refs == ({"kind": "deepcoin_write", "method": "place_order", "ordinal": 1, "order_id": "order-7"},)
    assert "api_key" not in repr(outcome)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DeepcoinDefiniteRejection("rejected"), "confirmed_rejected"),
        (DeepcoinRequestOutcomeUnknown("timeout"), "outcome_unknown"),
        (RuntimeError("transport failed"), "outcome_unknown"),
    ],
)
def test_boundary_envelope_preserves_write_exception_semantics(error, expected):
    tracker = ExecutionBoundaryTracker()
    client = TrackedDeepcoinClient(_Client(error=error), tracker)

    with pytest.raises(type(error)):
        client.place_order({"clientOrderId": "client-7"})

    outcome = build_execution_boundary_outcome(
        {"status": "failed", "reason": type(error).__name__}, tracker
    )
    assert outcome.exchange_effect == expected


def test_multi_leg_unknown_dominates_a_prior_confirmed_write():
    tracker = ExecutionBoundaryTracker()
    first = TrackedDeepcoinClient(_Client(), tracker)
    second = TrackedDeepcoinClient(
        _Client(error=DeepcoinRequestOutcomeUnknown("timeout")), tracker
    )
    first.place_order({"clientOrderId": "leg-1"})
    with pytest.raises(DeepcoinRequestOutcomeUnknown):
        second.place_order({"clientOrderId": "leg-2"})

    outcome = build_execution_boundary_outcome({"status": "unknown"}, tracker)
    assert outcome.exchange_effect == "outcome_unknown"


def test_multi_leg_definite_rejection_after_applied_leg_is_still_unknown():
    tracker = ExecutionBoundaryTracker()
    first = TrackedDeepcoinClient(_Client(), tracker)
    second = TrackedDeepcoinClient(
        _Client(error=DeepcoinDefiniteRejection("rejected")), tracker
    )
    first.place_order({"clientOrderId": "leg-1"})
    with pytest.raises(DeepcoinDefiniteRejection):
        second.place_order({"clientOrderId": "leg-2"})

    outcome = build_execution_boundary_outcome(
        {"status": "failed", "reason": "second_leg_rejected"}, tracker
    )
    assert outcome.exchange_effect == "outcome_unknown"


def test_completed_item_results_require_separate_canonical_contract_evidence():
    tracker = ExecutionBoundaryTracker()

    outcome = build_execution_boundary_outcome(
        {
            "status": "completed",
            "items": [
                {"item_id": 41, "status": "submitted"},
                {"item_id": 42, "status": "succeeded"},
            ],
        },
        tracker,
    )

    assert outcome.exchange_effect == "outcome_unknown"


def test_completed_item_results_accept_verified_live_contract_evidence():
    tracker = ExecutionBoundaryTracker()
    canonical_evidence = (
        {
            "kind": "instruction_execution_contract",
            "contract_id": 51,
            "item_id": 41,
            "attempted_exchange_write": True,
            "terminal_kind": "verified_entry",
            "completion_scope": "full",
        },
        {
            "kind": "instruction_execution_contract",
            "contract_id": 52,
            "item_id": 42,
            "attempted_exchange_write": True,
            "terminal_kind": "verified_management",
            "completion_scope": "full",
        },
    )

    outcome = build_execution_boundary_outcome(
        {
            "status": "completed",
            "items": [
                {"item_id": 41, "status": "submitted"},
                {"item_id": 42, "status": "succeeded"},
            ],
        },
        tracker,
        canonical_item_evidence=canonical_evidence,
    )

    assert outcome.exchange_effect == "confirmed_applied"
    assert outcome.evidence_refs == (
        {
            "kind": "instruction_execution_contract",
            "contract_id": 51,
            "item_id": 41,
            "attempted_exchange_write": True,
            "terminal_kind": "verified_entry",
            "completion_scope": "full",
        },
        {
            "kind": "instruction_execution_contract",
            "contract_id": 52,
            "item_id": 42,
            "attempted_exchange_write": True,
            "terminal_kind": "verified_management",
            "completion_scope": "full",
        },
    )


def test_disabled_or_shadow_succeeded_item_is_not_exchange_evidence():
    outcome = build_execution_boundary_outcome(
        {
            "status": "completed",
            "items": [{"item_id": 41, "status": "succeeded"}],
        },
        ExecutionBoundaryTracker(),
    )

    assert outcome.status == "outcome_unknown"
    assert outcome.exchange_effect == "outcome_unknown"


def test_verified_refusal_contract_is_not_misclassified_as_applied():
    outcome = build_execution_boundary_outcome(
        {
            "status": "completed",
            "items": [{"item_id": 41, "status": "succeeded"}],
        },
        ExecutionBoundaryTracker(),
        canonical_item_evidence=(
            {
                "kind": "instruction_execution_contract",
                "contract_id": 51,
                "item_id": 41,
                "attempted_exchange_write": False,
                "terminal_kind": "verified_refusal",
                "completion_scope": "full",
            },
        ),
    )

    assert outcome.exchange_effect == "not_started"


def test_mixed_applied_and_rejected_contract_evidence_is_unknown():
    outcome = build_execution_boundary_outcome(
        {
            "status": "completed",
            "items": [
                {"item_id": 41, "status": "submitted"},
                {"item_id": 42, "status": "submitted"},
            ],
        },
        ExecutionBoundaryTracker(),
        canonical_item_evidence=(
            {
                "kind": "instruction_execution_contract",
                "contract_id": 51,
                "item_id": 41,
                "attempted_exchange_write": True,
                "terminal_kind": "verified_entry",
                "completion_scope": "full",
            },
            {
                "kind": "instruction_execution_contract",
                "contract_id": 52,
                "item_id": 42,
                "attempted_exchange_write": True,
                "terminal_kind": "verified_refusal",
                "completion_scope": "full",
            },
        ),
    )

    assert outcome.exchange_effect == "outcome_unknown"

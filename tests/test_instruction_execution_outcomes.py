import pytest

from telegram_kol_research.instruction_execution_outcomes import (
    InstructionOutcome,
    InstructionOutcomeContractError,
    VISIBILITY_DEFER_REASONS,
    interpret_instruction_outcome,
    legacy_status_for_instruction_result,
    legacy_status_for_instruction_outcome,
)


@pytest.mark.parametrize(
    ("result", "intent_kind", "expected"),
    [
        (
            {
                "status": "deferred",
                "reason": "adjacent_entry_context_pending",
            },
            "entry",
            InstructionOutcome(
                state="deferred",
                reason_code="adjacent_entry_context_pending",
            ),
        ),
        (
            {"status": "reconciling", "reason": "exchange_readback_pending"},
            "management",
            InstructionOutcome(
                state="submitting",
                reason_code="exchange_readback_pending",
                attempted_exchange_write=True,
            ),
        ),
        (
            {"status": "submitted", "order_id": "entry-1"},
            "entry",
            InstructionOutcome(
                state="verified",
                reason_code="legacy_submitted",
                terminal_kind="verified_entry",
                attempted_exchange_write=True,
            ),
        ),
        (
            {
                "status": "succeeded",
                "management_action": "full_exit",
                "reason": "position_flat_verified",
            },
            "management",
            InstructionOutcome(
                state="verified",
                reason_code="position_flat_verified",
                terminal_kind="verified_exit",
            ),
        ),
        (
            {"status": "skipped", "reason": "auto_trade_disabled"},
            "entry",
            InstructionOutcome(
                state="verified",
                reason_code="auto_trade_disabled",
                terminal_kind="verified_refusal",
            ),
        ),
        (
            {"status": "shadow_planned", "batch_id": 17},
            "management",
            InstructionOutcome(
                state="verified",
                reason_code="legacy_shadow_planned",
                terminal_kind="verified_refusal",
            ),
        ),
        (
            {
                "status": "recovery_required",
                "reason": "protection_recovery_required",
            },
            "management",
            InstructionOutcome(
                state="submit_unknown",
                reason_code="protection_recovery_required",
                attempted_exchange_write=True,
            ),
        ),
        (
            {
                "status": "submitted",
                "legs": [{"status": "submit_unknown"}],
            },
            "management",
            InstructionOutcome(
                state="submit_unknown",
                reason_code="legacy_submitted",
                attempted_exchange_write=True,
            ),
        ),
        (
            {"status": "failed", "reason": "preflight_failed"},
            "entry",
            InstructionOutcome(
                state="failed",
                reason_code="preflight_failed",
            ),
        ),
        (
            {"status": "partial_failed", "reason": "one_leg_failed"},
            "management",
            InstructionOutcome(
                state="failed",
                reason_code="one_leg_failed",
                attempted_exchange_write=True,
            ),
        ),
        (
            {"status": "blocked", "reason": "safety_gate_closed"},
            "entry",
            InstructionOutcome(
                state="failed",
                reason_code="safety_gate_closed",
            ),
        ),
        (
            {"status": "expired", "reason": "deadline_elapsed"},
            "entry",
            InstructionOutcome(
                state="expired",
                reason_code="deadline_elapsed",
            ),
        ),
    ],
)
def test_current_legacy_outcomes_have_explicit_mappings(
    result, intent_kind, expected
):
    assert interpret_instruction_outcome(result, intent_kind=intent_kind) == expected


def test_defer_reason_registry_is_single_and_complete():
    assert VISIBILITY_DEFER_REASONS == frozenset(
        {
            "adjacent_entry_context_pending",
            "target_strategy_binding_not_visible_yet",
            "preceding_entry_context_unresolved",
        }
    )


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"status": "mystery"},
        {"status": "deferred", "reason": "unregistered_wait"},
        {"status": "deferred", "reason": "adjacent_entry_context_pending", "submitted": True},
        {"status": "skipped", "reason": "disabled", "submitted": True},
        {"status": "submitted", "submitted": False},
        {"status": "failed", "reason": "unknown_exchange_outcome"},
    ],
)
def test_unknown_missing_or_contradictory_outcomes_fail_closed(result):
    with pytest.raises(InstructionOutcomeContractError):
        interpret_instruction_outcome(result, intent_kind="entry")


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (InstructionOutcome("deferred", "wait"), "pending"),
        (InstructionOutcome("submitting", "write"), "executing"),
        (
            InstructionOutcome(
                "submit_unknown", "unknown", attempted_exchange_write=True
            ),
            "unknown",
        ),
        (
            InstructionOutcome(
                "verified",
                "entry_verified",
                terminal_kind="verified_entry",
                attempted_exchange_write=True,
            ),
            "submitted",
        ),
        (
            InstructionOutcome(
                "verified",
                "refused",
                terminal_kind="verified_refusal",
            ),
            "succeeded",
        ),
        (InstructionOutcome("failed", "failed"), "failed"),
        (InstructionOutcome("expired", "expired"), "failed"),
    ],
)
def test_legacy_status_converter_is_explicit(outcome, expected):
    assert legacy_status_for_instruction_outcome(outcome) == expected


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_pre_enforcement_mode_preserves_known_legacy_mirror(mode):
    assert legacy_status_for_instruction_result(
        {"status": "reconciling", "submitted": True},
        intent_kind="management",
        enforcement_mode=mode,
    ) == "submitted"
    assert legacy_status_for_instruction_result(
        {"status": "skipped", "reason": "auto_trade_disabled"},
        intent_kind="entry",
        enforcement_mode=mode,
    ) == "succeeded"


def test_live_enforcement_uses_nonterminal_compatibility_state():
    assert legacy_status_for_instruction_result(
        {"status": "reconciling", "submitted": True},
        intent_kind="management",
        enforcement_mode="live",
    ) == "executing"

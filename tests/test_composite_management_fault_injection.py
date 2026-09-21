"""Cross-boundary fault matrix for durable composite management.

These aliases intentionally rerun the production-path tests as one deployment
gate. Each underlying test asserts exchange write counts and durable states.
"""

from test_composite_remainder_market_close import (
    test_a_position_that_vanished_without_our_confirmed_close_stops_for_a_person
    as test_remainder_close_unattributable_exit_boundary,
    test_restart_confirms_from_a_confirmed_intent_and_a_flat_position
    as test_remainder_close_confirmation_gap_boundary,
    test_restart_during_the_entry_cancel_stops_for_a_person
    as test_remainder_close_deferred_cancel_crash_boundary,
    test_restart_with_a_reserved_intent_blocks_it_and_allows_a_new_attempt
    as test_remainder_close_reserved_before_write_boundary,
    test_restart_with_a_terminal_intent_and_a_live_position_retries
    as test_remainder_close_terminal_without_fill_boundary,
    test_restart_with_an_unknown_intent_stays_awaiting_and_writes_nothing
    as test_remainder_close_unknown_outcome_boundary,
    test_the_books_are_terminalized_in_the_same_transaction_as_the_success
    as test_remainder_close_terminalization_atomicity_boundary,
)
from test_semantic_disagreement_review import (
    test_composite_semantic_review_input_preserves_mimo_authority_and_outcomes
    as test_auxiliary_model_boundary_is_advisory_only,
)
from test_strategy_management_executor import (
    test_composite_close_definite_rejection_retries_from_fresh_unchanged_position
    as test_definite_rejection_boundary,
    test_composite_close_http_acceptance_without_target_evidence_stays_awaiting
    as test_stale_readback_boundary,
    test_composite_close_unknown_never_retries_on_restart
    as test_unknown_outcome_boundary,
    test_composite_protection_duplicate_new_order_id_retains_old_stops
    as test_duplicate_order_id_boundary,
    test_composite_restart_closes_transition_gap_after_exchange_confirmation
    as test_response_persistence_failure_boundary,
    test_composite_restart_unresolved_protection_write_stays_read_only
    as test_service_restart_boundary,
)
from test_strategy_records import (
    test_composite_completion_rejects_pending_consumed_tp_and_false_success
    as test_ui_completion_boundary,
)
from test_system_operator_bot import (
    test_composite_completion_notification_is_bounded_and_blocks_recovering_state
    as test_notification_failure_boundary,
)


__all__ = [name for name in globals() if name.startswith("test_")]

"""Detection rules D1a-D1d, D2, D4, D5, D6a-D6c and the read-only discipline."""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

import pytest

from oncall_test_support import (
    CHAT_ID,
    NOW,
    ProductionFixture,
    build_open_position_case,
    build_recognition_failure_case,
    build_sealed_lane_case,
    build_voided_message_case,
    sqlite_write_authorizer,
)
from telegram_kol_research.oncall_detector import (
    COUNTER_READ_FAILED_ROUNDS,
    COUNTER_SKIPPED_ATTRIBUTION_UNKNOWN,
    COUNTER_SKIPPED_NO_POSITION,
    COUNTER_STOP_LADDER_LEVEL_UNRECORDED,
    HEALTH_CASE_DB_READ,
    HEALTH_CASE_STALLED_JOBS,
    HEALTH_CASE_WORKER_LOOP,
    DetectorConfig,
    ProductionReader,
    run_detection_round,
)
from telegram_kol_research.oncall_state import OncallStateStore


@pytest.fixture
def production(tmp_path) -> ProductionFixture:
    return ProductionFixture(tmp_path / "research.db")


@pytest.fixture
def store(tmp_path) -> OncallStateStore:
    with OncallStateStore(tmp_path / "state.db") as opened:
        yield opened


def run_round(production, store, *, now=NOW, config=None, probe=None, readers=None):
    created: list[ProductionReader] = readers if readers is not None else []

    def factory() -> ProductionReader:
        reader = ProductionReader(production.path)
        created.append(reader)
        return reader

    return run_detection_round(
        reader_factory=factory,
        store=store,
        now=now,
        config=config or DetectorConfig(),
        worker_health_probe=probe,
    )


def only_case(store):
    cases = store.open_cases()
    assert len(cases) == 1, [case.case_key for case in cases]
    return cases[0]


# --------------------------------------------------------------- watermark


def test_first_start_takes_the_current_max_id_and_never_replays_history(
    production, store
):
    build_open_position_case(production)

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()
    assert store.get_watermark("message_instruction_items") is not None


def test_a_restart_on_the_same_state_opens_no_second_case(production, store, tmp_path):
    run_round(production, store)
    build_open_position_case(production)
    first = run_round(production, store)
    assert len(first.new_case_ids) == 1

    store.close()
    with OncallStateStore(tmp_path / "state.db") as reopened:
        second = run_round(production, reopened)
        assert second.new_case_ids == ()
        assert len(reopened.open_cases()) == 1


# ---------------------------------------------------------------- D1a/D1b


def test_d1a_opens_a_case_for_a_failed_management_item_with_a_position(
    production, store
):
    run_round(production, store)
    build_open_position_case(production)

    outcome = run_round(production, store)

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.rule == "D1a"
    assert case.severity == "high"
    assert case.reason_code == "prior_partial_batch_unresolved"
    assert case.evidence["group_name"] == "龚有财群"
    assert case.evidence["position_state"] == "verified_open"


def test_d1b_opens_a_case_for_a_skipped_result_that_is_not_the_users_own_switch(
    production, store
):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="succeeded",
        result={"status": "skipped", "reason": "management_stop_action_conflict"},
    )

    run_round(production, store)

    case = only_case(store)
    assert case.rule == "D1b"
    assert case.reason_code == "management_stop_action_conflict"


@pytest.mark.parametrize(
    "reason",
    [
        "kol_or_group_auto_trade_disabled",
        "symbol_not_allowed",
        "group_not_configured_for_auto_trade",
        "confidence_below_minimum",
    ],
)
def test_d1b_ignores_the_four_reasons_that_are_the_users_own_configuration(
    production, store, reason
):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="succeeded",
        result={"status": "skipped", "reason": reason},
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_a_successful_management_item_opens_nothing(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="succeeded",
        result={"status": "succeeded", "submitted": True},
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()


def test_an_entry_side_action_is_out_of_scope(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id,
        management_action="replace_entry",
        target_lifecycle_id=lifecycle_id,
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "whatever"},
    )

    assert run_round(production, store).new_case_ids == ()


# -------------------------------------------------------------------- D1c


def test_d1c_needs_ten_minutes_of_waiting_before_it_becomes_a_case(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    production.add_binding()
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=None
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="awaiting_user_confirmation",
        result={"confirmation_reason_code": "target_ambiguous"},
        updated_at=NOW - timedelta(minutes=4),
    )

    assert run_round(production, store).new_case_ids == ()

    outcome = run_round(production, store, now=NOW + timedelta(minutes=7))

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.rule == "D1c"
    assert case.target_uncertain is True
    assert case.evidence["group_open_positions"] == ["ETH short"]


# -------------------------------------------------------------------- D1d


def test_d1d_is_medium_and_only_after_five_minutes_in_flight(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="executing",
        updated_at=NOW - timedelta(minutes=2),
    )

    assert run_round(production, store).new_case_ids == ()

    run_round(production, store, now=NOW + timedelta(minutes=4))

    case = only_case(store)
    assert case.rule == "D1d"
    assert case.severity == "medium"
    assert case.reason_code == "instruction_stuck_executing"


# ------------------------------------------------- 4.1, the position gate


def test_no_binding_means_no_case_and_a_counter_instead(production, store):
    run_round(production, store)
    build_open_position_case(
        production,
        reason="target_strategy_binding_visibility_retry_expired",
        with_binding=False,
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert outcome.skipped_no_position == 1
    assert store.get_int_meta(COUNTER_SKIPPED_NO_POSITION) == 1


def test_a_closed_binding_is_not_a_position(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding(
        status="closed", pos_id=None, last_exchange_status="entry_legs_terminal"
    )
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert outcome.skipped_no_position == 1


def test_an_attribution_conflict_is_counted_separately_rather_than_alerted(
    production, store
):
    """Phase 1 follows the spec and stays quiet, but leaves a number behind."""

    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding(
        status="unknown", last_exchange_status="position_attribution_conflict"
    )
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert outcome.skipped_no_position == 0
    assert store.get_int_meta(COUNTER_SKIPPED_ATTRIBUTION_UNKNOWN) == 1


def test_the_items_own_strategy_instance_is_the_second_test(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    production.add_binding(strategy_instance_id="strategy-77")
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=None
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
        strategy_instance_id="strategy-77",
    )

    run_round(production, store)

    case = only_case(store)
    assert case.evidence["position_rule"] == "strategy_instance_binding"
    assert case.target_uncertain is False


def test_a_same_chat_position_is_the_third_test_and_marks_the_target_uncertain(
    production, store
):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    production.add_binding(symbol="ETH", side="short")
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id,
        symbol="ETH",
        side="short",
        target_lifecycle_id=None,
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "target_strategy_binding_visibility_retry_expired"},
    )

    run_round(production, store)

    case = only_case(store)
    assert case.evidence["position_rule"] == "chat_open_binding"
    assert case.target_uncertain is True


def test_a_same_chat_position_on_the_other_side_does_not_count(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    production.add_binding(symbol="BTC", side="long")
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id,
        symbol="ETH",
        side="short",
        target_lifecycle_id=None,
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "target_strategy_binding_visibility_retry_expired"},
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert outcome.skipped_no_position == 1


def test_a_binding_reconcile_has_not_refreshed_still_opens_a_case(production, store):
    """A frozen worker leaves stale bindings; silence there is the worst answer."""

    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding(recovered_at=NOW - timedelta(hours=3))
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
    )

    run_round(production, store)

    case = only_case(store)
    assert case.evidence["position_state"] == "open_snapshot_stale"


# ---------------------------------------------------------------------- D2


def test_d2_opens_a_case_for_a_faulted_batch_older_than_two_minutes(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    production.add_management_batch(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=binding_id,
        status="recovery_required",
        reason_code="recovery_timeout",
    )

    run_round(production, store)

    case = only_case(store)
    assert case.rule == "D2"
    assert case.reason_code == "recovery_timeout"


def test_d2_ignores_a_plan_only_block(production, store):
    run_round(production, store)
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    production.add_management_batch(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=binding_id,
        status="blocked",
        reason_code="management_disabled_plan_only",
    )

    assert run_round(production, store).new_case_ids == ()


def test_d2_waits_two_minutes(production, store):
    run_round(production, store)
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    production.add_management_batch(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=binding_id,
        status="partial_failed",
        updated_at=NOW - timedelta(seconds=30),
    )

    assert run_round(production, store).new_case_ids == ()


def test_d1_and_d2_on_the_same_message_are_one_case(production, store):
    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message()
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id, target_lifecycle_id=lifecycle_id
    )
    item_id = production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "prior_partial_batch_unresolved"},
    )
    batch_id = production.add_management_batch(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=binding_id,
        status="partial_failed",
    )

    outcome = run_round(production, store)

    case = only_case(store)
    assert len(outcome.new_case_ids) == 1
    assert case.rule == "D1a+D2"
    assert case.item_ids == (item_id,)
    assert case.batch_ids == (batch_id,)


def test_a_batch_whose_exchange_verb_differs_from_its_intent_is_still_one_case(
    production, store
):
    """Production, 2026-09-20, raw 17813: ``partial_then_break_even`` planned as
    ``partial_close``. Keyed on the verb, one message became two alerts."""

    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message(text="现在你就移动止损")
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        management_action="partial_then_break_even",
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="failed",
        error={"reason": "management_stop_action_conflict"},
    )
    production.add_management_batch(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=binding_id,
        status="blocked",
        reason_code="management_stop_action_conflict",
        intent="partial_then_break_even",
        effective_action="partial_close",
    )

    outcome = run_round(production, store)

    case = only_case(store)
    assert len(outcome.new_case_ids) == 1
    assert case.case_key == f"mgmt:{raw_message_id}:partial_then_break_even"
    assert case.rule == "D1a+D2"


# ------------------------------------------------------ resolution, stale


def test_a_case_whose_item_later_succeeds_is_resolved(production, store):
    run_round(production, store)
    built = build_open_position_case(production)
    first = run_round(production, store)
    case_id = first.new_case_ids[0]

    production.set_item_status(
        built["item_id"],
        status="succeeded",
        result={"status": "succeeded", "submitted": True},
    )
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == (case_id,)
    assert store.get_case(case_id).status == "resolved"


def test_a_case_open_for_six_hours_goes_stale(production, store):
    run_round(production, store)
    build_open_position_case(production)
    first = run_round(production, store)
    case_id = first.new_case_ids[0]

    outcome = run_round(production, store, now=NOW + timedelta(hours=7))

    assert case_id in outcome.stale_case_ids
    assert store.get_case(case_id).status == "stale"


# ---------------------------------------------------------------------- D4


def test_d4_fires_when_a_job_has_been_queued_for_ten_minutes(production, store):
    run_round(production, store)
    raw_message_id = production.add_raw_message()
    job_id = production.add_processing_job(
        raw_message_id=raw_message_id, enqueued_at=NOW - timedelta(minutes=12)
    )

    run_round(production, store)

    case = store.get_case_by_key(HEALTH_CASE_STALLED_JOBS)
    assert case is not None and case.status == "open"
    assert case.evidence["oldest_job_id"] == job_id

    production.set_job_status(job_id, status="succeeded")
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert case.id in outcome.resolved_case_ids
    assert store.get_case(case.id).status == "resolved"


def test_a_stall_that_comes_back_opens_the_health_case_again(production, store):
    run_round(production, store)
    job_id = production.add_processing_job(
        raw_message_id=production.add_raw_message(),
        enqueued_at=NOW - timedelta(minutes=12),
    )
    run_round(production, store)
    production.set_job_status(job_id, status="succeeded")
    run_round(production, store, now=NOW + timedelta(minutes=1))
    assert store.get_case_by_key(HEALTH_CASE_STALLED_JOBS).status == "resolved"

    later = NOW + timedelta(minutes=40)
    production.add_processing_job(
        raw_message_id=production.add_raw_message(),
        enqueued_at=later - timedelta(minutes=12),
    )
    outcome = run_round(production, store, now=later)

    reopened = store.get_case_by_key(HEALTH_CASE_STALLED_JOBS)
    assert reopened.status == "open"
    assert reopened.id in outcome.new_case_ids


def test_d4_leaves_a_fresh_job_alone(production, store):
    run_round(production, store)
    raw_message_id = production.add_raw_message()
    production.add_processing_job(
        raw_message_id=raw_message_id, enqueued_at=NOW - timedelta(seconds=20)
    )

    assert run_round(production, store).new_case_ids == ()


# ---------------------------------------------------------------------- D5


def test_five_consecutive_read_failures_are_needed_and_a_failure_is_never_healthy(
    production, store, tmp_path
):
    missing = tmp_path / "gone.db"

    def factory():
        return ProductionReader(missing)

    outcomes = [
        run_detection_round(reader_factory=factory, store=store, now=NOW)
        for _ in range(4)
    ]
    assert all(outcome.read_failed for outcome in outcomes)
    assert all(outcome.new_case_ids == () for outcome in outcomes)
    assert store.get_case_by_key(HEALTH_CASE_DB_READ) is None

    fifth = run_detection_round(reader_factory=factory, store=store, now=NOW)

    assert fifth.read_failed is True
    assert store.get_case_by_key(HEALTH_CASE_DB_READ) is not None
    assert store.get_int_meta(COUNTER_READ_FAILED_ROUNDS) == 5


def test_a_recovered_read_resolves_the_health_case(production, store, tmp_path):
    missing = tmp_path / "gone.db"
    for _ in range(5):
        run_detection_round(
            reader_factory=lambda: ProductionReader(missing), store=store, now=NOW
        )
    case = store.get_case_by_key(HEALTH_CASE_DB_READ)

    outcome = run_round(production, store)

    assert case.id in outcome.resolved_case_ids


def test_the_worker_health_probe_needs_three_failures_and_recovers(production, store):
    run_round(production, store, probe=lambda: False)
    run_round(production, store, probe=lambda: False)
    assert store.get_case_by_key(HEALTH_CASE_WORKER_LOOP) is None

    run_round(production, store, probe=lambda: False)
    case = store.get_case_by_key(HEALTH_CASE_WORKER_LOOP)
    assert case is not None and case.status == "open"

    outcome = run_round(production, store, probe=lambda: True)
    assert case.id in outcome.resolved_case_ids


def test_a_probe_that_raises_counts_as_a_failure(production, store):
    def probe() -> bool:
        raise TimeoutError("no answer")

    for _ in range(3):
        run_round(production, store, probe=probe)

    assert store.get_case_by_key(HEALTH_CASE_WORKER_LOOP) is not None


# ------------------------------------------------------- stop ladder count


def _ladder_fill(production, *, level=2, updated_at=NOW - timedelta(minutes=10)):
    binding_id = production.add_binding()
    leg_id = production.add_order_leg(execution_binding_id=binding_id)
    production.add_protection_ledger_row(
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        purpose="take_profit",
        order_id="tp-2",
        status="filled",
        pos_id="pos-1",
        evidence={
            "take_profit_fill": {
                "level": level,
                "evidence_form": "trigger_history",
                "order_id": "tp-2",
            }
        },
        updated_at=updated_at,
    )
    return binding_id


def test_the_ladder_counts_a_fill_the_shadow_has_not_recorded(production, store):
    run_round(production, store)
    _ladder_fill(production)

    outcome = run_round(production, store)

    assert store.get_int_meta(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, 0) == 1
    # A counter, not a case: the user ruled out alerting on this entirely.
    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_a_shadow_that_recorded_the_same_level_counts_nothing(production, store):
    run_round(production, store)
    binding_id = _ladder_fill(production)
    production.add_execution_event(
        execution_binding_id=binding_id,
        action="stop_ladder_would_replace",
        status="shadow",
        reason=None,
        pos_id="pos-1",
        after={"filled_level": 2, "target_price": "79800"},
    )

    run_round(production, store)

    assert store.get_int_meta(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, 0) == 0


def test_fresh_evidence_is_given_five_minutes_before_it_counts(production, store):
    run_round(production, store)
    _ladder_fill(production, updated_at=NOW - timedelta(minutes=1))

    run_round(production, store)
    assert store.get_int_meta(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, 0) == 0

    run_round(production, store, now=NOW + timedelta(minutes=10))
    assert store.get_int_meta(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, 0) == 1


def test_one_fill_is_counted_once_however_many_rounds_run(production, store):
    run_round(production, store)
    _ladder_fill(production)

    for minute in range(3):
        run_round(production, store, now=NOW + timedelta(minutes=minute))

    assert store.get_int_meta(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, 0) == 1


def test_a_ledger_row_without_a_ladder_level_is_never_counted(production, store):
    """``protection_health`` writes the fill without a level; nothing to compare."""

    run_round(production, store)
    binding_id = production.add_binding()
    leg_id = production.add_order_leg(execution_binding_id=binding_id)
    production.add_protection_ledger_row(
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        purpose="take_profit",
        order_id="tp-9",
        status="filled",
        evidence={"take_profit_fill": {"evidence_tier": "trigger_history_clean_trigger"}},
        updated_at=NOW - timedelta(minutes=30),
    )

    run_round(production, store)

    assert store.get_int_meta(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, 0) == 0


def test_a_stop_ledger_row_is_not_a_ladder_rung(production, store):
    run_round(production, store)
    binding_id = production.add_binding()
    leg_id = production.add_order_leg(execution_binding_id=binding_id)
    production.add_protection_ledger_row(
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        purpose="stop_loss",
        order_id="sl-1",
        evidence={"take_profit_fill": {"level": 3}},
        updated_at=NOW - timedelta(minutes=30),
    )

    run_round(production, store)

    assert store.get_int_meta(COUNTER_STOP_LADDER_LEVEL_UNRECORDED, 0) == 0


# -------------------------------------------------------------------- D3


def test_d3_opens_a_case_when_recognition_failed_over_a_live_position(
    production, store
):
    run_round(production, store)
    built = build_recognition_failure_case(production)

    outcome = run_round(production, store)

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.case_key == f"recog:{built['raw_message_id']}"
    assert case.rule == "D3"
    assert case.severity == "high"
    assert case.reason_code == "mimo_authoritative_failed"
    assert case.chat_id == CHAT_ID
    assert case.evidence["group_name"] == "龚有财群"
    assert case.evidence["group_open_positions"] == ["ETH short"]
    assert case.evidence["message_text"].startswith("ETH 这波先减一半")


@pytest.mark.parametrize(
    "reason",
    [
        "target_not_verifiable",
        "authoritative_gap_recovery_expired",
        "lifecycle_apply_failed",
        "management_recognition_unresolved",
    ],
)
def test_d3_opens_a_case_for_every_lossy_skip_the_design_names(
    production, store, reason
):
    run_round(production, store)
    build_recognition_failure_case(
        production,
        agreement_status="pending",
        automation_status="skipped",
        automation_reason=reason,
    )

    run_round(production, store)

    case = only_case(store)
    assert case.rule == "D3"
    assert case.reason_code == reason


def test_d3_falls_back_to_the_agreement_status_when_no_reason_was_recorded(
    production, store
):
    run_round(production, store)
    build_recognition_failure_case(
        production, automation_status=None, automation_reason=None
    )

    run_round(production, store)

    assert only_case(store).reason_code == "authoritative_failed"


def test_d3_does_not_open_a_case_when_the_group_has_no_real_position(
    production, store
):
    run_round(production, store)
    build_recognition_failure_case(production, with_binding=False)

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()
    assert outcome.skipped_no_position == 1
    assert store.get_int_meta(COUNTER_SKIPPED_NO_POSITION, 0) == 1


def test_d3_stands_down_when_a_management_instruction_item_already_exists(
    production, store
):
    """D1 owns that message; two rules must not page twice for one failure."""

    run_round(production, store)
    built = build_recognition_failure_case(production)
    candidate_id = production.add_candidate(raw_message_id=built["raw_message_id"])
    production.add_instruction_item(
        raw_message_id=built["raw_message_id"],
        signal_candidate_id=candidate_id,
        status="succeeded",
        result={"status": "completed"},
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_d3_stands_down_when_a_management_batch_already_exists(production, store):
    run_round(production, store)
    built = build_recognition_failure_case(production)
    lifecycle_id = production.add_lifecycle(execution_binding_id=built["binding_id"])
    production.add_management_batch(
        raw_message_id=built["raw_message_id"],
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=built["binding_id"],
        status="succeeded",
    )

    outcome = run_round(production, store)

    assert [case.case_key for case in store.open_cases()] == []
    assert outcome.new_case_ids == ()


def test_d3_ignores_a_decision_that_recognised_the_message_normally(production, store):
    run_round(production, store)
    build_recognition_failure_case(
        production,
        agreement_status="pending",
        automation_status="skipped",
        automation_reason="auto_trade_not_configured",
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()
    assert outcome.skipped_no_position == 0


def test_d3_waits_for_the_pipelines_own_retry_before_opening(production, store):
    """The main pipeline retries a failed recognition after 60 seconds."""

    run_round(production, store)
    build_recognition_failure_case(production, updated_at=NOW - timedelta(minutes=1))

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()
    # And it does open once the grace period has passed.
    later = run_round(production, store, now=NOW + timedelta(minutes=10))
    assert len(later.new_case_ids) == 1


def test_d3_resolves_when_the_message_is_later_recognised(production, store):
    run_round(production, store)
    built = build_recognition_failure_case(production)
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1

    production.set_recognition_decision(
        built["decision_id"],
        agreement_status="pending",
        automation_status="completed",
        automation_reason=None,
    )
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == opened.new_case_ids
    assert store.get_case(opened.new_case_ids[0]).status == "resolved"


def test_d3_resolves_when_an_instruction_item_finally_appears(production, store):
    run_round(production, store)
    built = build_recognition_failure_case(production)
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1

    candidate_id = production.add_candidate(raw_message_id=built["raw_message_id"])
    production.add_instruction_item(
        raw_message_id=built["raw_message_id"],
        signal_candidate_id=candidate_id,
        status="succeeded",
        result={"status": "completed"},
    )
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == opened.new_case_ids


def test_d3_goes_stale_after_six_hours_like_every_other_case(production, store):
    run_round(production, store)
    build_recognition_failure_case(production)
    opened = run_round(production, store)

    outcome = run_round(production, store, now=NOW + timedelta(hours=7))

    assert outcome.stale_case_ids == opened.new_case_ids


def test_the_lossy_reason_codes_are_the_pipelines_own_spelling():
    """The watcher may not import the pipeline, so the strings are copied.

    A copy drifts silently, and a drifted code means D3 goes quiet for the
    exact failure it exists for. This test is the link the import cannot be.
    """

    from telegram_kol_research import recognition_failure_attribution as attribution
    from telegram_kol_research.oncall_detector import LOSSY_RECOGNITION_REASONS

    assert attribution.TARGET_NOT_VERIFIABLE in LOSSY_RECOGNITION_REASONS
    assert attribution.MIMO_AUTHORITATIVE_FAILED in LOSSY_RECOGNITION_REASONS
    assert attribution.GAP_RECOVERY_EXPIRED in LOSSY_RECOGNITION_REASONS
    assert attribution.APPLY_FAILED in LOSSY_RECOGNITION_REASONS
    # And the benign outcomes stay out: alerting on them is what buried the
    # real losses for two months (see that module's own docstring).
    assert attribution.NO_ACTIONABLE_INTENT not in LOSSY_RECOGNITION_REASONS
    assert attribution.NO_TARGET_NAMED not in LOSSY_RECOGNITION_REASONS


def test_d3_never_replays_the_recognition_history_that_predates_the_watcher(
    production, store
):
    build_recognition_failure_case(production)

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.get_watermark("recognition_decisions") is not None


# ------------------------------------------------------------------- D6a


def test_d6a_opens_a_case_once_the_lane_has_been_sealed_for_six_hours(
    production, store
):
    """陈哥 BTC-long, sealed 2026-09-15 to 09-25 with nobody told."""

    run_round(production, store)
    built = build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=12))

    outcome = run_round(production, store)

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.case_key == f"lane:{built['exit_id']}"
    assert case.rule == "D6a"
    assert case.severity == "high"
    assert case.reason_code == "source_deletion_exit_sealed_lane"
    assert case.chat_id == CHAT_ID
    assert case.evidence["group_name"] == "龚有财群"
    assert case.evidence["symbol"] == "BTC"
    assert case.evidence["side"] == "long"
    assert case.evidence["exit_id"] == built["exit_id"]
    assert case.evidence["minutes_sealed"] == 12 * 60
    assert case.evidence["exit_last_reason"] == "exit_has_no_known_position"


def test_d6a_leaves_a_lane_alone_while_the_system_can_still_heal_it(
    production, store
):
    """The system's own sweep runs at 120 minutes; the watch waits for six hours."""

    run_round(production, store)
    build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=5))

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()
    # And it does open once the horizon passes.
    later = run_round(production, store, now=NOW + timedelta(hours=2))
    assert len(later.new_case_ids) == 1


def test_d6a_case_text_carries_the_symbol_side_and_how_many_were_voided(
    production, store
):
    """Translating "exit 310 is stuck" into "you are two strategies down"."""

    run_round(production, store)
    build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=12))
    for text in ("BTC 80400 进多", "BTC 83300-83500 进多"):
        build_voided_message_case(production, text=text)
    # A later message the system did *not* void must not be counted.
    production.add_recognition_decision(
        raw_message_id=production.add_raw_message(text="BTC 空单减半"),
        automation_reason="management_stop_action_conflict",
    )

    run_round(production, store)

    lane = next(
        case for case in store.open_cases() if case.case_key.startswith("lane:")
    )
    assert lane.evidence["voided_messages"] == 2
    assert lane.evidence["voided_scan_examined"] >= 3


def test_d6a_resolves_when_the_exit_finally_succeeds(production, store):
    run_round(production, store)
    built = build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=12))
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1

    production.set_deletion_exit_state(built["exit_id"], state="succeeded")
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == opened.new_case_ids
    assert store.get_case(opened.new_case_ids[0]).status == "resolved"


def test_d6a_keeps_the_case_open_while_the_exit_is_merely_touched(
    production, store
):
    """The barrier reopens the lane on ``succeeded`` alone, so nothing else clears."""

    run_round(production, store)
    built = build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=12))
    opened = run_round(production, store)

    production.set_deletion_exit_state(
        built["exit_id"], state="recovery_required", updated_at=NOW
    )
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == ()
    assert store.get_case(opened.new_case_ids[0]).status == "open"


def test_d6a_ignores_an_unbound_exit_because_it_seals_nothing(production, store):
    """91 rows in production. ``raw_message_id`` NULL is not in the barrier's join."""

    run_round(production, store)
    build_sealed_lane_case(
        production,
        unbound=True,
        state="recovery_required",
        updated_at=NOW - timedelta(days=30),
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_d6a_ignores_an_exit_whose_message_never_named_a_symbol_and_side(
    production, store
):
    """The barrier's join also needs a candidate with both, so this seals nothing."""

    run_round(production, store)
    build_sealed_lane_case(
        production, with_candidate=False, updated_at=NOW - timedelta(hours=12)
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


_ACTIVE_SEALING_STATES = (
    "pending",
    "cancelling_entries",
    "closing_positions",
    "reconciling",
)


@pytest.mark.parametrize("state", _ACTIVE_SEALING_STATES)
def test_d6a_opens_a_case_for_an_active_state_that_stopped_moving(
    production, store, state
):
    """The gap the first round left open (status document, 2026-09-26).

    ``source_execution_barrier`` shuts the lane on ``state != 'succeeded'``, so
    an exit stalled in one of the worker's own active states seals it exactly as
    a ``recovery_required`` one does -- and unlike that one, no sweep will ever
    release it.
    """

    run_round(production, store)
    built = build_sealed_lane_case(
        production, state=state, updated_at=NOW - timedelta(hours=12)
    )

    outcome = run_round(production, store)

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.case_key == f"lane:{built['exit_id']}"
    assert case.rule == "D6a"
    assert case.severity == "high"
    assert case.reason_code == "source_deletion_exit_stalled_lane"
    assert case.evidence["stall_class"] == "active"
    assert case.evidence["exit_state"] == state
    assert case.evidence["symbol"] == "BTC"
    assert case.evidence["side"] == "long"


def test_d6a_tells_the_two_stall_causes_apart_by_reason_code(production, store):
    """Same rule, same severity, same key namespace -- a different cause."""

    run_round(production, store)
    build_sealed_lane_case(
        production, state="pending", updated_at=NOW - timedelta(hours=12)
    )
    build_sealed_lane_case(
        production,
        chat_id=CHAT_ID + 7,
        symbol="ETH",
        side="short",
        state="recovery_required",
        updated_at=NOW - timedelta(hours=12),
    )

    run_round(production, store)

    cases = store.open_cases()
    by_reason = {case.reason_code: case for case in cases}
    assert set(by_reason) == {
        "source_deletion_exit_stalled_lane",
        "source_deletion_exit_sealed_lane",
    }
    stalled = by_reason["source_deletion_exit_stalled_lane"]
    sealed = by_reason["source_deletion_exit_sealed_lane"]
    assert stalled.evidence["stall_class"] == "active"
    assert sealed.evidence["stall_class"] == "unclaimable"
    assert {case.rule for case in cases} == {"D6a"}
    assert {case.severity for case in cases} == {"high"}


def test_d6a_says_nothing_about_a_succeeded_exit(production, store):
    """The one state the barrier lets through is the one state that is fine."""

    run_round(production, store)
    build_sealed_lane_case(
        production, state="succeeded", updated_at=NOW - timedelta(hours=12)
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


@pytest.mark.parametrize("state", _ACTIVE_SEALING_STATES)
def test_d6a_waits_the_same_six_hours_for_an_active_state(production, store, state):
    """One threshold for both causes, and no second number to keep in step."""

    run_round(production, store)
    build_sealed_lane_case(
        production, state=state, updated_at=NOW - timedelta(hours=5)
    )

    assert run_round(production, store).new_case_ids == ()
    later = run_round(production, store, now=NOW + timedelta(hours=2))
    assert len(later.new_case_ids) == 1


@pytest.mark.parametrize("state", _ACTIVE_SEALING_STATES)
def test_d6a_ignores_an_unbound_exit_in_any_sealing_state(production, store, state):
    """``raw_message_id`` NULL cannot reach the barrier's join, whatever the state.

    ``unbound`` is the state those 91 production rows actually carry, but a row
    can also be left with a NULL message in one of these states, and that one
    seals nothing either.
    """

    run_round(production, store)
    build_sealed_lane_case(
        production, unbound=True, state=state, updated_at=NOW - timedelta(days=30)
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_d6a_never_looks_at_the_unbound_state_itself(production, store):
    """``unbound`` is a real state, and deliberately not in the sweep's list."""

    from telegram_kol_research.oncall_detector import SEALED_LANE_SEALING_STATES

    assert "unbound" not in SEALED_LANE_SEALING_STATES
    run_round(production, store)
    build_sealed_lane_case(
        production, state="unbound", updated_at=NOW - timedelta(days=30)
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


@pytest.mark.parametrize("state", _ACTIVE_SEALING_STATES)
def test_d6a_resolves_an_active_state_case_once_the_exit_succeeds(
    production, store, state
):
    run_round(production, store)
    built = build_sealed_lane_case(
        production, state=state, updated_at=NOW - timedelta(hours=12)
    )
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1

    production.set_deletion_exit_state(built["exit_id"], state="succeeded")
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == opened.new_case_ids
    assert store.get_case(opened.new_case_ids[0]).status == "resolved"


def test_d6a_keeps_one_case_when_an_active_stall_gives_up_into_recovery(
    production, store
):
    """The lane never reopened, so this is one story that changed cause."""

    run_round(production, store)
    built = build_sealed_lane_case(
        production, state="closing_positions", updated_at=NOW - timedelta(hours=12)
    )
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1

    production.set_deletion_exit_state(
        built["exit_id"],
        state="recovery_required",
        updated_at=NOW - timedelta(hours=12),
    )
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.new_case_ids == ()
    assert outcome.resolved_case_ids == ()
    case = only_case(store)
    assert case.id == opened.new_case_ids[0]
    assert case.status == "open"
    assert case.reason_code == "source_deletion_exit_sealed_lane"
    assert case.evidence["stall_class"] == "unclaimable"


def test_the_sealed_lane_states_are_the_deletion_paths_own_spellings():
    """A drifted copy would make D6a silent for a state it exists for.

    Two sources, because the two halves of the list have two owners: the stuck
    state belongs to the timeout sweep, and the four active ones to the worker
    that claims them.
    """

    from telegram_kol_research import source_deletion_exit_timeout as sweeper
    from telegram_kol_research import source_message_deletion_worker as worker
    from telegram_kol_research.oncall_detector import (
        SEALED_LANE_ACTIVE_STATES,
        SEALED_LANE_RELEASED_STATE,
        SEALED_LANE_SEALING_STATES,
        SEALED_LANE_STUCK_STATE,
    )

    assert SEALED_LANE_STUCK_STATE == sweeper.STUCK_STATE
    assert SEALED_LANE_ACTIVE_STATES == worker._ACTIVE_STATES
    assert SEALED_LANE_SEALING_STATES == (
        *worker._ACTIVE_STATES,
        sweeper.STUCK_STATE,
    )
    assert SEALED_LANE_RELEASED_STATE not in SEALED_LANE_SEALING_STATES


def test_waiting_is_a_counter_label_and_never_a_stored_exit_state():
    """Checked against the worker, not taken on trust from the state list.

    ``source_message_deletion_worker`` sets ``final_state = "waiting"`` and
    counts it, but the row it writes in that branch says ``reconciling``. A
    reader who mistook that word for a state would add a sixth entry to
    :data:`SEALED_LANE_SEALING_STATES` that no row can ever match.
    """

    import inspect

    from telegram_kol_research import source_message_deletion_worker as worker
    from telegram_kol_research.oncall_detector import SEALED_LANE_SEALING_STATES

    source = inspect.getsource(worker)
    # It exists, and it is a local label and a counter key.
    assert 'final_state = "waiting"' in source
    assert 'counts["waiting"]' in source
    # And it is never written to a row. Every spelling that would write it:
    for spelling in (
        '.state = "waiting"',
        'state="waiting"',
        'new_state="waiting"',
        "SourceMessageDeletionExit.state: \"waiting\"",
    ):
        assert spelling not in source, spelling
    # The branch that sets the label writes ``reconciling`` to the row instead.
    assert '_mark_reconciliation_waiting' in source
    assert 'deletion_exit.state = "reconciling"' in source
    assert "waiting" not in SEALED_LANE_SEALING_STATES


# ------------------------------------------------------------------- D6b


def test_d6b_opens_a_case_for_a_message_the_system_voided(production, store):
    run_round(production, store)
    built = build_voided_message_case(production)

    outcome = run_round(production, store)

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.case_key == f"voided:{built['raw_message_id']}"
    assert case.rule == "D6b"
    assert case.severity == "high"
    assert case.reason_code == "deferred_expired"
    assert case.chat_id == CHAT_ID
    assert case.evidence["group_name"] == "龚有财群"
    assert case.evidence["symbol"] == "BTC"
    assert case.evidence["side"] == "long"
    assert case.evidence["message_text"].startswith("BTC 83000-83300")


def test_d6b_opens_a_case_even_though_the_group_holds_no_position(
    production, store
):
    """The whole reason D6b is not in ``LOSSY_RECOGNITION_REASONS``.

    D3 drops a lossy decision when the group has no open binding. Four of
    陈哥's eleven voided messages were *entries* -- no position by definition --
    and that filter would have swallowed every one of them.
    """

    run_round(production, store)
    build_voided_message_case(production, with_binding=False)

    outcome = run_round(production, store)

    assert len(outcome.new_case_ids) == 1
    assert outcome.skipped_no_position == 0
    assert store.get_int_meta(COUNTER_SKIPPED_NO_POSITION, 0) == 0
    assert only_case(store).rule == "D6b"


def test_deferred_expired_is_deliberately_not_a_lossy_recognition_reason():
    """Adding it to D3's set is the one implementation mistake to prevent."""

    from telegram_kol_research.oncall_detector import (
        DEFERRED_EXPIRED_REASON,
        DEFERRED_HOLD_REASON,
        LOSSY_RECOGNITION_REASONS,
    )

    assert DEFERRED_EXPIRED_REASON not in LOSSY_RECOGNITION_REASONS
    assert DEFERRED_HOLD_REASON not in LOSSY_RECOGNITION_REASONS


def test_the_deferral_reason_codes_are_the_pipelines_own_spelling():
    from telegram_kol_research import deferred_instruction_recovery as recovery
    from telegram_kol_research.oncall_detector import (
        DEFERRED_EXPIRED_REASON,
        DEFERRED_HOLD_REASON,
    )

    assert DEFERRED_EXPIRED_REASON == recovery.DEFERRED_EXPIRED_REASON
    assert DEFERRED_HOLD_REASON == recovery.DEFERRED_HOLD_REASON


def test_d6b_says_nothing_while_the_message_is_merely_waiting(production, store):
    """``waiting_source_deletion_exit`` may still resume and execute normally."""

    run_round(production, store)
    built = build_voided_message_case(
        production, automation_reason="waiting_source_deletion_exit"
    )

    outcome = run_round(production, store)
    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()

    # But the row stays under watch, so the terminal state is not missed.
    production.set_recognition_decision(
        built["decision_id"],
        automation_status="deferred",
        automation_reason="deferred_expired",
    )
    later = run_round(production, store, now=NOW + timedelta(minutes=35))
    assert len(later.new_case_ids) == 1
    assert only_case(store).rule == "D6b"


def test_d6b_names_the_exit_that_ate_the_message(production, store):
    """So the D6a case and the D6b case read as one event, not two faults."""

    run_round(production, store)
    built = build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=12))
    build_voided_message_case(production)

    run_round(production, store)

    voided = next(
        case for case in store.open_cases() if case.case_key.startswith("voided:")
    )
    assert voided.evidence["blocking_exit_id"] == built["exit_id"]
    assert voided.evidence["blocking_exit_state"] == "recovery_required"


def test_d6b_never_replays_the_voided_history_that_predates_the_watcher(
    production, store
):
    build_voided_message_case(production)

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.get_watermark("recognition_decisions") is not None


# ------------------------------------------------------------------- D6c


def _still_shouting(production, **kwargs) -> int:
    defaults = dict(
        source_kind="source_deletion_exit",
        source_record_id="310",
        incident_type="source_deletion_exit_stuck",
        severity="high",
        status="pending",
        repeat_count=356933,
        first_occurred_at=NOW - timedelta(days=11),
        last_occurred_at=NOW - timedelta(minutes=2),
        notified_at=None,
    )
    defaults.update(kwargs)
    return production.add_runtime_incident(**defaults)


def test_d6c_opens_a_case_for_an_alarm_still_ringing_that_nobody_was_told_about(
    production, store
):
    run_round(production, store)
    incident_id = _still_shouting(production)

    outcome = run_round(production, store)

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.case_key == f"unheard:{incident_id}"
    assert case.rule == "D6c"
    assert case.severity == "high"
    assert case.reason_code == "runtime_incident_never_notified"
    assert case.evidence["incident_type"] == "source_deletion_exit_stuck"
    assert case.evidence["repeat_count"] == 356933
    assert case.evidence["minutes_since_last_occurrence"] == 2


def test_d6c_opens_a_case_when_the_last_notification_is_three_days_old(
    production, store
):
    run_round(production, store)
    _still_shouting(production, notified_at=NOW - timedelta(days=11))

    run_round(production, store)

    assert only_case(store).reason_code == "runtime_incident_notification_stale"


def test_d6c_says_nothing_about_an_alarm_that_has_stopped(production, store):
    """"Happened a lot in the past" is not the criterion; "happening now" is."""

    run_round(production, store)
    _still_shouting(production, last_occurred_at=NOW - timedelta(days=4))

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_d6c_says_nothing_when_somebody_was_told_recently(production, store):
    run_round(production, store)
    _still_shouting(production, notified_at=NOW - timedelta(hours=6))

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


@pytest.mark.parametrize("severity", ["info", "low", "medium"])
def test_d6c_leaves_the_quiet_severities_alone(production, store, severity):
    run_round(production, store)
    _still_shouting(production, severity=severity)

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()


@pytest.mark.parametrize("status", ["claimed", "diagnosed", "resolved", "closed"])
def test_d6c_only_looks_at_incidents_nobody_has_picked_up(
    production, store, status
):
    run_round(production, store)
    _still_shouting(production, status=status)

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()


def test_d6c_resolves_when_the_alarm_stops_progressing(production, store):
    run_round(production, store)
    incident_id = _still_shouting(production)
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1

    outcome = run_round(production, store, now=NOW + timedelta(hours=3))

    assert outcome.resolved_case_ids == opened.new_case_ids
    assert store.get_case(opened.new_case_ids[0]).status == "resolved"
    assert incident_id


def test_d6c_resolves_when_somebody_is_finally_told(production, store):
    run_round(production, store)
    incident_id = _still_shouting(production)
    opened = run_round(production, store)

    production.set_runtime_incident(incident_id, notified_at=NOW)
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == opened.new_case_ids


def test_d6c_resolves_when_the_incident_is_picked_up(production, store):
    run_round(production, store)
    incident_id = _still_shouting(production)
    opened = run_round(production, store)

    production.set_runtime_incident(incident_id, status="claimed")
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == opened.new_case_ids


def test_the_incident_status_and_severities_are_the_ledgers_own_spelling():
    from telegram_kol_research import runtime_incidents
    from telegram_kol_research.oncall_detector import (
        INCIDENT_LOUD_SEVERITIES,
        INCIDENT_PENDING_STATUS,
    )

    assert INCIDENT_PENDING_STATUS == runtime_incidents._CLAIMABLE_STATUS
    assert INCIDENT_LOUD_SEVERITIES <= set(runtime_incidents._SEVERITY_RANKS)
    # And they really are the loud end of that scale.
    ranks = runtime_incidents._SEVERITY_RANKS
    assert min(ranks[name] for name in INCIDENT_LOUD_SEVERITIES) > ranks["medium"]


def test_the_three_d6_thresholds_are_the_designs_own_numbers():
    from telegram_kol_research.oncall_detector import (
        INCIDENT_NOTIFICATION_SILENCE,
        INCIDENT_STILL_OCCURRING_WITHIN,
        SEALED_LANE_STUCK_AFTER,
        DetectorConfig,
    )

    assert SEALED_LANE_STUCK_AFTER == timedelta(hours=6)
    assert INCIDENT_STILL_OCCURRING_WITHIN == timedelta(hours=1)
    assert INCIDENT_NOTIFICATION_SILENCE == timedelta(days=3)
    defaults = DetectorConfig()
    assert defaults.sealed_lane_stuck_after == SEALED_LANE_STUCK_AFTER
    assert defaults.incident_still_occurring_within == INCIDENT_STILL_OCCURRING_WITHIN
    assert defaults.incident_notification_silence == INCIDENT_NOTIFICATION_SILENCE


def test_the_sql_cutoff_is_spelled_the_way_production_stores_a_timestamp():
    """Rules D6a and D6c compare timestamps inside SQL, which is lexicographic."""

    from telegram_kol_research.oncall_detector import as_production_text

    assert as_production_text(NOW) == "2026-09-19 06:00:00.000000"


# ------------------------------------------------------- read discipline


_WATERMARK_SHAPE = re.compile(r"WHERE id > \? ORDER BY id LIMIT \?$")
_POINT_SHAPE = re.compile(r"WHERE id (?:= \?|IN \(\?(?:,\?)*\))$")
_BOUNDED_SHAPES = (
    re.compile(r"^SELECT MAX\(id\) AS max_id FROM [a-z_]+$"),
    re.compile(r"WHERE strategy_instance_id = \? ORDER BY id DESC LIMIT 20$"),
    re.compile(
        r"WHERE chat_id = \? AND venue = 'deepcoin' AND status IN \('open', 'active'\) "
        r"ORDER BY id DESC LIMIT 50$"
    ),
    re.compile(r"^SELECT chat_title FROM strategy_alerts WHERE chat_id = \? "
               r"ORDER BY message_id DESC LIMIT 1$"),
    re.compile(r"^SELECT custom_label, display_name FROM sources WHERE chat_id = \? "
               r"ORDER BY id LIMIT 1$"),
    re.compile(
        r"^SELECT id, action, after_json FROM execution_events "
        r"WHERE pos_id = \? ORDER BY id DESC LIMIT 20$"
    ),
    # D3's "has this message already produced management work?" lookups, D1d's
    # batch check and D6a's lane naming, which share one shape.
    re.compile(r"WHERE raw_message_id = \? ORDER BY id (?:DESC )?LIMIT \d+$"),
    # D6a's sweep over ix_source_message_deletion_exits_state (state, updated_at).
    # The ``IN`` list is the five lane-sealing states; SQLite seeks the index
    # once per listed state, which the plan test below checks is really so.
    re.compile(
        r"FROM source_message_deletion_exits "
        r"WHERE state IN \(\?(?:, \?)*\) AND updated_at <= \? ORDER BY id LIMIT \?$"
    ),
    # D6a's "how many has this lane already eaten", both halves.
    re.compile(
        r"^SELECT id FROM raw_messages WHERE chat_id = \? AND id > \? "
        r"ORDER BY id LIMIT \?$"
    ),
    re.compile(r"WHERE raw_message_id IN \(\?(?:,\?)*\)$"),
    # D6c's sweep over ix_runtime_incidents_claimable (status, ...).
    re.compile(
        r"FROM runtime_incidents "
        r"WHERE status = \? AND last_occurred_at >= \? ORDER BY id LIMIT \?$"
    ),
)


def _statement_is_allowed(sql: str) -> bool:
    collapsed = " ".join(sql.split())
    if _WATERMARK_SHAPE.search(collapsed) or _POINT_SHAPE.search(collapsed):
        return True
    return any(shape.search(collapsed) for shape in _BOUNDED_SHAPES)


def test_every_production_statement_is_a_watermark_a_point_query_or_a_bounded_lookup(
    production, store
):
    run_round(production, store)
    build_open_position_case(production)
    build_recognition_failure_case(production)
    build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=12))
    build_voided_message_case(production)
    _still_shouting(production)
    production.add_processing_job(raw_message_id=production.add_raw_message())
    _ladder_fill(production)
    readers: list[ProductionReader] = []
    run_round(production, store, readers=readers)
    run_round(production, store, now=NOW + timedelta(minutes=20), readers=readers)

    statements = [sql for reader in readers for sql in reader.statements]
    assert statements
    # The ladder's own read is in there, not just the shapes that predate it.
    assert any("FROM execution_events" in sql for sql in statements)
    # So are D3's.
    assert any("FROM recognition_decisions" in sql for sql in statements)
    assert any(
        "FROM message_instruction_items WHERE raw_message_id" in " ".join(sql.split())
        for sql in statements
    )
    # And D6's three sweeps, which are the newest.
    assert any("FROM source_message_deletion_exits" in sql for sql in statements)
    assert any("FROM runtime_incidents" in sql for sql in statements)
    assert any(
        "FROM recognition_decisions WHERE raw_message_id IN" in " ".join(sql.split())
        for sql in statements
    )
    offenders = [sql for sql in statements if not _statement_is_allowed(sql)]
    assert offenders == []
    assert all("SELECT" in sql.upper() for sql in statements)


_INDEX_BACKED_SWEEPS = (
    (
        "SELECT id FROM source_message_deletion_exits "
        "WHERE state IN (?, ?, ?, ?, ?) AND updated_at <= ? ORDER BY id LIMIT ?",
        "ix_source_message_deletion_exits_state",
    ),
    (
        "SELECT id FROM runtime_incidents "
        "WHERE status = ? AND last_occurred_at >= ? ORDER BY id LIMIT ?",
        "ix_runtime_incidents_claimable",
    ),
    (
        "SELECT id FROM raw_messages WHERE chat_id = ? AND id > ? ORDER BY id LIMIT ?",
        "ix_raw_messages_chat_id",
    ),
)


@pytest.mark.parametrize("sql,index_name", _INDEX_BACKED_SWEEPS)
def test_the_new_sweeps_really_do_use_their_index(production, sql, index_name):
    """The shape regexes say what was written; the planner says what runs.

    A bounded-looking statement over an unindexed column is exactly the
    full-table scan that froze the worker's event loop eight times on
    2026-09-15, and the regex above cannot tell the difference.
    """

    reader = ProductionReader(production.path)
    try:
        plan = reader.connection.execute(
            "EXPLAIN QUERY PLAN " + sql, tuple(None for _ in range(sql.count("?")))
        ).fetchall()
    finally:
        reader.close()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert index_name in detail, detail
    assert "SCAN" not in detail, detail


def test_the_sweeps_own_statement_is_the_one_the_detector_sends(production, store):
    """The plan test above checks a statement written out by hand.

    This one checks that the hand-written spelling is the spelling the module
    actually uses, so the two cannot drift apart: the placeholder count follows
    ``SEALED_LANE_SEALING_STATES`` at runtime.
    """

    from telegram_kol_research.oncall_detector import SEALED_LANE_SEALING_STATES

    build_sealed_lane_case(production, updated_at=NOW - timedelta(hours=12))
    readers: list[ProductionReader] = []
    run_round(production, store, readers=readers)

    statements = [
        " ".join(sql.split())
        for reader in readers
        for sql in reader.statements
        if "FROM source_message_deletion_exits WHERE state IN" in " ".join(sql.split())
    ]
    assert statements
    placeholders = ", ".join("?" for _ in SEALED_LANE_SEALING_STATES)
    assert all(f"WHERE state IN ({placeholders})" in sql for sql in statements)
    assert len(SEALED_LANE_SEALING_STATES) == 5


def test_the_spelling_the_sweep_rejected_really_does_scan(production):
    """Evidence for the warning in ``ALLOWED_QUERY_SHAPES``.

    ``state NOT IN (...)`` is the obvious way to say "every state but these two"
    and it is the reason the positive list exists: the planner cannot use
    ``ix_source_message_deletion_exits_state`` for it at all.
    """

    reader = ProductionReader(production.path)
    try:
        plan = reader.connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM source_message_deletion_exits "
            "WHERE state NOT IN (?, ?) AND updated_at <= ? ORDER BY id LIMIT ?",
            (None, None, None, None),
        ).fetchall()
    finally:
        reader.close()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "SCAN" in detail, detail


def test_the_detector_never_writes_to_the_production_database(production, store):
    run_round(production, store)
    build_open_position_case(production)
    denied: list[str] = []
    readers: list[ProductionReader] = []

    original = ProductionReader.__init__

    def instrumented(self, database_path, **kwargs):
        original(self, database_path, **kwargs)
        self.connection.set_authorizer(sqlite_write_authorizer(denied))

    ProductionReader.__init__ = instrumented
    try:
        run_round(production, store, readers=readers)
    finally:
        ProductionReader.__init__ = original

    assert denied == []
    assert store.open_cases()


def test_the_connection_string_is_read_only_and_query_only(production):
    reader = ProductionReader(production.path)
    try:
        assert "mode=ro" in reader.uri
        with pytest.raises(sqlite3.Error):
            reader.connection.execute("CREATE TABLE oops (id INTEGER)")
    finally:
        reader.close()


def test_an_unreadable_database_raises_the_watchers_own_error(tmp_path):
    from telegram_kol_research.oncall_detector import ProductionReadError

    with pytest.raises(ProductionReadError):
        ProductionReader(tmp_path / "does-not-exist.db")


def test_a_case_seen_again_every_round_keeps_a_bounded_rule_name(production, store):
    """Production, 2026-09-21: case 4's rule grew by one ``D1a+`` per round."""

    from telegram_kol_research.oncall_state import _combine_rules

    assert _combine_rules("D1a", "D1a") == "D1a"
    assert _combine_rules("D1a", "D1a+D2") == "D1a+D2"
    assert _combine_rules("D1a+D2", "D1a+D2") == "D1a+D2"
    assert _combine_rules("D1a+D1a+D1a", "D1a") == "D1a"  # heals a row that already grew
    assert _combine_rules("", "D4") == "D4"


def test_an_item_left_in_submitted_is_not_a_case_once_its_batch_succeeded(production, store):
    """Production, 2026-09-21, raw 18089: stop moved, item status never updated."""

    run_round(production, store)
    production.add_group_name()
    raw_message_id = production.add_raw_message(text="及时移动止损")
    binding_id = production.add_binding()
    lifecycle_id = production.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = production.add_candidate(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        management_action="move_stop_to_break_even",
    )
    production.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status="submitted",
        updated_at=NOW - timedelta(minutes=30),
    )
    production.add_management_batch(
        raw_message_id=raw_message_id,
        target_lifecycle_id=lifecycle_id,
        execution_binding_id=binding_id,
        status="succeeded",
        intent="move_stop_to_break_even",
        effective_action="break_even_by_market",
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


# ------------------------------------------------------------------
# D6a's third cause (2026-09-26): the exit that is always moving and
# never finishing. ``updated_at`` is fresh every round, so the "how long
# since anything happened" test can never see it; ``created_at`` can.
# ------------------------------------------------------------------


@pytest.mark.parametrize("state", _ACTIVE_SEALING_STATES)
def test_d6a_opens_a_case_for_an_exit_that_moves_constantly_and_never_finishes(
    production, store, state
):
    """Claimed every few seconds, back in the same state every time.

    Every claim writes ``updated_at = now``, so this row's idle age never grows
    past a tick and the original criterion is blind to it. The row's own age is
    what gives it away.
    """

    run_round(production, store)
    built = build_sealed_lane_case(
        production,
        state=state,
        created_at=NOW - timedelta(hours=12),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=2097,
    )

    outcome = run_round(production, store)

    case = only_case(store)
    assert outcome.new_case_ids == (case.id,)
    assert case.case_key == f"lane:{built['exit_id']}"
    assert case.rule == "D6a"
    assert case.severity == "high"
    assert case.reason_code == "source_deletion_exit_churning_lane"
    assert case.evidence["stall_class"] == "churning"
    assert case.evidence["exit_state"] == state
    # The evidence that separates this cause from the other two: somebody has
    # been working on it two thousand times over.
    assert case.evidence["attempt_count"] == 2097
    assert case.evidence["minutes_unfinished"] == 12 * 60
    assert case.evidence["minutes_sealed"] == 0


def test_d6a_waits_the_same_six_hours_before_calling_an_exit_churning(
    production, store
):
    """One bar for both age tests -- no second threshold to keep in step."""

    run_round(production, store)
    build_sealed_lane_case(
        production,
        state="closing_positions",
        created_at=NOW - timedelta(hours=5),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=400,
    )

    assert run_round(production, store).new_case_ids == ()
    later = run_round(production, store, now=NOW + timedelta(hours=2))
    assert len(later.new_case_ids) == 1
    assert only_case(store).reason_code == "source_deletion_exit_churning_lane"


def test_d6a_says_nothing_about_a_succeeded_exit_however_old_the_row_is(
    production, store
):
    """The new age test must not resurrect the one state the barrier allows.

    A ``succeeded`` exit from a month ago is a finished job, not a shut lane.
    """

    run_round(production, store)
    build_sealed_lane_case(
        production,
        state="succeeded",
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=12,
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_d6a_tells_all_three_stall_causes_apart_by_reason_code(production, store):
    """Same rule, same severity, same key namespace -- three different causes."""

    run_round(production, store)
    build_sealed_lane_case(
        production,
        state="pending",
        created_at=NOW - timedelta(hours=12),
        updated_at=NOW - timedelta(hours=12),
    )
    build_sealed_lane_case(
        production,
        chat_id=CHAT_ID + 7,
        symbol="ETH",
        side="short",
        state="recovery_required",
        created_at=NOW - timedelta(hours=12),
        updated_at=NOW - timedelta(hours=12),
    )
    build_sealed_lane_case(
        production,
        chat_id=CHAT_ID + 9,
        symbol="SOL",
        side="long",
        state="reconciling",
        created_at=NOW - timedelta(hours=12),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=888,
    )

    run_round(production, store)

    cases = store.open_cases()
    by_reason = {case.reason_code: case for case in cases}
    assert set(by_reason) == {
        "source_deletion_exit_stalled_lane",
        "source_deletion_exit_sealed_lane",
        "source_deletion_exit_churning_lane",
    }
    assert by_reason["source_deletion_exit_stalled_lane"].evidence["stall_class"] == (
        "active"
    )
    assert by_reason["source_deletion_exit_sealed_lane"].evidence["stall_class"] == (
        "unclaimable"
    )
    assert by_reason["source_deletion_exit_churning_lane"].evidence["stall_class"] == (
        "churning"
    )
    assert {case.rule for case in cases} == {"D6a"}
    assert {case.severity for case in cases} == {"high"}
    assert len(cases) == 3


def test_d6a_opens_one_case_when_both_age_tests_fire_on_the_same_exit(
    production, store
):
    """Two criteria, one case key, so no chance of two alerts for one lane.

    The idle test wins the wording: a row that was created long ago and has also
    stopped moving is standing still, not churning.
    """

    run_round(production, store)
    build_sealed_lane_case(
        production,
        state="cancelling_entries",
        created_at=NOW - timedelta(hours=30),
        updated_at=NOW - timedelta(hours=12),
        attempt_count=3,
    )

    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1
    again = run_round(production, store, now=NOW + timedelta(minutes=10))
    assert again.new_case_ids == ()

    case = only_case(store)
    assert case.reason_code == "source_deletion_exit_stalled_lane"
    assert case.evidence["stall_class"] == "active"


def test_d6a_keeps_one_case_when_a_churning_exit_finally_goes_quiet(
    production, store
):
    """The lane never reopened, so this is one story that changed cause."""

    run_round(production, store)
    built = build_sealed_lane_case(
        production,
        state="closing_positions",
        created_at=NOW - timedelta(hours=12),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=500,
    )
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1
    assert only_case(store).reason_code == "source_deletion_exit_churning_lane"

    production.set_deletion_exit_state(
        built["exit_id"],
        state="closing_positions",
        updated_at=NOW - timedelta(hours=12),
    )
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.new_case_ids == ()
    assert outcome.resolved_case_ids == ()
    case = only_case(store)
    assert case.id == opened.new_case_ids[0]
    assert case.status == "open"
    assert case.reason_code == "source_deletion_exit_stalled_lane"
    assert case.evidence["stall_class"] == "active"


@pytest.mark.parametrize("state", _ACTIVE_SEALING_STATES)
def test_d6a_resolves_a_churning_case_once_the_exit_succeeds(
    production, store, state
):
    run_round(production, store)
    built = build_sealed_lane_case(
        production,
        state=state,
        created_at=NOW - timedelta(hours=12),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=90,
    )
    opened = run_round(production, store)
    assert len(opened.new_case_ids) == 1

    production.set_deletion_exit_state(built["exit_id"], state="succeeded")
    outcome = run_round(production, store, now=NOW + timedelta(minutes=1))

    assert outcome.resolved_case_ids == opened.new_case_ids
    assert store.get_case(opened.new_case_ids[0]).status == "resolved"


def test_d6a_leaves_a_freshly_touched_recovery_required_exit_alone(
    production, store
):
    """The deliberate limit of the ``created_at`` test, written down.

    It applies to the active states only. ``recovery_required`` cannot show this
    shape in production: the one writer that touches such a row is
    ``source_deletion_exit_timeout._release``, and the same statement sets the
    state to ``succeeded`` -- there is no path that refreshes a stuck row's
    ``updated_at`` and leaves it stuck. So a fabricated row like this one, whose
    ``updated_at`` is seconds old, stays silent, and this test is the record of
    that decision rather than an endorsement of it.
    """

    run_round(production, store)
    build_sealed_lane_case(
        production,
        state="recovery_required",
        created_at=NOW - timedelta(days=11),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=2,
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


@pytest.mark.parametrize("state", _ACTIVE_SEALING_STATES)
def test_d6a_still_ignores_an_unbound_exit_that_never_finishes(
    production, store, state
):
    """``seals_a_lane`` comes first: a row holding nothing is not a shut lane."""

    run_round(production, store)
    build_sealed_lane_case(
        production,
        unbound=True,
        state=state,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(seconds=5),
        attempt_count=700,
    )

    outcome = run_round(production, store)

    assert outcome.new_case_ids == ()
    assert store.open_cases() == ()


def test_the_three_stall_classes_and_their_reason_codes_are_distinct():
    """A duplicated word would silently merge two causes into one story."""

    from telegram_kol_research.oncall_detector import (
        REASON_CHURNING_LANE,
        REASON_SEALED_LANE,
        REASON_STALLED_LANE,
        SEALED_LANE_STUCK_AFTER,
    )
    from telegram_kol_research.oncall_state import (
        LANE_STALL_ACTIVE,
        LANE_STALL_CHURNING,
        LANE_STALL_UNCLAIMABLE,
    )

    assert len({LANE_STALL_ACTIVE, LANE_STALL_CHURNING, LANE_STALL_UNCLAIMABLE}) == 3
    assert len({REASON_STALLED_LANE, REASON_CHURNING_LANE, REASON_SEALED_LANE}) == 3
    assert REASON_CHURNING_LANE == "source_deletion_exit_churning_lane"
    assert LANE_STALL_CHURNING == "churning"
    # Both age tests share this one number; there is no second threshold.
    assert SEALED_LANE_STUCK_AFTER == timedelta(hours=6)


def test_the_sealed_lane_sweep_still_seeks_its_index_with_the_real_projection(
    production,
):
    """The hand-written plan test above projects ``id``, which is covering.

    The statement the module actually sends reads ``last_reason`` and the rest,
    so its plan is ``USING INDEX`` rather than ``USING COVERING INDEX``. Two
    columns were added to that projection for this rule, and this test is the
    proof the seek survived it -- built from the module's own constants so the
    two cannot drift.
    """

    from telegram_kol_research.oncall_detector import (
        _EXIT_COLUMNS,
        SEALED_LANE_SEALING_STATES,
    )

    assert "created_at" in _EXIT_COLUMNS and "attempt_count" in _EXIT_COLUMNS
    placeholders = ", ".join("?" for _ in SEALED_LANE_SEALING_STATES)
    sql = (
        f"SELECT {_EXIT_COLUMNS} FROM source_message_deletion_exits "
        f"WHERE state IN ({placeholders}) AND updated_at <= ? ORDER BY id LIMIT ?"
    )
    reader = ProductionReader(production.path)
    try:
        plan = reader.connection.execute(
            "EXPLAIN QUERY PLAN " + sql, tuple(None for _ in range(sql.count("?")))
        ).fetchall()
    finally:
        reader.close()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "ix_source_message_deletion_exits_state" in detail, detail
    assert "SCAN" not in detail, detail

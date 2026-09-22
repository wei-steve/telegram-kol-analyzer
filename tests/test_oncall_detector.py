"""Detection rules D1a-D1d, D2, D4, D5 and the read-only discipline."""

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
    production.add_processing_job(raw_message_id=production.add_raw_message())
    _ladder_fill(production)
    readers: list[ProductionReader] = []
    run_round(production, store, readers=readers)
    run_round(production, store, now=NOW + timedelta(minutes=20), readers=readers)

    statements = [sql for reader in readers for sql in reader.statements]
    assert statements
    # The ladder's own read is in there, not just the shapes that predate it.
    assert any("FROM execution_events" in sql for sql in statements)
    offenders = [sql for sql in statements if not _statement_is_allowed(sql)]
    assert offenders == []
    assert all("SELECT" in sql.upper() for sql in statements)


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

"""The one predicate for "did this take profit fill", against real rows."""

from deepcoin_production_rows import TRIGGER_HISTORY_KEYS, trigger_history_row

from telegram_kol_research.take_profit_fill_predicate import (
    REASON_HISTORY_ABSENT,
    REASON_HISTORY_AMBIGUOUS,
    REASON_NOT_TRIGGERED,
    REASON_ORDER_IDENTITY_MISSING,
    REASON_POSITION_NOT_DECREASED,
    REASON_SNAPSHOT_INCOMPLETE,
    REASON_TRIGGER_FAILED,
    TIER_POSITION_DECREASE,
    TIER_RECORDED_ORDER_STATUS,
    TIER_TRIGGER_HISTORY,
    take_profit_fill_proven,
    trigger_row_failed,
    trigger_row_fired,
)


def _history(**overrides):
    return trigger_history_row(
        ord_id="tp-1",
        inst_id="ETH-USDT-SWAP",
        pos_side="long",
        trigger_price="2690",
        size="0.7",
        **overrides,
    )


def test_the_venue_sends_no_state_field_at_all():
    assert "state" not in TRIGGER_HISTORY_KEYS
    assert "state" not in _history()
    assert "posId" not in _history()


def test_a_clean_trigger_plus_a_complete_pending_read_proves_a_fill():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[_history()],
        pending_snapshot_complete=True,
    )

    assert verdict.proven
    assert verdict.evidence_tier == TIER_TRIGGER_HISTORY


def test_the_recorded_order_status_is_preferred_and_needs_no_history():
    verdict = take_profit_fill_proven(
        order_id="tp-1", recorded_order_statuses=["filled"]
    )

    assert verdict.proven
    assert verdict.evidence_tier == TIER_RECORDED_ORDER_STATUS


def test_an_active_or_expired_recorded_status_is_not_a_fill():
    for status in ("active", "expired", "cancelled", "cancel_requested"):
        verdict = take_profit_fill_proven(
            order_id="tp-1", recorded_order_statuses=[status]
        )
        assert not verdict.proven, status
        assert verdict.reason_code == REASON_HISTORY_ABSENT


def test_a_non_zero_error_code_is_never_a_fill():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[_history(error_code="51004", error_message="rejected")],
        pending_snapshot_complete=True,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_TRIGGER_FAILED


def test_every_clean_error_code_spelling_is_accepted():
    for code in ("", "0", "00000"):
        verdict = take_profit_fill_proven(
            order_id="tp-1",
            trigger_history=[_history(error_code=code)],
            pending_snapshot_complete=True,
        )
        assert verdict.proven, code


def test_an_untriggered_row_is_never_a_fill():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[_history(trigger_time="0")],
        pending_snapshot_complete=True,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_NOT_TRIGGERED


def test_an_incomplete_pending_read_is_unknown_not_a_fill():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[_history()],
        pending_snapshot_complete=False,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_SNAPSHOT_INCOMPLETE


def test_two_history_rows_for_one_order_are_ambiguous():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[_history(), _history()],
        pending_snapshot_complete=True,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_HISTORY_AMBIGUOUS


def test_an_unnamed_order_is_never_proven():
    verdict = take_profit_fill_proven(order_id="")

    assert not verdict.proven
    assert verdict.reason_code == REASON_ORDER_IDENTITY_MISSING


def test_another_orders_history_row_proves_nothing():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[
            trigger_history_row(
                ord_id="tp-2",
                inst_id="ETH-USDT-SWAP",
                pos_side="long",
                trigger_price="2720",
                size="0.4",
            )
        ],
        pending_snapshot_complete=True,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_HISTORY_ABSENT


def test_an_untriggered_row_has_not_failed():
    row = _history(trigger_time="0", error_code="51004")

    assert not trigger_row_fired(row)
    assert not trigger_row_failed(row)


# --------------------------------------------------------------------------
# Form B -- the order is gone from a complete pending read, nothing in the
# history explains it, and the position got smaller between two complete
# observations (stop-ladder phase 1 spec 2.2).
# --------------------------------------------------------------------------


def test_a_position_decrease_proves_a_fill_when_the_history_is_silent():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[],
        pending_snapshot_complete=True,
        position_decrease_proven=True,
    )

    assert verdict.proven
    assert verdict.evidence_tier == TIER_POSITION_DECREASE


def test_form_b_needs_no_particular_amount_only_a_decrease():
    """Quantity is never compared: the caller answers "smaller", not "by how much"."""

    assert take_profit_fill_proven(
        order_id="tp-1",
        pending_snapshot_complete=True,
        position_decrease_proven=True,
    ).proven


def test_form_b_refuses_on_an_incomplete_pending_read():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        pending_snapshot_complete=False,
        position_decrease_proven=True,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_SNAPSHOT_INCOMPLETE


def test_form_b_refuses_when_the_position_did_not_shrink():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        pending_snapshot_complete=True,
        position_decrease_proven=False,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_POSITION_NOT_DECREASED


def test_without_delta_evidence_the_answer_is_still_history_absent():
    """Every caller that predates form B keeps its exact reason code."""

    verdict = take_profit_fill_proven(
        order_id="tp-1", pending_snapshot_complete=True
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_HISTORY_ABSENT


def test_a_failed_trigger_is_never_rescued_by_a_position_decrease():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[_history(error_code="51004", error_message="rejected")],
        pending_snapshot_complete=True,
        position_decrease_proven=True,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_TRIGGER_FAILED


def test_an_untriggered_history_row_is_never_rescued_by_a_position_decrease():
    verdict = take_profit_fill_proven(
        order_id="tp-1",
        trigger_history=[_history(trigger_time="0")],
        pending_snapshot_complete=True,
        position_decrease_proven=True,
    )

    assert not verdict.proven
    assert verdict.reason_code == REASON_NOT_TRIGGERED

"""Consuming one take-profit stage, against rows the venue really returns.

The fixtures this file used to carry invented two fields the exchange does not
send: ``posId`` on a pending ``TPSL`` row, and ``state: "filled"`` on a
trigger-orders-history row.  Both defects the suite could not see were in the
production code that read them, and between them they refused every composite
instruction from 2026-09-04 onward.
"""

from deepcoin_production_rows import (
    pending_stop_row,
    pending_take_profit_row,
    trigger_history_row,
)

from telegram_kol_research.protection_authority import (
    FREEZE_ORDER_UNATTRIBUTABLE,
    GROUP_STOP,
    GROUP_TAKE_PROFIT,
    ProtectionAuthority,
    ProtectionOrderRef,
)
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
)
from telegram_kol_research.strategy_management_take_profit_consumption import (
    plan_take_profit_consumption,
)


INSTRUMENT = "BTC-USDT-SWAP"
POS_ID = "1001125123045253"


def _contract():
    return ManagementInstructionContract(
        version=2,
        target_lifecycle_id=1,
        strategy_instance_id="strategy-1",
        symbol="BTC",
        side="long",
        close_fraction="0.5",
        stop_mode="actual_entry_price",
        stop_price=None,
        stop_price_source=None,
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=True,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text="止盈50%，止损移动至开仓价",
    )


TARGET_LEG = {
    "execution_binding_id": 10,
    "execution_order_leg_id": 20,
    "pos_id": POS_ID,
    "instrument_id": INSTRUMENT,
    "side": "long",
}


def _ledger(order_id, size, *, stage, owner_leg=20, status="verified"):
    return {
        "order_id": order_id,
        "execution_binding_id": 10,
        "execution_order_leg_id": owner_leg,
        "pos_id": POS_ID,
        "instrument_id": INSTRUMENT,
        "side": "long",
        "purpose": "take_profit",
        "status": status,
        "size_text": str(size),
        "trigger_price": str(64000 + stage * 1000),
        "stage_index": stage,
    }


def _pending(order_id, size, *, stage=None):
    stage = int(str(order_id).rsplit("-", 1)[-1]) if stage is None else stage
    return pending_take_profit_row(
        ord_id=order_id,
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price=str(64000 + stage * 1000),
        size=str(size),
    )


def _authority(pending_rows=(), *, frozen_reason=None, pos_id=POS_ID):
    """The protection set the binding chain resolves for this position."""

    if frozen_reason is not None:
        return ProtectionAuthority(
            status="frozen",
            pos_id=pos_id,
            instrument_id=INSTRUMENT,
            side="long",
            reason_code=frozen_reason,
        )
    stops = []
    take_profits = []
    for row in pending_rows:
        is_stop = row.get("slTriggerPrice") not in (None, "", "0")
        ref = ProtectionOrderRef(
            order_id=str(row["ordId"]),
            group=GROUP_STOP if is_stop else GROUP_TAKE_PROFIT,
            purpose="stop_loss" if is_stop else "take_profit",
            trigger_price=(
                row["slTriggerPrice"] if is_stop else row["tpTriggerPrice"]
            ),
            size_text=row["sz"],
            ledger_trigger_price=None,
            ledger_size_text=None,
            source="ledger",
            row=dict(row),
        )
        (stops if is_stop else take_profits).append(ref)
    return ProtectionAuthority(
        status="resolved",
        pos_id=pos_id,
        instrument_id=INSTRUMENT,
        side="long",
        execution_binding_id=10,
        execution_order_leg_id=20,
        stop_orders=tuple(stops),
        take_profit_orders=tuple(take_profits),
    )


def _plan(
    *,
    ledger,
    pending=(),
    trigger_history=(),
    order_history=(),
    fills=(),
    target="5",
    authority=None,
    recorded_order_statuses=None,
    pending_snapshot_complete=True,
    live="6",
):
    pending = list(pending)
    return plan_take_profit_consumption(
        contract=_contract(),
        target_leg=TARGET_LEG,
        pending_orders=pending,
        trigger_history=list(trigger_history),
        order_history=list(order_history),
        trade_fills=list(fills),
        protection_ledger=list(ledger),
        trusted_start_size="10",
        target_remaining_size=target,
        protection_authority=(
            _authority(pending) if authority is None else authority
        ),
        pending_snapshot_complete=pending_snapshot_complete,
        recorded_order_statuses=recorded_order_statuses,
        live_position_size=live,
    )


def test_one_full_position_take_profit_is_cancelled_completely():
    result = _plan(
        ledger=[_ledger("tp-1", 10, stage=1)],
        pending=[_pending("tp-1", 10)],
    )

    assert result.refusal_code is None
    assert result.cancel_order_ids == ("tp-1",)
    assert result.retained_rows == ()
    assert result.proven_filled_quantity == "0"


def test_several_stages_consume_first_and_bound_retained_total():
    result = _plan(
        ledger=[
            _ledger("tp-1", 4, stage=1),
            _ledger("tp-2", 3, stage=2),
            _ledger("tp-3", 3, stage=3),
        ],
        pending=[
            _pending("tp-1", 4),
            _pending("tp-2", 3),
            _pending("tp-3", 3),
        ],
    )

    assert result.cancel_order_ids == ("tp-1", "tp-2")
    assert [row["order_id"] for row in result.retained_rows] == ["tp-3"]
    assert [row["desired_size"] for row in result.retained_rows] == ["3"]
    assert result.resize_rows == ()


def test_first_take_profit_already_filled_counts_exact_quantity():
    """No ``state`` field exists.  ``triggerTime`` plus a clean ``errorCode``."""

    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1, status="filled")],
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="65000",
                size="4",
            )
        ],
        fills=[{"ordId": "tp-1", "posId": POS_ID, "fillSz": "4"}],
    )

    assert result.refusal_code is None
    assert result.cancel_order_ids == ()
    assert result.proven_filled_quantity == "4"
    assert result.evidence_tier == "trigger_history_clean_trigger"


def test_recorded_order_status_is_accepted_as_durable_fill_proof():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1, status="protection_missing")],
        recorded_order_statuses={"tp-1": ("filled",)},
    )

    assert result.refusal_code is None
    assert result.proven_filled_quantity == "4"
    assert result.evidence_tier == "recorded_take_profit_order_status"


# --- The account owner's rule of 2026-09-22 ----------------------------------


def test_a_filled_first_stage_targets_the_live_position_not_the_fraction():
    """A filled TP1 *is* the reduction, so nothing later is excess."""

    result = _plan(
        ledger=[
            _ledger("tp-1", 4, stage=1, status="filled"),
            _ledger("tp-2", 3, stage=2),
            _ledger("tp-3", 3, stage=3),
        ],
        pending=[_pending("tp-2", 3), _pending("tp-3", 3)],
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="65000",
                size="4",
            )
        ],
        target="5",
        live="6",
    )

    assert result.refusal_code is None
    assert result.first_stage_consumed_by_fill is True
    assert result.effective_target_remaining_size == "6"
    # Against the contract's 5 the pair would have been 1 over and tp-2 would
    # have been released for a reduction that is not going to happen.
    assert result.cancel_order_ids == ()
    assert [row["order_id"] for row in result.retained_rows] == ["tp-2", "tp-3"]


def test_a_filled_first_stage_still_releases_stages_beyond_the_position():
    """Not reducing never means leaving more take profit than position."""

    result = _plan(
        ledger=[
            _ledger("tp-1", 4, stage=1, status="filled"),
            _ledger("tp-2", 3, stage=2),
            _ledger("tp-3", 4, stage=3),
        ],
        pending=[_pending("tp-2", 3), _pending("tp-3", 4)],
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="65000",
                size="4",
            )
        ],
        target="5",
        live="6",
    )

    assert result.refusal_code is None
    assert result.cancel_order_ids == ("tp-2",)
    assert [row["order_id"] for row in result.retained_rows] == ["tp-3"]


def test_a_filled_first_stage_without_a_live_size_refuses():
    """The live size is what the rule means; unread is not "unchanged"."""

    result = _plan(
        ledger=[
            _ledger("tp-1", 4, stage=1, status="filled"),
            _ledger("tp-2", 3, stage=2),
        ],
        pending=[_pending("tp-2", 3)],
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="65000",
                size="4",
            )
        ],
        live=None,
    )

    assert result.refusal_code == "target_live_position_not_unique"
    assert result.cancel_order_ids == ()


def test_a_pending_first_stage_keeps_the_contract_target():
    """The other half of the rule: still resting means reduce as planned."""

    result = _plan(
        ledger=[
            _ledger("tp-1", 4, stage=1),
            _ledger("tp-2", 3, stage=2),
            _ledger("tp-3", 3, stage=3),
        ],
        pending=[_pending("tp-1", 4), _pending("tp-2", 3), _pending("tp-3", 3)],
        target="5",
        live="10",
    )

    assert result.first_stage_consumed_by_fill is False
    assert result.effective_target_remaining_size == "5"
    assert result.cancel_order_ids == ("tp-1", "tp-2")


def test_a_cancelled_first_stage_is_not_a_fill():
    """``exact_terminal_no_fill`` took no profit, so the fraction stands."""

    result = _plan(
        ledger=[
            _ledger("tp-1", 4, stage=1),
            _ledger("tp-2", 3, stage=2),
            _ledger("tp-3", 3, stage=3),
        ],
        pending=[_pending("tp-2", 3), _pending("tp-3", 3)],
        order_history=[{"ordId": "tp-1", "state": "cancelled"}],
        target="5",
        live="10",
    )

    assert result.refusal_code is None
    assert result.evidence_tier == "exact_terminal_no_fill"
    assert result.first_stage_consumed_by_fill is False
    assert result.effective_target_remaining_size == "5"
    assert result.cancel_order_ids == ("tp-2",)


def test_pending_first_take_profit_produces_exact_cancel_action():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 4)],
    )

    assert result.cancel_actions == (
        {"order_id": "tp-1", "pos_id": POS_ID, "size": "4"},
    )


def test_absent_take_profit_without_terminal_evidence_is_unknown():
    result = _plan(ledger=[_ledger("tp-1", 4, stage=1)])

    assert result.refusal_code == "take_profit_terminal_state_unknown"
    assert result.cancel_order_ids == ()


def test_duplicate_order_id_or_conflicting_ledger_owner_blocks():
    duplicate = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 4), _pending("tp-1", 4)],
    )
    conflict = _plan(
        ledger=[
            _ledger("tp-1", 4, stage=1),
            _ledger("tp-1", 4, stage=1, owner_leg=99),
        ],
        pending=[_pending("tp-1", 4)],
    )

    assert duplicate.refusal_code == "take_profit_order_identity_conflict"
    assert conflict.refusal_code == "take_profit_order_identity_conflict"


def test_retained_total_above_target_removes_earliest_rows_deterministically():
    result = _plan(
        ledger=[
            _ledger("tp-1", 2, stage=1),
            _ledger("tp-2", 4, stage=2),
            _ledger("tp-3", 4, stage=3),
        ],
        pending=[
            _pending("tp-1", 2),
            _pending("tp-2", 4),
            _pending("tp-3", 4),
        ],
        target="3",
    )

    assert result.cancel_order_ids == ("tp-1", "tp-2", "tp-3")
    assert result.retained_rows == ()


def test_manual_partial_close_is_not_take_profit_fill_proof():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        order_history=[{"ordId": "manual-close", "state": "filled", "sz": "4"}],
        fills=[{"ordId": "manual-close", "posId": POS_ID, "fillSz": "4"}],
    )

    assert result.refusal_code == "take_profit_terminal_state_unknown"
    assert result.proven_filled_quantity == "0"


# --- Ownership is the chain's answer, not the row's ``posId`` -----------------


def test_a_resting_take_profit_needs_no_pos_id_to_be_ours():
    """The whole of batches 146, 150 and 153: real rows carry no ``posId``."""

    row = _pending("tp-1", 4)
    assert "posId" not in row

    result = _plan(ledger=[_ledger("tp-1", 4, stage=1)], pending=[row])

    assert result.refusal_code is None
    assert result.cancel_order_ids == ("tp-1",)


def test_another_legs_take_profit_on_the_same_instrument_is_skipped():
    """Binding 320's shape: two legs, one instrument, each with its own stages.

    The other leg's order carries no position id, exactly like ours.  Only the
    chain can place it, and a *resolved* authority has already placed every
    row on the instrument -- so a row it did not give us is one it gave
    somebody else.  Skipping is not claiming: nothing cancels or resizes it.
    """

    ours = _pending("tp-1", 4)
    theirs = pending_take_profit_row(
        ord_id="other-leg-tp",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="66000",
        size="11",
    )

    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[ours, theirs],
        authority=_authority([ours]),
    )

    assert result.refusal_code is None
    assert result.cancel_order_ids == ("tp-1",)
    assert "other-leg-tp" not in result.cancel_order_ids


def test_a_row_naming_another_position_is_never_treated_as_ours():
    ours = {**_pending("tp-1", 4), "posId": "1009999999999999"}

    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[ours],
        authority=_authority([ours]),
    )

    assert result.refusal_code == "take_profit_order_identity_conflict"


def test_an_unattributable_same_side_take_profit_freezes():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 4)],
        authority=_authority(frozen_reason=FREEZE_ORDER_UNATTRIBUTABLE),
    )

    assert result.refusal_code == "take_profit_unattributable_pending_order"
    assert result.cancel_order_ids == ()


def test_a_stop_on_the_opposite_direction_is_not_our_take_profit():
    ours = _pending("tp-1", 4)
    other_side = pending_take_profit_row(
        ord_id="short-leg-tp",
        inst_id=INSTRUMENT,
        pos_side="short",
        trigger_price="60000",
        size="2",
    )

    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[ours, other_side],
        authority=_authority([ours]),
    )

    assert result.refusal_code is None
    assert result.cancel_order_ids == ("tp-1",)


def test_our_own_stop_is_not_mistaken_for_a_take_profit():
    ours = _pending("tp-1", 4)
    stop = pending_stop_row(
        ord_id="our-stop",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="60000",
        size="0",
    )

    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[ours, stop],
        authority=_authority([ours, stop]),
    )

    assert result.refusal_code is None
    assert result.cancel_order_ids == ("tp-1",)


# --- Ledger history rows, and no live stage at all ---------------------------


def test_retired_and_superseded_ledger_rows_are_history_not_conflicts():
    result = _plan(
        ledger=[
            _ledger("old-tp", 4, stage=1, status="retired"),
            _ledger("gone-tp", 4, stage=2, status="superseded"),
            _ledger("tp-3", 3, stage=3),
        ],
        pending=[_pending("tp-3", 3)],
    )

    assert result.refusal_code is None
    assert result.cancel_order_ids == ("tp-3",)


def test_a_leg_with_no_take_profit_ledger_row_requires_no_cancel():
    """Batch 159's shape: two full-size stops and no take profit at all."""

    stop = pending_stop_row(
        ord_id="1001125123045252",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="79200",
        size="10",
    )

    result = _plan(ledger=[], pending=[stop], authority=_authority([stop]))

    assert result.refusal_code is None
    assert result.cancel_order_ids == ()
    assert result.evidence_tier == "no_take_profit_ledger_row"


def test_a_resting_take_profit_with_no_ledger_row_still_refuses():
    """"Nothing to do" must not be reachable while an unrecorded TP rests."""

    orphan = _pending("tp-9", 4, stage=9)

    result = _plan(ledger=[], pending=[orphan], authority=_authority([orphan]))

    assert result.refusal_code == "take_profit_order_identity_conflict"


# --- What must still be refused ----------------------------------------------


def test_price_or_size_drift_between_ledger_and_exchange_refuses():
    price_drift = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 4, stage=2)],
    )
    size_drift = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 5, stage=1)],
    )

    assert price_drift.refusal_code == "take_profit_order_identity_conflict"
    assert size_drift.refusal_code == "take_profit_order_identity_conflict"


def test_a_failed_trigger_is_never_a_fill():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="65000",
                size="4",
                error_code="51004",
                error_message="insufficient position",
            )
        ],
    )

    assert result.refusal_code == "take_profit_terminal_state_unknown"
    assert result.proven_filled_quantity == "0"


def test_an_untriggered_history_row_is_never_a_fill():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="65000",
                size="4",
                trigger_time="0",
            )
        ],
    )

    assert result.refusal_code == "take_profit_terminal_state_unknown"


def test_an_incomplete_pending_snapshot_is_never_a_fill():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="65000",
                size="4",
            )
        ],
        pending_snapshot_complete=False,
    )

    assert result.refusal_code == "take_profit_terminal_state_unknown"


def test_a_closing_side_inconsistent_with_the_position_refuses():
    row = {**_pending("tp-1", 4), "side": "buy"}

    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[row],
        authority=_authority([row]),
    )

    assert result.refusal_code == "take_profit_order_identity_conflict"


def test_an_unresolved_authority_never_yields_a_plan():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 4)],
        authority=_authority(frozen_reason="position_ownership_not_verified"),
    )

    assert result.refusal_code == "position_ownership_not_verified"


def test_a_missing_authority_never_yields_a_plan():
    result = plan_take_profit_consumption(
        contract=_contract(),
        target_leg=TARGET_LEG,
        pending_orders=[_pending("tp-1", 4)],
        trigger_history=[],
        order_history=[],
        trade_fills=[],
        protection_ledger=[_ledger("tp-1", 4, stage=1)],
        trusted_start_size="10",
        target_remaining_size="5",
    )

    assert result.refusal_code == "take_profit_protection_authority_unavailable"


def test_an_authority_for_another_position_never_yields_a_plan():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 4)],
        authority=_authority([_pending("tp-1", 4)], pos_id="1009999999999999"),
    )

    assert result.refusal_code == "take_profit_order_identity_conflict"

from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
)
from telegram_kol_research.strategy_management_take_profit_consumption import (
    plan_take_profit_consumption,
)


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
    "pos_id": "pos-1",
    "instrument_id": "BTC-USDT-SWAP",
    "side": "long",
}


def _ledger(order_id, size, *, stage, owner_leg=20, status="verified"):
    return {
        "order_id": order_id,
        "execution_binding_id": 10,
        "execution_order_leg_id": owner_leg,
        "pos_id": "pos-1",
        "instrument_id": "BTC-USDT-SWAP",
        "side": "long",
        "purpose": "take_profit",
        "status": status,
        "size_text": str(size),
        "trigger_price": str(64000 + stage * 1000),
        "stage_index": stage,
    }


def _pending(order_id, size):
    stage = int(str(order_id).rsplit("-", 1)[-1])
    return {
        "ordId": order_id,
        "posId": "pos-1",
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "tpTriggerPx": str(64000 + stage * 1000),
        "sz": str(size),
    }


def _plan(*, ledger, pending=(), trigger_history=(), order_history=(), fills=(), target="5"):
    return plan_take_profit_consumption(
        contract=_contract(),
        target_leg=TARGET_LEG,
        pending_orders=list(pending),
        trigger_history=list(trigger_history),
        order_history=list(order_history),
        trade_fills=list(fills),
        protection_ledger=list(ledger),
        trusted_start_size="10",
        target_remaining_size=target,
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
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1, status="filled")],
        trigger_history=[{"ordId": "tp-1", "state": "filled", "posId": "pos-1"}],
        fills=[{"ordId": "tp-1", "posId": "pos-1", "fillSz": "4"}],
    )

    assert result.cancel_order_ids == ()
    assert result.proven_filled_quantity == "4"
    assert result.evidence_tier == "exact_terminal_fill"


def test_pending_first_take_profit_produces_exact_cancel_action():
    result = _plan(
        ledger=[_ledger("tp-1", 4, stage=1)],
        pending=[_pending("tp-1", 4)],
    )

    assert result.cancel_actions == (
        {"order_id": "tp-1", "pos_id": "pos-1", "size": "4"},
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
        fills=[{"ordId": "manual-close", "posId": "pos-1", "fillSz": "4"}],
    )

    assert result.refusal_code == "take_profit_terminal_state_unknown"
    assert result.proven_filled_quantity == "0"

from telegram_kol_research.take_profit_fill_evidence import (
    prove_first_take_profit_fill,
)


def _tp_order():
    return {
        "order_id": "tp-1",
        "pos_id": "pos-1",
        "size_text": "5",
        "trigger_price": "62400",
        "status": "active",
    }


def _protection_leg():
    return {
        "role": "take_profit",
        "leg_index": 1,
        "exchange_order_id": "tp-1",
        "pos_id": "pos-1",
        "planned_size": "5",
    }


def _observation(size, orders, *, complete=True):
    return {
        "pos_id": "pos-1",
        "instrument_id": "BTC-USDT-SWAP",
        "side": "short",
        "size_text": str(size),
        "snapshot_complete": complete,
        "pending_tpsl": [
            {
                "order_id": order_id,
                "pos_id": "pos-1",
                "position_side": "short",
                "size_text": str(order_size),
                "trigger_price": str(price),
            }
            for order_id, order_size, price in orders
        ],
    }


def test_proves_tp1_from_exact_terminal_order_history():
    result = prove_first_take_profit_fill(
        tp_order=_tp_order(),
        protection_leg=_protection_leg(),
        previous_observation=None,
        current_observation=None,
        trigger_history=[{
            "ordId": "tp-1",
            "posId": "pos-1",
            "posSide": "short",
            "sz": "5",
            "state": "filled",
        }],
        order_history=[],
        trade_fills=[],
        conflicting_mutations=[],
    )

    assert result.proven is True
    assert result.evidence_tier == "exact_order_terminal"
    assert result.trigger_order_id == "tp-1"
    assert result.filled_size == "5"


def test_rejects_exact_terminal_row_with_wrong_position_or_size():
    result = prove_first_take_profit_fill(
        tp_order=_tp_order(),
        protection_leg=_protection_leg(),
        previous_observation=None,
        current_observation=None,
        trigger_history=[{
            "ordId": "tp-1",
            "posId": "other-pos",
            "posSide": "short",
            "sz": "4",
            "state": "filled",
        }],
        order_history=[],
        trade_fills=[],
        conflicting_mutations=[],
    )

    assert result.proven is False
    assert result.reason_code == "tp1_exact_history_conflict"


def test_proves_tp1_from_complete_exact_position_delta():
    previous = _observation(
        10,
        [("tp-1", 5, 62400), ("tp-2", 3, 61700), ("tp-3", 2, 61000)],
    )
    current = _observation(
        5,
        [("tp-2", 3, 61700), ("tp-3", 2, 61000)],
    )
    result = prove_first_take_profit_fill(
        tp_order=_tp_order(),
        protection_leg=_protection_leg(),
        previous_observation=previous,
        current_observation=current,
        trigger_history=[],
        order_history=[],
        trade_fills=[],
        conflicting_mutations=[],
    )

    assert result.proven is True
    assert result.evidence_tier == "exchange_position_delta"
    assert result.filled_size == "5"


def test_position_delta_fails_closed_for_ambiguous_or_incomplete_evidence():
    previous = _observation(
        10,
        [("tp-1", 5, 62400), ("tp-2", 3, 61700), ("tp-3", 2, 61000)],
    )
    cases = [
        (_observation(10, [("tp-2", 3, 61700), ("tp-3", 2, 61000)]), [], "tp1_size_delta_mismatch"),
        (_observation(5, [("tp-2", 3, 61700)], complete=True), [], "tp1_remaining_orders_changed"),
        (_observation(5, [("tp-2", 3, 61700), ("tp-3", 2, 61000)], complete=False), [], "tp1_snapshot_incomplete"),
        (_observation(5, [("tp-2", 3, 61700), ("tp-3", 2, 61000)]), [{"operation": "close_position"}], "tp1_conflicting_mutation"),
    ]

    for current, conflicts, reason in cases:
        result = prove_first_take_profit_fill(
            tp_order=_tp_order(),
            protection_leg=_protection_leg(),
            previous_observation=previous,
            current_observation=current,
            trigger_history=[],
            order_history=[],
            trade_fills=[],
            conflicting_mutations=conflicts,
        )
        assert result.proven is False
        assert result.reason_code == reason


def test_non_first_take_profit_leg_never_authorizes_trigger():
    protection = _protection_leg()
    protection["leg_index"] = 2
    result = prove_first_take_profit_fill(
        tp_order=_tp_order(),
        protection_leg=protection,
        previous_observation=None,
        current_observation=None,
        trigger_history=[{"ordId": "tp-1", "state": "filled", "sz": "5"}],
        order_history=[],
        trade_fills=[],
        conflicting_mutations=[],
    )

    assert result.proven is False
    assert result.reason_code == "take_profit_leg_not_first"


def _seed_persisted_tp1(session):
    binding = ExecutionBinding(
        strategy_instance_id="deepcoin:1:2:BTC:short",
        kol_id="group:1",
        chat_id=1,
        message_id=2,
        symbol="BTC",
        side="short",
        venue="deepcoin",
        pos_id="pos-1",
        status="active",
    )
    session.add(binding)
    session.flush()
    entry_leg = ExecutionOrderLeg(
        execution_binding_id=binding.id,
        strategy_instance_id=binding.strategy_instance_id,
        leg_index=1,
        purpose="entry",
        order_kind="market",
        order_id="pos-1",
        pos_id="pos-1",
        venue="deepcoin",
        attribution_status="verified",
        status="active",
    )
    session.add(entry_leg)
    session.flush()
    tp = PositionTakeProfitOrder(
        venue="deepcoin",
        execution_binding_id=binding.id,
        execution_order_leg_id=entry_leg.id,
        pos_id="pos-1",
        order_id="tp-1",
        trigger_price="62400",
        size_text="5",
        status="active",
    )
    protection = PositionProtectionLeg(
        venue="deepcoin",
        execution_binding_id=binding.id,
        execution_order_leg_id=entry_leg.id,
        role="take_profit",
        leg_index=1,
        planned_trigger_price="62400",
        planned_size="5",
        pos_id="pos-1",
        exchange_order_id="tp-1",
        status="verified",
    )
    session.add_all([tp, protection])
    session.flush()
    return binding, entry_leg, tp, protection


def test_reconcile_exact_tp1_fill_updates_both_durable_ledgers(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _, _, tp, protection = _seed_persisted_tp1(session)
        reconcile_trigger_take_profit_order_history(
            session,
            positions=[{"posId": "pos-1", "pos": "5"}],
            pending_orders=[],
            trigger_history=[{
                "ordId": "tp-1",
                "posId": "pos-1",
                "posSide": "short",
                "sz": "5",
                "state": "filled",
            }],
            order_history=[],
            trade_fills=[],
            observed_at=datetime(2026, 8, 2, 8, 0),
        )
        session.commit()

        assert tp.status == "filled"
        assert protection.status == "filled"
        assert json.loads(tp.evidence_json)["tp1_fill"]["evidence_tier"] == (
            "exact_order_terminal"
        )


def test_reconcile_tp1_from_persisted_complete_position_delta(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding, entry_leg, tp, protection = _seed_persisted_tp1(session)
        previous_orders = [
            {"order_id": "tp-1", "pos_id": "pos-1", "position_side": "short", "size_text": "5", "trigger_price": "62400"},
            {"order_id": "tp-2", "pos_id": "pos-1", "position_side": "short", "size_text": "3", "trigger_price": "61700"},
        ]
        current_orders = [previous_orders[1]]
        session.add_all([
            PositionReconciliationObservation(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=entry_leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="pos-1",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                size_text="10",
                avg_entry_price="63076.7",
                pending_tpsl_json=json.dumps(previous_orders),
                snapshot_complete=True,
                snapshot_fingerprint="a" * 64,
                observed_at=datetime(2026, 8, 2, 7, 0),
            ),
            PositionReconciliationObservation(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=entry_leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="pos-1",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                size_text="5",
                avg_entry_price="63076.7",
                pending_tpsl_json=json.dumps(current_orders),
                snapshot_complete=True,
                snapshot_fingerprint="b" * 64,
                observed_at=datetime(2026, 8, 2, 7, 30),
            ),
        ])
        session.flush()

        reconcile_trigger_take_profit_order_history(
            session,
            positions=[{"posId": "pos-1", "pos": "5"}],
            pending_orders=[],
            trigger_history=[],
            order_history=[],
            trade_fills=[],
            observed_at=datetime(2026, 8, 2, 8, 0),
        )
        session.commit()

        assert tp.status == "filled"
        assert protection.status == "filled"
        assert json.loads(tp.evidence_json)["tp1_fill"]["evidence_tier"] == (
            "exchange_position_delta"
        )
import json
from datetime import datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    PositionReconciliationObservation,
    PositionTakeProfitOrder,
)
from telegram_kol_research.position_take_profit_orders import (
    reconcile_trigger_take_profit_order_history,
)

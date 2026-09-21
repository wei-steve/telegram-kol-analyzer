"""F1: the planner's ledger/exchange comparison, against real Deepcoin rows.

Batches 159, 166 and 172 were each refused with
``protection_price_or_size_mismatch`` while the exchange rows agreed with the
ledger in every respect a person would check.  The comparison read ``side`` --
the *closing* direction -- as an alias of the position's direction, so every
real ``TPSL`` row produced ``{"long", "short"}`` and no real row could ever
match.  ``356630dd`` fixed exactly this in two executors and missed the
planner.

Every fixture here is built by ``tests/deepcoin_production_rows``, which copies
the venue's own key set.
"""

from __future__ import annotations

import importlib
import json
from collections import Counter

import pytest

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
)

from deepcoin_production_rows import (
    pending_stop_row,
    pending_take_profit_row,
    position_row,
)


BINDING_ID = 363
LEG_ID = 579
POS_ID = "1001125231241310"
INSTRUMENT = "ETH-USDT-SWAP"


def _planner():
    return importlib.import_module(
        "telegram_kol_research.strategy_management_planner"
    )


def _binding_and_leg():
    binding = ExecutionBinding(id=BINDING_ID, strategy_instance_id="strategy-eth-long")
    leg = ExecutionOrderLeg(
        id=LEG_ID,
        execution_binding_id=BINDING_ID,
        strategy_instance_id="strategy-eth-long",
    )
    return binding, leg


def _ledger(
    *,
    order_id: str,
    purpose: str,
    trigger_price: str,
    size_text: str,
    status: str = "verified",
):
    return PositionProtectionLedger(
        execution_binding_id=BINDING_ID,
        execution_order_leg_id=LEG_ID,
        strategy_instance_id="strategy-eth-long",
        pos_id=POS_ID,
        instrument_id=INSTRUMENT,
        side="long",
        order_id=order_id,
        purpose=purpose,
        trigger_price=trigger_price,
        size_text=size_text,
        status=status,
    )


def _position(size: str = "0.8"):
    return {
        **position_row(
            pos_id=POS_ID, inst_id=INSTRUMENT, pos_side="long", size=size, avg_price="2650"
        ),
        "pos_id": POS_ID,
    }


def _confirm(ledger_rows, tpsl_rows, *, position=None):
    planner = _planner()
    binding, leg = _binding_and_leg()
    return planner._ledger_confirmed_position_protection(
        position=position if position is not None else _position(),
        entry_leg=leg,
        binding=binding,
        tpsl_orders=list(tpsl_rows),
        ledger_rows=list(ledger_rows),
        global_order_id_counts=Counter(
            str(row.get("ordId")) for row in tpsl_rows
        ),
    )


# --- The defect itself -------------------------------------------------------


def test_real_tpsl_row_closing_side_is_not_a_position_side_alias():
    """A long position's stop carries ``side: "sell"``.  That is not "short"."""

    row = pending_stop_row(
        ord_id="1001125231241999",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2600",
        size="1.5",
    )
    assert row["side"] == "sell"
    assert row["posSide"] == "long"
    assert "posId" not in row

    protection = _confirm(
        [
            _ledger(
                order_id="1001125231241999",
                purpose="stop_loss",
                trigger_price="2600",
                size_text="1.5",
            )
        ],
        [row],
    )

    assert protection is not None
    assert protection.status == "verified"
    assert protection.order_ids == ["1001125231241999"]
    assert protection.stop_loss == 2600.0


def test_backup_stop_reads_the_stop_price_keys_not_the_take_profit_keys():
    """``backup_stop`` is a stop.  Reading ``tpTriggerPrice`` for it never matches."""

    row = pending_stop_row(
        ord_id="1001125231242000",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2595",
        size="1.5",
    )

    protection = _confirm(
        [
            _ledger(
                order_id="1001125231242000",
                purpose="backup_stop",
                trigger_price="2595",
                size_text="1.5",
            )
        ],
        [row],
    )

    assert protection is not None and protection.status == "verified"
    assert protection.stop_loss == 2595.0


def test_zero_price_alias_is_absent_not_a_second_price():
    """A stop row whose ``closeSLTriggerPrice`` is ``"0"`` still has one price."""

    row = pending_stop_row(
        ord_id="1001125231242001",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2600",
        size="1.5",
        close_trigger_price="0",
    )

    protection = _confirm(
        [
            _ledger(
                order_id="1001125231242001",
                purpose="stop_loss",
                trigger_price="2600",
                size_text="1.5",
            )
        ],
        [row],
    )

    assert protection is not None and protection.status == "verified"


def test_batch_172_shape_confirms_every_owned_protection_order():
    """ETH long: adopted stop 1.5, a backup stop, TP2 and TP3 pending at 0.4.

    TP1 (0.7) has already filled, so it is no longer in the pending list.  The
    ledger still names it; a ledger row with no pending row is skipped, not a
    mismatch, and the remaining four still confirm.
    """

    rows = [
        pending_stop_row(
            ord_id="stop-primary",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2600",
            size="1.5",
        ),
        pending_stop_row(
            ord_id="stop-backup",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2595",
            size="1.5",
        ),
        pending_take_profit_row(
            ord_id="tp-2",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2720",
            size="0.4",
        ),
        pending_take_profit_row(
            ord_id="tp-3",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2750",
            size="0.4",
        ),
    ]
    ledger_rows = [
        _ledger(order_id="stop-primary", purpose="stop_loss", trigger_price="2600", size_text="1.5"),
        _ledger(order_id="stop-backup", purpose="backup_stop", trigger_price="2595", size_text="1.5"),
        _ledger(
            order_id="tp-1",
            purpose="take_profit",
            trigger_price="2690",
            size_text="0.7",
            status="filled",
        ),
        _ledger(order_id="tp-2", purpose="take_profit", trigger_price="2720", size_text="0.4"),
        _ledger(order_id="tp-3", purpose="take_profit", trigger_price="2750", size_text="0.4"),
    ]

    protection = _confirm(ledger_rows, rows)

    assert protection is not None and protection.status == "verified"
    assert set(protection.order_ids) == {
        "stop-primary", "stop-backup", "tp-2", "tp-3",
    }
    assert sorted(protection.take_profits) == [2720.0, 2750.0]


def test_batch_159_shape_two_full_size_stops_and_no_take_profit():
    """Batch 159's position: two stops sized for the whole position, no TP."""

    rows = [
        pending_stop_row(
            ord_id="1001125123045252",
            inst_id="BTC-USDT-SWAP",
            pos_side="long",
            trigger_price="79200",
            size="10",
            close_trigger_price="",
        ),
        pending_stop_row(
            ord_id="1001125123048630",
            inst_id="BTC-USDT-SWAP",
            pos_side="long",
            trigger_price="79041.6",
            size="0",
        ),
    ]
    ledger_rows = [
        PositionProtectionLedger(
            execution_binding_id=BINDING_ID,
            execution_order_leg_id=LEG_ID,
            strategy_instance_id="strategy-eth-long",
            pos_id=POS_ID,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="1001125123045252",
            purpose="stop_loss",
            trigger_price="79200",
            size_text="10",
            status="verified",
        ),
        PositionProtectionLedger(
            execution_binding_id=BINDING_ID,
            execution_order_leg_id=LEG_ID,
            strategy_instance_id="strategy-eth-long",
            pos_id=POS_ID,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="1001125123048630",
            purpose="backup_stop",
            trigger_price="79041.6",
            size_text="0",
            status="verified",
        ),
    ]
    position = {
        **position_row(
            pos_id=POS_ID,
            inst_id="BTC-USDT-SWAP",
            pos_side="long",
            size="6",
            avg_price="80000",
        ),
        "pos_id": POS_ID,
    }

    protection = _confirm(ledger_rows, rows, position=position)

    assert protection is not None and protection.status == "verified"
    assert set(protection.order_ids) == {
        "1001125123045252", "1001125123048630",
    }
    assert protection.take_profits == []


# --- What must still be refused ----------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "expected_field"),
    [
        ({"slTriggerPrice": "2601", "closeSLTriggerPrice": "2601"}, "trigger_price"),
        ({"sz": "1.4"}, "size"),
        ({"posSide": "short"}, "position_side"),
        ({"side": "buy"}, "closing_side"),
        ({"instId": "BTC-USDT-SWAP"}, "instrument_id"),
        ({"posId": "1009999999999999"}, "pos_id"),
        ({"triggerOrderType": "Conditional"}, "order_type"),
    ],
)
def test_drift_between_ledger_and_exchange_still_refuses(mutation, expected_field):
    row = {
        **pending_stop_row(
            ord_id="stop-primary",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2600",
            size="1.5",
        ),
        **mutation,
    }
    ledger = _ledger(
        order_id="stop-primary", purpose="stop_loss", trigger_price="2600", size_text="1.5"
    )

    assert _confirm([ledger], [row]) is None

    planner = _planner()
    mismatch = planner._ledger_row_protection_mismatch(
        ledger, row, position=_position()
    )
    assert mismatch is not None
    assert mismatch["field"] == expected_field
    assert mismatch["order_id"] == "stop-primary"


def test_mismatch_detail_names_the_ledger_value_and_the_exchange_value():
    planner = _planner()
    row = pending_stop_row(
        ord_id="stop-primary",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2601",
        size="1.5",
    )
    ledger = _ledger(
        order_id="stop-primary", purpose="stop_loss", trigger_price="2600", size_text="1.5"
    )

    mismatch = planner._ledger_row_protection_mismatch(ledger, row, position=_position())

    assert mismatch == {
        "order_id": "stop-primary",
        "field": "trigger_price",
        "ledger": "2600.0",
        "exchange": "2601.0",
    }


def test_unowned_global_order_is_still_a_global_refusal():
    """The global-uniqueness rule is untouched: a duplicate ordId refuses."""

    row = pending_stop_row(
        ord_id="stop-primary",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2600",
        size="1.5",
    )
    ledger = _ledger(
        order_id="stop-primary", purpose="stop_loss", trigger_price="2600", size_text="1.5"
    )
    planner = _planner()
    binding, leg = _binding_and_leg()

    assert (
        planner._ledger_confirmed_position_protection(
            position=_position(),
            entry_leg=leg,
            binding=binding,
            tpsl_orders=[row, dict(row)],
            ledger_rows=[ledger],
            global_order_id_counts=Counter({"stop-primary": 2}),
        )
        is None
    )


def test_blocked_snapshot_records_which_field_disagreed():
    """Batches 157 and 159 left ``positions: []`` and nothing to diagnose."""

    planner = _planner()
    details = [
        {
            "order_id": "stop-primary",
            "field": "trigger_price",
            "ledger": "2600.0",
            "exchange": "2601.0",
        }
    ]
    snapshot = planner._blocked_target_snapshot(
        execution_mode="live",
        lifecycle_id=1,
        binding_id=BINDING_ID,
        strategy_instance_id="strategy-eth-long",
        reason_code="protection_price_or_size_mismatch",
        stop_gate_evidence=None,
        protection_mismatches=details,
    )

    assert snapshot["blocked_reason"] == "protection_price_or_size_mismatch"
    assert snapshot["protection_mismatches"] == details
    # The snapshot is fingerprinted and persisted; it must stay small.
    assert len(json.dumps(snapshot)) < 4096


def test_blocked_snapshot_bounds_the_recorded_mismatches():
    planner = _planner()
    details = [
        {"order_id": f"ord-{index}", "field": "size", "ledger": "1", "exchange": "2"}
        for index in range(50)
    ]

    snapshot = planner._blocked_target_snapshot(
        execution_mode="live",
        lifecycle_id=1,
        binding_id=BINDING_ID,
        strategy_instance_id="strategy-eth-long",
        reason_code="protection_price_or_size_mismatch",
        stop_gate_evidence=None,
        protection_mismatches=details,
    )

    assert len(snapshot["protection_mismatches"]) == planner.MAX_RECORDED_PROTECTION_MISMATCHES
    assert snapshot["protection_mismatch_count"] == 50

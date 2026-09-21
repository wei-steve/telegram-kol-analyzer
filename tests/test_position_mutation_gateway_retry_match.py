"""Re-arming a rejected cancel from a fresh pending row.

The predicate demanded ``posId`` on the pending row and read ``side`` as a
position-direction alias.  Neither holds for a real ``trigger-orders-pending``
TPSL row, so the retry path could never fire in production -- a rejected cancel
stayed rejected regardless of what the exchange actually showed.
"""

from deepcoin_production_rows import (
    pending_conditional_entry_row,
    pending_stop_row,
    pending_take_profit_row,
)

from telegram_kol_research.position_mutation_authority import (
    PositionMutationAuthority,
)
from telegram_kol_research.position_mutation_gateway import (
    _pending_cancel_retry_matches_authority,
)


INSTRUMENT = "ETH-USDT-SWAP"
POS_ID = "1001125231241310"


def _authority(**overrides):
    values = {
        "venue": "deepcoin",
        "strategy_instance_id": "strategy-eth-long",
        "execution_binding_id": 363,
        "execution_order_leg_id": 579,
        "pos_id": POS_ID,
        "instrument_id": INSTRUMENT,
        "side": "long",
        "position_fingerprint": "pos-fp",
        "protection_fingerprint": "prot-fp",
    }
    values.update(overrides)
    return PositionMutationAuthority(**values)


def _matches(row, *, order_id="tp-2", authority=None):
    return _pending_cancel_retry_matches_authority(
        row, authority=authority or _authority(), order_id=order_id
    )


def test_a_real_pending_row_without_pos_id_re_arms_the_cancel():
    row = pending_take_profit_row(
        ord_id="tp-2",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2720",
        size="0.4",
    )
    assert "posId" not in row
    assert row["side"] == "sell"

    assert _matches(row) is True


def test_a_stop_row_re_arms_the_same_way():
    row = pending_stop_row(
        ord_id="stop-1",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2600",
        size="1.5",
    )

    assert _matches(row, order_id="stop-1") is True


def test_a_row_naming_another_position_never_re_arms():
    row = {
        **pending_take_profit_row(
            ord_id="tp-2",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2720",
            size="0.4",
        ),
        "posId": "1009999999999999",
    }

    assert _matches(row) is False


def test_the_other_direction_never_re_arms():
    row = pending_take_profit_row(
        ord_id="tp-2",
        inst_id=INSTRUMENT,
        pos_side="short",
        trigger_price="2400",
        size="0.4",
    )

    assert _matches(row) is False


def test_an_inconsistent_closing_side_never_re_arms():
    row = {
        **pending_take_profit_row(
            ord_id="tp-2",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2720",
            size="0.4",
        ),
        "side": "buy",
    }

    assert _matches(row) is False


def test_another_instrument_never_re_arms():
    row = pending_take_profit_row(
        ord_id="tp-2",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        trigger_price="80000",
        size="0.4",
    )

    assert _matches(row) is False


def test_another_order_id_never_re_arms():
    row = pending_take_profit_row(
        ord_id="tp-3",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2750",
        size="0.4",
    )

    assert _matches(row) is False


def test_a_resting_conditional_entry_never_re_arms():
    row = pending_conditional_entry_row(
        ord_id="tp-2",
        inst_id=INSTRUMENT,
        pos_side="long",
        trigger_price="2700",
        size="1.5",
        attached_stop="2600",
    )

    assert _matches(row) is False

import pytest

from telegram_kol_research.deepcoin_order_matching import DeepcoinOrderMatchError
from telegram_kol_research.deepcoin_order_matching import extract_pending_protection_orders
from telegram_kol_research.deepcoin_order_matching import pending_tpsl_order_ids_for_position
from telegram_kol_research.deepcoin_order_matching import resolve_stop_loss_adjustment_target
from telegram_kol_research.deepcoin_order_matching import select_position_tpsl_orders
from telegram_kol_research.deepcoin_readonly import DeepcoinOrderBinding
from telegram_kol_research.native_tpsl import NativeTpslExpectation
from telegram_kol_research.native_tpsl import match_native_tpsl_order


def _binding(**overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "source_message_id": 55,
        "symbol": "BTC",
        "side": "long",
        "pos_id": "pos-1",
        "order_id": "entry-1",
        "client_order_id": "client-entry-1",
    }
    values.update(overrides)
    return DeepcoinOrderBinding(**values)


def test_extract_pending_protection_orders_splits_tpsl_payload_into_price_legs():
    orders = extract_pending_protection_orders(
        [
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "orderId": "tpsl-1",
                "posId": "pos-1",
                "slTriggerPrice": "67500",
                "tpTriggerPrice": "69000",
                "sz": "5",
                "cTime": "1782801429000",
            }
        ]
    )

    assert [(order.purpose, order.trigger_price) for order in orders] == [
        ("stop_loss", 67500.0),
        ("take_profit", 69000.0),
    ]
    assert orders[0].trigger_order_id == "tpsl-1"
    assert orders[0].pos_id == "pos-1"


def test_resolve_stop_loss_adjustment_target_prefers_matching_position_id():
    target = resolve_stop_loss_adjustment_target(
        binding=_binding(pos_id="pos-1"),
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "orderId": "stop-1",
                "posId": "pos-1",
                "slTriggerPrice": "67500",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "orderId": "stop-2",
                "posId": "pos-2",
                "slTriggerPrice": "67400",
            },
        ],
        target_pos_id="pos-1",
    )

    assert target.action == "replace_pending_stop_loss"
    assert target.reason == "matched_pending_stop_loss_by_pos_id"
    assert target.order is not None
    assert target.order.trigger_order_id == "stop-1"


def test_resolve_stop_loss_adjustment_target_matches_ledger_owned_order_id():
    target = resolve_stop_loss_adjustment_target(
        binding=_binding(pos_id=None, client_order_id="sl-client-1"),
        pending_trigger_orders=[
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "ordId": "sl-owned-1",
                "clOrdId": "sl-client-1",
                "slTriggerPx": "67500",
            }
        ],
        ledger_owned_order_ids={"sl-owned-1"},
    )

    assert target.action == "replace_pending_stop_loss"
    assert target.reason == "matched_pending_stop_loss_by_ledger_order_id"
    assert target.order is not None
    assert target.order.trigger_order_id == "sl-owned-1"


def test_resolve_stop_loss_adjustment_target_rejects_ambiguous_symbol_side_matches():
    with pytest.raises(DeepcoinOrderMatchError) as exc:
        resolve_stop_loss_adjustment_target(
            binding=_binding(pos_id=None, order_id=None, client_order_id=None),
            pending_trigger_orders=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "orderId": "stop-1",
                    "slTriggerPrice": "67500",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "orderId": "stop-2",
                    "slTriggerPrice": "67400",
                },
            ],
        )

    assert str(exc.value) == "no_deepcoin_stop_loss_adjustment_target"


def test_resolve_stop_loss_adjustment_target_refuses_active_position_fallback():
    with pytest.raises(
        DeepcoinOrderMatchError,
        match="no_deepcoin_stop_loss_adjustment_target",
    ):
        resolve_stop_loss_adjustment_target(
            binding=_binding(pos_id="pos-1"),
            pending_trigger_orders=[],
            live_positions=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "3",
                }
            ],
        )


def test_resolve_stop_loss_adjustment_target_does_not_replace_open_entry_order_sltp():
    with pytest.raises(DeepcoinOrderMatchError, match="no_deepcoin_stop_loss_adjustment_target"):
        resolve_stop_loss_adjustment_target(
            binding=_binding(pos_id=None, order_id="entry-1,entry-2"),
            pending_trigger_orders=[],
            live_positions=[],
        )


def test_select_position_tpsl_orders_skips_zero_size_rows_without_position_id():
    position = {
        "instId": "ETH-USDT-SWAP",
        "posId": "pos-eth",
        "posSide": "long",
        "pos": "5.2",
        "cTime": "1782788831000",
    }
    orders = [
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "cTime": "1782788831000",
            "triggerOrderType": "TPSL",
            "ordId": "tp-1",
            "tpTriggerPrice": "1680",
        },
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "cTime": "1782788831000",
            "triggerOrderType": "TPSL",
            "ordId": "sl-1",
            "slTriggerPrice": "1555",
        },
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "sz": "0",
            "cTime": "1782900000000",
            "triggerOrderType": "TPSL",
            "ordId": "other-position",
            "slTriggerPrice": "1500",
        },
    ]

    matched = select_position_tpsl_orders(position=position, pending_trigger_orders=orders)

    assert matched == []
    assert pending_tpsl_order_ids_for_position(
        position=position,
        pending_trigger_orders=orders,
    ) == []


def test_select_position_tpsl_orders_skips_zero_size_rows_created_after_position():
    position = {
        "instId": "ETH-USDT-SWAP",
        "posId": "1001123821237494",
        "posSide": "long",
        "pos": "3.3",
        "cTime": "1782887701000",
        "uTime": "1782887701000",
    }
    orders = [
        {
            "ordId": "1001123824502195",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "side": "sell",
            "triggerOrderType": "TPSL",
            "tpTriggerPx": None,
            "slTriggerPx": None,
            "triggerPx": "0",
            "sz": "0",
            "cTime": "1782889341000",
            "posId": None,
        },
        {
            "ordId": "old-other",
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "side": "sell",
            "triggerOrderType": "TPSL",
            "triggerPx": "0",
            "sz": "0",
            "cTime": "1782800000000",
            "posId": None,
        },
    ]

    assert pending_tpsl_order_ids_for_position(
        position=position,
        pending_trigger_orders=orders,
    ) == []


def test_select_position_tpsl_orders_deduplicates_combined_tpsl_order_id():
    position = {
        "instId": "ETH-USDT-SWAP",
        "posId": "pos-eth",
        "posSide": "long",
        "pos": "1",
        "cTime": "1782801429000",
    }
    orders = [
        {
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "posId": "pos-eth",
            "sz": "1",
            "cTime": "1782801429000",
            "triggerOrderType": "TPSL",
            "ordId": "combined-1",
            "tpTriggerPrice": "1600",
            "slTriggerPrice": "1560",
        },
    ]

    assert pending_tpsl_order_ids_for_position(
        position=position,
        pending_trigger_orders=orders,
    ) == ["combined-1"]


def _native_tpsl_position(**overrides):
    position = {
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-btc",
        "posSide": "long",
        "pos": "6",
        "cTime": "1784897294000",
    }
    position.update(overrides)
    return position


def test_native_tpsl_match_prefers_the_persisted_exchange_order_id():
    match = match_native_tpsl_order(
        position=_native_tpsl_position(),
        orders=[
            {
                "ordId": "system-stop-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "posId": "pos-btc",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "63200",
                "sz": "6",
                "cTime": "1784897294000",
            }
        ],
        expected=NativeTpslExpectation(
            ord_id="system-stop-1",
            purpose="stop_loss",
            trigger_price="63200",
            size="6",
        ),
    )

    assert match.status == "verified"
    assert match.order is not None
    assert match.order.ord_id == "system-stop-1"


def test_native_tpsl_exact_persisted_order_id_survives_missing_position_id():
    first_position = _native_tpsl_position(posId="pos-btc-1")
    second_position = _native_tpsl_position(posId="pos-btc-2")

    match = match_native_tpsl_order(
        position=first_position,
        open_positions=[first_position, second_position],
        orders=[
            {
                "ordId": "system-zero-stop-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "63000",
                "sz": "0",
                "cTime": "1784962399000",
            }
        ],
        expected=NativeTpslExpectation(
            ord_id="system-zero-stop-1",
            purpose="stop_loss",
            trigger_price="63000",
            size="0",
        ),
    )

    assert match.status == "verified"
    assert match.order is not None
    assert match.order.ord_id == "system-zero-stop-1"


def test_native_tpsl_match_refuses_zero_size_order_without_full_open_position_context():
    match = match_native_tpsl_order(
        position=_native_tpsl_position(),
        orders=[
            {
                "ordId": "manual-stop-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "63000",
                "sz": "0",
                "cTime": "1784962399000",
            }
        ],
        expected=NativeTpslExpectation(
            purpose="stop_loss",
            trigger_price="63000",
            size="0",
        ),
    )

    assert match.status == "not_found"
    assert match.order is None


def test_native_tpsl_match_refuses_zero_size_order_without_ledger_order_id():
    position = _native_tpsl_position()

    match = match_native_tpsl_order(
        position=position,
        open_positions=[position],
        orders=[
            {
                "ordId": "manual-stop-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "63000",
                "sz": "0",
                "cTime": "1784962399000",
            }
        ],
        expected=NativeTpslExpectation(
            purpose="stop_loss",
            trigger_price="63000",
            size="0",
        ),
    )

    assert match.status == "not_found"
    assert match.order is None


def test_native_tpsl_match_refuses_ambiguous_zero_size_stop():
    orders = [
        {
            "ordId": order_id,
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "triggerOrderType": "TPSL",
            "slTriggerPrice": "63000",
            "sz": "0",
            "cTime": "1784962399000",
        }
        for order_id in ("manual-stop-1", "manual-stop-2")
    ]

    match = match_native_tpsl_order(
        position=_native_tpsl_position(),
        orders=orders,
        expected=NativeTpslExpectation(
            purpose="stop_loss",
            trigger_price="63000",
            size="0",
        ),
    )

    assert match.status == "not_found"
    assert match.order is None


def test_native_tpsl_match_does_not_attribute_zero_size_order_to_either_same_side_split():
    first_position = _native_tpsl_position(posId="pos-btc-1")
    second_position = _native_tpsl_position(posId="pos-btc-2")
    orders = [
        {
            "ordId": "zero-size-stop-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "triggerOrderType": "TPSL",
            "slTriggerPrice": "63000",
            "sz": "0",
            "cTime": "1784962399000",
        }
    ]
    expected = NativeTpslExpectation(
        purpose="stop_loss",
        trigger_price="63000",
        size="0",
    )

    first_match = match_native_tpsl_order(
        position=first_position,
        open_positions=[first_position, second_position],
        orders=orders,
        expected=expected,
    )
    second_match = match_native_tpsl_order(
        position=second_position,
        open_positions=[first_position, second_position],
        orders=orders,
        expected=expected,
    )

    assert first_match.status == "not_found"
    assert first_match.order is None
    assert second_match.status == "not_found"
    assert second_match.order is None


def test_native_tpsl_match_requires_explicit_position_side_not_closing_order_side():
    short_position = _native_tpsl_position(posId="pos-short", posSide="short")

    match = match_native_tpsl_order(
        position=short_position,
        orders=[
            {
                "ordId": "close-short-by-buy",
                "instId": "BTC-USDT-SWAP",
                "side": "sell",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "65000",
                "sz": "6",
                "cTime": "1784897294000",
            }
        ],
        expected=NativeTpslExpectation(
            ord_id="close-short-by-buy",
            purpose="stop_loss",
            trigger_price="65000",
            size="6",
        ),
    )

    assert match.status == "mismatch"
    assert match.order is not None


def test_native_tpsl_match_requires_position_pos_side_not_position_order_side():
    position = _native_tpsl_position()
    position.pop("posSide")
    position["side"] = "buy"

    match = match_native_tpsl_order(
        position=position,
        orders=[
            {
                "ordId": "system-stop-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "63200",
                "sz": "6",
                "cTime": "1784897294000",
            }
        ],
        expected=NativeTpslExpectation(
            ord_id="system-stop-1",
            purpose="stop_loss",
            trigger_price="63200",
            size="6",
        ),
    )

    assert match.status == "mismatch"
    assert match.order is not None


def test_native_tpsl_exact_order_id_rejects_missing_position_side_on_both_payloads():
    position = _native_tpsl_position()
    position.pop("posSide")

    match = match_native_tpsl_order(
        position=position,
        orders=[
            {
                "ordId": "system-stop-1",
                "instId": "BTC-USDT-SWAP",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "63200",
                "sz": "6",
                "cTime": "1784897294000",
            }
        ],
        expected=NativeTpslExpectation(
            ord_id="system-stop-1",
            purpose="stop_loss",
            trigger_price="63200",
            size="6",
        ),
    )

    assert match.status == "mismatch"
    assert match.order is not None


def test_select_position_tpsl_orders_skips_zero_size_order_without_position_id():
    selected = select_position_tpsl_orders(
        position=_native_tpsl_position(),
        pending_trigger_orders=[
            {
                "ordId": "zero-size-stop-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "63000",
                "sz": "0",
                "cTime": "1784962399000",
            }
        ],
    )

    assert selected == []


@pytest.mark.parametrize(
    ("purpose", "trigger_key", "expected_price", "actual_price", "expected_size", "actual_size"),
    [
        ("stop_loss", "slTriggerPx", "63200", "63199", "6", "6"),
        ("stop_loss", "slTriggerPx", "63200", "63200", "6", "5"),
        ("take_profit", "tpTriggerPx", "65000", "64999", "2", "2"),
        ("take_profit", "tpTriggerPx", "65000", "65000", "2", "1"),
    ],
)
def test_native_tpsl_match_requires_each_leg_trigger_price_and_size_to_match(
    purpose,
    trigger_key,
    expected_price,
    actual_price,
    expected_size,
    actual_size,
):
    match = match_native_tpsl_order(
        position=_native_tpsl_position(),
        orders=[
            {
                "ordId": "system-leg-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "posId": "pos-btc",
                "triggerOrderType": "TPSL",
                trigger_key: actual_price,
                "sz": actual_size,
                "cTime": "1784897294000",
            }
        ],
        expected=NativeTpslExpectation(
            ord_id="system-leg-1",
            purpose=purpose,
            trigger_price=expected_price,
            size=expected_size,
        ),
    )

    assert match.status == "mismatch"
    assert match.order is not None

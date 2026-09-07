from decimal import Decimal

import pytest

from telegram_kol_research.native_tpsl import (
    is_protection_order_row,
    normalize_native_tpsl,
)


def test_normalize_native_tpsl_accepts_official_position_id_fields():
    order = normalize_native_tpsl(
        {
            "triggerOrderType": "TPSL",
            "OrderSysID": "order-1",
            "PositionID": "pos-1",
            "InstrumentID": "BTC-USDT-SWAP",
            "PosiDirection": "0",
            "Volume": "3",
            "CreateTime": "1720000000000",
            "SLTriggerPrice": "62000",
            "TPTriggerPrice": "68000",
        }
    )

    assert order is not None
    assert order.ord_id == "order-1"
    assert order.pos_id == "pos-1"
    assert order.inst_id == "BTC-USDT-SWAP"
    assert order.size == Decimal("3")
    assert order.created_time == "1720000000000"
    assert order.stop_loss_trigger_price == Decimal("62000")
    assert order.take_profit_trigger_price == Decimal("68000")


def test_normalize_native_tpsl_rejects_official_conditional_row():
    assert (
        normalize_native_tpsl(
            {"triggerOrderType": "Conditional", "OrderSysID": "conditional-1"}
        )
        is None
    )


@pytest.mark.parametrize(
    "row",
    [
        # The verbatim 2026-09-07 incident row: a Conditional entry leg whose
        # side opens the position and which carries the stop it will attach.
        {
            "ordId": "1001125163581473", "triggerOrderType": "Conditional",
            "side": "buy", "posSide": "long", "sz": "10",
            "triggerPx": "79190", "closeSLTriggerPrice": "78500",
        },
        {"triggerOrderType": "Conditional", "trigger_order_type": "conditional"},
        {"triggerOrderType": "Limit"},
    ],
)
def test_is_protection_order_row_rejects_non_tpsl_rows(row):
    assert is_protection_order_row(row) is False


@pytest.mark.parametrize(
    "row",
    [
        # A real position TPSL row: side closes the position.
        {
            "ordId": "1001125157891310", "triggerOrderType": "TPSL",
            "side": "sell", "posSide": "long", "sz": "0",
            "slTriggerPrice": "2430", "closeSLTriggerPrice": "2430",
        },
        {"triggerOrderType": "tpsl", "trigger_order_type": "TPSL"},
        # A market reduce-only close still typed TPSL by the endpoint.
        {"triggerOrderType": "TPSL", "tpOrdPx": "-1", "reduceOnly": "true"},
        # Absent or self-contradictory type: cannot be proven a non-protection
        # row, so protective invariants still apply and it fails closed.
        {"ordId": "no-type", "slTriggerPrice": "78500"},
        {"triggerOrderType": ""},
        {"triggerOrderType": "TPSL", "trigger_order_type": "Conditional"},
    ],
)
def test_is_protection_order_row_holds_tpsl_and_untyped_rows(row):
    assert is_protection_order_row(row) is True
